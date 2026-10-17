"""Configuration loading. Stdlib only, no external deps.

config.json is per-deployment and gitignored (it holds the LINE channel secret and access
token); config.example.json is the committed template. Every key has a default here, so a
missing config.json means "runnable, but LINE calls will fail" rather than a crash - which
is what you want when running the parser tests offline.
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "host": "127.0.0.1",          # never bind 0.0.0.0: the Funnel is the only way in
    "port": 8788,                 # 8787 is stock-dashboard, 3000 is next.js
    # Funnel mounts us at a subpath so stock-dashboard keeps "/". Whether Tailscale strips
    # the prefix before forwarding is unverified, so server.py accepts both forms.
    "pathPrefix": "/cmu",

    # --- LINE ---
    "channelSecret": "",          # Basic settings tab; used to verify x-line-signature
    "channelAccessToken": "",     # Messaging API tab, long-lived
    "lineApiBase": "https://api.line.me",
    "dryRunLine": False,          # True = print the outbound JSON instead of sending it
    # Whoever finds this bot's ID can otherwise burn the 200-push budget, or use it as an
    # oracle to test 身分證 numbers against the hospital. Everyone else gets one refusal
    # reply and never causes a hospital request. Empty list = nobody but ownerUserId.
    "ownerUserId": "",
    "allowedUserIds": [],
    "maxIdLookupsPerDay": 5,      # per user; the ID path is the one worth rate-limiting
    # Free "light" plan is 200 pushes/month and cannot be topped up. Replies are NOT
    # counted, so only watcher.py notifications spend this. Kept below 200 as a buffer
    # against miscounting (a push that times out after LINE accepted it still counts).
    "monthlyPushBudget": 180,

    # --- hospitals ---
    # Which adapters (bot/hospitals/) are switched on. The first is the default for anyone
    # who has not picked one yet.
    "enabledHospitals": ["cmu_hch"],
    # Per-hospital overrides: {"<id>": {"minRequestInterval": 2.0, "sessions": {...}}}.
    # The keys below configure cmu_hch for backwards compatibility.
    "hospitals": {},

    # --- hospital scraping (cmu_hch) ---
    "hospitalBase": "https://www.cmu-hch.com/cgi-bin/hc",
    "requestTimeout": 15.0,
    # The hospital answers as Apache/2.2.15 (CentOS) - a CentOS-6-era box. One in-flight
    # request at a time, spaced a full second apart. Nothing here is urgent enough to
    # justify going faster, and raising this is the fastest way to become a nuisance.
    "minRequestInterval": 1.2,
    # Per-(room, timecode) response cache. Sized so that N subscribers watching one room
    # still cause one fetch per poll round, and a user hammering the bot cannot amplify.
    "fetchCacheSeconds": 45,

    # --- watcher ---
    "pollSeconds": 75,            # inside a clinic session; outside it the thread sleeps
    "defaultNotifyAhead": 5,      # ping when this many people are still ahead
    # ...or when the estimate says this many minutes are left, whichever comes first. Only
    # applies once there is enough history to estimate from (bot/estimate.py).
    "notifyMinutesAhead": 10,

    # --- 回診 / 領藥 reminders (bot/reminders.py) ---
    "apptEveTime": "19:00",       # the evening before an appointment
    "refillTime": "09:00",        # the morning a refill window opens
    # Pushes kept back for "it's your turn": appointment and refill reminders stop once the
    # month's remaining budget falls to this.
    "reminderReserve": 20,
    # Clinic sessions in local time, from the hospital's own 門診時間表.
    # timecode -> [start, end]. The watcher only polls inside these windows.
    "sessions": {
        "1": ["08:30", "12:00"],
        "2": ["13:30", "17:00"],
        "3": ["18:00", "21:00"],
    },
    # Rooms are 3 digits and cluster in the low end of each hundred (101-121, 201-216,
    # 501-505, 601...). Sweeping 100-699 is 600 rooms for ~25% hits; this range covers
    # every room seen so far at a third of the cost. Widen if a user reports a miss.
    "sweepRanges": [[100, 130], [200, 230], [300, 330],
                    [400, 430], [500, 530], [600, 630]],

    "dbPath": "data/bot.db",
}


def load(path=None):
    cfg = dict(DEFAULTS)
    path = path or os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        # keys starting with _ are comments (config.example.json documents itself that way)
        user_cfg = {k: v for k, v in user_cfg.items() if not k.startswith("_")}
        unknown = set(user_cfg) - set(DEFAULTS)
        if unknown:
            raise ValueError("unknown config keys: %s" % ", ".join(sorted(unknown)))
        cfg.update(user_cfg)
    cfg["dbPath"] = os.path.join(ROOT, cfg["dbPath"])
    return cfg
