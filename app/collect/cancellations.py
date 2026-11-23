"""Cancelled trips and service alerts, stored beside (not inside) the tracker's arrival logic.

The server returns a cancelled trip or skipped stop as an arrival flagged `cancelled` with its scheduled
time only. Rows are keyed (trip_id, stop_id, scheduled) so repeated polls update one row; a call later seen
as a normal arrival is marked cleared (the cancellation was withdrawn). A trip that drops out of the feed
before it is polled is never recorded.
"""
import json


def record_cancellations(c, route, cancelled, live, now) -> None:
    """cancelled / live: Arrival lists for this route and tick. Caller owns the transaction."""
    for a in cancelled:
        if a.scheduled is None:
            continue
        c.execute(
            "insert into cancellations(trip_id, stop_id, scheduled, route, first_seen, last_seen, cleared) "
            "values (?,?,?,?,?,?,0) on conflict(trip_id, stop_id, scheduled) do update set last_seen=excluded.last_seen, cleared=0",
            (a.trip_id, a.stop_id, a.scheduled, route, now, now))
    seen = {(a.trip_id, a.stop_id, a.scheduled) for a in live if a.scheduled is not None}
    if seen:
        open_rows = c.execute("select trip_id, stop_id, scheduled from cancellations where route=? and cleared=0 and last_seen<?",
                              (route, now)).fetchall()
        for k in open_rows:
            if tuple(k) in seen:
                c.execute("update cancellations set cleared=1 where trip_id=? and stop_id=? and scheduled=?", k)


def record_alerts(c, alerts, now) -> None:
    for al in alerts:
        aid = al.get("id")
        if not isinstance(aid, str) or not aid:
            continue
        c.execute(
            "insert into alerts(alert_id, header, description, effect, routes, stops, starts, ends, first_seen, last_seen) "
            "values (?,?,?,?,?,?,?,?,?,?) on conflict(alert_id) do update set header=excluded.header, "
            "description=excluded.description, effect=excluded.effect, routes=excluded.routes, stops=excluded.stops, "
            "starts=excluded.starts, ends=excluded.ends, last_seen=excluded.last_seen",
            (aid, str(al.get("header") or "")[:500], str(al.get("description") or "")[:2000], str(al.get("effect") or "")[:64],
             json.dumps(al.get("routeIds") or []), json.dumps(al.get("stopIds") or []),
             al.get("startEpochSeconds"), al.get("endEpochSeconds"), now, now))


def route_summary(c, route, since, until) -> dict:
    """Cancelled calls scheduled in [since, until) vs. arrivals the tracker recorded in the same window."""
    cancelled, = c.execute("select count(*) from cancellations where route=? and cleared=0 and scheduled>=? and scheduled<?",
                           (route, since, until)).fetchone()
    cleared, = c.execute("select count(*) from cancellations where route=? and cleared=1 and scheduled>=? and scheduled<?",
                         (route, since, until)).fetchone()
    scheduled, = c.execute("select count(*) from arrivals where route=? and scheduled>=? and scheduled<?",
                           (route, since, until)).fetchone()
    total = cancelled + scheduled
    recent = [{"trip_id": t, "stop_id": s, "scheduled": sch, "first_seen": fs}
              for t, s, sch, fs in c.execute(
                  "select trip_id, stop_id, scheduled, first_seen from cancellations where route=? and cleared=0 "
                  "and scheduled>=? and scheduled<? order by scheduled desc limit 20", (route, since, until))]
    return {"route": route, "since": since, "until": until, "cancelled": cancelled, "withdrawn": cleared,
            "observed": scheduled, "rate": cancelled / total if total else None, "recent": recent}
