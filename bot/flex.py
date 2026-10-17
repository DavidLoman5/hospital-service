"""Flex Message construction.

Kept apart from convo.py because a Flex bubble is a deep nested literal and mixing it into
the conversation logic buries both. Every builder here returns a complete message object
ready to hand to line_api.reply().

Written for older eyes first: the three numbers someone actually came for - the number
being called, how many are ahead, roughly how long - are the biggest things on the card,
in plain words ("前面還有 3 位"), and everything else is smaller or gone. Buttons are full
height and there are at most two of them.

Every bubble carries an altText: that is what shows in the notification list and in any
client that cannot render Flex, so it must stand alone as a sentence.
"""

from bot import clock, estimate, waiting

# How close is close. Also the colour ramp - the number of people ahead is the one thing
# someone reads at a glance, so it changes colour rather than just wording.
URGENT, SOON = 5, 15
RED, ORANGE, GREEN, GREY = "#D32F2F", "#E65100", "#1B7F3B", "#6B7280"
INK, MUTED, LINE_BG, BLUE = "#111827", "#4B5563", "#F2F4F7", "#1D4ED8"


def _ahead_colour(ahead):
    if ahead is None:
        return GREY
    if ahead <= URGENT:
        return RED
    if ahead <= SOON:
        return ORANGE
    return GREEN


def _text(value, **kw):
    node = {"type": "text", "text": value, "wrap": True}
    node.update(kw)
    return node


def _button(label, text, primary=False, colour=BLUE):
    node = {"type": "button", "height": "md",
            "style": "primary" if primary else "secondary",
            "action": {"type": "message", "label": label, "text": text}}
    if primary:
        node["color"] = colour
    return node


def _link(url):
    return {"type": "button", "style": "link", "height": "sm",
            "action": {"type": "uri", "label": "看醫院官網", "uri": url}}


def _header(adapter, room, session, doctor, colour=BLUE, badge=""):
    top = adapter.short + ("　" + badge if badge else "")
    return {
        "type": "box", "layout": "vertical", "paddingAll": "16px",
        "backgroundColor": colour, "spacing": "xs",
        "contents": [
            _text(top, color="#F1F5F9", size="md"),
            _text("%s 診　%s" % (room, session), color="#FFFFFF", size="xxl",
                  weight="bold"),
            _text(doctor or "—", color="#FFFFFF", size="lg"),
        ]}


def ahead_words(ahead, approx):
    if ahead == 0:
        return "下一位就是你"
    return ("前面大約還有 %d 位" if approx else "前面還有 %d 位") % ahead


def progress(adapter, room, timecode, snap, ticket_no=None, ahead=None, minutes=None,
             refresh_text="", subscribed=False):
    """The clinic progress card.

    Two modes: without a ticket number it reports the room ("還有 22 位在等"); with one
    it reports the person ("前面還有 5 位"), which is what they actually asked.
    """
    session = clock.SESSION_NAMES.get(timecode, "")
    light = waiting.light_number(snap)
    approx = waiting.is_approx(snap)
    where = "%s %s 診" % (adapter.short, room)

    if light is None:
        caption, headline, colour = "目前狀態", "還沒開始看診", GREY
        alt = "%s %s：還沒開始看診" % (where, session)
    else:
        caption, headline = "現在看到", "%d 號" % light
        if ticket_no is not None and ahead is not None:
            colour = _ahead_colour(ahead)
            alt = "%s 現在看到 %d 號，你%s" % (where, light, ahead_words(ahead, approx))
        else:
            colour = INK
            alt = "%s 現在看到 %d 號" % (where, light)

    body = [
        _text(caption, size="lg", color=MUTED, align="center"),
        _text(headline, size="5xl" if light is not None else "xxl", weight="bold",
              align="center", color=colour, margin="sm"),
    ]

    if ticket_no is not None and ahead is not None:
        mine = [_text("你是 %d 號" % ticket_no, size="xl", color=INK, align="center"),
                _text(ahead_words(ahead, approx), size="3xl", weight="bold",
                      align="center", color=colour)]
        when = estimate.describe(minutes)
        if when:
            mine.append(_text(when, size="xl", align="center", color=INK, margin="sm"))
        body.append({
            "type": "box", "layout": "vertical", "margin": "xl", "spacing": "sm",
            "backgroundColor": LINE_BG, "cornerRadius": "12px", "paddingAll": "16px",
            "contents": mine})
    elif light is not None:
        left = waiting.still_waiting(snap)
        if left is not None:
            body.append({
                "type": "box", "layout": "vertical", "margin": "xl",
                "backgroundColor": LINE_BG, "cornerRadius": "12px", "paddingAll": "16px",
                "contents": [_text("還有 %d 位在等" % left, size="xl", weight="bold",
                                   align="center", color=INK)]})
    elif snap.get("tickets"):
        body.append(_text("已經有 %d 位掛號" % len(snap["tickets"]), size="lg",
                          align="center", color=MUTED, margin="lg"))

    if approx and light is not None:
        body.append(_text("這家醫院只公布目前的號碼，人數是用號碼推算的，實際可能比較少。",
                          size="sm", color=MUTED, margin="lg"))
    if snap.get("note"):
        body.append(_text("📢 " + snap["note"], size="md", color=INK, margin="lg"))
    if snap.get("unknown_states"):
        # Surfaced rather than swallowed: an unfamiliar state is how the real vocabulary
        # got discovered, and it means this estimate may be off.
        body.append(_text("出現沒看過的狀態：%s" % "、".join(snap["unknown_states"]),
                          size="xs", color=ORANGE, margin="md"))

    footer = []
    if refresh_text:
        footer.append(_button("🔄 重新整理", refresh_text, primary=True))
    if light is not None or snap.get("tickets"):
        if subscribed:
            footer.append(_button("取消提醒", "取消"))
        elif ticket_no is None:
            footer.append(_button("🔔 快到時通知我", "設提醒", primary=True, colour=GREEN))
    footer.append(_link(adapter.room_url(room, timecode)))

    return {
        "type": "flex", "altText": alt,
        "contents": {
            "type": "bubble", "size": "mega",
            "header": _header(adapter, room, session, snap.get("doctor")),
            "body": {"type": "box", "layout": "vertical", "paddingAll": "20px",
                     "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "md",
                       "paddingAll": "12px", "contents": footer},
        }}


def subscribed(adapter, room, timecode, snap, ticket_no, ahead, notify_ahead, minutes=None,
               refresh_text=""):
    """Confirmation card after a reminder is set."""
    session = clock.SESSION_NAMES.get(timecode, "")
    approx = waiting.is_approx(snap)
    rows = [
        _text("你是 %d 號" % ticket_no, size="xl", weight="bold", color=INK),
        _text(ahead_words(ahead, approx), size="xxl", weight="bold",
              color=_ahead_colour(ahead)),
    ]
    when = estimate.describe(minutes)
    if when:
        rows.append(_text(when, size="lg", color=INK))
    rows += [
        {"type": "separator", "margin": "lg"},
        _text("前面剩 %d 位的時候，我會傳訊息通知你。\n可以先去休息，不用一直盯著看。"
              % notify_ahead, size="lg", color=INK, margin="lg"),
    ]
    footer = []
    if refresh_text:
        footer.append(_button("🔄 看現在進度", refresh_text, primary=True))
    footer.append(_button("取消提醒", "取消"))
    return {
        "type": "flex",
        "altText": "已設定提醒：%s %s 診 %d 號" % (adapter.short, room, ticket_no),
        "contents": {
            "type": "bubble", "size": "mega",
            "header": _header(adapter, room, session, snap.get("doctor"),
                              colour=GREEN, badge="🔔 已設定提醒"),
            "body": {"type": "box", "layout": "vertical", "paddingAll": "20px",
                     "spacing": "sm", "contents": rows},
            "footer": {"type": "box", "layout": "vertical", "spacing": "md",
                       "paddingAll": "12px", "contents": footer},
        }}
