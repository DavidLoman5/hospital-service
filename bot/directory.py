"""Builds the (hospital, room, session) -> doctor directory so people can search by doctor's
name instead of remembering a room number.

A hospital adapter either lists today's clinics itself (list_clinics) or it does not, and
then the only way to learn which rooms are running is to ask about each one. That makes
this the only part of the project that looks like scraping, so it is deliberately shaped to
be small and quiet:

  * Probing covers adapter.sweep_rooms() - for 中國醫新竹, configured ranges that hold every
    room seen so far at a third of the cost of the whole numeric space.
  * "No such room" and "that room has no clinic in this session today" are usually
    indistinguishable. So one sweep only ever learns the rooms that happen to be running
    that day, and the whitelist is a union accumulated over many days. That is why --daily
    keeps probing the whitelist even on days a room is idle.
  * Run it out of clinic hours. Hospitals serve *today's* data from midnight, so 06:00 is
    both quiet and accurate.

    python3 -m bot.directory --full [hospital_id]    # widen the whitelist (slow, occasional)
    python3 -m bot.directory --daily [hospital_id]   # refresh today's doctors (each morning)
"""

import sys

from bot import clock, config, db, hospitals
from bot.fetch import HospitalError


def _probe(conn, adapter, rooms, codes, day, label):
    """Commits after every hit, deliberately.

    Batching the whole sweep into one transaction holds sqlite's write lock for the entire
    run - eleven minutes during which every webhook fails with "database is locked" and the
    bot silently stops answering. Under WAL a commit is cheap; a sweep is not a place to
    economise on them.
    """
    hits = 0
    for room in rooms:
        for code in codes:
            try:
                snap = adapter.fetch_room(room, code)
            except HospitalError as exc:
                print("  %s %s/%s failed: %s" % (adapter.id, room, code, exc))
                continue
            if snap["kind"] == "ok" and snap["doctor"]:
                db.upsert_room(conn, adapter.id, snap["room"] or room, code,
                               snap["date"] or day, snap["doctor"])
                conn.commit()
                hits += 1
    print("%s %s: %d clinic(s) found across %d room(s)" % (adapter.id, label, hits, len(rooms)))
    return hits


def _from_listing(conn, adapter, codes, day):
    """For hospitals that publish the day's clinics: one request per session."""
    hits = 0
    for code in codes:
        try:
            listing = adapter.list_clinics(code)
        except HospitalError as exc:
            print("  %s listing %s failed: %s" % (adapter.id, code, exc))
            continue
        for room, doctor in listing or ():
            db.upsert_room(conn, adapter.id, room, code, day, doctor)
            hits += 1
        conn.commit()
    print("%s listing: %d clinic(s)" % (adapter.id, hits))
    return hits


def full_sweep(conn, adapter):
    codes = sorted(adapter.sessions)
    if adapter.has_listing:
        return _from_listing(conn, adapter, codes, clock.today())
    rooms = adapter.sweep_rooms()
    print("%s full sweep: %d rooms x %d sessions = %d requests, ~%.0f min at %.1fs apart"
          % (adapter.id, len(rooms), len(codes), len(rooms) * len(codes),
             len(rooms) * len(codes) * adapter.min_interval / 60.0, adapter.min_interval))
    return _probe(conn, adapter, rooms, codes, clock.today(), "full sweep")


def daily_refresh(conn, adapter):
    """Only the rooms that have ever answered. Falls back to a full sweep on a cold DB."""
    codes = sorted(adapter.sessions)
    if adapter.has_listing:
        return _from_listing(conn, adapter, codes, clock.today())
    rooms = db.whitelist(conn, adapter.id)
    if not rooms:
        print("%s whitelist is empty - doing a full sweep first" % adapter.id)
        return full_sweep(conn, adapter)
    print("%s daily refresh: %d known rooms x %d sessions = %d requests"
          % (adapter.id, len(rooms), len(codes), len(rooms) * len(codes)))
    return _probe(conn, adapter, rooms, codes, clock.today(), "daily refresh")


def main(argv):
    cfg = config.load()
    conn = db.init(cfg["dbPath"])
    registry = hospitals.build(cfg)
    wanted = [a for a in argv if not a.startswith("--")]
    for adapter in registry.all():
        if wanted and adapter.id not in wanted:
            continue
        if "--full" in argv:
            full_sweep(conn, adapter)
        else:
            daily_refresh(conn, adapter)


if __name__ == "__main__":
    main(sys.argv[1:])
