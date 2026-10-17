"""Queue arithmetic shared by every hospital.

Adapters translate each hospital's page into a common snapshot; everything here works on
that snapshot only and knows nothing about where it came from:

    {"kind": "ok" | "no_room" | "bad_room" | "no_registration" | "unknown",
     "date": "2026-08-25", "session": "下午", "room": "115", "doctor": "...",
     "light": "37" | "(未開診)", "note": "",
     "tickets": [{"no": 38, "state": NOT_CHECKED_IN}, ...],
     "ticket_list": True,      # False when the hospital only publishes the current number
     "unknown_states": []}

Ticket states are normalised by the adapter to the four below (the vocabulary of the first
hospital supported, measured against live clinics). A state an adapter cannot map is kept
verbatim and listed in unknown_states - surfaced, never guessed at.
"""

import re

#   未看診          registered but NOT checked in. Once the light passes this number the
#                   person has almost certainly not shown up.
#   未看診(已報到)   checked in and physically waiting. If the light has passed them they were
#                   skipped (stepped out, sent for a test) and will still be called back.
#   保留待診        held for later - same practical meaning as a skipped check-in.
#   完成            done.
NOT_CHECKED_IN = "未看診"
CHECKED_IN = "未看診(已報到)"
HELD = "保留待診"
DONE = "完成"

# Present in the room and still owed a turn.
PRESENT = {CHECKED_IN, HELD}
# Anything not yet finished, whether or not they have shown up.
UNFINISHED = {NOT_CHECKED_IN, CHECKED_IN, HELD}
KNOWN_STATES = {NOT_CHECKED_IN, CHECKED_IN, HELD, DONE}

# What my_state() reports for a hospital without a ticket list once the light has reached
# a number - the only thing such a page can tell us.
CALLED = "已叫號"
WAITING = "候診中"


def has_list(snap):
    return snap.get("ticket_list", True)


def light_number(snap):
    """The number currently being called, or None before the clinic opens."""
    digits = re.search(r"\d+", snap.get("light") or "")
    return int(digits.group()) if digits else None


def waiting_ahead(snap, ticket_no):
    """How many people are realistically still ahead of ticket_no.

    With a ticket list, two corrections over the obvious approaches, both measured against
    live clinics:

      * Not `ticket_no - light`. Numbers are sparse - one clinic ran 1..94 with 42 real
        tickets - so subtracting overstates the wait about twofold.
      * Not simply "every unfinished number below mine" either. Numbers the light has
        already passed while still 未看診 are people who never checked in; counting
        them overstates badly in the other direction (a clinic on number 102 had 23 such
        ghosts against 3 genuinely waiting). Below the light, only those who checked in
        and were skipped still count.

    Without a ticket list, subtraction is all there is; is_approx() tells the UI to say so.

    Returns None when the answer is unknown (ticket_no not in the list, or no list and the
    clinic not open yet).
    """
    light = light_number(snap)
    if not has_list(snap):
        if light is None:
            return None
        return max(0, ticket_no - light - 1)
    if not any(t["no"] == ticket_no for t in snap["tickets"]):
        return None
    ahead = 0
    for t in snap["tickets"]:
        if t["no"] >= ticket_no:
            continue
        if light is None:
            # Not open yet: nobody has been skipped, so everyone unfinished is ahead.
            ahead += t["state"] in UNFINISHED
        elif t["no"] < light:
            ahead += t["state"] in PRESENT        # skipped, but still in the room
        else:
            ahead += t["state"] in UNFINISHED     # simply not reached yet
    return ahead


def is_approx(snap):
    """True when waiting_ahead() had to subtract - an upper bound, not a count."""
    return not has_list(snap)


def still_waiting(snap):
    """How many people in the room are still owed a turn - the same accounting as
    waiting_ahead, from the front of the queue rather than from one person's number.
    None when the hospital does not publish a list."""
    if not has_list(snap):
        return None
    light = light_number(snap)
    if light is None:
        return sum(1 for t in snap["tickets"] if t["state"] in UNFINISHED)
    return sum(1 for t in snap["tickets"]
               if (t["state"] in PRESENT if t["no"] < light else t["state"] in UNFINISHED))


def done_count(snap):
    """People seen so far - the progress measure the wait estimate is built on. Falls back
    to the light itself where there is no list."""
    if not has_list(snap):
        return light_number(snap)
    return sum(1 for t in snap["tickets"] if t["state"] == DONE)


def my_state(snap, ticket_no):
    if not has_list(snap):
        light = light_number(snap)
        if light is not None and light >= ticket_no:
            return CALLED
        return WAITING if light is not None else None
    for t in snap["tickets"]:
        if t["no"] == ticket_no:
            return t["state"]
    return None
