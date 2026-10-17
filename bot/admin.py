"""Small CLI for the things that need a human decision.

    python3 -m bot.admin pending          # who has messaged the bot and is waiting
    python3 -m bot.admin allow U1234...   # let a family member in
    python3 -m bot.admin block U1234...
    python3 -m bot.admin users
    python3 -m bot.admin subs             # today's live reminders
    python3 -m bot.admin quota            # pushes spent this month
    python3 -m bot.admin reminders        # every live 回診 / 領藥 reminder
    python3 -m bot.admin forget U1234...  # delete one person's reminders and history

A LINE userId is printed to the log the first time someone messages the bot, and shows up
in `pending`.
"""

import sys

from bot import clock, config, db, reminders


def main(argv):
    cfg = config.load()
    conn = db.init(cfg["dbPath"])
    cmd = argv[0] if argv else "users"

    if cmd == "pending":
        rows = db.pending_users(conn)
        if not rows:
            print("no pending users")
        for r in rows:
            print("%s  first seen %s" % (r["line_user_id"], r["created_at"]))
        return

    if cmd in ("allow", "block"):
        if len(argv) < 2:
            return print("usage: python3 -m bot.admin %s <lineUserId>" % cmd)
        status = "active" if cmd == "allow" else "blocked"
        n = db.set_user_status(conn, argv[1], status)
        print("%s -> %s" % (argv[1], status) if n else "no such user (have they messaged the bot yet?)")
        return

    if cmd == "users":
        for r in conn.execute("SELECT * FROM users ORDER BY created_at"):
            print("%-40s %-8s last seen %s"
                  % (r["line_user_id"], r["status"], r["last_seen_at"]))
        return

    if cmd == "subs":
        rows = db.active_subscriptions(conn, clock.today())
        if not rows:
            print("no live reminders for %s" % clock.today())
        for r in rows:
            print("#%-4d %s %s room %s %s no.%-4d %s"
                  % (r["id"], r["line_user_id"][:12], r["hospital"], r["room"],
                     clock.SESSION_NAMES.get(r["timecode"], "?"), r["ticket_no"], r["state"]))
        return

    if cmd == "reminders":
        rows = db.active_reminders(conn)
        if not rows:
            print("no live 回診 / 領藥 reminders")
        for r in rows:
            print("#%-4d %s %s" % (r["id"], r["line_user_id"][:12],
                                   reminders.describe(r, clock.now().date())))
        return

    if cmd == "forget":
        if len(argv) < 2:
            return print("usage: python3 -m bot.admin forget <lineUserId>")
        print("removed %d row(s) for %s" % (db.forget_user(conn, argv[1]), argv[1]))
        return

    if cmd == "quota":
        used = db.pushes_this_month(conn)
        print("pushes this month: %d / %d budgeted (LINE free tier is 200, replies are free)"
              % (used, cfg["monthlyPushBudget"]))
        return

    print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
