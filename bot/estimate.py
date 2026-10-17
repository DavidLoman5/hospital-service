"""How many minutes until my turn - estimated from how fast this clinic has been moving.

Every real fetch logs the clinic's public progress (bot/db.log_progress). The rate is
people seen per minute over the recent window; minutes = people ahead / rate, shown as a
range because doctors do not see patients at a steady pace.

It says nothing rather than something misleading:
  * fewer than MIN_SAMPLES readings, or a window shorter than MIN_SPAN minutes - a clinic
    that has seen two people in three minutes is not running at forty an hour;
  * nobody seen across the window - a doctor stepped out, the rate is not zero forever.
"""

import time

from bot import db, waiting

WINDOW_MINUTES = 40
MIN_SAMPLES = 3
MIN_SPAN_MINUTES = 10
MIN_SEEN = 2


def record(conn, hospital, room, timecode, visit_date, snap, when=None):
    light = waiting.light_number(snap)
    if light is None:
        return                  # not open: nothing is moving yet
    db.log_progress(conn, hospital, room, timecode, visit_date,
                    when if when is not None else time.time(),
                    light, waiting.done_count(snap))


def rate_per_minute(conn, hospital, room, timecode, visit_date, now=None):
    now = now if now is not None else time.time()
    rows = db.progress_since(conn, hospital, room, timecode, visit_date,
                             now - WINDOW_MINUTES * 60)
    rows = [r for r in rows if r["done_count"] is not None]
    if len(rows) < MIN_SAMPLES:
        return None
    span = (rows[-1]["observed_at"] - rows[0]["observed_at"]) / 60.0
    seen = rows[-1]["done_count"] - rows[0]["done_count"]
    if span < MIN_SPAN_MINUTES or seen < MIN_SEEN:
        return None
    return seen / span


def minutes_range(ahead, rate):
    """(low, high) in minutes, rounded to 5, or None. Zero ahead is "any moment" and is
    worded by the caller, not estimated."""
    if ahead is None or rate is None or rate <= 0 or ahead <= 0:
        return None
    mid = ahead / rate
    low = max(0, int(mid * 0.8 // 5 * 5))
    high = max(5, int(-(-mid * 1.25 // 5) * 5))
    if low == high:
        high += 5
    return low, high


def minutes_for(conn, hospital, room, timecode, visit_date, ahead, now=None):
    return minutes_range(ahead, rate_per_minute(conn, hospital, room, timecode,
                                                visit_date, now))


def describe(span):
    """'大約再 15–25 分鐘', or '' when there is no estimate."""
    if not span:
        return ""
    low, high = span
    if high <= 5:
        return "大約 5 分鐘內"
    if low >= 180:
        return "可能還要 3 小時以上"
    return "大約再 %d–%d 分鐘" % (low, high)
