-- Nothing in this schema may hold an identifier. The 身分證 lookup (中國醫 reg62) is
-- use-and-discard: it is resolved to (room, ticket_no) inside a single request and the
-- identifier never reaches a column, a log line, or the filesystem.
--
-- Days are ISO dates ('2026-08-25', Taipei). `hospital` is an adapter id from
-- bot/hospitals/ ('cmu_hch', ...).
--
-- Schema version 1 (PRAGMA user_version). A version-0 database (single hospital, ROC
-- dates) is rebuilt into this shape by db._migrate_v1 before this script runs.

CREATE TABLE IF NOT EXISTS users (
    line_user_id TEXT PRIMARY KEY,
    -- 'pending' is the default on purpose. Anyone who finds this bot's ID could otherwise
    -- burn the 200-push monthly budget, or use it as an oracle to test 身分證 numbers
    -- against the hospital. A pending user gets one refusal reply and never causes a
    -- hospital request. Promote with: python3 -m bot.admin allow U...
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'active', 'blocked')),
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    -- The hospital a bare "115" means for this person: the last one they named.
    default_hospital TEXT,
    -- The day we last mentioned "you have an appointment today" in a reply, so it is said
    -- once a day rather than on every message.
    appt_hint_date   TEXT
);

-- One row per "ping me when the queue nears my number".
-- state: active -> notified (the "almost your turn" push went out) -> done.
-- cancelled is terminal and user-initiated.
CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id TEXT NOT NULL REFERENCES users(line_user_id) ON DELETE CASCADE,
    hospital     TEXT NOT NULL,
    room         TEXT NOT NULL,
    timecode     TEXT NOT NULL,
    ticket_no    INTEGER NOT NULL,
    visit_date   TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'active',
    notify_ahead INTEGER NOT NULL DEFAULT 5,
    -- The queue only ever moves forwards within a session. Keeping the previous reading
    -- lets the watcher ignore a number that went backwards, which means a parse error
    -- rather than a real call. pending_current holds a reading that WOULD fire but has
    -- not been confirmed by a second fetch yet - a false "it is your turn" push cannot be
    -- taken back, so it is worth one poll interval of latency to confirm it.
    last_current    INTEGER,
    pending_current INTEGER,
    created_at   TEXT NOT NULL,
    notified_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_subs_active ON subscriptions(state, visit_date);
-- One person cannot subscribe twice to the same seat; re-sending just updates it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_seat
    ON subscriptions(line_user_id, hospital, room, timecode, ticket_no, visit_date);

-- (hospital, room, timecode) -> doctor for a given day. The key includes timecode because
-- the same room runs different doctors morning vs afternoon.
CREATE TABLE IF NOT EXISTS rooms (
    hospital   TEXT NOT NULL,
    room       TEXT NOT NULL,
    timecode   TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    doctor     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (hospital, room, timecode, visit_date)
);
CREATE INDEX IF NOT EXISTS idx_rooms_doctor ON rooms(doctor);

-- Rooms that have ever answered with real data. Lets the daily refresh probe the rooms
-- that exist instead of re-sweeping a hospital's whole numeric range.
CREATE TABLE IF NOT EXISTS room_whitelist (
    hospital       TEXT NOT NULL,
    room           TEXT NOT NULL,
    last_seen_date TEXT NOT NULL,
    PRIMARY KEY (hospital, room)
);

-- Every push is one unit of the monthly free quota. Replies are free and are NOT logged
-- here. ref_type/ref_id say what the push was about ('sub' -> subscriptions.id,
-- 'reminder' -> reminders.id); occurrence separates the repeats of one reminder (refill
-- number 2 and refill number 3 are different pushes).
CREATE TABLE IF NOT EXISTS push_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id TEXT NOT NULL,
    ref_type     TEXT,
    ref_id       INTEGER,
    occurrence   TEXT NOT NULL DEFAULT '',
    sent_at      TEXT NOT NULL,
    kind         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_sent ON push_log(sent_at);
-- One push of each kind per thing, ever - enforced by the database rather than by
-- remembering to check a flag. A retry, a double tick, or a restart mid-push cannot spend
-- the quota twice or wake someone twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_push_once
    ON push_log(ref_type, ref_id, kind, occurrence) WHERE ref_id IS NOT NULL;

-- What each person actually looks at. Families go back to the same clinic for the same
-- follow-up, so remembering it turns a repeat query into one tap. Room numbers and doctor
-- names are public schedule data - this says nothing about anyone's condition.
CREATE TABLE IF NOT EXISTS recent_rooms (
    line_user_id TEXT NOT NULL REFERENCES users(line_user_id) ON DELETE CASCADE,
    hospital     TEXT NOT NULL,
    room         TEXT NOT NULL,
    timecode     TEXT NOT NULL,
    doctor       TEXT NOT NULL DEFAULT '',
    last_used    TEXT NOT NULL,
    use_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (line_user_id, hospital, room, timecode)
);

-- Public clinic progress over time - the light and how many have been seen - recorded on
-- every real fetch. This is what "大約還要 15 分鐘" is computed from. No user appears here.
-- Pruned to the last few days by the watcher.
CREATE TABLE IF NOT EXISTS progress_log (
    hospital    TEXT NOT NULL,
    room        TEXT NOT NULL,
    timecode    TEXT NOT NULL,
    visit_date  TEXT NOT NULL,
    observed_at REAL NOT NULL,          -- unix seconds
    light       INTEGER,
    done_count  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_progress_clinic
    ON progress_log(hospital, room, timecode, visit_date, observed_at);

-- 回診 and 領藥 reminders. Unlike everything above this IS personal: who goes to which
-- clinic on which day, and that they are on a long-term prescription. It is kept because
-- the bot serves one family on a private whitelist, it holds no identifier and no
-- diagnosis or drug name, and "刪除我的資料" / `admin forget` remove it.
--
--   appt:   hospital/room/doctor/timecode are whatever the person gave (any may be empty);
--           appt_date is the day. One push the evening before.
--   refill: a 慢性處方箋 of fills_total fills of days_per_fill days each. last_fill_date
--           is when fill number fills_done was collected. NHI rules allow the next fill
--           within the 10 days before the current one runs out.
CREATE TABLE IF NOT EXISTS reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id   TEXT NOT NULL REFERENCES users(line_user_id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('appt', 'refill')),
    state          TEXT NOT NULL DEFAULT 'active'
                   CHECK (state IN ('active', 'done', 'cancelled')),
    hospital       TEXT NOT NULL DEFAULT '',
    room           TEXT NOT NULL DEFAULT '',
    doctor         TEXT NOT NULL DEFAULT '',
    timecode       TEXT NOT NULL DEFAULT '',
    appt_date      TEXT,
    last_fill_date TEXT,
    days_per_fill  INTEGER,
    fills_total    INTEGER,
    fills_done     INTEGER,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(line_user_id, state);
