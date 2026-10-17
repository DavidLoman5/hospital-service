"""Offline tests. No network, no LINE, tmpdir only:

    python3 -m unittest discover -s bot -t .

The fixtures are raw Big5 BYTES on purpose. A fixture stored as str would exercise the
parser but never the codec, and the codec is where two of this project's three known traps
live (see bot/hospitals/cmu_hch.py).
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from bot import clock, config, db, estimate, fetch, hospitals, reminders, waiting
from bot.hospitals import cmu_hch

DAY = "2026-08-25"
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name + ".html"), "rb") as fh:
        return fh.read().decode("big5hkscs", errors="replace")


class TestParse(unittest.TestCase):
    def test_normal_room(self):
        p = cmu_hch.parse(fixture("room_ok_gappy"))
        self.assertEqual(p["kind"], "ok")
        self.assertEqual(p["room"], "101")
        self.assertEqual(p["doctor"], "陳威仁")
        self.assertEqual(p["session"], "下午")
        self.assertEqual(p["light"], "(未開診)")
        self.assertEqual(len(p["tickets"]), 42)
        self.assertEqual(p["unknown_states"], [])

    def test_doctor_name_needs_entity_unescaping(self):
        """林圀宏: Big5 cannot encode 圀, so the CGI emits &#22272; instead. Decoding
        without unescaping yields a mangled name - this is the regression guard."""
        p = cmu_hch.parse(fixture("room_ok_entity"))
        self.assertEqual(p["doctor"], "林圀宏")
        self.assertNotIn("&#", p["doctor"])

    def test_response_shapes(self):
        self.assertEqual(cmu_hch.parse(fixture("room_none"))["kind"], "no_room")
        self.assertEqual(cmu_hch.parse(fixture("room_badformat"))["kind"], "bad_room")
        self.assertEqual(cmu_hch.parse(fixture("id_none"))["kind"], "no_registration")

    def test_garbage_does_not_raise(self):
        for junk in ["", "<html></html>", "not html at all", "<tr><td>x</td></tr>"]:
            self.assertEqual(cmu_hch.parse(junk)["kind"], "unknown")

    def test_unrecognised_state_is_kept_verbatim(self):
        """The four live states are known now, but the hospital can still print something
        new. An unknown state must be preserved and reported, never guessed at, never
        fatal - that is how the four below were discovered in the first place."""
        page = ('<tr><td align="center">診間號: 115  醫師: 張三</td></tr>'
                '<tr><td>診間目前燈號: 23</td></tr>'
                '<tr><td>1</td><td>完成</td></tr>'
                '<tr><td>2</td><td>暫停看診</td></tr>')
        p = cmu_hch.parse(page)
        self.assertEqual(p["kind"], "ok")
        self.assertEqual(p["light"], "23")
        self.assertEqual(p["unknown_states"], ["暫停看診"])
        self.assertEqual(p["tickets"][1]["state"], "暫停看診")


class TestWaitingAhead(unittest.TestCase):
    """The accuracy of every reminder rests on this one function."""

    def setUp(self):
        real = cmu_hch.parse(fixture("room_ok_gappy"))
        self.nums = [t["no"] for t in real["tickets"]]
        # Same clinic, simulated mid-session: the light is on 23, everyone below is done.
        self.snap = {"light": "23",
                     "tickets": [{"no": n, "state": ("完成" if n <= 23 else "未看診")}
                                 for n in self.nums]}

    def test_counts_rows_not_arithmetic(self):
        # 1..94 holds only 42 real tickets, so subtracting overstates by more than 2x.
        self.assertEqual(waiting.waiting_ahead(self.snap, 38), 7)
        self.assertEqual(38 - 23, 15)          # what the naive version would have said
        self.assertEqual(waiting.waiting_ahead(self.snap, 94), 29)
        self.assertEqual(94 - 23, 71)

    def test_next_in_line_is_zero(self):
        self.assertEqual(waiting.waiting_ahead(self.snap, 24), 0)

    def test_number_not_in_clinic(self):
        self.assertIsNone(waiting.waiting_ahead(self.snap, 999))

    def test_finished_rows_never_count(self):
        snap = {"light": "94", "tickets": [{"no": n, "state": "完成"} for n in self.nums]}
        self.assertEqual(waiting.waiting_ahead(snap, 94), 0)

    def test_no_shows_below_the_light_are_not_counted(self):
        """Numbers the light has already passed while still 未看診 are people who never
        checked in. Counting them is the failure mode that would have made every estimate
        useless in a busy clinic - one real clinic on number 102 had 23 of them."""
        snap = {"light": "50",
                "tickets": [{"no": 10, "state": "未看診"},      # ghost, light passed it
                            {"no": 20, "state": "未看診(已報到)"},  # skipped, still here
                            {"no": 30, "state": "保留待診"},        # held, still here
                            {"no": 40, "state": "完成"},
                            {"no": 55, "state": "未看診"},          # not reached yet
                            {"no": 60, "state": "未看診"}]}
        self.assertEqual(waiting.waiting_ahead(snap, 60), 3)   # 20, 30, 55

    def test_before_opening_everyone_unfinished_is_ahead(self):
        snap = {"light": "(未開診)",
                "tickets": [{"no": n, "state": "未看診"} for n in (1, 2, 3, 38)]}
        self.assertIsNone(waiting.light_number(snap))
        self.assertEqual(waiting.waiting_ahead(snap, 38), 3)


class TestLiveFixtures(unittest.TestCase):
    """Recorded from clinics that were actually running (2026-08-25 morning). These are the
    only fixtures that exercise the open-clinic vocabulary."""

    def test_open_clinic_parses(self):
        p = cmu_hch.parse(fixture("room_open_live"))
        self.assertEqual(p["kind"], "ok")
        self.assertEqual(p["room"], "201")
        self.assertEqual(p["doctor"], "林圀宏")
        self.assertEqual(waiting.light_number(p), 37)
        self.assertEqual(p["unknown_states"], [],
                         "a live page must not contain states we do not model")

    def test_estimate_beats_both_naive_methods(self):
        p = cmu_hch.parse(fixture("room_open_big"))       # light 102, 57 tickets
        self.assertEqual(waiting.light_number(p), 102)
        self.assertEqual(waiting.waiting_ahead(p, 106), 3)
        naive_all = sum(1 for t in p["tickets"]
                        if t["no"] < 106 and t["state"] != waiting.DONE)
        self.assertEqual(naive_all, 26, "counting every unfinished row overstates ~9x here")

    def test_still_waiting_uses_the_same_model(self):
        """5 people below the light who checked in and were skipped, plus 24 at or above it
        who have not been reached. The 26 who never checked in are excluded."""
        p = cmu_hch.parse(fixture("room_open_live"))
        self.assertEqual(waiting.still_waiting(p), 29)
        everyone_unfinished = sum(1 for t in p["tickets"] if t["state"] != waiting.DONE)
        self.assertGreater(everyone_unfinished, 29)


class TestPrivacy(unittest.TestCase):
    """The 身分證 is use-and-discard. These assertions are what make that structural
    rather than aspirational."""

    def test_no_column_can_hold_an_identifier(self):
        conn = db.init(os.path.join(tempfile.mkdtemp(), "t.db"))
        cols = set()
        # sqlite_sequence and friends are the engine's own bookkeeping, not our schema.
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'"):
            for c in conn.execute("PRAGMA table_info(%s)" % row["name"]):
                cols.add(c["name"])
        # Doctor names are public schedule data and are meant to be here; patient identity
        # is not. This list is a tripwire for a future change that adds somewhere to put it.
        for forbidden in ("crt_id", "crtidno", "id_no", "patient_id", "national_id",
                          "birthday", "birth_date", "smonth", "sday",
                          "patient_name", "raw_body", "message_text"):
            self.assertNotIn(forbidden, cols,
                             "schema gained a column that could hold personal data")

    def test_identifier_cannot_reach_a_traceback(self):
        adapter = cmu_hch.CmuHsinchu(fetch.Fetcher(2.0),
                                     {"base": "https://127.0.0.1:1"})   # nothing listening
        secret = "A123456789"
        with self.assertRaises(fetch.HospitalError) as ctx:
            adapter.fetch_by_id(secret, "01", "01")
        import traceback
        text = "".join(traceback.format_exception(
            type(ctx.exception), ctx.exception, ctx.exception.__traceback__))
        self.assertNotIn(secret, text)
        self.assertIsNone(ctx.exception.__cause__)


class TestDb(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(os.path.join(tempfile.mkdtemp(), "t.db"))

    def test_strangers_land_pending(self):
        self.assertEqual(db.touch_user(self.conn, "Ustranger")["status"], "pending")
        self.assertEqual(db.touch_user(self.conn, "Ume", owner_id="Ume")["status"], "active")

    def test_push_is_claimable_once(self):
        db.touch_user(self.conn, "U1", owner_id="U1")
        db.add_subscription(self.conn, "U1", "cmu_hch", "115", "2", 38, DAY, 5)
        sid = db.active_subscriptions(self.conn, DAY)[0]["id"]
        self.assertTrue(db.claim_push(self.conn, "U1", "sub", sid, "almost"))
        self.assertFalse(db.claim_push(self.conn, "U1", "sub", sid, "almost"))
        self.assertTrue(db.claim_push(self.conn, "U1", "sub", sid, "turn"))
        self.assertEqual(db.pushes_this_month(self.conn), 2)

    def test_resubscribing_same_seat_rearms(self):
        db.touch_user(self.conn, "U1", owner_id="U1")
        db.add_subscription(self.conn, "U1", "cmu_hch", "115", "2", 38, DAY, 5)
        sid = db.active_subscriptions(self.conn, DAY)[0]["id"]
        db.set_subscription_state(self.conn, sid, "cancelled")
        db.add_subscription(self.conn, "U1", "cmu_hch", "115", "2", 38, DAY, 3)
        rows = db.active_subscriptions(self.conn, DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["notify_ahead"], 3)

    def test_doctor_search_escapes_like_wildcards(self):
        db.upsert_room(self.conn, "cmu_hch", "115", "2", DAY, "張巨成")
        self.conn.commit()
        self.assertEqual(len(db.find_by_doctor(self.conn, "張巨", DAY)), 1)
        # Without ESCAPE, "%" would list the entire day's clinics.
        self.assertEqual(len(db.find_by_doctor(self.conn, "%", DAY)), 0)

    def test_stale_days_are_closed(self):
        db.touch_user(self.conn, "U1", owner_id="U1")
        db.add_subscription(self.conn, "U1", "cmu_hch", "115", "2", 38, "2026-08-24", 5)
        self.assertEqual(db.close_stale(self.conn, DAY), 1)
        self.assertEqual(len(db.active_subscriptions(self.conn, "2026-08-24")), 0)


class FakeClient(cmu_hch.CmuHsinchu):
    """Stands in for the hospital: the real adapter with the network call scripted. Each
    real fetch returns the next state. The response cache is off so every call counts."""

    def __init__(self, states, sessions=None):
        super().__init__(fetch.Fetcher(1.0), {"sessions": sessions or {"2": ["00:00", "23:59"]},
                                              "fetchCacheSeconds": 0})
        self.states = list(states)
        self.calls = 0

    def _fetch_room(self, room, timecode):
        self.calls += 1
        return dict(self.states[min(self.calls - 1, len(self.states) - 1)])


def registry_of(*adapters):
    return hospitals.Registry(list(adapters))


def clinic(nums, light, mine=None, mine_state=None):
    """A parsed-looking snapshot with the light on `light`; everything at or below it is
    finished unless overridden. `light=None` means the clinic has not opened."""
    tickets = []
    for n in nums:
        if mine is not None and n == mine and mine_state:
            tickets.append({"no": n, "state": mine_state})
        else:
            done = light is not None and n <= light
            tickets.append({"no": n, "state": "完成" if done else "未看診"})
    return {"kind": "ok", "date": DAY, "session": "下午", "room": "115",
            "doctor": "張三", "light": "(未開診)" if light is None else str(light),
            "note": "", "tickets": tickets, "unknown_states": []}


class TestWatcher(unittest.TestCase):
    NUMS = [1, 2, 3, 10, 20, 30, 31, 32, 33, 38, 40]

    def setUp(self):
        from bot import watcher
        self.watcher = watcher
        self.conn = db.init(os.path.join(tempfile.mkdtemp(), "t.db"))
        self.cfg = dict(config.DEFAULTS)
        self.cfg["dryRunLine"] = True
        self.cfg["defaultNotifyAhead"] = 5
        db.touch_user(self.conn, "U1", owner_id="U1")
        db.add_subscription(self.conn, "U1", "cmu_hch", "115", "2", 38, DAY, 5)

    def sub(self):
        return db.active_subscriptions(self.conn, DAY)[0]

    def run_tick(self, client, last_poll=None):
        self.watcher.tick(self.conn, self.cfg, registry_of(client), DAY,
                          last_poll if last_poll is not None else {})

    def test_far_away_does_not_notify(self):
        # 5 people are ahead only once seen_upto reaches 30; at 3 there are 7 ahead.
        self.run_tick(FakeClient([clinic(self.NUMS, 3)]))
        self.assertEqual(db.pushes_this_month(self.conn), 0)
        self.assertEqual(self.sub()["state"], "active")

    def test_threshold_needs_two_confirmations(self):
        """One reading at the threshold arms it; the second fires. A single garbled parse
        must not wake anyone."""
        snap = clinic(self.NUMS, 20)          # ahead = 4 (30,31,32,33 -> wait, see below)
        self.run_tick(FakeClient([snap]))
        self.assertEqual(db.pushes_this_month(self.conn), 0, "first sighting must only arm")
        self.assertIsNotNone(self.sub()["pending_current"])
        self.run_tick(FakeClient([snap]), last_poll={})
        self.assertEqual(db.pushes_this_month(self.conn), 1)
        self.assertEqual(self.sub()["state"], "notified")

    def test_never_pushes_twice(self):
        snap = clinic(self.NUMS, 20)
        for _ in range(6):
            self.run_tick(FakeClient([snap]), last_poll={})
        self.assertEqual(db.pushes_this_month(self.conn), 1)

    def test_light_reaching_my_number_pushes_turn(self):
        """There is no 看診中 state on these pages: the light passing your number is the
        only evidence your turn has come."""
        snap = clinic(self.NUMS, 38, mine=38, mine_state="未看診(已報到)")
        self.run_tick(FakeClient([snap]))
        rows = self.conn.execute("SELECT kind FROM push_log").fetchall()
        self.assertEqual([r["kind"] for r in rows], ["turn"])
        self.assertEqual(len(db.active_subscriptions(self.conn, DAY)), 0)

    def test_called_but_never_checked_in_gets_a_different_warning(self):
        snap = clinic(self.NUMS, 40, mine=38, mine_state="未看診")
        self.run_tick(FakeClient([snap]))
        self.assertEqual(db.pushes_this_month(self.conn), 1)
        self.assertEqual(len(db.active_subscriptions(self.conn, DAY)), 0)

    def test_finished_closes_without_pushing(self):
        snap = clinic(self.NUMS, 40, mine=38, mine_state="完成")
        self.run_tick(FakeClient([snap]))
        self.assertEqual(db.pushes_this_month(self.conn), 0,
                         "notifying someone who has already been seen is only noise")
        self.assertEqual(len(db.active_subscriptions(self.conn, DAY)), 0)

    def test_not_open_yet_is_quiet(self):
        snap = clinic(self.NUMS, None)
        self.run_tick(FakeClient([snap]))
        self.run_tick(FakeClient([snap]), last_poll={})
        self.assertEqual(db.pushes_this_month(self.conn), 0)

    def test_one_fetch_serves_every_subscriber_in_a_room(self):
        db.touch_user(self.conn, "U2", owner_id="U2")
        db.add_subscription(self.conn, "U2", "cmu_hch", "115", "2", 40, DAY, 5)
        client = FakeClient([clinic(self.NUMS, 20)])
        self.run_tick(client)
        self.assertEqual(client.calls, 1, "two subscribers in one room = one request")

    def test_out_of_session_makes_no_requests(self):
        client = FakeClient([clinic(self.NUMS, 20)], sessions={"2": ["03:00", "03:01"]})
        self.run_tick(client)
        self.assertEqual(client.calls, 0)

    def test_budget_exhaustion_stops_pushes(self):
        self.cfg["monthlyPushBudget"] = 0
        snap = clinic(self.NUMS, 20)
        self.run_tick(FakeClient([snap]))
        self.run_tick(FakeClient([snap]), last_poll={})
        self.assertEqual(db.pushes_this_month(self.conn), 0)


class TestRouting(unittest.TestCase):
    def test_prefix_tolerance(self):
        from bot.server import normalize_path
        for raw in ("/cmu/callback", "/callback", "/cmu/callback?x=1"):
            self.assertEqual(normalize_path(raw, "/cmu"), "/callback")
        self.assertEqual(normalize_path("/cmu/healthz", "/cmu"), "/healthz")
        self.assertEqual(normalize_path("/cmu", "/cmu"), "/")
        self.assertEqual(normalize_path("/cmu/", "/cmu"), "/")
        # A path that merely starts with the same letters must not be mangled.
        self.assertEqual(normalize_path("/cmulative", "/cmu"), "/cmulative")


class TestDirectoryDoesNotHoldTheWriteLock(unittest.TestCase):
    """A sweep runs for ten minutes or more. If it batches into one transaction it holds
    sqlite's write lock the whole time and every webhook fails with "database is locked" -
    the bot goes silent without logging anything obviously wrong. It also loses the whole
    sweep if interrupted."""

    def test_each_hit_is_visible_to_another_connection_immediately(self):
        from bot import directory
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        conn = db.init(path)
        observer = db.connect(path)          # a second connection, as the webhook has

        seen = []

        class OneRoomClient:
            id = "cmu_hch"

            def fetch_room(self, room, code):
                # Between probes, an independent connection must be able to read AND write.
                seen.append(observer.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"])
                observer.execute("BEGIN IMMEDIATE")   # raises if the sweep holds the lock
                observer.rollback()
                return {"kind": "ok", "room": room, "doctor": "王醫師",
                        "date": DAY, "tickets": [], "light": "1",
                        "note": "", "session": "上午", "unknown_states": []}

        directory._probe(conn, OneRoomClient(), ["101", "102", "103"], ["1"],
                         DAY, "test")
        self.assertEqual(seen, [0, 1, 2], "each hit must be committed before the next probe")


# --- multi-hospital ---------------------------------------------------------------------

V0_SCHEMA = """
CREATE TABLE users (line_user_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id TEXT NOT NULL REFERENCES users(line_user_id) ON DELETE CASCADE,
    room TEXT NOT NULL, timecode TEXT NOT NULL, ticket_no INTEGER NOT NULL,
    roc_date TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
    notify_ahead INTEGER NOT NULL DEFAULT 5, last_current INTEGER, pending_current INTEGER,
    created_at TEXT NOT NULL, notified_at TEXT);
CREATE INDEX idx_subs_active ON subscriptions(state, roc_date);
CREATE UNIQUE INDEX idx_subs_seat ON subscriptions(line_user_id, room, timecode, ticket_no, roc_date);
CREATE TABLE rooms (room TEXT NOT NULL, timecode TEXT NOT NULL, roc_date TEXT NOT NULL,
    doctor TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (room, timecode, roc_date));
CREATE INDEX idx_rooms_doctor ON rooms(doctor);
CREATE TABLE room_whitelist (room TEXT PRIMARY KEY, last_seen_date TEXT NOT NULL);
CREATE TABLE push_log (id INTEGER PRIMARY KEY AUTOINCREMENT, line_user_id TEXT NOT NULL,
    sub_id INTEGER, sent_at TEXT NOT NULL, kind TEXT NOT NULL);
CREATE INDEX idx_push_sent ON push_log(sent_at);
CREATE UNIQUE INDEX idx_push_once ON push_log(sub_id, kind) WHERE sub_id IS NOT NULL;
CREATE TABLE recent_rooms (line_user_id TEXT NOT NULL REFERENCES users(line_user_id) ON DELETE CASCADE,
    room TEXT NOT NULL, timecode TEXT NOT NULL, doctor TEXT NOT NULL DEFAULT '',
    last_used TEXT NOT NULL, use_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (line_user_id, room, timecode));
INSERT INTO users VALUES ('U1', 'active', '2026-08-25T00:00:00Z', '2026-08-25T00:00:00Z');
INSERT INTO subscriptions (line_user_id, room, timecode, ticket_no, roc_date, created_at)
    VALUES ('U1', '115', '2', 38, '115/08/25', '2026-08-25T00:00:00Z');
INSERT INTO rooms VALUES ('101', '1', '115/08/25', '曾宗煒', '2026-08-25T01:56:02Z');
INSERT INTO room_whitelist VALUES ('101', '115/08/25');
INSERT INTO push_log (line_user_id, sub_id, sent_at, kind) VALUES ('U1', 1, '2026-08-25T02:00:00Z', 'almost');
INSERT INTO recent_rooms VALUES ('U1', '115', '2', '張三', '2026-08-25T00:00:00Z', 3);
"""


class TestMigration(unittest.TestCase):
    """The live database predates multi-hospital support. Its rows must survive, belong to
    中國醫新竹, and move to ISO dates."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(self.path)
        old.executescript(V0_SCHEMA)
        old.commit()
        old.close()

    def test_v0_rows_survive_and_belong_to_cmu(self):
        conn = db.init(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        sub = conn.execute("SELECT * FROM subscriptions").fetchone()
        self.assertEqual((sub["hospital"], sub["visit_date"], sub["ticket_no"]),
                         ("cmu_hch", DAY, 38))
        room = conn.execute("SELECT * FROM rooms").fetchone()
        self.assertEqual((room["hospital"], room["visit_date"]), ("cmu_hch", DAY))
        self.assertEqual(db.whitelist(conn, "cmu_hch"), ["101"])
        self.assertEqual(db.recent_rooms(conn, "U1")[0]["use_count"], 3)
        push = conn.execute("SELECT * FROM push_log").fetchone()
        self.assertEqual((push["ref_type"], push["ref_id"]), ("sub", 1))
        # The dedupe key still holds for the migrated push.
        self.assertFalse(db.claim_push(conn, "U1", "sub", 1, "almost"))
        self.assertIsNone(conn.execute("SELECT default_hospital FROM users").fetchone()[0])

    def test_migrating_twice_is_a_no_op(self):
        db.init(self.path)
        db.close_all()
        conn = db.init(self.path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0], 1)

    def tearDown(self):
        db.close_all()


class FakeOther(cmu_hch.CmuHsinchu):
    """A second hospital that only publishes the current number - the shape several
    hospitals are expected to have."""
    id = "other"
    name = "測試醫院新竹分院"
    short = "測試新竹"
    aliases = ("測試",)
    has_ticket_list = False
    supports_id_lookup = False

    def __init__(self, light="20"):
        super().__init__(fetch.Fetcher(1.0), {"fetchCacheSeconds": 0,
                                              "sessions": {"2": ["00:00", "23:59"]}})
        self.light = light
        self.calls = 0

    def normalize_room(self, text):
        # Room numbers of any length, as most hospitals other than 中國醫 use.
        from bot import validate
        if not (text.isdigit() and 1 <= len(text) <= 3):
            raise validate.Invalid("診間號碼是數字")
        return text

    def _fetch_room(self, room, timecode):
        self.calls += 1
        return {"kind": "ok", "date": DAY, "session": "下午", "room": room,
                "doctor": "李四", "light": self.light, "note": "", "tickets": [],
                "unknown_states": [], "ticket_list": False}


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.cmu = FakeClient([clinic([1, 2], 1)])
        self.other = FakeOther()
        self.reg = registry_of(self.cmu, self.other)

    def test_name_at_the_front_or_as_a_word(self):
        self.assertEqual(self.reg.match("中國醫新竹 115 下午"), (self.cmu, "115 下午"))
        self.assertEqual(self.reg.match("115 下午 測試新竹"), (self.other, "115 下午"))
        self.assertEqual(self.reg.match("115 下午"), (None, "115 下午"))

    def test_longest_name_wins(self):
        """'測試新竹' must not be read as '測試' followed by '新竹'."""
        adapter, rest = self.reg.match("測試新竹 7")
        self.assertIs(adapter, self.other)
        self.assertEqual(rest, "7")

    def test_build_from_config(self):
        cfg = dict(config.DEFAULTS)
        reg = hospitals.build(cfg)
        self.assertEqual([a.id for a in reg.all()], ["cmu_hch"])
        self.assertIs(reg.only(), reg.get("cmu_hch"))
        self.assertEqual(reg.get("cmu_hch").base, cfg["hospitalBase"])
        cfg["enabledHospitals"] = ["nope"]
        with self.assertRaises(ValueError):
            hospitals.build(cfg)

    def test_cache_is_per_hospital(self):
        a = FakeClient([clinic([1], 1)])
        a.cache_seconds = 60
        a.fetch_room("115", "2")
        a.fetch_room("115", "2")
        self.assertEqual(a.calls, 1)


class TestNoTicketList(unittest.TestCase):
    """Hospitals that only show the current number: subtraction is all there is, and the
    UI must say it is approximate."""

    def snap(self, light):
        return {"light": light, "tickets": [], "ticket_list": False}

    def test_subtraction_flagged_approximate(self):
        self.assertEqual(waiting.waiting_ahead(self.snap("20"), 25), 4)
        self.assertTrue(waiting.is_approx(self.snap("20")))
        self.assertIsNone(waiting.still_waiting(self.snap("20")))

    def test_called_and_not_open(self):
        self.assertEqual(waiting.my_state(self.snap("25"), 25), waiting.CALLED)
        self.assertIsNone(waiting.my_state(self.snap("(未開診)"), 25))
        self.assertIsNone(waiting.waiting_ahead(self.snap("(未開診)"), 25))

    def test_watcher_pushes_turn_without_a_list(self):
        conn = db.init(os.path.join(tempfile.mkdtemp(), "t.db"))
        cfg = dict(config.DEFAULTS, dryRunLine=True)
        db.touch_user(conn, "U1", owner_id="U1")
        db.add_subscription(conn, "U1", "other", "7", "2", 25, DAY, 5)
        from bot import watcher
        watcher.tick(conn, cfg, registry_of(FakeOther(light="25")), DAY, {})
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM push_log")]
        self.assertEqual(kinds, ["turn"])


# --- wait estimate ----------------------------------------------------------------------

class TestEstimate(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(os.path.join(tempfile.mkdtemp(), "t.db"))

    def log(self, minute, done, light=None):
        db.log_progress(self.conn, "cmu_hch", "115", "2", DAY, 10000 + minute * 60,
                        light or done, done)

    def test_needs_enough_history(self):
        self.log(0, 10)
        self.log(5, 12)
        self.assertIsNone(estimate.rate_per_minute(self.conn, "cmu_hch", "115", "2", DAY,
                                                   now=10000 + 6 * 60))

    def test_short_window_says_nothing(self):
        for m, d in ((0, 10), (3, 12), (6, 14)):
            self.log(m, d)
        self.assertIsNone(estimate.rate_per_minute(self.conn, "cmu_hch", "115", "2", DAY,
                                                   now=10000 + 7 * 60))

    def test_stalled_clinic_says_nothing(self):
        """A doctor who stepped out is not a clinic that will never move."""
        for m in (0, 10, 20):
            self.log(m, 10)
        self.assertIsNone(estimate.rate_per_minute(self.conn, "cmu_hch", "115", "2", DAY,
                                                   now=10000 + 21 * 60))

    def test_steady_clinic(self):
        for m, d in ((0, 10), (10, 13), (20, 16), (30, 19)):     # 3 every 10 minutes
            self.log(m, d)
        rate = estimate.rate_per_minute(self.conn, "cmu_hch", "115", "2", DAY,
                                        now=10000 + 31 * 60)
        self.assertAlmostEqual(rate, 0.3)
        span = estimate.minutes_range(6, rate)                   # 6 ahead -> ~20 min
        self.assertEqual(span, (15, 25))
        self.assertEqual(estimate.describe(span), "大約再 15–25 分鐘")

    def test_old_readings_fall_out_of_the_window(self):
        for m, d in ((0, 0), (10, 30), (60, 31), (70, 32), (80, 33)):
            self.log(m, d)
        rate = estimate.rate_per_minute(self.conn, "cmu_hch", "115", "2", DAY,
                                        now=10000 + 81 * 60)
        self.assertAlmostEqual(rate, 0.1)

    def test_wording_edges(self):
        self.assertEqual(estimate.describe(None), "")
        self.assertEqual(estimate.describe((0, 5)), "大約 5 分鐘內")
        self.assertIsNone(estimate.minutes_range(0, 0.5))

    def test_fetch_feeds_the_log(self):
        adapter = FakeClient([clinic([1, 2, 3, 10], 2)])
        seen = []
        adapter.on_snapshot = lambda a, r, t, s: seen.append((a.id, r, t, s["light"]))
        adapter.fetch_room("115", "2")
        self.assertEqual(seen, [("cmu_hch", "115", "2", "2")])
        estimate.record(self.conn, "cmu_hch", "115", "2", DAY, clinic([1, 2, 3], 2), when=1)
        row = self.conn.execute("SELECT * FROM progress_log").fetchone()
        self.assertEqual((row["light"], row["done_count"]), (2, 2))


# --- 回診 / 領藥 ------------------------------------------------------------------------

CFG = dict(config.DEFAULTS, dryRunLine=True)


def refill(last, days=28, total=3, done=1):
    return {"kind": "refill", "last_fill_date": last, "days_per_fill": days,
            "fills_total": total, "fills_done": done}


def appt(day, **kw):
    row = {"kind": "appt", "appt_date": day, "hospital": "", "room": "", "doctor": "",
           "timecode": ""}
    row.update(kw)
    return row


def at(day, hhmm):
    return reminders.now_at(date.fromisoformat(day), hhmm)


class TestReminderDates(unittest.TestCase):
    def test_parse_date(self):
        today = date(2026, 10, 17)
        p = reminders.parse_date
        self.assertEqual(p("11/20", today), date(2026, 11, 20))
        self.assertEqual(p("1/5", today), date(2027, 1, 5), "an appointment rolls forward")
        self.assertEqual(p("12/30", date(2026, 1, 10), future=False), date(2025, 12, 30),
                         "a fill already collected rolls back across new year")
        self.assertEqual(p("10/20", today, future=False), date(2026, 10, 20),
                         "a few days ahead is a typo to report, not last year")
        self.assertEqual(p("10/10", today), date(2026, 10, 10),
                         "a week ago is a past appointment to report, not next year")
        self.assertEqual(p("115/11/20", today), date(2026, 11, 20), "民國年")
        self.assertEqual(p("11月20日", today), date(2026, 11, 20))
        self.assertEqual(p("明天", today), date(2026, 10, 18))
        self.assertIsNone(p("上午", today))
        with self.assertRaises(reminders.Invalid):
            p("2/30", today)

    def test_refill_window_follows_the_10_day_rule(self):
        plan = reminders.refill_plan(refill("2026-10-17"))
        self.assertEqual(plan["runout"], date(2026, 11, 14))
        self.assertEqual(plan["open"], date(2026, 11, 4))
        self.assertEqual(plan["close"], date(2026, 11, 13))
        self.assertEqual(plan["next_fill"], 2)

    def test_window_across_the_year_end(self):
        plan = reminders.refill_plan(refill("2026-12-10", days=30))
        self.assertEqual(plan["open"], date(2026, 12, 30))
        self.assertEqual(plan["close"], date(2027, 1, 8))

    def test_open_push_waits_for_the_morning(self):
        row = refill("2026-10-17")
        self.assertEqual(reminders.due(row, at("2026-11-03", "10:00"), CFG)[0], [])
        self.assertEqual(reminders.due(row, at("2026-11-04", "08:59"), CFG)[0], [])
        kinds = [k for k, _, _ in reminders.due(row, at("2026-11-04", "09:00"), CFG)[0]]
        self.assertEqual(kinds, ["refill_open"])

    def test_last_call_in_the_final_three_days(self):
        row = refill("2026-10-17")
        kinds = [k for k, _, _ in reminders.due(row, at("2026-11-10", "09:30"), CFG)[0]]
        self.assertEqual(kinds, ["refill_open"])
        kinds = [k for k, _, _ in reminders.due(row, at("2026-11-11", "09:30"), CFG)[0]]
        self.assertEqual(kinds, ["refill_open", "refill_last"])

    def test_occurrence_is_the_fill_number(self):
        """Fill 2 and fill 3 are different pushes; the dedupe key must tell them apart."""
        owed2 = reminders.due(refill("2026-10-17", done=1), at("2026-11-04", "09:00"), CFG)[0]
        owed3 = reminders.due(refill("2026-11-06", done=2), at("2026-11-24", "09:00"), CFG)[0]
        self.assertEqual(owed2[0][1], "2")
        self.assertEqual(owed3[0][1], "3")

    def test_last_fill_reminds_to_revisit(self):
        row = refill("2026-10-17", done=3)
        self.assertEqual(reminders.due(row, at("2026-11-06", "09:00"), CFG)[0], [])
        kinds = [k for k, _, _ in reminders.due(row, at("2026-11-07", "09:00"), CFG)[0]]
        self.assertEqual(kinds, ["refill_revisit"])
        self.assertTrue(reminders.due(row, at("2026-12-01", "09:00"), CFG)[1])

    def test_appointment_eve(self):
        row = appt("2026-11-20")
        self.assertEqual(reminders.due(row, at("2026-11-19", "18:59"), CFG)[0], [])
        owed, _ = reminders.due(row, at("2026-11-19", "19:00"), CFG)
        self.assertEqual(owed[0][0], "appt_eve")
        self.assertIn("11/20（五）", owed[0][2])
        # After 23:00 "tomorrow" is about to become wrong; better silent than late.
        self.assertEqual(reminders.due(row, at("2026-11-19", "23:30"), CFG)[0], [])
        self.assertEqual(reminders.due(row, at("2026-11-21", "09:00"), CFG), ([], True))


class TestReminderPushes(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(os.path.join(tempfile.mkdtemp(), "t.db"))
        db.touch_user(self.conn, "U1", owner_id="U1")

    def test_each_push_once_however_often_tick_runs(self):
        db.add_refill(self.conn, "U1", "2026-10-17", 28, 3, 1)
        for _ in range(5):
            reminders.tick(self.conn, CFG, now=at("2026-11-04", "09:00"))
        self.assertEqual(db.pushes_this_month(self.conn), 1)

    def test_reserve_keeps_pushes_for_turn_alerts(self):
        cfg = dict(CFG, monthlyPushBudget=20, reminderReserve=20)
        db.add_appt(self.conn, "U1", "2026-11-20")
        reminders.tick(self.conn, cfg, now=at("2026-11-19", "19:00"))
        self.assertEqual(db.pushes_this_month(self.conn), 0)

    def test_past_appointments_close(self):
        rid = db.add_appt(self.conn, "U1", "2026-11-20")
        reminders.tick(self.conn, CFG, now=at("2026-11-21", "09:00"))
        self.assertEqual(db.get_reminder(self.conn, rid)["state"], "done")


# --- conversation -----------------------------------------------------------------------

def texts(messages):
    return "\n".join(m.get("text") or m.get("altText", "") for m in messages)


class TestConversation(unittest.TestCase):
    NOW = datetime(2026, 8, 25, 14, 0, tzinfo=clock.TAIPEI)

    def setUp(self):
        from bot import convo
        self.convo = convo
        convo._pending.clear()
        self.conn = db.init(os.path.join(tempfile.mkdtemp(), "t.db"))
        self.cfg = dict(config.DEFAULTS, dryRunLine=True, ownerUserId="U1")
        self.cmu = FakeClient([clinic([1, 2, 3, 10, 20, 30, 38], 10)])
        self.other = FakeOther()
        db.touch_user(self.conn, "U1", owner_id="U1")
        self.patch = mock.patch("bot.clock.now", return_value=self.NOW)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def say(self, text, reg=None):
        return self.convo.handle_text(self.conn, self.cfg, reg or registry_of(self.cmu),
                                      "U1", text)

    def test_strangers_are_refused_without_hospital_traffic(self):
        out = self.convo.handle_text(self.conn, self.cfg, registry_of(self.cmu), "U9", "115")
        self.assertEqual(texts(out), self.convo.REFUSED)
        self.assertEqual(self.cmu.calls, 0)

    def test_room_then_number_subscribes(self):
        out = self.say("115")
        self.assertEqual(out[0]["type"], "flex")
        self.assertIn("現在看到 10 號", out[0]["altText"])
        out = self.say("38")
        self.assertIn("已設定提醒", out[0]["altText"])
        sub = db.user_subscriptions(self.conn, "U1", DAY)[0]
        self.assertEqual((sub["hospital"], sub["room"], sub["ticket_no"]),
                         ("cmu_hch", "115", 38))

    def test_one_shot_subscribe_and_bare_session_digit(self):
        self.say("115 下午 38")
        self.assertEqual(db.user_subscriptions(self.conn, "U1", DAY)[0]["ticket_no"], 38)
        self.say("115 2 30")
        tickets = sorted(r["ticket_no"] for r in db.user_subscriptions(self.conn, "U1", DAY))
        self.assertEqual(tickets, [30, 38])

    def test_with_two_hospitals_you_are_asked_once(self):
        reg = registry_of(self.cmu, self.other)
        out = self.say("115", reg)
        self.assertIn("請先選醫院", texts(out))
        self.assertEqual(self.cmu.calls + self.other.calls, 0)
        out = self.say("測試新竹", reg)
        self.assertIn("之後查的都是", texts(out))
        out = self.say("7 下午", reg)
        self.assertIn("測試新竹 7 診", out[0]["altText"])
        self.assertEqual(self.other.calls, 1)
        # Naming the other hospital in a query switches the default.
        self.say("中國醫新竹 115", reg)
        user = db.touch_user(self.conn, "U1", owner_id="U1")
        self.assertEqual(user["default_hospital"], "cmu_hch")

    def test_approximate_count_is_labelled(self):
        reg = registry_of(self.other)
        self.say("7 下午 25", reg)
        out = self.say("7 下午", reg)
        self.assertIn("前面大約還有 4 位", out[0]["altText"])

    def test_identifier_only_where_supported(self):
        reg = registry_of(self.cmu, self.other)
        self.say("測試新竹", reg)
        out = self.say("A123456789 0304", reg)
        self.assertIn("只支援中國醫新竹", texts(out))

    def test_appointment_flow(self):
        out = self.say("回診 9/3 上午 115 王醫師")
        self.assertIn("記好了", texts(out))
        self.assertIn("9/3（四）", texts(out))
        self.assertIn("晚上 7 點", texts(out))
        row = db.active_reminders(self.conn, "U1", "appt")[0]
        self.assertEqual((row["appt_date"], row["room"], row["timecode"], row["doctor"],
                          row["hospital"]), ("2026-09-03", "115", "1", "王醫師", "cmu_hch"))
        self.assertIn("日期一定要有", texts(self.say("回診")))
        self.assertIn("日期一定要有", texts(self.say("回診 上午")))
        self.assertIn("已經過了", texts(self.say("回診 8/20")))

    def test_appointment_day_hint_arms_the_room(self):
        """On the day, the first reply mentions it once, and the room is remembered so a bare
        ticket number subscribes."""
        db.add_appt(self.conn, "U1", DAY, "cmu_hch", "115", "", "2")
        out = self.say("說明")
        self.assertIn("今天要回診", texts(out))
        self.assertNotIn("今天要回診", texts(self.say("說明")), "only once a day")
        self.say("38")
        self.assertEqual(db.user_subscriptions(self.conn, "U1", DAY)[0]["room"], "115")

    def test_refill_flow(self):
        out = self.say("領藥 8/20 28天 3次")
        self.assertIn("第 2 次可以在 9/7（一） 到 9/16（三） 之間領", texts(out))
        self.patch.stop()
        self.patch = mock.patch("bot.clock.now",
                                return_value=datetime(2026, 9, 8, 10, 0, tzinfo=clock.TAIPEI))
        self.patch.start()
        out = self.say("已領")
        self.assertIn("9/8（二） 領了第 2 次", texts(out))
        self.assertIn("第 3 次可以在 9/26（六） 到 10/5（一） 之間領", texts(out))
        self.say("已領")
        out = self.say("我的提醒")
        self.assertIn("慢箋已領完 3 次", texts(out))
        self.assertIn("目前沒有要領的藥", texts(self.say("已領")))

    def test_refill_validation(self):
        self.assertIn("還沒到", texts(self.say("領藥 8/30 28天")))
        self.assertIn("7 到 90 天", texts(self.say("領藥 8/20 200天")))
        self.assertIn("次數看起來不對", texts(self.say("領藥 8/20 28天 3次 第4次")))
        self.assertIn("領藥提醒的寫法", texts(self.say("領藥")))

    def test_cancel_one_reminder_only_your_own(self):
        rid = db.add_appt(self.conn, "U1", "2026-09-03")
        db.touch_user(self.conn, "U2", owner_id="U2")
        other = db.add_appt(self.conn, "U2", "2026-09-03")
        self.assertIn("找不到", texts(self.say("取消 #%d" % other)))
        self.assertIn("取消了", texts(self.say("取消 #%d" % rid)))

    def test_forget_needs_confirmation(self):
        db.add_appt(self.conn, "U1", "2026-09-03")
        self.say("刪除我的資料")
        self.assertEqual(len(db.active_reminders(self.conn, "U1")), 1)
        self.say("確定刪除我的資料")
        self.assertEqual(len(db.active_reminders(self.conn, "U1")), 0)

    def test_every_reply_fits_line_limits(self):
        """LINE rejects a reply with more than 5 messages, quick-reply labels over 20
        characters, or a button label over 40."""
        self.say("領藥 8/20 28天 3次")
        db.add_appt(self.conn, "U1", DAY, "cmu_hch", "115", "", "2")
        for text in ("說明", "115", "38", "我的提醒", "查進度", "回診 9/3 115", "張三"):
            out = self.say(text)
            self.assertLessEqual(len(out), 5)
            for m in out:
                for item in (m.get("quickReply") or {}).get("items", []):
                    self.assertLessEqual(len(item["action"]["label"]), 20)


if __name__ == "__main__":
    unittest.main()
