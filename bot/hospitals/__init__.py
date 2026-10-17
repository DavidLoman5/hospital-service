"""Hospital registry: which adapters exist, which are switched on, and which one a message
is talking about.

    reg = hospitals.build(cfg)
    reg.get("cmu_hch").fetch_room("115", "2")
    adapter, rest = reg.match("台大新竹 7 下午")
"""

from bot import fetch
from bot.hospitals import cmu_hch

# id -> class. Adding a hospital is one import and one line here.
ADAPTERS = {
    cmu_hch.CmuHsinchu.id: cmu_hch.CmuHsinchu,
}


class Registry:
    def __init__(self, adapters):
        self._by_id = {a.id: a for a in adapters}
        # Longest first, so "台大新竹" wins over "台大" when both are enabled.
        self._names = sorted(((n, a) for a in adapters for n in a.all_names()),
                             key=lambda pair: -len(pair[0]))

    def get(self, hospital_id):
        return self._by_id.get(hospital_id)

    def all(self):
        return list(self._by_id.values())

    def only(self):
        """The adapter, when exactly one is enabled - then nobody has to pick."""
        return next(iter(self._by_id.values())) if len(self._by_id) == 1 else None

    def match(self, text):
        """(adapter, the text with the hospital's name taken out), or (None, text).

        The name may lead the message ("台大新竹 7") or stand anywhere as its own word
        ("7 下午 台大新竹").
        """
        text = text.strip()
        for name, adapter in self._names:
            if text.startswith(name):
                return adapter, text[len(name):].strip()
        tokens = text.split()
        for name, adapter in self._names:
            if name in tokens:
                tokens.remove(name)
                return adapter, " ".join(tokens)
        return None, text

    def set_recorder(self, fn):
        for adapter in self._by_id.values():
            adapter.on_snapshot = fn


def build(cfg, fetcher=None):
    fetcher = fetcher or fetch.Fetcher(cfg["requestTimeout"])
    overrides = cfg.get("hospitals") or {}
    adapters = []
    for hid in cfg["enabledHospitals"]:
        cls = ADAPTERS.get(hid)
        if cls is None:
            raise ValueError("unknown hospital id in enabledHospitals: %s" % hid)
        settings = dict(overrides.get(hid) or {})
        if hid == cmu_hch.CmuHsinchu.id:
            # The pre-multi-hospital keys still configure the original hospital.
            settings.setdefault("base", cfg["hospitalBase"])
            settings.setdefault("sessions", cfg["sessions"])
            settings.setdefault("minRequestInterval", cfg["minRequestInterval"])
            settings.setdefault("fetchCacheSeconds", cfg["fetchCacheSeconds"])
            settings.setdefault("sweepRanges", cfg["sweepRanges"])
        adapters.append(cls(fetcher, settings))
    return Registry(adapters)
