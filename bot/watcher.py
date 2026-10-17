"""Background poller: watches the clinics people are waiting in and pushes when their turn
approaches.

Three properties this is built around:

  * Zero subscriptions, or a time outside clinic hours, means zero requests to the
    hospital. The loop wakes up, finds nothing to do, and goes back to sleep.
  * A false "it's your turn" cannot be taken back - it wakes someone at the wrong moment
    and spends 1/200 of the month. So a reading that would fire has to be confirmed by a
    second, independent fetch before anything is sent (see _evaluate).
  * The poll rate follows how close the queue is. Somebody 30 people back does not need to
    be checked every minute.

The same loop also runs the 回診 / 領藥 reminders (bot/reminders.py), which need no hospital
traffic at all - only the clock.
"""

import time
from datetime import date, timedelta

from bot import clock, db, estimate, line_api, reminders, waiting
from bot.fetch import HospitalError

TICK_SECONDS = 20
PROGRESS_KEEP_DAYS = 3


def run(cfg, registry):
    """Thread entry point. Never raises: a poll failure must not take the webhook down."""
    last_day = None
    last_poll = {}          # (hospital, room, timecode) -> monotonic time of last fetch
    while True:
        try:
            conn = db.get(cfg["dbPath"])
            today = clock.today()
            if today != last_day:
                closed = db.close_stale(conn, today)
                if closed:
                    print("watcher: closed %d subscription(s) from previous days" % closed)
                cutoff = (date.fromisoformat(today)
                          - timedelta(days=PROGRESS_KEEP_DAYS)).isoformat()
                db.prune_progress(conn, cutoff)
                last_day = today
                last_poll.clear()
            tick(conn, cfg, registry, today, last_poll)
            reminders.tick(conn, cfg, registry)
        except Exception as exc:
            print("watcher error: %s: %s" % (type(exc).__name__, exc))
        time.sleep(TICK_SECONDS)


def tick(conn, cfg, registry, today, last_poll):
    subs = db.active_subscriptions(conn, today)
    if not subs:
        return

    groups = {}
    for sub in subs:
        adapter = registry.get(sub["hospital"])
        if adapter is None or not adapter.in_session(sub["timecode"]):
            continue                    # hospital switched off, or session not running
        groups.setdefault((sub["hospital"], sub["room"], sub["timecode"]), []).append(sub)
    if not groups:
        return

    now = time.monotonic()
    for key, members in groups.items():
        hospital_id, room, timecode = key
        interval = _interval_for(members)
        if now - last_poll.get(key, 0) < interval:
            continue
        last_poll[key] = now
        adapter = registry.get(hospital_id)
        try:
            # One fetch serves every subscriber in this room, which is the point of
            # grouping: five family members in one clinic is still one request.
            snap = adapter.fetch_room(room, timecode)
        except HospitalError as exc:
            print("watcher: %s %s/%s fetch failed: %s" % (hospital_id, room, timecode, exc))
            continue
        if snap["kind"] != "ok":
            continue
        for sub in members:
            _evaluate(conn, cfg, adapter, sub, snap)


def _interval_for(members):
    """Poll faster the closer the nearest subscriber is to being called."""
    gaps = [m["last_current"] for m in members if m["last_current"] is not None]
    gap = min(gaps) if gaps else None
    if gap is None:
        return 180.0        # nothing measured yet
    if gap > 20:
        return 300.0
    if gap > 10:
        return 180.0
    if gap > 5:
        return 90.0
    return 45.0


def _evaluate(conn, cfg, adapter, sub, snap):
    state = waiting.my_state(snap, sub["ticket_no"])
    if state is None:
        return                          # not in this clinic's list; leave it alone

    if state == waiting.DONE:
        db.set_subscription_state(conn, sub["id"], "done")
        return

    light = waiting.light_number(snap)
    if light is None:
        return                          # not open yet; nothing to say

    where = "%s %s 診" % (adapter.short, sub["room"])
    if light >= sub["ticket_no"]:
        # There is no "看診中" state on these pages - the light passing your number is the
        # only signal that your turn has come.
        if state == waiting.NOT_CHECKED_IN:
            # Called, but never checked in at the kiosk. Worth saying, because this is the
            # one the person can still fix by going to the counter.
            text = ("⚠️ %s 已經叫到 %d 號了\n"
                    "你是 %d 號，但系統顯示還沒報到。\n"
                    "請趕快到櫃檯或報到機報到。" % (where, light, sub["ticket_no"]))
        else:
            text = ("📣 輪到你了！\n%s %d 號\n請進診間。" % (where, sub["ticket_no"]))
        _push(conn, cfg, adapter, sub, "turn", text + _link(adapter, sub))
        db.set_subscription_state(conn, sub["id"], "done", notified=True)
        return

    ahead = waiting.waiting_ahead(snap, sub["ticket_no"])
    if ahead is None:
        return

    minutes = estimate.minutes_for(conn, sub["hospital"], sub["room"], sub["timecode"],
                                   sub["visit_date"], ahead)
    near = ahead <= sub["notify_ahead"] or (
        minutes is not None and minutes[0] <= cfg["notifyMinutesAhead"])

    if sub["state"] == "notified" or not near:
        db.remember_reading(conn, sub["id"], ahead, None)
        return

    # Threshold reached. Confirm it against the next fetch before spending a push: a single
    # garbled parse should cost a poll interval, not a false alarm.
    if sub["pending_current"] is None:
        db.remember_reading(conn, sub["id"], ahead, ahead)
        return

    ahead_text = ("前面大約還有 %d 位" if waiting.is_approx(snap) else "前面還有 %d 位") % ahead
    when = estimate.describe(minutes)
    _push(conn, cfg, adapter, sub, "almost",
          "🔔 快輪到你了！\n%s %d 號\n%s%s\n請回到診間門口等候。\n"
          "（進度僅供參考，以診間叫號為準）"
          % (where, sub["ticket_no"], ahead_text, "，" + when if when else "")
          + _link(adapter, sub))
    db.set_subscription_state(conn, sub["id"], "notified", notified=True)
    db.remember_reading(conn, sub["id"], ahead, None)


def _link(adapter, sub):
    """A notification is exactly when someone wants to check for themselves."""
    return "\n\n🔗 醫院官網：\n" + adapter.room_url(sub["room"], sub["timecode"])


def _push(conn, cfg, adapter, sub, kind, text):
    sent = line_api.push(conn, cfg, sub["line_user_id"], text, kind, ("sub", sub["id"]))
    if sent:
        print("watcher: pushed %s for %s room %s #%d"
              % (kind, adapter.id, sub["room"], sub["ticket_no"]))
    return sent
