"""Taipei time.

Hospital pages only ever describe *today*, so "which day is it" is a domain question, not a
formatting detail. Days are stored as ISO dates ("2026-08-25"); adapters convert whatever
their hospital prints (中國醫 uses the ROC calendar, 115/08/25) before it gets this far.

ZoneInfo is used explicitly rather than relying on the host clock: this host happens to be
on Asia/Taipei, but a test that passes only because of that is a test that will fail on the
next machine, and the day boundary is exactly where a reminder would misfire.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")

SESSION_NAMES = {"1": "上午", "2": "下午", "3": "晚上"}
NAME_TO_CODE = {"上午": "1", "早上": "1", "am": "1",
                "下午": "2", "午後": "2", "pm": "2",
                "晚上": "3", "夜診": "3", "夜間": "3", "night": "3"}


def now():
    return datetime.now(TAIPEI)


def today(when=None):
    """'2026-08-25' in Taipei - the day key for subscriptions, the directory and reminders.
    Lexicographic order is date order, which the reminder queries rely on."""
    return (when or now()).date().isoformat()


def guess_timecode(when=None):
    """Which session a bare room number most likely means.

    Boundaries sit in the gaps between sessions (morning runs to 12:00, afternoon from
    13:30), so a query at 12:30 gets the afternoon - the one about to start is more useful
    than the one that just ended. The reply always states which session it assumed.
    """
    t = (when or now()).time()
    if t < time(12, 30):
        return "1"
    if t < time(17, 40):
        return "2"
    return "3"
