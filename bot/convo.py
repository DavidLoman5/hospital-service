"""The conversation: an inbound LINE event becomes a list of reply messages.

Everything here answers with reply(), which LINE does not count against the monthly
message quota. Only bot/watcher.py (and the reminders it runs) pushes. That split is why a
family can query as much as they like on a free plan.

Which hospital a message means: one named in the message wins (and becomes that person's
default), otherwise their default, otherwise the only enabled one, otherwise they are asked
once with a row of buttons. So "台大新竹 7" switches, and every bare "7" afterwards stays
there - nobody has to repeat the hospital on every message.

Pending conversation state (the room someone just looked at, so a bare ticket number knows
what it refers to) lives in a plain in-memory dict, not the database. It holds nothing
personal, it is worthless after ten minutes, and a restart clearing it costs the user one
re-query.
"""

import re
import time

from bot import clock, db, estimate, flex, line_api, reminders, validate, waiting
from bot.fetch import HospitalError

PENDING_TTL = 600
_pending = {}          # line_user_id -> (expires_at, {"hospital", "room", "timecode"})

REFUSED = "這是私人使用的服務，沒有開放給其他人，抱歉。"

HELP_WORDS = ("說明", "help", "?", "？", "使用說明", "怎麼用")
RECENT_WORDS = ("查進度", "常用", "最近")
LIST_WORDS = ("我的提醒", "我的", "提醒")
CANCEL_WORDS = ("取消", "取消提醒", "全部取消")
SWITCH_WORDS = ("換醫院", "選醫院", "醫院", "切換醫院")
APPT_WORDS = ("回診", "預約", "看診日")
REFILL_WORDS = ("領藥", "慢箋", "拿藥", "慢性處方箋")
FILLED_WORDS = ("已領", "已領藥", "領好了", "領了")
FORGET_ASK = "刪除我的資料"
FORGET_CONFIRM = "確定刪除我的資料"
CANCEL_ONE_RE = re.compile(r"^取消\s*#(\d+)$")
ID_RE = re.compile(r"^#(\d+)$")


def _help(registry):
    names = "、".join(a.short for a in registry.all())
    lines = [
        "🏥 醫院看診小幫手",
        "可查：%s" % names,
        "",
        "【查進度】",
        "・直接傳診間號碼：115",
        "・指定時段：115 下午",
        "・換醫院：傳醫院名字，例如「%s」" % registry.all()[0].short,
        "・用醫師名字找：王大明",
        "",
        "【快到時通知我】",
        "・查完進度後，傳你的看診號碼",
        "・或一次傳：115 下午 38",
        "",
        "【回診、領藥提醒】",
        "・回診 11/20 上午 115",
        "・領藥 10/17 28天 3次",
        "　（10/17 領了第 1 次，每次 28 天份，共 3 次）",
        "・領完藥傳「已領」",
        "",
        "・我的提醒 → 看全部、取消",
        "",
        "⚠️ 進度僅供參考，請以診間叫號為準。",
    ]
    if any(a.supports_id_lookup for a in registry.all()):
        idx = lines.index("【快到時通知我】") - 1
        lines.insert(idx, "・不知道診間？傳「身分證 生日」，例如：A123456789 0304"
                          "（僅%s）" % "、".join(a.short for a in registry.all()
                                               if a.supports_id_lookup))
    return "\n".join(lines)


def _remember(user_id, hospital, room, timecode):
    _pending[user_id] = (time.monotonic() + PENDING_TTL,
                         {"hospital": hospital, "room": room, "timecode": timecode})


def _recall(user_id):
    hit = _pending.get(user_id)
    if not hit or hit[0] < time.monotonic():
        _pending.pop(user_id, None)
        return None
    return hit[1]


def _cmd(registry, adapter, room, timecode):
    """The text a button sends to re-ask about a room. Names the hospital only when there
    is more than one to choose from - otherwise it is just noise on a small button."""
    session = clock.SESSION_NAMES.get(timecode, "")
    if registry.only():
        return "%s %s" % (room, session)
    return "%s %s %s" % (adapter.short, room, session)


def handle_text(conn, cfg, registry, user_id, text):
    """Returns a list of LINE message objects. Never raises for user input."""
    raw = validate.normalize(text)
    user = db.touch_user(conn, user_id, cfg.get("ownerUserId", ""),
                         tuple(cfg.get("allowedUserIds", ())))
    if user["status"] != "active":
        # One flat refusal. A stranger must not be able to reach the hospital through us,
        # or spend a push, or use us to probe identifiers.
        return [line_api.text_msg(REFUSED)]

    out = _dispatch(conn, cfg, registry, user, raw)
    hint = _appt_today_hint(conn, registry, user_id)
    if hint and len(out) < 5:
        out.append(hint)
    return out


def _dispatch(conn, cfg, registry, user, raw):
    user_id = user["line_user_id"]
    parts = raw.split()
    head = parts[0] if parts else ""

    if raw in HELP_WORDS or not raw:
        return [line_api.text_msg(_help(registry))]
    if raw in RECENT_WORDS:
        return _recent_menu(conn, registry, user_id)
    if raw in LIST_WORDS:
        return _list_all(conn, registry, user_id)
    if raw in CANCEL_WORDS:
        n = db.cancel_subscriptions(conn, user_id, clock.today())
        return [line_api.text_msg("好，已取消今天 %d 個叫號提醒。" % n if n
                                  else "今天沒有叫號提醒。",
                                  [("我的提醒", "我的提醒")])]
    if raw in SWITCH_WORDS:
        return _choose_hospital(registry, "要查哪一家醫院？")
    if raw == "設提醒":
        ctx = _recall(user_id)
        if ctx:
            return [line_api.text_msg("請傳你的看診號碼（掛號單上的號碼），例如：38")]
        return [line_api.text_msg("請先傳診間號碼查進度，再傳你的看診號碼。")]
    if raw == FORGET_ASK:
        return [line_api.text_msg(
            "這會刪掉你所有的提醒和常用診間，刪了就找不回來。確定嗎？",
            [("確定刪除", FORGET_CONFIRM), ("不要刪", "我的提醒")])]
    if raw == FORGET_CONFIRM:
        db.forget_user(conn, user_id)
        _pending.pop(user_id, None)
        return [line_api.text_msg("已經刪除你的提醒和查詢紀錄。")]

    if head in APPT_WORDS:
        return _add_appt(conn, cfg, registry, user, parts[1:])
    if head in REFILL_WORDS:
        return _add_refill(conn, user_id, parts[1:])
    if head in FILLED_WORDS:
        return _mark_filled(conn, user_id, parts[1:])
    m = CANCEL_ONE_RE.match(raw)
    if m:
        n = db.cancel_reminder(conn, user_id, int(m.group(1)))
        return [line_api.text_msg("好，這個提醒取消了。" if n else "找不到這個提醒。",
                                  [("我的提醒", "我的提醒")])]

    # "A123456789 0304" - identifier and birthday in ONE message, deliberately. There is no
    # two-step flow because a two-step flow would have to hold the identifier somewhere
    # between the messages, and then "we never store it" would depend on remembering to
    # delete it rather than on there being nowhere to put it.
    if _looks_like_identifier(head):
        return _lookup_by_identifier(conn, cfg, registry, user, parts)

    named, rest = registry.match(raw)
    if named:
        db.set_default_hospital(conn, user_id, named.id)
        if not rest:
            return [line_api.text_msg(
                "好，之後查的都是「%s」。\n請傳診間號碼，例如：%s"
                % (named.name, "115" if named.room_hint.endswith("115") else "診間號碼"),
                _recent_quick(conn, registry, user_id, named.id))]
        raw, adapter = rest, named
        parts = raw.split()
        head = parts[0]
    else:
        adapter = _current(registry, user)
    if adapter is None:
        return _choose_hospital(registry, "請先選醫院，之後就會記住：")

    if adapter.looks_like_room(head):
        return _room_query(conn, cfg, registry, adapter, user_id, parts)

    # A bare number after a room query is the answer to "which number are you?"
    ctx = _recall(user_id)
    if head.isdigit() and ctx:
        target = registry.get(ctx["hospital"]) or adapter
        return _subscribe(conn, cfg, registry, target, user_id, ctx["room"],
                          ctx["timecode"], head)
    if head.isdigit():
        return [line_api.text_msg(
            "%s的診間號碼請輸入%s。\n如果這是你的看診號碼，請先傳診間號碼查進度。"
            % (adapter.short, adapter.room_hint))]

    return _doctor_search(conn, registry, user_id, raw)


def _current(registry, user):
    if user["default_hospital"]:
        adapter = registry.get(user["default_hospital"])
        if adapter:
            return adapter
    return registry.only()


def _choose_hospital(registry, prompt):
    return [line_api.text_msg(
        prompt + "\n\n" + "\n".join("・%s" % a.name for a in registry.all()),
        [(a.short, a.short) for a in registry.all()])]


# --- room progress ---

def _room_query(conn, cfg, registry, adapter, user_id, parts):
    try:
        room = adapter.normalize_room(parts[0])
    except validate.Invalid as exc:
        return [line_api.text_msg(str(exc))]

    timecode, used = _read_timecode(parts[1:])
    # "115 下午 38" subscribes in one shot.
    tail = [p for i, p in enumerate(parts[1:]) if p.isdigit() and i != used]
    if tail:
        return _subscribe(conn, cfg, registry, adapter, user_id, room, timecode, tail[-1])

    try:
        snap = adapter.fetch_room(room, timecode)
    except HospitalError:
        return [line_api.text_msg("暫時連不上%s的系統，請稍後再試，或改看醫院官網：\n%s"
                                  % (adapter.short, adapter.room_url(room, timecode)))]

    return _render_room(conn, registry, adapter, user_id, room, timecode, snap)


def _render_room(conn, registry, adapter, user_id, room, timecode, snap):
    session = clock.SESSION_NAMES.get(timecode, "")
    quick = [(name, _cmd(registry, adapter, room, code))
             for code, name in clock.SESSION_NAMES.items()]

    if snap["kind"] == "bad_room":
        return [line_api.text_msg("診間號碼請輸入%s。" % adapter.room_hint)]
    if snap["kind"] == "no_room":
        return [line_api.text_msg(
            "%s %s 診%s沒有門診資料，可能是今天沒有這一診，或時段不對。\n"
            "可以按下面的按鈕換時段。" % (adapter.short, room, session), quick)]
    if snap["kind"] != "ok":
        return [line_api.text_msg("醫院回應的格式看不懂，請改看醫院官網：\n%s"
                                  % adapter.room_url(room, timecode))]

    _remember(user_id, adapter.id, room, timecode)
    db.remember_room(conn, user_id, adapter.id, room, timecode, snap.get("doctor"))

    # If they already have a reminder here, answer the question they actually care about
    # ("how many ahead of ME") instead of the room-level count.
    ticket_no = ahead = minutes = None
    for sub in db.user_subscriptions(conn, user_id, clock.today()):
        if (sub["hospital"], sub["room"], sub["timecode"]) == (adapter.id, room, timecode):
            ticket_no = sub["ticket_no"]
            ahead = waiting.waiting_ahead(snap, ticket_no)
            minutes = estimate.minutes_for(conn, adapter.id, room, timecode,
                                           clock.today(), ahead)
            break

    refresh = _cmd(registry, adapter, room, timecode)
    card = flex.progress(adapter, room, timecode, snap, ticket_no, ahead, minutes,
                         refresh_text=refresh, subscribed=ticket_no is not None)
    if ticket_no is None:
        card["quickReply"] = line_api.text_msg("", quick)["quickReply"]
    else:
        card["quickReply"] = line_api.text_msg(
            "", [("重新整理", refresh), ("我的提醒", "我的提醒"),
                 ("取消", "取消")])["quickReply"]
    return [card]


def _read_timecode(rest):
    """(session code, index of the token it came from or None)."""
    for i, token in enumerate(rest):
        if token in clock.NAME_TO_CODE:
            return clock.NAME_TO_CODE[token], i
    # A lone 1/2/3 right after the room is a session code only when a ticket number
    # follows it ("115 2 38"); "115 2" means ticket number 2.
    if len(rest) >= 2 and rest[0] in ("1", "2", "3"):
        return rest[0], 0
    return clock.guess_timecode(), None


# --- subscriptions ---

def _subscribe(conn, cfg, registry, adapter, user_id, room, timecode, number):
    try:
        ticket = validate.ticket_no(number)
    except validate.Invalid as exc:
        return [line_api.text_msg(str(exc))]

    try:
        snap = adapter.fetch_room(room, timecode)
    except HospitalError:
        return [line_api.text_msg("暫時連不上%s的系統，請稍後再試。" % adapter.short)]

    session = clock.SESSION_NAMES.get(timecode, "")
    where = "%s %s 診%s" % (adapter.short, room, session)
    if snap["kind"] != "ok":
        return [line_api.text_msg("%s目前查不到門診資料，請確認診間和時段。" % where)]

    ahead = waiting.waiting_ahead(snap, ticket)
    if ahead is None:
        if waiting.has_list(snap):
            return [line_api.text_msg(
                "%s的名單裡沒有 %d 號，請對一下掛號單上的診間和號碼。" % (where, ticket))]
        ahead = 0 if waiting.light_number(snap) is None else None

    db.add_subscription(conn, user_id, adapter.id, room, timecode, ticket, clock.today(),
                        cfg["defaultNotifyAhead"])
    db.remember_room(conn, user_id, adapter.id, room, timecode, snap.get("doctor"))
    minutes = estimate.minutes_for(conn, adapter.id, room, timecode, clock.today(), ahead)

    refresh = _cmd(registry, adapter, room, timecode)
    card = flex.subscribed(adapter, room, timecode, snap, ticket, ahead or 0,
                           cfg["defaultNotifyAhead"], minutes, refresh_text=refresh)
    card["quickReply"] = line_api.text_msg(
        "", [("看現在進度", refresh), ("我的提醒", "我的提醒"),
             ("取消", "取消")])["quickReply"]

    left = line_api.budget_left(conn, cfg)
    if left < 10:
        return [card, line_api.text_msg(
            "⚠️ 這個月能主動通知的次數只剩 %d 次，用完之後請自己查進度。" % left)]
    return [card]


def _recent_quick(conn, registry, user_id, hospital=None):
    quick = []
    for r in db.recent_rooms(conn, user_id, limit=8):
        adapter = registry.get(r["hospital"])
        if adapter is None or (hospital and r["hospital"] != hospital):
            continue
        text = _cmd(registry, adapter, r["room"], r["timecode"])
        quick.append((text, text))
    return quick[:4]


def _recent_menu(conn, registry, user_id):
    rows = [r for r in db.recent_rooms(conn, user_id, limit=6) if registry.get(r["hospital"])]
    if not rows:
        return [line_api.text_msg(
            "還沒有常用的診間。查過一次之後我就會記住，下次按一下就能查。\n\n"
            "請直接傳診間號碼，例如：115")]
    quick = []
    lines = ["最近查過的診間（按下面按鈕就能查）："]
    for r in rows:
        adapter = registry.get(r["hospital"])
        text = _cmd(registry, adapter, r["room"], r["timecode"])
        lines.append("・%s　%s" % (text, r["doctor"] or ""))
        quick.append((text, text))
    return [line_api.text_msg("\n".join(lines), quick)]


def _list_all(conn, registry, user_id):
    today = clock.today()
    subs = db.user_subscriptions(conn, user_id, today)
    rems = db.active_reminders(conn, user_id)
    if not subs and not rems:
        return [line_api.text_msg(
            "目前沒有任何提醒。\n\n"
            "・叫號提醒：查完進度後傳看診號碼\n"
            "・回診提醒：回診 11/20 上午 115\n"
            "・領藥提醒：領藥 10/17 28天 3次")]
    lines, quick = [], []
    if subs:
        lines.append("🔔 今天的叫號提醒")
        for r in subs:
            adapter = registry.get(r["hospital"])
            lines.append("・%s %s 診 %s %d 號%s" % (
                adapter.short if adapter else "", r["room"],
                clock.SESSION_NAMES.get(r["timecode"], ""), r["ticket_no"],
                "（已通知）" if r["state"] == "notified" else ""))
        quick.append(("取消今天叫號", "取消"))
    if rems:
        if lines:
            lines.append("")
        lines.append("📋 回診與領藥")
        day = clock.now().date()
        for r in rems:
            lines.append("・%s" % reminders.describe(r, day, registry))
            label = ("取消回診 %s" % reminders.show(r["appt_date"]).split("（")[0]
                     if r["kind"] == "appt" else "取消領藥 #%d" % r["id"])
            quick.append((label, "取消 #%d" % r["id"]))
    return [line_api.text_msg("\n".join(lines), quick)]


# --- doctor lookup ---

def _doctor_search(conn, registry, user_id, name):
    enabled = {a.id for a in registry.all()}
    rows = db.find_by_doctor(conn, name, clock.today(), enabled)
    if not rows:
        return [line_api.text_msg(
            "找不到「%s」今天的門診。\n可以直接傳診間號碼（例如 115），"
            "或傳「說明」看怎麼用。" % name)]
    lines = ["找到 %d 個門診：" % len(rows)]
    quick = []
    for r in rows[:13]:
        adapter = registry.get(r["hospital"])
        text = _cmd(registry, adapter, r["room"], r["timecode"])
        lines.append("・%s　%s" % (r["doctor"], text if registry.only() else
                                  "%s %s 診 %s" % (adapter.short, r["room"],
                                                   clock.SESSION_NAMES.get(r["timecode"], ""))))
        quick.append((text, text))
    return [line_api.text_msg("\n".join(lines), quick)]


# --- 回診 / 領藥 ---

APPT_USAGE = ("回診提醒的寫法：\n"
              "回診 11/20 上午 115\n"
              "（日期一定要有；時段、醫院、診間、醫師可以不寫）")
REFILL_USAGE = ("領藥提醒的寫法：\n"
                "領藥 10/17 28天 3次\n"
                "意思是 10/17 領了第 1 次，每次 28 天份，這張慢箋共 3 次。\n"
                "如果 10/17 領的是第 2 次，就寫：領藥 10/17 28天 3次 第2次")


def _add_appt(conn, cfg, registry, user, tokens):
    today = clock.now().date()
    day = timecode = None
    room = ""
    rest = []
    try:
        for tok in tokens:
            d = None if day else reminders.parse_date(tok, today, future=True)
            if d:
                day = d
            elif tok in clock.NAME_TO_CODE and not timecode:
                timecode = clock.NAME_TO_CODE[tok]
            else:
                rest.append(tok)
    except reminders.Invalid as exc:
        return [line_api.text_msg(str(exc))]
    if day is None:
        return [line_api.text_msg(APPT_USAGE)]
    if day < today:
        return [line_api.text_msg("%s 已經過了，請確認日期。" % reminders.show(day))]

    adapter, leftover = registry.match(" ".join(rest))
    adapter = adapter or _current(registry, user)
    doctor = []
    for tok in leftover.split():
        bare = tok[:-1] if tok.endswith("診") and len(tok) > 1 else tok
        if not room and adapter and adapter.looks_like_room(bare):
            room = adapter.normalize_room(bare)
        else:
            doctor.append(tok)
    doctor_name = " ".join(doctor)[:20]

    rid = db.add_appt(conn, user["line_user_id"], day.isoformat(),
                      adapter.id if adapter else "", room, doctor_name, timecode or "")
    row = db.get_reminder(conn, rid)
    msg = "✅ 記好了\n%s\n\n" % reminders.describe(row, today, registry)
    if day == today:
        msg += "就是今天！掛號後把看診號碼傳給我，快輪到時我會通知你。"
    else:
        msg += "前一天%s我會提醒你，記得帶健保卡。" % _clock_words(cfg["apptEveTime"])
    return [line_api.text_msg(msg, [("我的提醒", "我的提醒")])]


def _clock_words(hhmm):
    """'19:00' -> '晚上 7 點', the way people say it."""
    h, m = (int(x) for x in hhmm.split(":"))
    part = "早上" if h < 12 else ("下午" if h < 18 else "晚上")
    return "%s %d 點%s" % (part, h - 12 if h > 12 else h, " %d 分" % m if m else "")


def _add_refill(conn, user_id, tokens):
    today = clock.now().date()
    day, days, total, nth = None, None, None, None
    try:
        for tok in tokens:
            if day is None:
                d = reminders.parse_date(tok, today, future=False)
                if d:
                    day = d
                    continue
            m = reminders.DAYS_RE.match(tok)
            if m:
                days = int(m.group(1))
                continue
            m = reminders.FILLS_RE.match(tok)
            if m:
                total = int(m.group(1))
                continue
            m = reminders.NTH_RE.match(tok)
            if m:
                nth = int(m.group(1))
                continue
            if tok.isdigit() and days is None:
                days = int(tok)
                continue
            if tok.isdigit() and total is None:
                total = int(tok)
                continue
            return [line_api.text_msg("看不懂「%s」。\n\n%s" % (tok, REFILL_USAGE))]
    except reminders.Invalid as exc:
        return [line_api.text_msg(str(exc))]
    if days is None:
        return [line_api.text_msg(REFILL_USAGE)]
    day = day or today
    total = total or 3
    nth = nth or 1
    if day > today:
        return [line_api.text_msg("領藥日期 %s 還沒到。請寫已經領藥的那一天。"
                                  % reminders.show(day))]
    if not 7 <= days <= 90:
        return [line_api.text_msg("每次的天數請寫 7 到 90 天，例如 28天。")]
    if not 1 <= total <= 6 or not 1 <= nth <= total:
        return [line_api.text_msg("次數看起來不對：共 %d 次、第 %d 次。\n\n%s"
                                  % (total, nth, REFILL_USAGE))]

    rid = db.add_refill(conn, user_id, day.isoformat(), days, total, nth)
    row = db.get_reminder(conn, rid)
    return [line_api.text_msg(
        "✅ 記好了\n%s 領了第 %d 次（每次 %d 天份，共 %d 次）\n\n%s"
        % (reminders.show(day), nth, days, total, _refill_next_words(row)),
        [("我的提醒", "我的提醒")])]


def _refill_next_words(row):
    plan = reminders.refill_plan(row)
    if plan["final"]:
        return ("這是最後一次了。%s 藥會用完，前一個禮拜我會提醒你回診拿新處方。"
                % reminders.show(plan["runout"]))
    return ("第 %d 次可以在 %s 到 %s 之間領。\n%s 早上我會提醒你。"
            % (plan["next_fill"], reminders.show(plan["open"]),
               reminders.show(plan["close"]), reminders.show(plan["open"])))


def _mark_filled(conn, user_id, tokens):
    today = clock.now().date()
    refills = [r for r in db.active_reminders(conn, user_id, "refill")
               if not reminders.refill_plan(r)["final"]]
    if not refills:
        return [line_api.text_msg("目前沒有要領的藥。\n\n" + REFILL_USAGE)]
    chosen, day = None, today
    try:
        for tok in tokens:
            m = ID_RE.match(tok)
            if m:
                chosen = next((r for r in refills if r["id"] == int(m.group(1))), None)
                continue
            d = reminders.parse_date(tok, today, future=False)
            if d:
                day = d
    except reminders.Invalid as exc:
        return [line_api.text_msg(str(exc))]
    if chosen is None:
        if len(refills) > 1:
            return [line_api.text_msg(
                "你有 %d 張慢箋，是哪一張？\n" % len(refills) + "\n".join(
                    "・#%d %s" % (r["id"], reminders.describe(r, today)) for r in refills),
                [("#%d 已領" % r["id"], "已領 #%d" % r["id"]) for r in refills])]
        chosen = refills[0]
    if day > today:
        return [line_api.text_msg("領藥日期不能是未來的日子。")]

    row = db.record_fill(conn, chosen["id"], day.isoformat())
    return [line_api.text_msg(
        "✅ 好，記下 %s 領了第 %d 次。\n\n%s"
        % (reminders.show(day), row["fills_done"], _refill_next_words(row)),
        [("我的提醒", "我的提醒")])]


def _appt_today_hint(conn, registry, user_id):
    """Said once on the day of an appointment, in a reply - free, unlike a morning push.
    If the appointment named a room, that room becomes the pending context, so the person
    only has to send their ticket number."""
    today = clock.today()
    appts = [r for r in db.active_reminders(conn, user_id, "appt") if r["appt_date"] == today]
    if not appts or not db.claim_appt_hint(conn, user_id, today):
        return None
    r = appts[0]
    adapter = registry.get(r["hospital"])
    where = " ".join(x for x in (adapter.short if adapter else "",
                                 "%s 診" % r["room"] if r["room"] else "",
                                 clock.SESSION_NAMES.get(r["timecode"], ""),
                                 r["doctor"]) if x)
    text = "📅 今天要回診%s。\n" % ("（%s）" % where if where else "")
    if adapter and r["room"] and r["timecode"] and not _recall(user_id):
        _remember(user_id, adapter.id, r["room"], r["timecode"])
        text += "掛號後直接把看診號碼傳給我，快輪到時我會通知你。"
    else:
        text += "掛號後把診間和看診號碼傳給我，例如：115 38"
    return line_api.text_msg(text)


# --- identifier lookup (use and discard) ---

def _looks_like_identifier(token):
    """Long enough and mixed enough that it cannot be a room number or a ticket number."""
    if len(token) < 8:
        return False
    try:
        validate.patient_id(token)
        return True
    except validate.Invalid:
        return False


def _lookup_by_identifier(conn, cfg, registry, user, parts):
    """The identifier lives in this function's frame and nowhere else. It is not written to
    the database, not cached, not logged, and not put in an exception (see
    cmu_hch.fetch_by_id). What survives this call is a room number and a ticket number.
    """
    user_id = user["line_user_id"]
    adapter = _current(registry, user)
    if adapter is None or not adapter.supports_id_lookup:
        able = [a for a in registry.all() if a.supports_id_lookup]
        if len(able) == 1 and adapter is None:
            adapter = able[0]
        else:
            return [line_api.text_msg(
                "用身分證查診間只支援%s。\n%s請直接傳掛號單上的診間號碼。"
                % ("、".join(a.short for a in able) or "（目前沒有醫院）",
                   (adapter.short + "：") if adapter else ""))]
    if len(parts) < 2:
        return [line_api.text_msg(
            "請把身分證和生日寫在同一則訊息裡，例如：\nA123456789 0304\n"
            "（生日寫月日各兩位。放在同一則訊息，我才不需要暫存你的證號。）")]
    try:
        crt_id = validate.patient_id(parts[0])
        month, day = validate.birth_md(parts[1])
    except validate.Invalid as exc:
        return [line_api.text_msg(str(exc))]

    try:
        snap = adapter.fetch_by_id(crt_id, month, day)
    except HospitalError as exc:
        return [line_api.text_msg(str(exc))]
    finally:
        del crt_id      # drops this frame's reference; see the note in fetch_by_id

    if snap["kind"] == "no_registration":
        return [line_api.text_msg(
            "查不到今天在%s的掛號。可能是還沒掛號、掛在別家醫院，或生日打錯了。"
            % adapter.short)]
    if snap["kind"] != "ok" or not snap["room"]:
        return [line_api.text_msg(
            "查到了資料但看不懂格式，請直接傳掛號單上的診間號碼（例如 115）。")]

    room = snap["room"]
    timecode = clock.NAME_TO_CODE.get(snap["session"], clock.guess_timecode())
    _remember(user_id, adapter.id, room, timecode)
    session = clock.SESSION_NAMES.get(timecode, "")
    return [line_api.text_msg(
        "✅ 找到了：%s %s 診・%s・%s\n"
        "🔒 你的證號已經用完就丟了，沒有存起來，我只留下診間號碼。\n"
        "👉 建議長按剛才那則訊息 →「收回」，我沒辦法幫你刪聊天室裡的訊息。\n"
        "💡 下次直接傳「%s」就不用再給證號。\n\n"
        "請傳你的看診號碼，我幫你設提醒。"
        % (adapter.short, room, snap["doctor"] or "—", snap["session"] or "",
           _cmd(registry, adapter, room, timecode)),
        [("查看進度", _cmd(registry, adapter, room, timecode))])]
