"""中國醫藥大學新竹附設醫院 - the clinic-progress CGI.

The public page at cmu-hch.cmu.edu.tw/OnlineAppointment/ClinicQuery carries <form action>
attributes that are decoys - inline JS rewrites them to these endpoints on cmu-hch.com:

    reg64x.cgi?CliRoom=<3 digits>&TimeCode=<1|2|3>   by room, needs no personal data
    reg62.cgi?CrtIdNo=<id>&sMonth=<MM>&sDay=<DD>     by patient identifier

Both answer HTTP 200 with Big5 HTML, for TODAY only, in the ROC calendar (115 = 2026).

Two decoding traps, both load-bearing:
  * the charset is Big5, and characters Big5 cannot represent (rare name glyphs like the
    圀 in 林圀宏) come back as HTML numeric entities instead. Decode with big5hkscs AND
    unescape, or you get a mangled doctor name.
  * ticket numbers are sparse - one observed clinic ran 1..94 with only 42 real numbers in
    between. "How many ahead of me" must COUNT the waiting rows below your number, never
    subtract the current light from it. See waiting.waiting_ahead().

The server is Apache/2.2.15 (CentOS) - a CentOS-6-era box. Keep minRequestInterval where
it is.
"""

import html
import re

from bot import waiting
from bot.fetch import HospitalError
from bot.hospitals.base import Adapter

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")

HEADER_RE = re.compile(r"日期:\s*(\S+)\s*時段:\s*(\S+)")
ROOM_RE = re.compile(r"診間號:\s*(\S+)\s+醫師:\s*(.+?)\s*$")
LIGHT_RE = re.compile(r"診間目前燈號:\s*(.*?)\s*$")
NOTE_RE = re.compile(r"診間特別訊息:\s*(.*?)\s*$")
ROC_RE = re.compile(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$")

# Confirmed against live clinics on 2026-08-25 morning. There is NO "看診中" and no "過號";
# the vocabulary is exactly the four states in bot/waiting.py, spelled as this hospital
# spells them - so no mapping is needed here.

# Response shapes other than a normal answer.
NO_ROOM = "查無此診間"
BAD_ROOM = "診間號碼"
NO_REGISTRATION = "查無此身份證字號"

DEFAULT_BASE = "https://www.cmu-hch.com/cgi-bin/hc"


def _text(fragment):
    """A table cell to plain text: strip tags, then unescape (order matters - unescaping
    first could introduce a '<' that the tag stripper would then eat)."""
    return html.unescape(TAG_RE.sub("", fragment)).replace("　", " ").strip()


def roc_to_iso(value):
    """'115/08/25' -> '2026-08-25'; anything else passes through unchanged."""
    m = ROC_RE.match(value or "")
    if not m:
        return value
    return "%04d-%02d-%02d" % (int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))


def decode(raw):
    return raw.decode("big5hkscs", errors="replace")


class CmuHsinchu(Adapter):
    id = "cmu_hch"
    name = "中國醫藥大學新竹附設醫院"
    short = "中國醫新竹"
    aliases = ("新竹附醫", "中國醫", "中國附醫新竹", "竹北中國醫", "中國醫竹北")
    has_ticket_list = True
    supports_id_lookup = True
    room_hint = "3 位數的診間號碼，例如 115"

    def __init__(self, fetcher, settings=None):
        super().__init__(fetcher, settings)
        self.base = (self.settings.get("base") or DEFAULT_BASE).rstrip("/")

    def _fetch_room(self, room, session):
        raw = self.get(self.base + "/reg64x.cgi", {"CliRoom": room, "TimeCode": session})
        return parse(decode(raw))

    def room_url(self, room, session):
        # The very page this module scrapes. It is a pre-viewport page, so it renders small
        # on a phone and needs a pinch to zoom.
        return "%s/reg64x.cgi?CliRoom=%s&TimeCode=%s" % (self.base, room, session)

    def sweep_rooms(self):
        """Rooms are 3 digits and cluster in the low end of each hundred (101-121, 201-216,
        501-505, 601...). The configured ranges cover every room seen so far at a third
        of the cost of sweeping 100-699."""
        rooms = []
        for start, end in self.settings.get("sweepRanges") or []:
            rooms.extend("%03d" % n for n in range(start, end + 1))
        return rooms

    def fetch_by_id(self, crt_id, month, day):
        """Resolve a patient identifier to their seat.

        The identifier is a parameter and a local variable and nothing else: it is never
        returned, cached, or logged. Callers must keep it that way - the result carries
        only room and ticket number.

        The failure path is deliberately different from fetch_room's. `from None` cuts the
        exception chain and the message is a fixed string, so that no matter what urllib
        puts in an error (it builds the URL from the identifier), an unhandled traceback
        reaching journald cannot carry it. Costs us the underlying reason on this one path;
        the reg62 URL is not something to be casual about.
        """
        try:
            raw = self.get(self.base + "/reg62.cgi",
                           {"CrtIdNo": crt_id, "sMonth": month, "sDay": day})
        except Exception:
            raise HospitalError("查詢失敗，請稍後再試") from None
        snap = parse(decode(raw))
        snap["ticket_list"] = True
        return snap


def parse(body):
    """Big5-decoded HTML -> snapshot. Never raises on unexpected content; an unrecognised
    page comes back as kind "unknown" so the caller can degrade gracefully."""
    flat = _text(body)
    out = {"kind": "ok", "date": "", "session": "", "room": "", "doctor": "",
           "light": "", "note": "", "tickets": [], "unknown_states": [],
           "ticket_list": True}

    header = HEADER_RE.search(flat)
    if header:
        out["date"], out["session"] = roc_to_iso(header.group(1)), header.group(2)

    if NO_REGISTRATION in flat:
        out["kind"] = "no_registration"
        return out
    if NO_ROOM in flat:
        out["kind"] = "no_room"
        return out
    # "您的「診間號碼」輸入錯誤" - only ever appears alongside 對不起
    if BAD_ROOM in flat and "對不起" in flat:
        out["kind"] = "bad_room"
        return out

    for row in ROW_RE.findall(body):
        cells = [_text(c) for c in CELL_RE.findall(row)]
        if len(cells) == 2 and cells[0].isdigit():
            state = cells[1]
            out["tickets"].append({"no": int(cells[0]), "state": state})
            if state not in waiting.KNOWN_STATES and state not in out["unknown_states"]:
                out["unknown_states"].append(state)
            continue
        for cell in cells:
            m = ROOM_RE.search(cell)
            if m:
                out["room"], out["doctor"] = m.group(1), m.group(2)
            m = LIGHT_RE.search(cell)
            if m:
                out["light"] = m.group(1)
            m = NOTE_RE.search(cell)
            if m:
                out["note"] = m.group(1)

    if not out["room"]:
        out["kind"] = "unknown"
    return out
