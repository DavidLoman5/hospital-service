"""回診 (appointment) and 領藥 (chronic-prescription refill) reminders.

Both are pure calendar arithmetic - no hospital traffic. The watcher calls tick() every
loop; due() decides, from a reminder row and the current time, which pushes are owed, and
the push log's unique index makes each one happen at most once however often tick runs.

慢性處方箋 (NHI 連續處方箋) rules this follows: a prescription covers fills_total fills of
days_per_fill days. Fill n+1 may be collected within the 10 days before fill n runs out.
So with fill n collected on day L:

    runs out    R = L + days        (the first day without medicine)
    window      R - 10 .. R - 1     (when fill n+1 can be collected)

Pushes, all in the morning (refillTime) so nobody is woken:
    refill_open     on the first day of the window          "可以去領第 n+1 次了"
    refill_last     3 days before the window closes, if still not collected
    refill_revisit  a week before the LAST fill runs out     "記得回診拿新處方"
and for appointments:
    appt_eve        the evening before (apptEveTime)

A push that should have gone out while the bot was down is still sent later the same
window (refills) or not at all (appt_eve after midnight - "tomorrow" would then be wrong).
"""

import re
from datetime import date, datetime, time, timedelta

from bot import clock, db, line_api

WINDOW_DAYS = 10
LAST_CALL_DAYS = 3
REVISIT_DAYS = 7
STALE_DAYS = 14             # a refill nobody reported collecting is closed after this
EVE_CUTOFF = time(23, 0)
ROLLOVER_DAYS = 180

WEEKDAYS = "一二三四五六日"
MD_RE = re.compile(r"^(?:(\d{2,4})[/.-])?(\d{1,2})[/.-](\d{1,2})$")
MD_ZH_RE = re.compile(r"^(?:(\d{2,4})年)?(\d{1,2})月(\d{1,2})[日號号]?$")
DAYS_RE = re.compile(r"^(\d{1,3})天(?:份)?$")
FILLS_RE = re.compile(r"^(?:共)?(\d)次$")
NTH_RE = re.compile(r"^第(\d)次$")


class Invalid(ValueError):
    """Message shown to the user as-is."""


# --- dates ---

def show(d):
    """date -> '11/20（四）'."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return "%d/%d（%s）" % (d.month, d.day, WEEKDAYS[d.weekday()])


def parse_date(token, today, future=True):
    """'11/20', '2026/11/20', '115/11/20', '11月20日', '今天', '明天', '後天' -> date.

    Without a year, `future` picks the next such date (an appointment) or the most recent
    one (a refill already collected) - but only across a year boundary, i.e. when the date
    is more than half a year the wrong way. "8/30" typed on 8/25 for a refill is a
    mistake to point out, not last August.
    """
    rel = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "後天": 2, "昨天": -1, "前天": -2}
    if token in rel:
        return today + timedelta(days=rel[token])
    m = MD_RE.match(token) or MD_ZH_RE.match(token)
    if not m:
        return None
    year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        if year:
            y = int(year)
            y = y + 1911 if y < 1000 else y           # 民國年
            return date(y, month, day)
        d = date(today.year, month, day)
    except ValueError:
        raise Invalid("日期 %s 不存在，請再確認一次。" % token) from None
    try:
        if future and (today - d).days > ROLLOVER_DAYS:
            d = date(today.year + 1, month, day)
        if not future and (d - today).days > ROLLOVER_DAYS:
            d = date(today.year - 1, month, day)
    except ValueError:          # 2/29 into a non-leap year
        pass
    return d


# --- refill arithmetic ---

def refill_plan(row):
    last = date.fromisoformat(row["last_fill_date"])
    runout = last + timedelta(days=row["days_per_fill"])
    return {"runout": runout,
            "open": runout - timedelta(days=WINDOW_DAYS),
            "close": runout - timedelta(days=1),
            "next_fill": row["fills_done"] + 1,
            "final": row["fills_done"] >= row["fills_total"]}


def due(row, now, cfg, registry=None):
    """[(kind, occurrence, text)] owed right now, plus whether the reminder is finished.
    Pure - takes the clock as an argument so tests can walk it through a month."""
    today = now.date()
    owed = []
    if row["kind"] == "appt":
        appt = date.fromisoformat(row["appt_date"])
        if today >= appt:
            return owed, today > appt
        eve_at = _at(cfg["apptEveTime"])
        if today == appt - timedelta(days=1) and eve_at <= now.time() < EVE_CUTOFF:
            owed.append(("appt_eve", "", appt_text(row, registry)))
        return owed, False

    plan = refill_plan(row)
    morning = now.time() >= _at(cfg["refillTime"])
    if plan["final"]:
        if today > plan["runout"] + timedelta(days=STALE_DAYS):
            return owed, True
        if morning and plan["runout"] - timedelta(days=REVISIT_DAYS) <= today < plan["runout"]:
            owed.append(("refill_revisit", str(row["fills_total"]),
                         "💊 慢性處方箋的藥快吃完了\n"
                         "最後一次領的藥會在 %s 用完。\n\n"
                         "記得掛號回診，請醫師開新的處方。" % show(plan["runout"])))
        return owed, False

    if today > plan["close"] + timedelta(days=STALE_DAYS):
        return owed, True
    n, total = plan["next_fill"], row["fills_total"]
    if morning and plan["open"] <= today <= plan["close"]:
        owed.append(("refill_open", str(n),
                     "💊 可以去領慢性病的藥了\n"
                     "這是第 %d 次（共 %d 次）\n"
                     "請在 %s 之前領。\n\n"
                     "記得帶健保卡和慢性處方箋。\n"
                     "領完跟我說「已領」。" % (n, total, show(plan["close"]))))
        if today >= plan["close"] - timedelta(days=LAST_CALL_DAYS - 1):
            owed.append(("refill_last", str(n),
                         "💊 提醒：第 %d 次的藥還沒領嗎？\n"
                         "最晚 %s 要領，只剩 %d 天。\n\n"
                         "如果已經領了，跟我說「已領」。"
                         % (n, show(plan["close"]), (plan["close"] - today).days + 1)))
    return owed, False


def appt_text(row, registry=None):
    where = " ".join(x for x in (_hospital_name(row, registry), _room(row), row["doctor"])
                     if x)
    session = clock.SESSION_NAMES.get(row["timecode"], "")
    return ("📅 明天要回診\n%s %s\n%s\n\n"
            "記得帶健保卡。\n"
            "掛號後把看診號碼傳給我，快輪到時我會通知你。"
            % (show(row["appt_date"]), session, where or "")).replace("\n\n\n", "\n\n")


def describe(row, today, registry=None):
    """One line for 「我的提醒」."""
    if row["kind"] == "appt":
        where = " ".join(x for x in (_hospital_name(row, registry), _room(row),
                                     row["doctor"]) if x)
        return "📅 回診 %s %s %s" % (show(row["appt_date"]),
                                     clock.SESSION_NAMES.get(row["timecode"], ""),
                                     where)
    plan = refill_plan(row)
    if plan["final"]:
        return "💊 慢箋已領完 %d 次，藥在 %s 用完，記得回診" % (
            row["fills_total"], show(plan["runout"]))
    if today < plan["open"]:
        return "💊 第 %d 次領藥：%s 起可以領（最晚 %s）" % (
            plan["next_fill"], show(plan["open"]), show(plan["close"]))
    return "💊 第 %d 次領藥：現在可以領，最晚 %s" % (plan["next_fill"], show(plan["close"]))


# --- the loop ---

def tick(conn, cfg, registry=None, now=None):
    """Send whatever is owed. Cheap enough to run every watcher loop: one small query."""
    now = now or clock.now()
    for row in db.active_reminders(conn):
        owed, finished = due(row, now, cfg, registry)
        for kind, occurrence, text in owed:
            if line_api.push(conn, cfg, row["line_user_id"], text, kind,
                             ("reminder", row["id"]), occurrence,
                             reserve=cfg["reminderReserve"]):
                print("reminders: pushed %s #%d %s" % (kind, row["id"], occurrence))
        if finished:
            db.set_reminder_state(conn, row["id"], "done")


def _at(hhmm):
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _hospital_name(row, registry):
    if not row["hospital"]:
        return ""
    adapter = registry.get(row["hospital"]) if registry else None
    return adapter.short if adapter else ""


def _room(row):
    return "%s 診" % row["room"] if row["room"] else ""


def now_at(day, hhmm):
    """Test helper: an aware datetime in Taipei."""
    return datetime.combine(day, _at(hhmm), tzinfo=clock.TAIPEI)
