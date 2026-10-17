"""sqlite3 access layer. Every query is parameterized - no string interpolation of user
input into SQL, ever.

Connections are thread-local: the webhook is served by ThreadingHTTPServer (a thread per
request) and the watcher runs on its own thread, and sqlite3 connections are not shareable
across threads by default.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

_local = threading.local()
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def now():
    """UTC ISO-8601 with second precision. Stored as TEXT so it sorts lexicographically."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path):
    parent = os.path.dirname(db_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get(db_path):
    """Thread-local connection, opened on first use in each thread."""
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    conn = cache.get(db_path)
    if conn is None:
        conn = cache[db_path] = connect(db_path)
    return conn


def close_all():
    for conn in getattr(_local, "conns", {}).values():
        conn.close()
    _local.conns = {}


# Columns added to an existing table after first release. CREATE TABLE IF NOT EXISTS does
# nothing to a DB that already exists, so new columns have to be applied by hand. Each entry
# must be nullable or carry a default, and adding one twice is a no-op. Changes that alter a
# key need a rebuild instead - see _migrate_v1.
MIGRATIONS = []

SCHEMA_VERSION = 1
# Tables whose key gained `hospital` in v1. users keeps its key and only gains columns.
_V1_REBUILT = ("subscriptions", "rooms", "room_whitelist", "recent_rooms", "push_log")
_V0_INDEXES = ("idx_subs_active", "idx_subs_seat", "idx_rooms_doctor",
               "idx_push_sent", "idx_push_once")


def _columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _roc_to_iso(value):
    """v0 stored the hospital's own ROC date, '115/08/25'."""
    try:
        y, m, d = (int(x) for x in value.split("/"))
        return "%04d-%02d-%02d" % (y + 1911, m, d)
    except (ValueError, AttributeError):
        return value


def _migrate_v1(conn, schema_sql):
    """Single-hospital v0 -> multi-hospital v1, in one transaction.

    SQLite cannot change a primary key in place, so each table whose key gains `hospital`
    is renamed aside, recreated from schema.sql, refilled, and the old copy dropped. Every
    v0 row belonged to the one hospital there was, cmu_hch. Dates move from the ROC form
    to ISO on the way.
    """
    if "hospital" in _columns(conn, "subscriptions"):
        return False
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        for idx in _V0_INDEXES:
            conn.execute("DROP INDEX IF EXISTS %s" % idx)
        for table in _V1_REBUILT:
            conn.execute("ALTER TABLE %s RENAME TO %s_v0" % (table, table))
        for column in ("default_hospital", "appt_hint_date"):
            if column not in _columns(conn, "users"):
                conn.execute("ALTER TABLE users ADD COLUMN %s TEXT" % column)
        # executescript would COMMIT the open transaction, so run statement by statement.
        for statement in _statements(schema_sql):
            conn.execute(statement)

        for r in conn.execute("SELECT * FROM subscriptions_v0").fetchall():
            conn.execute(
                "INSERT INTO subscriptions (id, line_user_id, hospital, room, timecode, "
                "ticket_no, visit_date, state, notify_ahead, last_current, pending_current, "
                "created_at, notified_at) VALUES (?, ?, 'cmu_hch', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["line_user_id"], r["room"], r["timecode"], r["ticket_no"],
                 _roc_to_iso(r["roc_date"]), r["state"], r["notify_ahead"],
                 r["last_current"], r["pending_current"], r["created_at"], r["notified_at"]))
        for r in conn.execute("SELECT * FROM rooms_v0").fetchall():
            conn.execute(
                "INSERT OR REPLACE INTO rooms (hospital, room, timecode, visit_date, doctor, "
                "updated_at) VALUES ('cmu_hch', ?, ?, ?, ?, ?)",
                (r["room"], r["timecode"], _roc_to_iso(r["roc_date"]), r["doctor"],
                 r["updated_at"]))
        for r in conn.execute("SELECT * FROM room_whitelist_v0").fetchall():
            conn.execute(
                "INSERT OR REPLACE INTO room_whitelist (hospital, room, last_seen_date) "
                "VALUES ('cmu_hch', ?, ?)", (r["room"], _roc_to_iso(r["last_seen_date"])))
        conn.execute(
            "INSERT INTO recent_rooms (line_user_id, hospital, room, timecode, doctor, "
            "last_used, use_count) SELECT line_user_id, 'cmu_hch', room, timecode, doctor, "
            "last_used, use_count FROM recent_rooms_v0")
        conn.execute(
            "INSERT INTO push_log (id, line_user_id, ref_type, ref_id, sent_at, kind) "
            "SELECT id, line_user_id, CASE WHEN sub_id IS NULL THEN NULL ELSE 'sub' END, "
            "sub_id, sent_at, kind FROM push_log_v0")

        for table in _V1_REBUILT:
            conn.execute("DROP TABLE %s_v0" % table)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def _statements(sql):
    """schema.sql split into statements. Comments are stripped first; the schema has no
    string literals containing ';' or '--' for this to trip over."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def init(db_path):
    conn = get(db_path)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscriptions'").fetchone()
    if has_tables and conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        if _migrate_v1(conn, schema_sql):
            print("database migrated to schema v1 (multi-hospital)")
    conn.executescript(schema_sql)
    for table, column, decl in MIGRATIONS:
        if column not in _columns(conn, table):
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    conn.commit()
    return conn


# --- users ---

def touch_user(conn, line_user_id, owner_id="", allowed=()):
    """Upsert on every inbound event, so a subscription always has a parent row.

    New users land as 'pending' (the schema default) unless config names them. Returns the
    row, so the caller can refuse a stranger before doing anything that costs money or
    hits the hospital.
    """
    conn.execute(
        "INSERT INTO users (line_user_id, created_at, last_seen_at) VALUES (?, ?, ?) "
        "ON CONFLICT(line_user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (line_user_id, now(), now()))
    if line_user_id == owner_id or line_user_id in allowed:
        conn.execute("UPDATE users SET status = 'active' WHERE line_user_id = ? "
                     "AND status = 'pending'", (line_user_id,))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE line_user_id = ?",
                        (line_user_id,)).fetchone()


def set_user_status(conn, line_user_id, status):
    cur = conn.execute("UPDATE users SET status = ? WHERE line_user_id = ?",
                       (status, line_user_id))
    conn.commit()
    return cur.rowcount


def pending_users(conn):
    return conn.execute("SELECT * FROM users WHERE status = 'pending' "
                        "ORDER BY created_at").fetchall()


def set_default_hospital(conn, line_user_id, hospital):
    conn.execute("UPDATE users SET default_hospital = ? WHERE line_user_id = ?",
                 (hospital, line_user_id))
    conn.commit()


def claim_appt_hint(conn, line_user_id, day):
    """True the first time it is called for this person on this day."""
    cur = conn.execute(
        "UPDATE users SET appt_hint_date = ? WHERE line_user_id = ? "
        "AND (appt_hint_date IS NULL OR appt_hint_date <> ?)", (day, line_user_id, day))
    conn.commit()
    return cur.rowcount == 1


def forget_user(conn, line_user_id):
    """Everything personal about one person, gone. The push log keeps its rows - they are
    the month's budget count - but loses the link to who they were for."""
    n = 0
    for table in ("reminders", "recent_rooms", "subscriptions"):
        n += conn.execute("DELETE FROM %s WHERE line_user_id = ?" % table,
                          (line_user_id,)).rowcount
    conn.execute("UPDATE push_log SET line_user_id = '' WHERE line_user_id = ?",
                 (line_user_id,))
    conn.execute("UPDATE users SET default_hospital = NULL, appt_hint_date = NULL "
                 "WHERE line_user_id = ?", (line_user_id,))
    conn.commit()
    return n


# --- subscriptions ---

def add_subscription(conn, line_user_id, hospital, room, timecode, ticket_no, visit_date,
                     notify_ahead):
    """Re-subscribing to the same seat resets it to active rather than erroring - the user
    may be re-arming a reminder they cancelled, or the seat may be from a previous day."""
    conn.execute(
        "INSERT INTO subscriptions (line_user_id, hospital, room, timecode, ticket_no, "
        "  visit_date, state, notify_ahead, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?) "
        "ON CONFLICT(line_user_id, hospital, room, timecode, ticket_no, visit_date) "
        "DO UPDATE SET state = 'active', notify_ahead = excluded.notify_ahead, "
        "  notified_at = NULL",
        (line_user_id, hospital, room, timecode, ticket_no, visit_date, notify_ahead, now()))
    conn.commit()


def active_subscriptions(conn, visit_date):
    return conn.execute(
        "SELECT * FROM subscriptions WHERE state IN ('active', 'notified') "
        "AND visit_date = ?", (visit_date,)).fetchall()


def user_subscriptions(conn, line_user_id, visit_date):
    return conn.execute(
        "SELECT * FROM subscriptions WHERE line_user_id = ? AND visit_date = ? "
        "AND state IN ('active', 'notified') ORDER BY hospital, room, ticket_no",
        (line_user_id, visit_date)).fetchall()


def remember_reading(conn, sub_id, last_current, pending_current):
    """Debounce bookkeeping: what we last saw, and what is awaiting confirmation."""
    conn.execute("UPDATE subscriptions SET last_current = ?, pending_current = ? WHERE id = ?",
                 (last_current, pending_current, sub_id))
    conn.commit()


def set_subscription_state(conn, sub_id, state, notified=False):
    if notified:
        conn.execute("UPDATE subscriptions SET state = ?, notified_at = ? WHERE id = ?",
                     (state, now(), sub_id))
    else:
        conn.execute("UPDATE subscriptions SET state = ? WHERE id = ?", (state, sub_id))
    conn.commit()


def cancel_subscriptions(conn, line_user_id, visit_date):
    cur = conn.execute(
        "UPDATE subscriptions SET state = 'cancelled' WHERE line_user_id = ? "
        "AND visit_date = ? AND state IN ('active', 'notified')",
        (line_user_id, visit_date))
    conn.commit()
    return cur.rowcount


def close_stale(conn, visit_date):
    """Sessions do not carry over: anything still open from an earlier day is done."""
    cur = conn.execute(
        "UPDATE subscriptions SET state = 'done' "
        "WHERE state IN ('active', 'notified') AND visit_date <> ?", (visit_date,))
    conn.commit()
    return cur.rowcount


# --- what each person keeps coming back to ---

def remember_room(conn, line_user_id, hospital, room, timecode, doctor):
    conn.execute(
        "INSERT INTO recent_rooms (line_user_id, hospital, room, timecode, doctor, last_used) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(line_user_id, hospital, room, timecode) DO UPDATE SET "
        "  doctor = excluded.doctor, last_used = excluded.last_used, "
        "  use_count = recent_rooms.use_count + 1",
        (line_user_id, hospital, room, timecode, doctor or "", now()))
    conn.commit()


def recent_rooms(conn, line_user_id, limit=4):
    """Most-used first, then most-recent. Someone who checks one clinic every month should
    see it above a room they opened once by mistake."""
    return conn.execute(
        "SELECT * FROM recent_rooms WHERE line_user_id = ? "
        "ORDER BY use_count DESC, last_used DESC LIMIT ?",
        (line_user_id, limit)).fetchall()


def forget_rooms(conn, line_user_id):
    conn.execute("DELETE FROM recent_rooms WHERE line_user_id = ?", (line_user_id,))
    conn.commit()


# --- room directory ---

def upsert_room(conn, hospital, room, timecode, visit_date, doctor):
    conn.execute(
        "INSERT INTO rooms (hospital, room, timecode, visit_date, doctor, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(hospital, room, timecode, visit_date) DO UPDATE SET "
        "  doctor = excluded.doctor, updated_at = excluded.updated_at",
        (hospital, room, timecode, visit_date, doctor, now()))
    conn.execute(
        "INSERT INTO room_whitelist (hospital, room, last_seen_date) VALUES (?, ?, ?) "
        "ON CONFLICT(hospital, room) DO UPDATE SET last_seen_date = excluded.last_seen_date",
        (hospital, room, visit_date))


def find_by_doctor(conn, name, visit_date, hospitals=None):
    """Substring match, so "張巨" finds "張巨成". LIKE wildcards in the user's text are
    escaped: without this, a message of "%" would list the entire day's clinics.
    `hospitals` limits the search to the enabled ones."""
    safe = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT * FROM rooms WHERE visit_date = ? AND doctor LIKE ? ESCAPE '\\' "
        "ORDER BY hospital, timecode, room", (visit_date, "%" + safe + "%")).fetchall()
    if hospitals is not None:
        rows = [r for r in rows if r["hospital"] in hospitals]
    return rows


def whitelist(conn, hospital):
    return [r["room"] for r in
            conn.execute("SELECT room FROM room_whitelist WHERE hospital = ? ORDER BY room",
                         (hospital,))]


# --- clinic progress history (feeds bot/estimate.py) ---

def log_progress(conn, hospital, room, timecode, visit_date, observed_at, light, done):
    conn.execute(
        "INSERT INTO progress_log (hospital, room, timecode, visit_date, observed_at, "
        "light, done_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (hospital, room, timecode, visit_date, observed_at, light, done))
    conn.commit()


def progress_since(conn, hospital, room, timecode, visit_date, since):
    return conn.execute(
        "SELECT observed_at, light, done_count FROM progress_log WHERE hospital = ? "
        "AND room = ? AND timecode = ? AND visit_date = ? AND observed_at >= ? "
        "ORDER BY observed_at", (hospital, room, timecode, visit_date, since)).fetchall()


def prune_progress(conn, before_date):
    cur = conn.execute("DELETE FROM progress_log WHERE visit_date < ?", (before_date,))
    conn.commit()
    return cur.rowcount


# --- push quota ---

def pushes_this_month(conn):
    """LINE's quota resets on a calendar month, counted in the account's own timezone.
    Close enough with UTC prefixes: a push in the last hours of a month is the only case
    that could land in the wrong bucket, and the budget carries slack for exactly that."""
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute("SELECT COUNT(*) AS n FROM push_log WHERE sent_at LIKE ?",
                       (prefix + "%",)).fetchone()
    return row["n"]


def claim_push(conn, line_user_id, ref_type, ref_id, kind, occurrence=""):
    """Reserve the right to send one push, atomically.

    Written before the HTTP call, not after: idx_push_once turns "have we already pinged
    this person about this" into an INSERT that either succeeds once or raises. Returns
    False if this push was already spent. A send that then fails costs one unit of an
    unusually generous budget - far better than a crash between send and log letting the
    same person be woken twice.
    """
    try:
        conn.execute(
            "INSERT INTO push_log (line_user_id, ref_type, ref_id, occurrence, sent_at, kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (line_user_id, ref_type, ref_id, occurrence, now(), kind))
    except sqlite3.IntegrityError:
        return False
    conn.commit()
    return True


# --- 回診 / 領藥 reminders ---

def add_appt(conn, line_user_id, appt_date, hospital="", room="", doctor="", timecode=""):
    cur = conn.execute(
        "INSERT INTO reminders (line_user_id, kind, hospital, room, doctor, timecode, "
        "appt_date, created_at) VALUES (?, 'appt', ?, ?, ?, ?, ?, ?)",
        (line_user_id, hospital, room, doctor, timecode, appt_date, now()))
    conn.commit()
    return cur.lastrowid


def add_refill(conn, line_user_id, last_fill_date, days_per_fill, fills_total, fills_done):
    cur = conn.execute(
        "INSERT INTO reminders (line_user_id, kind, last_fill_date, days_per_fill, "
        "fills_total, fills_done, created_at) VALUES (?, 'refill', ?, ?, ?, ?, ?)",
        (line_user_id, last_fill_date, days_per_fill, fills_total, fills_done, now()))
    conn.commit()
    return cur.lastrowid


def active_reminders(conn, line_user_id=None, kind=None):
    sql, args = "SELECT * FROM reminders WHERE state = 'active'", []
    if line_user_id is not None:
        sql += " AND line_user_id = ?"
        args.append(line_user_id)
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY COALESCE(appt_date, last_fill_date), id"
    return conn.execute(sql, args).fetchall()


def get_reminder(conn, reminder_id):
    return conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()


def set_reminder_state(conn, reminder_id, state):
    conn.execute("UPDATE reminders SET state = ? WHERE id = ?", (state, reminder_id))
    conn.commit()


def record_fill(conn, reminder_id, fill_date):
    """One more fill collected. Returns the updated row."""
    conn.execute("UPDATE reminders SET fills_done = fills_done + 1, last_fill_date = ? "
                 "WHERE id = ?", (fill_date, reminder_id))
    conn.commit()
    return get_reminder(conn, reminder_id)


def cancel_reminder(conn, line_user_id, reminder_id):
    """Only the owner can cancel - the id comes from a chat message."""
    cur = conn.execute("UPDATE reminders SET state = 'cancelled' WHERE id = ? "
                       "AND line_user_id = ? AND state = 'active'",
                       (reminder_id, line_user_id))
    conn.commit()
    return cur.rowcount
