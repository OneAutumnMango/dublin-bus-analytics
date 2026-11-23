import sqlite3

from app.collect.cancellations import record_alerts, record_cancellations, route_summary
from app.core.db import META_SCHEMA
from app.core.models import Arrival

T0 = 1_760_000_000


def arr(trip, sched, cancelled=False):
    return Arrival("s1", trip, "15", 5, sched, sched, None, False, cancelled=cancelled)


def db():
    c = sqlite3.connect(":memory:")
    c.executescript(META_SCHEMA)
    return c


def test_repeated_polls_update_one_row():
    c = db()
    record_cancellations(c, "15", [arr("t", T0 + 600, True)], [], T0)
    record_cancellations(c, "15", [arr("t", T0 + 600, True)], [], T0 + 30)
    assert c.execute("select first_seen, last_seen, cleared from cancellations").fetchall() == [(T0, T0 + 30, 0)]


def test_a_call_with_no_scheduled_time_is_ignored():
    c = db()
    a = arr("t", T0)
    a.scheduled = None
    record_cancellations(c, "15", [a], [], T0)
    assert c.execute("select count(*) from cancellations").fetchone() == (0,)


def test_a_withdrawn_cancellation_is_cleared_then_reopened_if_cancelled_again():
    c = db()
    record_cancellations(c, "15", [arr("t", T0 + 600, True)], [], T0)
    record_cancellations(c, "15", [], [arr("t", T0 + 600)], T0 + 30)
    assert c.execute("select cleared from cancellations").fetchone() == (1,)
    record_cancellations(c, "15", [arr("t", T0 + 600, True)], [], T0 + 60)
    assert c.execute("select cleared from cancellations").fetchone() == (0,)


def test_route_summary_rate_counts_only_open_cancellations_in_the_window():
    c = db()
    record_cancellations(c, "15", [arr("c1", T0 + 100, True), arr("c2", T0 + 200, True), arr("old", T0 - 99999, True)], [], T0)
    for i in range(6):
        c.execute("insert into arrivals(trip_id, stop_id, seq, service_date, scheduled, route) values (?,?,?,?,?,?)",
                  (f"t{i}", "s1", T0 + i, "d", T0 + i, "15"))
    s = route_summary(c, "15", T0, T0 + 1000)
    assert (s["cancelled"], s["observed"]) == (2, 6)
    assert s["rate"] == 0.25
    assert [r["trip_id"] for r in s["recent"]] == ["c2", "c1"]
    assert route_summary(c, "none", T0, T0 + 1000)["rate"] is None


def test_alerts_upsert_keeps_first_seen():
    c = db()
    record_alerts(c, [{"id": "a", "header": "h1", "routeIds": ["r"]}, {"header": "no id"}], T0)
    record_alerts(c, [{"id": "a", "header": "h2"}], T0 + 60)
    assert c.execute("select alert_id, header, first_seen, last_seen from alerts").fetchall() == [("a", "h2", T0, T0 + 60)]
