"""The one place that opens sockets to hospitals.

Politeness is enforced here, per host: one request in flight to a given hospital at a time,
never closer together than that hospital's minRequestInterval. Different hospitals are
different machines, so they get separate locks - a slow answer from one must not stall
queries against another - but within one host the lock is held across the request itself,
because overlapping requests would defeat the spacing.

Adapters (bot/hospitals/) decide WHAT to ask; this module decides WHEN.
"""

import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; personal clinic-progress notifier)"


class HospitalError(Exception):
    """Network or HTTP failure. The caller turns this into a "try the website" reply -
    never into a stack trace at the user."""


class Fetcher:
    def __init__(self, timeout=15.0):
        self._timeout = timeout
        self._guard = threading.Lock()     # protects the two dicts below
        self._locks = {}                   # host -> Lock
        self._last = {}                    # host -> monotonic time of last request

    def _lock_for(self, host):
        with self._guard:
            lock = self._locks.get(host)
            if lock is None:
                lock = self._locks[host] = threading.Lock()
            return lock

    def get(self, url, params, interval, data=None, headers=None):
        """Raw response bytes. Decoding is the adapter's business: each hospital has its
        own charset and its own traps."""
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        host = urllib.parse.urlsplit(url).netloc
        req = urllib.request.Request(url, data=data,
                                     headers={"User-Agent": UA, **(headers or {})})
        with self._lock_for(host):
            gap = interval - (time.monotonic() - self._last.get(host, 0.0))
            if gap > 0:
                time.sleep(gap)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return resp.read()
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise HospitalError(str(exc)) from exc
            finally:
                self._last[host] = time.monotonic()
