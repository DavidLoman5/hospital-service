"""What every hospital adapter provides.

An adapter knows one hospital's endpoints, charset and page shape, and nothing else. It
turns that hospital's answer into the common snapshot described in bot/waiting.py. All
politeness (spacing, one-at-a-time) lives in bot/fetch.py; the short response cache lives
here, so N subscribers to one room still cause one fetch whichever hospital it is.

Session codes are shared across hospitals - "1" 上午, "2" 下午, "3" 晚上 (clock.SESSION_NAMES)
- and each adapter maps them onto its own parameters and opening hours.

To add a hospital: subclass Adapter, implement _fetch_room and room_url, register the class
in bot/hospitals/__init__.py, and add raw-byte fixtures under fixtures/<id>/.
"""

import threading
import time
from datetime import time as dtime

from bot import clock, validate
from bot.fetch import HospitalError     # noqa: F401  (re-exported for callers)


class Adapter:
    id = ""
    name = ""                   # what the user sees: "中國醫藥大學新竹附設醫院"
    short = ""                  # in buttons and headers: "中國醫新竹"
    aliases = ()                # what a user might type; the short name is always included
    has_ticket_list = True
    supports_id_lookup = False
    has_listing = False         # True if list_clinics() is implemented
    # code -> (start, end) local time. The watcher polls only inside these windows.
    sessions = {"1": ("08:30", "12:00"), "2": ("13:30", "17:00"), "3": ("18:00", "21:00")}
    min_interval = 1.2          # seconds between requests to this hospital
    cache_seconds = 45
    room_hint = "診間號碼"       # how to describe a room in error messages

    def __init__(self, fetcher, settings=None):
        self.fetcher = fetcher
        settings = settings or {}
        self.min_interval = settings.get("minRequestInterval", self.min_interval)
        self.cache_seconds = settings.get("fetchCacheSeconds", self.cache_seconds)
        if "sessions" in settings:
            self.sessions = {k: tuple(v) for k, v in settings["sessions"].items()}
        self.settings = settings
        self._cache = {}
        self._cache_lock = threading.Lock()
        # Called with (adapter, room, session, snapshot) after every real fetch. The server
        # points it at the progress log that feeds the wait-time estimate.
        self.on_snapshot = None

    # --- to implement ---

    def _fetch_room(self, room, session):
        raise NotImplementedError

    def room_url(self, room, session):
        """The hospital's own page for this clinic - for people who want to check us, and
        the fallback if our parsing ever breaks."""
        raise NotImplementedError

    def normalize_room(self, text):
        """The room as this hospital spells it, or validate.Invalid with a message."""
        return validate.room(text)

    def looks_like_room(self, token):
        try:
            self.normalize_room(token)
            return True
        except validate.Invalid:
            return False

    def list_clinics(self, session):
        """[(room, doctor), ...] for today, for hospitals with a listing page (set
        has_listing). Without one the directory probes sweep_rooms() instead."""
        return None

    def sweep_rooms(self):
        return []

    # --- shared ---

    def fetch_room(self, room, session):
        key = (str(room), str(session))
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        snap = self._fetch_room(key[0], key[1])
        snap.setdefault("ticket_list", self.has_ticket_list)
        with self._cache_lock:
            self._cache[key] = (time.monotonic() + self.cache_seconds, snap)
        if self.on_snapshot and snap.get("kind") == "ok":
            try:
                self.on_snapshot(self, key[0], key[1], snap)
            except Exception as exc:      # bookkeeping must never fail a query
                print("on_snapshot error: %s: %s" % (type(exc).__name__, exc))
        return snap

    def get(self, url, params=None, **kw):
        return self.fetcher.get(url, params, self.min_interval, **kw)

    def in_session(self, code, when=None):
        """Is this clinic session running right now? Polling only inside these windows is
        what makes "wrong time of day = zero hospital traffic" true."""
        window = self.sessions.get(str(code))
        if not window:
            return False
        t = (when or clock.now()).time()
        start = dtime(*[int(x) for x in window[0].split(":")])
        end = dtime(*[int(x) for x in window[1].split(":")])
        return start <= t <= end

    def all_names(self):
        return tuple({self.short, self.name, *self.aliases} - {""})
