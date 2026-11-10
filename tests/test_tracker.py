import asyncio
import copy
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.core.db import open_meta, open_samples
from app.core.models import Arrival
from app.collect.forecast import extract_forecasts
from app.collect.tracker import FINALISED_TTL_S, NEG_TTL_S, Tracker

T0 = 1_760_000_000  # Oct 2025


def arr(due=5, expected=None, scheduled=T0 + 300, trip="t1", stop="s1", route="15",
        realtime=True, passed=False):
    return Arrival(stop_id=stop, trip_id=trip, route=route, due_min=due,
                   expected=T0 + due * 60 if expected is None else expected,
                   scheduled=scheduled, delay=None if scheduled is None else 0,
                   realtime=realtime, vehicle_id="v", possibly_passed=passed)


def make_lookup(calls=None):
    async def lookup(a):
        if calls is not None:
            calls.append((a.trip_id, a.stop_id, a.scheduled))
        # seq = rank of scheduled among the trip's calls at that stop; tests use scheduled offsets
        return 0 if a.scheduled is None else max(0, (a.scheduled - T0 - 300) // 3600)
    return lookup


@pytest.fixture
def tr(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    yield t
    t.close()


def rows(tmp_path, sql="select * from arrivals order by id"):
    c = open_meta(tmp_path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def samples(tmp_path, ts=T0):
    c = open_samples(tmp_path, ts)
    try:
        return c.execute("select ts,trip_id,stop_id,seq,due_min,expected,realtime from sample order by ts").fetchall()
    finally:
        c.close()


async def test_unchanged_sample_not_rewritten(tr, tmp_path):
    r1 = await tr.ingest("15", [arr(5)], T0)
    r2 = await tr.ingest("15", [arr(5)], T0 + 30)
    r3 = await tr.ingest("15", [arr(5, expected=T0 + 400)], T0 + 60)  # expected changed
    r4 = await tr.ingest("15", [arr(4, expected=T0 + 400)], T0 + 90)  # due changed
    r5 = await tr.ingest("15", [arr(4, expected=T0 + 400, realtime=False)], T0 + 120)  # realtime changed
    assert [r.samples_written for r in (r1, r2, r3, r4, r5)] == [1, 0, 1, 0, 1]  # due alone never writes
    assert [s[0] for s in samples(tmp_path)] == [T0, T0 + 60, T0 + 120]


async def test_due_zero_finalises_with_confidence_1_and_delay_actual_minus_scheduled(tr, tmp_path):
    await tr.ingest("15", [arr(2, expected=T0 + 400)], T0)
    res = await tr.ingest("15", [arr(0, expected=T0 + 410)], T0 + 30)
    assert (res.finalised, res.dropped) == (1, 0)
    (r,) = rows(tmp_path)
    _, trip, stop, seq, sd, sched, actual, delay, conf, rt, route = r
    assert (trip, stop, seq, sched, actual, delay, conf, rt, route) == (
        "t1", "s1", 0, T0 + 300, T0 + 410, 110, 1, 1, "15")


async def test_vanish_after_due_2_finalises_confidence_0_actual_last_expected(tr, tmp_path):
    await tr.ingest("15", [arr(3, expected=T0 + 500)], T0)
    await tr.ingest("15", [arr(2, expected=T0 + 420)], T0 + 30)
    res = await tr.ingest("15", [], T0 + 60)
    assert (res.finalised, res.dropped) == (1, 0)
    (r,) = rows(tmp_path)
    assert (r[6], r[7], r[8]) == (T0 + 420, 120, 0)


async def test_vanish_boundary_due_3_dropped_due_2_finalised(tr, tmp_path):
    await tr.ingest("15", [arr(3, trip="a"), arr(2, trip="b")], T0)
    res = await tr.ingest("15", [], T0 + 30)
    assert (res.finalised, res.dropped) == (1, 1)
    assert [r[1] for r in rows(tmp_path)] == ["b"]


async def test_vanish_with_due_5_dropped_not_stored(tr, tmp_path):
    await tr.ingest("15", [arr(5)], T0)
    res = await tr.ingest("15", [], T0 + 30)
    assert (res.finalised, res.dropped) == (0, 1)
    assert rows(tmp_path) == []
    assert (await tr.ingest("15", [], T0 + 60)).dropped == 0  # counted once


async def test_actual_is_last_expected_with_due_le_2_not_later_bigger_due(tr, tmp_path):
    await tr.ingest("15", [arr(2, expected=T0 + 400)], T0)
    await tr.ingest("15", [arr(3, expected=T0 + 999)], T0 + 30)  # due went up: not eligible for actual
    await tr.ingest("15", [], T0 + 60)  # last due 3 -> dropped per rule
    assert rows(tmp_path) == []


async def test_first_seen_at_due_0_finalises_once(tr, tmp_path):
    res = await tr.ingest("15", [arr(0)], T0)
    assert (res.samples_written, res.finalised) == (1, 1)
    res = await tr.ingest("15", [arr(0)], T0 + 30)
    assert (res.samples_written, res.finalised) == (0, 0)
    res = await tr.ingest("15", [], T0 + 60)
    assert (res.finalised, res.dropped) == (0, 0)
    assert len(rows(tmp_path)) == 1
    assert len(samples(tmp_path)) == 1


async def test_reingest_after_restart_is_noop_via_unique(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(0)], T0)
    t.close()
    t = Tracker(tmp_path, make_lookup())  # shortcut: in-flight state lost, UNIQUE is the backstop
    res = await t.ingest("15", [arr(0)], T0 + 30)
    t.close()
    assert res.finalised == 0
    assert len(rows(tmp_path)) == 1


async def test_loop_trip_two_seq_two_arrivals(tr, tmp_path):
    a0 = arr(0, scheduled=T0 + 300)
    a1 = arr(0, scheduled=T0 + 300 + 3600)
    res = await tr.ingest("15", [a0, a1], T0)
    assert res.finalised == 2
    assert sorted(r[3] for r in rows(tmp_path)) == [0, 1]


async def test_scheduled_only_stored_realtime_0(tr, tmp_path):
    await tr.ingest("15", [arr(2, realtime=False)], T0)
    await tr.ingest("15", [], T0 + 30)
    (r,) = rows(tmp_path)
    assert r[9] == 0 and r[8] == 0


async def test_realtime_if_ever_realtime(tr, tmp_path):
    await tr.ingest("15", [arr(2, realtime=True)], T0)
    await tr.ingest("15", [arr(1, realtime=False)], T0 + 30)
    await tr.ingest("15", [], T0 + 60)
    assert rows(tmp_path)[0][9] == 1


async def test_scheduled_none_delay_null_service_date_from_expected(tr, tmp_path):
    await tr.ingest("15", [arr(0, scheduled=None)], T0)
    (r,) = rows(tmp_path)
    assert r[5] is None and r[7] is None
    assert r[4] == datetime.fromtimestamp(T0, ZoneInfo("Europe/Dublin")).date().isoformat()


async def test_negative_delay_early_bus(tr, tmp_path):
    await tr.ingest("15", [arr(0, expected=T0 + 250)], T0)
    assert rows(tmp_path)[0][7] == -50


def dub(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("Europe/Dublin")).timestamp())


async def test_service_date_across_midnight(tr, tmp_path):
    sched = dub(2025, 11, 9, 0, 10)  # timetable 24:10 of the 8th; scheduled epoch's local date wins
    await tr.ingest("15", [arr(0, scheduled=sched, expected=sched + 60)], sched - 600)
    assert rows(tmp_path)[0][4] == "2025-11-09"


async def test_service_date_uses_dublin_not_utc_in_summer(tr, tmp_path):
    sched = dub(2025, 7, 1, 0, 30)  # 23:30 UTC on 30 June
    await tr.ingest("15", [arr(0, scheduled=sched, expected=sched)], sched - 600)
    assert rows(tmp_path)[0][4] == "2025-07-01"


async def test_service_date_across_dst_change(tr, tmp_path):
    # BST ends 2025-10-26 at 02:00 local (01:00 UTC)
    s1 = dub(2025, 10, 26, 0, 30)
    s2 = dub(2025, 10, 27, 0, 30)
    await tr.ingest("15", [arr(0, scheduled=s1, expected=s1, trip="a")], s1 - 600)
    await tr.ingest("15", [arr(0, scheduled=s2, expected=s2, trip="b")], s2 - 600)
    assert [r[4] for r in rows(tmp_path)] == ["2025-10-26", "2025-10-27"]


async def test_vanish_detection_is_per_route(tr, tmp_path):
    await tr.ingest("15", [arr(1, trip="a", route="15")], T0)
    await tr.ingest("16", [arr(1, trip="b", route="16")], T0)
    res = await tr.ingest("15", [], T0 + 30)
    assert res.finalised == 1
    assert [r[1] for r in rows(tmp_path)] == ["a"]
    res = await tr.ingest("16", [arr(0, trip="b", route="16")], T0 + 30)
    assert res.finalised == 1


async def test_possibly_passed_recorded_and_still_present(tr, tmp_path):
    r = await tr.ingest("15", [arr(1, passed=True)], T0)
    assert r.samples_written == 1
    r = await tr.ingest("15", [arr(1, passed=True)], T0 + 30)
    assert (r.finalised, r.dropped) == (0, 0)


async def test_stale_now_ignored(tr, tmp_path):
    await tr.ingest("15", [arr(5)], T0 + 100)
    for now in (T0, T0 + 100):  # older or equal to the last for the route: ignored entirely
        res = await tr.ingest("15", [], now)
        assert (res.samples_written, res.finalised, res.dropped) == (0, 0, 0)
    assert (await tr.ingest("15", [], T0 + 101)).dropped == 1
    # other routes have independent clocks
    assert (await tr.ingest("16", [arr(0, route="16", trip="z")], T0)).finalised == 1


async def test_seq_lookup_cached_per_key(tmp_path):
    calls = []
    t = Tracker(tmp_path, make_lookup(calls))
    for i in range(3):
        await t.ingest("15", [arr(5)], T0 + i * 30)
    t.close()
    assert len(calls) == 1


async def test_many_trips_many_stops(tr, tmp_path):
    al = [arr(0, trip=f"t{i}", stop=f"s{j}") for i in range(20) for j in range(15)]
    res = await tr.ingest("15", al, T0)
    assert (res.samples_written, res.finalised) == (300, 300)
    assert len(rows(tmp_path)) == 300


async def test_finalised_state_evicted_after_ttl_and_bounded(tr, tmp_path):
    await tr.ingest("15", [arr(0)], T0)
    assert len(tr._finalised) == 1
    await tr.ingest("15", [], T0 + FINALISED_TTL_S + 1)
    assert len(tr._finalised) == 0
    # after eviction, the UNIQUE backstop still prevents a duplicate row
    res = await tr.ingest("15", [arr(0)], T0 + FINALISED_TTL_S + 2)
    assert res.finalised == 0 and len(rows(tmp_path)) == 1


async def test_month_rollover_switches_samples_file(tr, tmp_path):
    end_oct = int(datetime(2025, 10, 31, 23, 59, 30, tzinfo=ZoneInfo("UTC")).timestamp())
    await tr.ingest("15", [arr(5, expected=end_oct + 300, scheduled=end_oct + 300)], end_oct)
    await tr.ingest("15", [arr(4, expected=end_oct + 301, scheduled=end_oct + 300)], end_oct + 60)
    assert len(samples(tmp_path, end_oct)) == 1
    assert len(samples(tmp_path, end_oct + 60)) == 1


async def test_exception_mid_ingest_rolls_back_whole_tick(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(5, trip="keep")], T0)
    calls = {"n": 0}
    orig = t._on_finalised

    def boom(arrival_id, row):
        calls["n"] += 1
        raise RuntimeError("boom")

    t._on_finalised = boom
    with pytest.raises(RuntimeError):
        await t.ingest("15", [arr(0, trip="x"), arr(0, trip="y"), arr(4, trip="keep", expected=T0 + 1)], T0 + 30)
    assert rows(tmp_path) == []
    assert len(samples(tmp_path)) == 1  # only the first tick's sample
    # in-memory state restored: retry succeeds and yields identical results to a clean run
    t._on_finalised = orig
    res = await t.ingest("15", [arr(0, trip="x"), arr(0, trip="y"), arr(4, trip="keep", expected=T0 + 1)], T0 + 30)
    assert (res.samples_written, res.finalised) == (3, 2)
    t.close()


async def test_on_finalised_hook_receives_id_and_row(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    got = []
    t._on_finalised = lambda i, row: got.append((i, row))
    await t.ingest("15", [arr(0)], T0)
    t.close()
    assert got[0][0] == rows(tmp_path)[0][0]
    assert got[0][1]["actual"] == T0


async def test_close_idempotent(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(5)], T0)
    t.close()
    t.close()


async def test_dropped_key_reappearing_keeps_change_only_and_evicts(tr, tmp_path):
    await tr.ingest("15", [arr(5)], T0)
    assert (await tr.ingest("15", [], T0 + 30)).dropped == 1
    res = await tr.ingest("15", [arr(5)], T0 + 60)  # identical to last sample
    assert res.samples_written == 0
    await tr.ingest("15", [], T0 + 90)
    await tr.ingest("15", [], T0 + 90 + FINALISED_TTL_S + 1)
    assert tr._live == {}


async def test_seq_lookup_failure_skips_only_that_arrival(tmp_path):
    async def lookup(a):
        if a.trip_id == "bad":
            raise RuntimeError("lookup down")
        return 0
    t = Tracker(tmp_path, lookup)
    res = await t.ingest("15", [arr(0, trip="good"), arr(0, trip="bad")], T0)
    t.close()
    assert (res.finalised, res.skipped) == (1, 1)
    assert [r[1] for r in rows(tmp_path)] == ["good"]


@pytest.mark.parametrize("kw", [dict(scheduled="x"), dict(trip=""), dict(trip=None), dict(due="3"),
                                dict(expected="9")])
async def test_invalid_arrival_skipped_not_raised(tr, tmp_path, kw):
    a = arr(**{k: v for k, v in kw.items() if k != "due" and k != "expected"})
    if "due" in kw:
        a.due_min = kw["due"]
    if "expected" in kw:
        a.expected = kw["expected"]
    res = await tr.ingest("15", [a, arr(0, trip="ok")], T0)
    assert (res.skipped, res.finalised) == (1, 1)


async def test_skipped_arrival_does_not_cause_false_vanish(tmp_path):
    fail = {"on": False}

    async def lookup(a):
        if fail["on"] and a.scheduled == T0 + 301:
            raise RuntimeError
        return 0
    t = Tracker(tmp_path, lookup)
    await t.ingest("15", [arr(1, scheduled=T0 + 300)], T0)
    fail["on"] = True
    res = await t.ingest("15", [arr(1, scheduled=T0 + 301)], T0 + 30)  # same trip/stop, lookup fails
    t.close()
    assert (res.skipped, res.finalised, res.dropped, res.samples_written) == (1, 0, 0, 0)  # shielded


async def test_negative_due_treated_as_arrived(tr, tmp_path):
    res = await tr.ingest("15", [arr(-1, expected=T0 + 290)], T0)
    assert res.finalised == 1
    assert rows(tmp_path)[0][8] == 1


async def test_stale_flag(tr):
    assert (await tr.ingest("15", [], T0)).stale is False
    assert (await tr.ingest("15", [], T0)).stale is True


async def test_state_bounded_over_days(tr):
    for day in range(5):
        base = T0 + day * 86400
        al = [arr(0, trip=f"f{day}-{i}", scheduled=base + 300) for i in range(20)]
        al += [arr(5, trip=f"d{day}-{i}", scheduled=base + 300) for i in range(20)]
        await tr.ingest("15", al, base)
        await tr.ingest("15", [], base + 30)  # drops the due-5 ones
    await tr.ingest("15", [], T0 + 5 * 86400)
    assert (len(tr._seq), len(tr._live), len(tr._finalised)) == (0, 0, 0)


async def test_gather_routes_one_failing_leaves_other_intact(tmp_path):
    async def lookup(a):
        await asyncio.sleep(0)
        if a.trip_id == "bad":
            raise RuntimeError
        return 0
    t = Tracker(tmp_path, lookup)
    r15, r16 = await asyncio.gather(
        t.ingest("15", [arr(0, trip="bad"), arr(0, trip="a")], T0),
        t.ingest("16", [arr(0, trip="b", route="16")], T0))
    t.close()
    assert (r15.finalised, r15.skipped, r16.finalised) == (1, 1, 1)
    assert sorted(r[1] for r in rows(tmp_path)) == ["a", "b"]


async def test_gather_across_month_boundary(tmp_path):
    end_oct = int(datetime(2025, 10, 31, 23, 59, 50, tzinfo=ZoneInfo("UTC")).timestamp())
    t = Tracker(tmp_path, make_lookup())
    await asyncio.gather(t.ingest("15", [arr(5, expected=end_oct + 300, scheduled=end_oct + 300)], end_oct),
                         t.ingest("16", [arr(5, route="16", trip="z", expected=end_oct + 300, scheduled=end_oct + 300)], end_oct + 20))
    t.close()
    assert len(samples(tmp_path, end_oct)) == 1 and len(samples(tmp_path, end_oct + 20)) == 1


async def test_failed_tick_restores_seq_and_state(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(5, trip="keep")], T0)
    before = (dict(t._seq), {k: v.__dict__.copy() for k, v in t._live.items()}, dict(t._last_now))
    t._on_finalised = lambda i, r: 1 / 0
    with pytest.raises(ZeroDivisionError):
        await t.ingest("15", [arr(0, trip="n"), arr(3, trip="keep", expected=T0 + 7)], T0 + 30)
    assert before == (dict(t._seq), {k: v.__dict__.copy() for k, v in t._live.items()}, dict(t._last_now))
    t.close()


# ---- property test -------------------------------------------------------------------

KEYS = [("t1", "s1", 0), ("t1", "s1", 1), ("t1", "s2", 0), ("t2", "s1", 0)]
ROUTE_OF = {k: "15" if i % 2 == 0 else "16" for i, k in enumerate(KEYS)}


@st.composite
def scenarios(draw):
    """ticks: list of (route, now, [(key, due, noise, realtime)])."""
    ticks = []
    now = T0
    for _ in range(draw(st.integers(1, 14))):
        now += draw(st.integers(-20, 60)) if draw(st.integers(0, 6)) == 0 else draw(st.integers(1, 60))
        route = draw(st.sampled_from(["15", "16"]))
        subset = draw(st.lists(st.sampled_from([k for k in KEYS if ROUTE_OF[k] == route]), unique=True, max_size=4))
        subset = draw(st.permutations(subset))
        al = [(k, draw(st.integers(-1, 8)), draw(st.integers(-30, 30)), draw(st.booleans())) for k in subset]
        if al and draw(st.booleans()):
            al.append(al[0])  # repeated key in one tick
        ticks.append((route, now, al))
    return ticks


@given(scenarios())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_property_invariants(tmp_path_factory, ticks):
    d = tmp_path_factory.mktemp("prop")

    async def lookup(a):
        return int(a.scheduled - T0 - 300) // 3600

    t = Tracker(d, lookup)
    total_final = 0
    # independent model of what the tracker should have seen, per key, over accepted ticks
    last_now: dict[str, int] = {}
    model: dict[tuple, dict] = {}
    for route, now, al in ticks:
        arrs = [arr(due, expected=T0 + due * 60 + noise, scheduled=T0 + 300 + k[2] * 3600,
                    trip=k[0], stop=k[1], realtime=rt, route=route) for k, due, noise, rt in al]
        res = await t.ingest(route, arrs, now)
        total_final += res.finalised
        if now <= last_now.get(route, -1):
            assert res.stale
            continue
        last_now[route] = now
        for k, due, noise, rt in {x[0]: x for x in al}.values():  # last occurrence wins
            m = model.setdefault(k, dict(actual=None, zero=False, done=False, last_due=None))
            if m["done"]:
                continue
            m["last_due"] = due
            if due <= 2:
                m["actual"] = T0 + due * 60 + noise
            if due <= 0:
                m["zero"] = m["done"] = True
        present = {x[0] for x in al}
        for k, m in model.items():  # vanish: live key of this route absent from this tick
            if ROUTE_OF[k] == route and not m["done"] and k not in present and m["last_due"] <= 2:
                m["done"] = True
    t.close()
    m = sqlite3.connect(d / "meta.db")
    try:
        arrivals = m.execute("select trip_id,stop_id,seq,actual,delay,scheduled,confidence from arrivals").fetchall()
    finally:
        m.close()
    assert total_final == len(arrivals)  # each key finalised at most once, no phantom rows
    sc = open_samples(d, T0)
    try:
        for key in sc.execute("select distinct trip_id,stop_id,seq from sample").fetchall():
            seq_rows = sc.execute("select due_min,expected,realtime from sample where trip_id=? and stop_id=? and seq=? order by ts", key).fetchall()
            # change-only: consecutive rows differ in (expected, realtime), except the due<=0 row
            assert all(x[1:] != y[1:] or y[0] <= 0 for x, y in zip(seq_rows, seq_rows[1:]))
    finally:
        sc.close()
    assert {(r[0], r[1], r[2]) for r in arrivals} == {k for k, v in model.items() if v["done"]}
    sc = open_samples(d, T0)  # independent recomputation of every forecast row
    m = sqlite3.connect(d / "meta.db")
    try:
        got = m.execute("select a.trip_id,a.stop_id,a.seq,f.horizon_min,f.predicted,f.error_s from forecast f "
                        "left join arrivals a on a.id=f.arrival_id").fetchall()
        want = [(tr_, st_, sq, *f) for tr_, st_, sq, act, *_ in arrivals
                for f in extract_forecasts([sc], tr_, st_, sq, act)]
    finally:
        m.close()
        sc.close()
    assert sorted(got) == sorted(want)
    assert len(got) <= 5 * len(arrivals)
    for trip, stop, seq, actual, delay, sched, conf in arrivals:
        mk = model[(trip, stop, seq)]
        assert delay == actual - sched
        assert actual == mk["actual"]  # last expected seen with due <= 2
        assert conf == int(mk["zero"])


# ---- fix round 2 -----------------------------------------------------------------------
import app.collect.tracker as trk  # noqa: E402


@pytest.mark.parametrize("kw", [dict(trip=["a"]), dict(trip={"a": 1}), dict(stop=["s"]), dict(stop=""),
                                dict(due=10**30), dict(due=True), dict(expected=0), dict(scheduled=True),
                                dict(scheduled=0), dict(expected=10**30)])
async def test_full_validation_skips_and_counts(tr, tmp_path, kw):
    a = arr(0)
    for k, v in kw.items():
        setattr(a, {"trip": "trip_id", "stop": "stop_id", "due": "due_min", "expected": "expected",
                    "scheduled": "scheduled"}[k], v)
    res = await tr.ingest("15", [a, arr(0, trip="ok")], T0)
    assert (res.skipped, res.finalised) == (1, 1)
    assert [r[1] for r in rows(tmp_path)] == ["ok"]


async def test_open_samples_failure_at_rollover_is_clean(tmp_path, monkeypatch):
    E = int(datetime(2025, 10, 31, 23, 59, 40, tzinfo=ZoneInfo("UTC")).timestamp())
    real = trk.open_samples
    fail = {"on": False}

    def op(d, ts):
        if fail["on"] and ts > E + 15:
            raise OSError("ENOSPC")
        return real(d, ts)
    monkeypatch.setattr(trk, "open_samples", op)
    t = Tracker(tmp_path, make_lookup())
    sc = dict(expected=E + 300, scheduled=E + 300)
    await t.ingest("15", [arr(5, trip="x", **sc)], E)
    await t.ingest("16", [arr(5, trip="y", route="16", **sc)], E)
    before = (copy.deepcopy((t._live, t._finalised, t._seq, t._last_now)))
    fail["on"] = True
    with pytest.raises(OSError):
        await t.ingest("15", [arr(4, trip="x", expected=E + 301, scheduled=E + 300)], E + 30)
    assert before == copy.deepcopy((t._live, t._finalised, t._seq, t._last_now))
    fail["on"] = False
    res = await t.ingest("15", [arr(4, trip="x", expected=E + 301, scheduled=E + 300)], E + 30)  # same now: not stale
    assert res.stale is False and res.samples_written == 1
    assert (await t.ingest("16", [arr(5, trip="y", route="16", **sc)], E + 40)).stale is False
    t.close()


async def test_rollback_failure_does_not_hide_original_or_skip_undo(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(5)], T0)

    class Bad:
        def __init__(self, c): self.c = c
        def rollback(self): raise sqlite3.OperationalError("rollback failed")
        def __getattr__(self, k): return getattr(self.c, k)
    real = t.meta
    t.meta = Bad(real)
    t._on_finalised = lambda i, r: 1 / 0
    with pytest.raises(ZeroDivisionError):
        await t.ingest("15", [arr(0, trip="n")], T0 + 30)
    assert t._last_now["15"] == T0 and "n" not in {k[0] for k in t._live}
    t.meta = real
    t.close()


async def test_lookups_resolve_concurrently_outside_lock(tmp_path):
    async def slow(a):
        await asyncio.sleep(0.05)
        return 0
    t = Tracker(tmp_path, slow)
    work = [t.ingest(str(r), [arr(5, trip=f"{r}-{i}", route=str(r)) for i in range(5)], T0) for r in range(4)]
    t0 = time.perf_counter()
    await asyncio.gather(*work)
    elapsed = time.perf_counter() - t0
    t.close()
    assert elapsed < 20 * 0.05 / 2


async def test_hung_lookup_times_out_and_does_not_block_other_route(tmp_path):
    async def lookup(a):
        if a.route == "A":
            await asyncio.Event().wait()
        return 0
    t = Tracker(tmp_path, lookup, lookup_timeout=0.2)
    t0 = time.perf_counter()
    ra, rb = await asyncio.gather(t.ingest("A", [arr(0, route="A", trip="a")], T0),
                                  t.ingest("B", [arr(0, route="B", trip="b")], T0))
    assert time.perf_counter() - t0 < 1.0
    assert (ra.skipped, ra.finalised, rb.finalised) == (1, 0, 1)
    assert "A" in t._last_now and t._live == {}
    t.close()


async def test_failed_trip_looked_up_once_per_tick_and_once_per_minute(tmp_path):
    calls = []

    async def lookup(a):
        calls.append((a.trip_id, a.stop_id))
        raise RuntimeError("down")
    t = Tracker(tmp_path, lookup)
    al = [arr(5, trip="x", stop=f"s{i}") for i in range(4)]
    res = await t.ingest("15", al, T0)
    assert (len(calls), res.skipped) == (1, 4)
    await t.ingest("15", al, T0 + 30)  # within negative-cache window
    assert len(calls) == 1
    await t.ingest("15", al, T0 + 60)  # window over: one more attempt
    assert len(calls) == 2
    t.close()


async def test_duplicate_keys_in_tick_last_wins(tr, tmp_path):
    res = await tr.ingest("15", [arr(5, expected=T0 + 1), arr(4, expected=T0 + 2)], T0)
    assert res.samples_written == 1
    assert samples(tmp_path)[0][4:6] == (4, T0 + 2)


async def test_seq_last_used_refresh_keeps_entry_alive(tr):
    await tr.ingest("15", [arr(5)], T0)
    await tr.ingest("15", [arr(5)], T0 + FINALISED_TTL_S - 10)
    await tr.ingest("15", [], T0 + FINALISED_TTL_S + 10)  # age 20 since last use
    assert len(tr._seq) == 1


async def test_ttl_boundary_exactly_ttl_kept_one_over_evicted(tr):
    await tr.ingest("15", [arr(0)], T0)  # finalised and seq cached at T0
    await tr.ingest("15", [], T0 + FINALISED_TTL_S)
    assert (len(tr._finalised), len(tr._seq)) == (1, 1)
    await tr.ingest("15", [], T0 + FINALISED_TTL_S + 1)
    assert (len(tr._finalised), len(tr._seq)) == (0, 0)


async def test_stale_precheck_skips_lookups(tmp_path):
    calls = []
    t = Tracker(tmp_path, make_lookup(calls))
    await t.ingest("15", [], T0)
    res = await t.ingest("15", [arr(5)], T0)
    assert res.stale and calls == []
    t.close()


async def test_concurrent_same_route_same_now_second_is_stale(tr):
    r1, r2 = await asyncio.gather(tr.ingest("15", [arr(5)], T0), tr.ingest("15", [arr(5)], T0))
    assert sorted([r1.stale, r2.stale]) == [False, True]


# ---- fix round 3 -----------------------------------------------------------------------
def guarded(coro, t=10):
    return asyncio.wait_for(coro, t)


def good_samples(tmp_path):
    return [s for s in samples(tmp_path) if s[1] == "good"]


@pytest.mark.parametrize("bad", [
    dict(expected=1_760_000_000_000, scheduled=None), dict(scheduled=1_760_000_000_300),
    dict(realtime=None), dict(realtime=1),
])
async def test_poison_arrival_never_blocks_route(tmp_path, bad):
    t = Tracker(tmp_path, make_lookup())
    for i in range(3):
        res = await guarded(t.ingest("15", [arr(0 if bad.get("expected") else 4, trip="bad", **bad),
                                            arr(5 - i, trip="good")], T0 + 30 * i))
        assert res.skipped == 1
    assert len(good_samples(tmp_path)) == 3
    t.close()


@pytest.mark.parametrize("v", [None, "0", 1.5, True, -1])
async def test_poison_seq_lookup_result_never_blocks_route(tmp_path, v):
    async def lk(a):
        return v if a.trip_id == "odd" else 0
    t = Tracker(tmp_path, lk)
    for i in range(3):
        res = await guarded(t.ingest("15", [arr(0, trip="odd"), arr(5 - i, trip="good")], T0 + 30 * i))
        assert res.skipped == 1
    assert len(good_samples(tmp_path)) == 3
    t.close()


async def test_epoch_window_boundary(tr):
    ok = arr(5, expected=T0 + 86400, scheduled=T0 - 86400)
    bad = arr(5, trip="b", expected=T0 + 86401)
    res = await tr.ingest("15", [ok, bad], T0)
    assert (res.samples_written, res.skipped) == (1, 1)


async def test_unexpected_error_in_one_arrival_skips_only_it(tmp_path, monkeypatch):
    orig = Tracker._row

    def row(key, st):
        if key[0] == "boom":
            raise RuntimeError("unexpected")
        return orig(key, st)
    monkeypatch.setattr(Tracker, "_row", staticmethod(row))
    t = Tracker(tmp_path, make_lookup())
    res = await t.ingest("15", [arr(0, trip="boom"), arr(0, trip="ok")], T0)
    assert (res.skipped, res.finalised) == (1, 1)
    assert "boom" not in {k[0] for k in t._live} and [r[1] for r in rows(tmp_path)] == ["ok"]
    t.close()


def table_lookup(table, fail=None):
    async def lk(a):
        if fail and fail["on"]:
            raise RuntimeError("down")
        return table[a.scheduled]
    return lk


async def test_loop_fallback_does_not_misassign_second_visit(tmp_path):
    fail = {"on": False}
    S0, S1 = T0 + 300, T0 + 300 + 3600
    t = Tracker(tmp_path, table_lookup({S0: 0, S1: 1}, fail))
    v0 = lambda due: arr(due, scheduled=S0)
    v1 = lambda due: arr(due, scheduled=S1, expected=T0 + 3600 + due * 60)
    await t.ingest("15", [v0(2)], T0)
    fail["on"] = True
    r = await t.ingest("15", [v0(1), v1(45)], T0 + 30)
    assert r.skipped == 1
    await t.ingest("15", [v1(44)], T0 + 60)  # visit 0 vanished after due 1 -> finalised
    (row,) = rows(tmp_path)
    assert (row[3], row[6]) == (0, T0 + 60)
    assert all(s[3] == 0 for s in samples(tmp_path)) and len(samples(tmp_path)) == 2
    t.close()


async def test_resolve_before_lock_four_routes_one_slow_key_each(tmp_path):
    T = 0.3

    async def slow(a):
        await asyncio.sleep(T)
        return 0
    t = Tracker(tmp_path, slow)
    t0 = time.perf_counter()
    await guarded(asyncio.gather(*(t.ingest(str(r), [arr(5, trip=f"k{r}", route=str(r))], T0) for r in range(4))))
    assert time.perf_counter() - t0 < 3 * T  # serial would be 4T; slack for loaded machines
    t.close()


async def test_route_b_completes_while_route_a_lookup_pending(tmp_path):
    gate = asyncio.Event()

    async def lk(a):
        if a.route == "A":
            await gate.wait()
        return 0
    t = Tracker(tmp_path, lk, lookup_timeout=30)
    ta = asyncio.create_task(t.ingest("A", [arr(0, route="A", trip="a")], T0))
    await asyncio.sleep(0)
    rb = await guarded(t.ingest("B", [arr(0, route="B", trip="b")], T0))
    assert rb.finalised == 1 and not ta.done()
    gate.set()
    assert (await guarded(ta)).finalised == 1
    t.close()


async def test_semaphore_wait_counts_against_timeout(tmp_path):
    async def lk(a):
        if a.route == "A":
            await asyncio.Event().wait()
        return 0
    TO = 0.2
    t = Tracker(tmp_path, lk, lookup_timeout=TO)
    t0 = time.perf_counter()
    ra, rb = await guarded(asyncio.gather(
        t.ingest("A", [arr(5, route="A", trip=f"a{i}") for i in range(16)], T0),
        t.ingest("B", [arr(0, route="B", trip="b")], T0)))
    assert time.perf_counter() - t0 < 2 * TO + 0.3
    assert ra.skipped == 16
    t.close()


async def test_neg_cache_entries_swept(tmp_path):
    async def bad(a):
        raise RuntimeError
    t = Tracker(tmp_path, bad)
    await t.ingest("15", [arr(5)], T0)
    assert "t1" in t._neg
    await t.ingest("15", [], T0 + NEG_TTL_S)  # age == TTL: swept (>=)
    assert t._neg == {}
    t.close()


async def test_persistent_lookup_failure_never_borrows_another_visit(tmp_path):
    fail = {"on": False}
    S = T0 + 300
    t = Tracker(tmp_path, table_lookup({S: 0}, fail))
    await t.ingest("15", [arr(2, expected=T0 + 400, scheduled=S)], T0)
    fail["on"] = True
    for i, due in enumerate((1, 0)):  # new scheduled each tick -> uncached -> lookup raises
        r = await t.ingest("15", [arr(due, expected=T0 + 500 + i, scheduled=S + 1 + i)], T0 + 30 * (i + 1))
        assert (r.skipped, r.samples_written, r.finalised, r.dropped) == (1, 0, 0, 0)
    r = await t.ingest("15", [], T0 + 200)  # bus gone: finalised late from its own data only
    assert r.finalised == 1
    (row,) = rows(tmp_path)
    assert (row[6], row[8]) == (T0 + 400, 0)
    t.close()


async def test_invalid_arrival_shields_live_key_from_vanish(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(1)], T0)
    r = await t.ingest("15", [arr(1, realtime=None)], T0 + 30)
    assert (r.skipped, r.finalised, r.dropped) == (1, 0, 0)
    r = await t.ingest("15", [], T0 + 60)
    assert r.finalised == 1
    t.close()


def snapshot(t):
    return copy.deepcopy(({k: v.__dict__ for k, v in t._live.items()}, t._finalised, t._seq, t._last_now))


async def test_failed_tick_restores_every_live_state(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(5, trip="A"), arr(5, trip="C"), arr(2, trip="B")], T0)
    before = snapshot(t)
    calls = []

    def hook(i, row):
        calls.append(i)
        if len(calls) == 2:
            raise RuntimeError("later key")
    t._on_finalised = hook
    # A updated; D finalised (hook 1); C dropped; B vanish-finalised (hook 2 raises)
    with pytest.raises(RuntimeError):
        await t.ingest("15", [arr(4, trip="A", expected=T0 + 9), arr(0, trip="D")], T0 + 30)
    assert before == snapshot(t)
    assert rows(tmp_path) == []
    t.close()


class Boom(Exception):
    pass


class _Inj:
    point = None
    n = 0

    def hit(self, p):
        if self.point == p:
            self.n -= 1
            if self.n <= 0:
                self.point = None
                raise Boom(p)


class _Proxy:
    def __init__(self, c, tag, inj):
        self._c, self._tag, self._inj = c, tag, inj

    def execute(self, sql, *a):
        if not sql.lstrip().startswith("select"):  # reads (forecast extraction) are not tick-write failure points
            self._inj.hit(self._tag + ".execute")
        return self._c.execute(sql, *a)

    def executemany(self, *a):
        self._inj.hit(self._tag + ".execute")
        return self._c.executemany(*a)

    def commit(self):
        self._inj.hit(self._tag + ".commit")
        return self._c.commit()

    def __getattr__(self, k):
        return getattr(self._c, k)


def dbdump(d):
    from pathlib import Path
    out = []
    c = sqlite3.connect(Path(d) / "meta.db")
    out.append(c.execute("select trip_id,stop_id,seq,actual,confidence from arrivals order by 1,2,3").fetchall())
    c.close()
    for f in sorted(Path(d).glob("samples-*.db")):
        c = sqlite3.connect(f)
        r = c.execute("select * from sample order by 1,2,3,4").fetchall()
        c.close()
        if r:  # a freshly created empty month file is not a state change
            out.append((f.name, r))
    c = sqlite3.connect(Path(d) / "meta.db")
    out.insert(1, c.execute("select a.trip_id,a.stop_id,a.seq,f.horizon_min,f.predicted,f.error_s from forecast f "
                            "left join arrivals a on a.id=f.arrival_id order by 1,2,3,4").fetchall())
    c.close()
    return out


POINTS = ["meta.execute", "meta.commit", "samples.execute", "samples.commit", "open_samples", "hook", "lookup_cancel"]


@given(st.integers(0, 10**6))
@settings(max_examples=40, deadline=None)
async def test_failure_injection_oracle(seed):
    import random
    import tempfile
    rnd = random.Random(seed)
    inj = _Inj()
    base = int(datetime(2025, 10, 31, 23, 50, tzinfo=ZoneInfo("UTC")).timestamp())  # month rollover inside
    d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    real_open = trk.open_samples

    def open_samples(d, ts):
        inj.hit("open_samples")
        return _Proxy(real_open(d, ts), "samples", inj)

    async def lookup(a):
        if inj.point == "lookup_cancel":
            inj.n -= 1
            if inj.n <= 0:
                inj.point = None
                raise asyncio.CancelledError()
        return max(0, (a.scheduled - base - 300) // 3600)

    async def plain(a):
        return max(0, (a.scheduled - base - 300) // 3600)

    trk.open_samples = open_samples
    try:
        t = Tracker(d1, lookup)
        t.meta = _Proxy(t.meta, "meta", inj)
        ref = Tracker(d2, plain)
        real_hook = t._on_finalised

        def hook(i, r):
            real_hook(i, r)  # forecast rows are inserted before the injected failure
            inj.hit("hook")
        t._on_finalised = hook
        keys = [(f"t{i}", f"s{j}", k) for i in range(3) for j in range(2) for k in range(2)]
        now = base
        sc_failed = False
        for _ in range(15):
            now += rnd.choice([15, 30, 30, 45, 600])
            al = [Arrival(stop_id=k[1], trip_id=k[0], route="r", due_min=(due := rnd.randint(-1, 6)),
                          expected=now + due * 60 + rnd.randint(-5, 5), scheduled=base + 300 + k[2] * 3600,
                          delay=0, realtime=rnd.random() < .8, vehicle_id=None, possibly_passed=False)
                  for k in rnd.sample(keys, rnd.randint(0, len(keys)))]
            before, dbb = snapshot(t), dbdump(d1)
            point = None
            if rnd.random() < 0.4:
                point = inj.point = rnd.choice(POINTS)
                inj.n = rnd.randint(1, 4)
            try:
                await t.ingest("r", al, now)
                failed = False
            except (Boom, asyncio.CancelledError):
                failed = True
            inj.point = None
            if failed:
                assert snapshot(t) == before, point
                if point == "samples.commit":
                    sc_failed = True  # meta commit already happened: documented non-atomic window
                else:
                    assert dbdump(d1) == dbb, point
            else:
                await ref.ingest("r", al, now)
        t.close()
        ref.close()
        if not sc_failed:
            assert dbdump(d1)[:2] == dbdump(d2)[:2]
    finally:
        trk.open_samples = real_open


async def countdown(t, start, exp_at=lambda due: T0 + 1800, rt=lambda due: True, trip="t1"):
    n = 0
    for i, due in enumerate(range(start, -1, -1)):
        r = await t.ingest("15", [arr(due, expected=exp_at(due), realtime=rt(due), trip=trip,
                                      scheduled=T0 + 1800)], T0 + 60 * i)
        n += r.samples_written
    return n


async def test_countdown_unchanged_expected_writes_two_rows(tr, tmp_path):
    assert await countdown(tr, 30) == 2
    assert [s[4] for s in samples(tmp_path)] == [30, 0]
    (row,) = rows(tmp_path)
    assert (row[8], row[6]) == (1, T0 + 1800)


async def test_expected_change_writes_row_each_change(tr):
    assert await countdown(tr, 30, exp_at=lambda due: T0 + 1800 + (due // 10) * 5) == 1 + 3 + 1  # 30->20->10->0 changes + zero row


async def test_realtime_flip_writes_row(tr):
    assert await countdown(tr, 10, rt=lambda due: due > 5) == 1 + 1 + 1  # first, flip, zero row


async def test_scheduled_only_countdown_two_rows(tr, tmp_path):
    assert await countdown(tr, 30, rt=lambda due: False) == 2
    assert {s[6] for s in samples(tmp_path)} == {0}
    assert rows(tmp_path)[0][9] == 0


async def test_bus_60_to_0_prediction_change_every_5_min(tr):
    n = await countdown(tr, 60, exp_at=lambda due: T0 + 3600 + (due // 5) * 7)
    # before the storage rule: one row per minute (61); after: first + 12 prediction changes + zero row
    assert n == 14


async def test_pure_phase_failure_shields_same_trip_stop_live_keys(tmp_path, monkeypatch):
    orig = Tracker._row

    def row(key, st):
        if key[2] == 1:
            raise RuntimeError("unexpected")
        return orig(key, st)
    monkeypatch.setattr(Tracker, "_row", staticmethod(row))
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(1, trip="x", scheduled=T0 + 300)], T0)
    r = await t.ingest("15", [arr(0, trip="x", scheduled=T0 + 300 + 3600)], T0 + 30)  # seq 1 raises
    assert (r.skipped, r.finalised, r.dropped) == (1, 0, 0)  # seq 0 not falsely vanished
    t.close()


async def feed(t, dues, exp, rt, start=T0, sched=T0 + 600):
    for i, due in enumerate(dues):
        await t.ingest("15", [arr(due, expected=exp, realtime=rt, scheduled=sched)], start + 60 * i)
    return start + 60 * len(dues)


async def test_state_updates_every_tick_nonrealtime_vanish_finalises(tr, tmp_path):
    now = await feed(tr, [5, 4, 3, 2, 1], T0 + 700, False)
    res = await tr.ingest("15", [], now)
    assert (res.finalised, res.dropped) == (1, 0)
    (r,) = rows(tmp_path)
    assert (r[5], r[6], r[7], r[8], r[9]) == (T0 + 600, T0 + 700, 100, 0, 0)
    assert len(samples(tmp_path)) == 1


async def test_state_updates_even_when_first_due_le_2_writes_no_row(tr, tmp_path):
    now = await feed(tr, [4, 3, 2], T0 + 700, True)  # due<=2 first seen at the 3rd tick: no row
    assert len(samples(tmp_path)) == 1
    await tr.ingest("15", [], now)
    assert rows(tmp_path)[0][6] == T0 + 700


async def test_realtime_unchanged_expected_countdown_confidence_1_two_rows(tr, tmp_path):
    await feed(tr, [6, 5, 4, 3, 2, 1, 0], T0 + 700, True)
    (r,) = rows(tmp_path)
    assert (r[6], r[8]) == (T0 + 700, 1)
    assert len(samples(tmp_path)) == 2


# ---- phase 5b: forecast rows ---------------------------------------------------------------
import logging  # noqa: E402

M = int(datetime(2025, 11, 1, tzinfo=ZoneInfo("UTC")).timestamp())


async def zero_lookup(a):
    return 0


def fc(tmp_path, sql="select a.trip_id,f.horizon_min,f.predicted,f.error_s from forecast f "
                     "join arrivals a on a.id=f.arrival_id order by 1,2 desc"):
    return rows(tmp_path, sql)


def no_orphans(tmp_path):
    assert rows(tmp_path, "select count(*) from forecast where arrival_id not in (select id from arrivals)") == [(0,)]


async def run(t, steps, trip="t1", base=T0, **kw):
    """steps: (ts offset, expected offset, due, realtime) fed in order for one bus."""
    for off, exp, due, rt in steps:
        await t.ingest("15", [arr(due, expected=base + exp, scheduled=base + 300, trip=trip, realtime=rt, **kw)], base + off)


async def test_forecast_rows_hand_computed(tr, tmp_path):
    await run(tr, [(0, 1300, 21, True), (700, 1250, 9, True), (1000, 1210, 3, True), (1190, 1200, 0, True)])
    # actual = T0+1200; h30 has no sample; h20/h10 -> sample@T0; h5 -> @T0+700; h2 -> @T0+1000
    assert fc(tmp_path) == [("t1", 20, T0 + 1300, -100), ("t1", 10, T0 + 1300, -100),
                            ("t1", 5, T0 + 1250, -50), ("t1", 2, T0 + 1210, -10)]


async def test_forecast_late_bus_positive_error(tr, tmp_path):
    await run(tr, [(0, 400, 7, True), (400, 460, 0, True)])  # predicted 400, actual 460
    assert [(h, p, e) for _, h, p, e in fc(tmp_path)] == [(5, T0 + 400, 60), (2, T0 + 400, 60)]
    no_orphans(tmp_path)


async def test_forecast_first_seen_due_7_only_h5_h2(tr, tmp_path):
    await run(tr, [(0, 420, 7, True), (400, 400, 0, True)])
    assert [(h, p, e) for _, h, p, e in fc(tmp_path)] == [(5, T0 + 420, -20), (2, T0 + 420, -20)]


async def test_scheduled_only_gets_arrival_but_no_forecast(tr, tmp_path):
    await run(tr, [(0, 1300, 21, False), (1190, 1200, 0, False)])
    assert len(rows(tmp_path)) == 1 and fc(tmp_path) == []


async def test_vanish_finalised_gets_forecast(tr, tmp_path):
    await run(tr, [(0, 540, 9, True), (400, 520, 2, True)])
    res = await tr.ingest("15", [], T0 + 430)
    assert res.finalised == 1 and rows(tmp_path, "select confidence from arrivals") == [(0,)]
    assert [(h, p, e) for _, h, p, e in fc(tmp_path)] == [(5, T0 + 540, -20), (2, T0 + 520, 0)]


def prev_month_file(tmp_path, samples):
    c = open_samples(tmp_path, M - 3600)
    c.executemany("insert into sample values(?,?,?,?,?,?,?)", samples)
    c.commit()
    c.close()


async def test_month_boundary_uses_previous_month(tmp_path):
    prev_month_file(tmp_path, [(M - 1200, "t1", "s1", 0, 30, M + 700, 1), (M - 100, "t1", "s1", 0, 12, M + 650, 1)])
    t = Tracker(tmp_path, zero_lookup)
    await run(t, [(60, 600, 9, True), (590, 600, 0, True)], base=M)
    t.close()
    assert [(h, p, e) for _, h, p, e in fc(tmp_path)] == [
        (30, M + 700, -100), (20, M + 700, -100), (10, M + 650, -50), (5, M + 600, 0), (2, M + 600, 0)]


async def test_previous_month_missing_is_fine_and_creates_nothing(tmp_path):
    t = Tracker(tmp_path, zero_lookup)
    await run(t, [(60, 600, 9, True), (590, 600, 0, True)], base=M)
    t.close()
    assert [h for _, h, _, _ in fc(tmp_path)] == [5, 2]
    assert not (tmp_path / "samples-2025-10.db").exists()


async def test_previous_month_not_needed_when_actual_exactly_30min_after_start(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(trk, "open_previous_month", lambda *a: calls.append(a))
    t = Tracker(tmp_path, zero_lookup)
    await run(t, [(0, 1800, 30, True), (1790, 1800, 0, True)], base=M)  # actual - 1800 == M
    t.close()
    assert calls == []


async def test_previous_month_needed_at_exact_edge(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(trk, "open_previous_month", lambda *a: calls.append(a))
    t = Tracker(tmp_path, zero_lookup)
    await run(t, [(0, 1799, 30, True), (1790, 1799, 0, True)], base=M)  # actual - 1800 = M-1 < M
    t.close()
    assert len(calls) == 1


async def test_corrupt_previous_month_keeps_arrivals_and_other_forecasts(tmp_path, caplog):
    (tmp_path / "samples-2025-10.db").write_bytes(b"garbage" * 500)
    t = Tracker(tmp_path, zero_lookup)
    steps = [(60, 600, 9, True), (590, 600, 0, True)]
    await run(t, steps[:1], base=M)
    await t.ingest("15", [arr(0, expected=M + 600, scheduled=M + 300, trip="t1"),
                          arr(9, expected=M + 4000, scheduled=M + 300, trip="far")], M + 590)
    await t.ingest("15", [arr(0, expected=M + 4000, scheduled=M + 300, trip="far")], M + 3990)
    t.close()
    assert len(rows(tmp_path)) == 2
    assert {r[0] for r in fc(tmp_path)} == {"far"}  # t1 skipped (corrupt prev month), far unaffected
    assert any("forecast extraction failed" in r.message and "t1" in r.message for r in caplog.records
               if r.levelno == logging.WARNING)


async def test_extraction_error_for_one_arrival_only(tmp_path, monkeypatch, caplog):
    orig = trk.extract_forecasts

    def flaky(conns, trip, *a):
        if trip == "bad":
            raise ValueError("x")
        return orig(conns, trip, *a)

    monkeypatch.setattr(trk, "extract_forecasts", flaky)
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(9, expected=T0 + 540, trip=x) for x in ("bad", "good")], T0)
    res = await t.ingest("15", [arr(0, expected=T0 + 540, trip=x) for x in ("bad", "good")], T0 + 540)
    t.close()
    assert res.finalised == 2 and len(rows(tmp_path)) == 2
    assert {r[0] for r in fc(tmp_path)} == {"good"}
    assert "bad" in caplog.text


async def test_duplicate_arrival_ignored_no_forecast_duplicates(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await run(t, [(0, 540, 9, True), (540, 540, 0, True)])
    t.close()
    before = fc(tmp_path)
    assert before
    t = Tracker(tmp_path, make_lookup())  # restart: same arrival again, ignored by UNIQUE
    res = await t.ingest("15", [arr(0, expected=T0 + 540)], T0 + 600)
    t.close()
    assert res.finalised == 0 and fc(tmp_path) == before and len(rows(tmp_path)) == 1


async def test_forecast_rows_roll_back_with_tick(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(9, expected=T0 + 540, trip=x) for x in ("x", "y")], T0)
    real, n = t._on_finalised, [0]

    def hook(i, r):
        real(i, r)
        n[0] += 1
        if n[0] == 2:
            raise RuntimeError("boom")

    t._on_finalised = hook
    with pytest.raises(RuntimeError):
        await t.ingest("15", [arr(0, expected=T0 + 540, trip=x) for x in ("x", "y")], T0 + 540)
    assert n[0] == 2 and rows(tmp_path) == [] and fc(tmp_path, "select * from forecast") == []
    t._on_finalised = real
    await t.ingest("15", [arr(0, expected=T0 + 540, trip=x) for x in ("x", "y")], T0 + 540)
    t.close()
    assert len(fc(tmp_path, "select * from forecast")) == 4  # 2 horizons x 2 arrivals, no leftovers


@pytest.mark.parametrize("fail", [False, True])
async def test_previous_month_connection_closed_after_tick(tmp_path, monkeypatch, fail):
    prev_month_file(tmp_path, [(M - 1200, "t1", "s1", 0, 30, M + 700, 1)])
    opened = []
    real = trk.open_previous_month
    monkeypatch.setattr(trk, "open_previous_month", lambda *a: opened.append(real(*a)) or opened[-1])
    t = Tracker(tmp_path, zero_lookup)
    await run(t, [(60, 600, 9, True)], base=M)
    if fail:
        real_hook = t._on_finalised

        def hook(i, r):
            real_hook(i, r)
            raise RuntimeError("boom")

        t._on_finalised = hook
        with pytest.raises(RuntimeError):
            await run(t, [(590, 600, 0, True)], base=M)
    else:
        await run(t, [(590, 600, 0, True)], base=M)
    t.close()
    assert len(opened) == 1 and t._prev is None
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("select 1")


async def test_previous_month_opened_once_per_tick(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(trk, "open_previous_month", lambda *a: opened.append(a))
    t = Tracker(tmp_path, zero_lookup)
    await t.ingest("15", [arr(9, expected=M + 600, scheduled=M + 300, trip=x) for x in ("a", "b")], M + 60)
    await t.ingest("15", [arr(0, expected=M + 600, scheduled=M + 300, trip=x) for x in ("a", "b")], M + 590)
    t.close()
    assert len(opened) == 1


async def test_forecast_follows_latest_sample_realtime_flag(tr, tmp_path):
    # realtime at T, schedule fallback at T+600, actual T+1950: only h30 has a realtime latest sample
    await run(tr, [(0, 2000, 33, True), (600, 1900, 22, False), (1940, 1950, 0, True)])
    assert [(h, p, e) for _, h, p, e in fc(tmp_path)] == [(30, T0 + 2000, -50)]


async def test_same_tick_uncommitted_sample_visible(tr, tmp_path):
    await tr.ingest("15", [arr(0, expected=T0 + 150)], T0)  # first seen at due 0; sample not yet committed
    assert [(h, p, e) for _, h, p, e in fc(tmp_path)] == [(2, T0 + 150, 0)]


async def test_hook_called_only_for_inserted_arrivals(tmp_path):
    t = Tracker(tmp_path, make_lookup())
    await run(t, [(0, 540, 9, True), (540, 540, 0, True)])
    t.close()
    before = fc(tmp_path)
    t = Tracker(tmp_path, make_lookup())
    calls = []
    real = t._on_finalised
    t._on_finalised = lambda i, r: (calls.append(i), real(i, r))
    await t.ingest("15", [arr(9, expected=T0 + 1), arr(1, expected=T0 + 1)], T0 + 600)  # new key
    res = await t.ingest("15", [arr(0, expected=T0 + 540)], T0 + 660)  # UNIQUE-ignored duplicate
    t.close()
    assert res.finalised == 0 and calls == [] and fc(tmp_path) == before
    no_orphans(tmp_path)


async def test_two_consecutive_ticks_both_need_previous_month(tmp_path, monkeypatch):
    prev_month_file(tmp_path, [(M - 1200, x, "s1", 0, 30, M + 700, 1) for x in ("a", "b")])
    opened = []
    real = trk.open_previous_month
    monkeypatch.setattr(trk, "open_previous_month", lambda *a: opened.append(1) or real(*a))
    t = Tracker(tmp_path, zero_lookup)
    await t.ingest("15", [arr(9, expected=M + 600, scheduled=M + 300, trip=x) for x in ("a", "b")], M + 60)
    await t.ingest("15", [arr(0, expected=M + 600, scheduled=M + 300, trip="a")], M + 590)
    await t.ingest("15", [arr(0, expected=M + 600, scheduled=M + 300, trip="b")], M + 600)
    t.close()
    assert len(opened) == 2
    assert [(r[0], r[1]) for r in fc(tmp_path)][:2] == [("a", 30), ("a", 20)]
    assert sorted({r[0] for r in fc(tmp_path) if r[1] == 30}) == ["a", "b"]


async def test_open_previous_month_failure_logged_for_every_arrival(tmp_path, monkeypatch, caplog):
    def boom(*a):
        raise sqlite3.DatabaseError("nope")
    monkeypatch.setattr(trk, "open_previous_month", boom)
    t = Tracker(tmp_path, zero_lookup)
    await t.ingest("15", [arr(9, expected=M + 600, scheduled=M + 300, trip=x) for x in ("a", "b")], M + 60)
    await t.ingest("15", [arr(0, expected=M + 600, scheduled=M + 300, trip=x) for x in ("a", "b")], M + 590)
    t.close()
    msgs = [r.getMessage() for r in caplog.records if "forecast extraction failed" in r.getMessage()]
    assert len(msgs) == 2 and "'a'" in msgs[0] and "'b'" in msgs[1]
    assert len(rows(tmp_path)) == 2


async def test_lost_samples_transaction_rolls_tick_back(tmp_path, monkeypatch):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(9, expected=T0 + 540)], T0)

    def lose(*a):
        t._samples.rollback()
        raise OSError("disk")
    monkeypatch.setattr(trk, "extract_forecasts", lose)
    with pytest.raises(OSError):
        await t.ingest("15", [arr(0, expected=T0 + 540)], T0 + 540)
    assert rows(tmp_path) == [] and len(samples(tmp_path)) == 1
    assert ("t1", "s1", 0) in t._live  # in-memory state restored


async def test_extraction_error_without_samples_written_is_swallowed(tmp_path, monkeypatch, caplog):
    t = Tracker(tmp_path, make_lookup())
    await t.ingest("15", [arr(2, expected=T0 + 540)], T0)
    monkeypatch.setattr(trk, "extract_forecasts", lambda *a: 1 / 0)
    res = await t.ingest("15", [], T0 + 60)  # vanish finalise; nothing written this tick
    t.close()
    assert res.finalised == 1 and "forecast extraction failed" in caplog.text


# ---- WAL / month rollover --------------------------------------------------------------
OCT31 = int(datetime(2025, 10, 31, tzinfo=ZoneInfo("UTC")).timestamp())


def hm(h, m):
    return OCT31 + h * 3600 + m * 60


async def reviewer_scenario(tmp_path, checkpoint=True):
    t = Tracker(tmp_path, zero_lookup)
    a = lambda due, exp, ts: t.ingest("15", [arr(due, expected=exp, scheduled=hm(23, 40), trip="t1")], ts)
    await a(38, hm(23, 58), hm(23, 20))
    if checkpoint:
        t._samples.execute("pragma wal_checkpoint(full)").fetchall()
    held = sqlite3.connect(f"{(tmp_path / 'samples-2025-10.db').as_uri()}?mode=ro", uri=True)
    held.execute("select count(*) from sample").fetchall()  # plain reader held open across rollover
    await a(10, hm(23, 57), hm(23, 47))
    await a(2, hm(23, 58), hm(23, 56))
    res = await t.ingest("15", [], OCT31 + 86400 + 600)  # Nov 1 00:10: vanish-finalise, rollover
    assert res.finalised == 1
    old_wal = tmp_path / "samples-2025-10.db-wal"
    t.close()
    held.close()
    return old_wal


async def test_rollover_with_held_reader_and_mid_month_checkpoint(tmp_path):
    old_wal = await reviewer_scenario(tmp_path)
    assert [(h, e) for _, h, _, e in fc(tmp_path)] == [(30, 0), (20, 0), (10, 60), (5, 60), (2, 0)]
    assert not old_wal.exists() or old_wal.stat().st_size == 0  # layer (a): checkpointed at rollover


async def test_rollover_without_any_checkpoint(tmp_path):
    await reviewer_scenario(tmp_path, checkpoint=False)
    assert [(h, e) for _, h, _, e in fc(tmp_path)] == [(30, 0), (20, 0), (10, 60), (5, 60), (2, 0)]


async def test_crashed_previous_month_with_big_wal(tmp_path):
    import subprocess
    import sys
    code = ("import os,sys\nfrom app.core.db import open_samples\n"
            "c=open_samples(sys.argv[1],int(sys.argv[2]))\nc.execute('pragma wal_autocheckpoint=0')\n"
            "c.executemany('insert into sample values(?,?,?,?,?,?,?)',[(%d,'t1','s1',0,5,%d,1),(%d,'t1','s1',0,5,%d,1)])\n"
            "c.commit()\nos._exit(0)" % (M - 1200, M + 700, M - 100, M + 650))
    subprocess.run([sys.executable, "-c", code, str(tmp_path), str(M - 3600)], check=True,
                   cwd=str(__import__("pathlib").Path(__file__).parent.parent), timeout=60)
    assert (tmp_path / "samples-2025-10.db-wal").stat().st_size > 0
    t = Tracker(tmp_path, zero_lookup)
    await run(t, [(60, 600, 9, True), (590, 600, 0, True)], base=M)
    t.close()
    assert [(h, p) for _, h, p, _ in fc(tmp_path)] == [(30, M + 700), (20, M + 700), (10, M + 650), (5, M + 600), (2, M + 600)]


class _CkptProxy:
    def __init__(self, c, mode):
        self._c, self._mode = c, mode

    def execute(self, sql, *a):
        if sql.startswith("pragma wal_checkpoint"):
            if self._mode == "raise":
                raise sqlite3.OperationalError("locked")
            return self._c.execute("select 1,0,0")  # busy flag set
        return self._c.execute(sql, *a)

    def __getattr__(self, k):
        return getattr(self._c, k)


@pytest.mark.parametrize("mode", ["busy", "raise"])
async def test_busy_checkpoint_at_rollover_does_not_abort_tick(tmp_path, caplog, mode):
    t = Tracker(tmp_path, zero_lookup)
    await t.ingest("15", [arr(9, expected=OCT31 + 100, scheduled=OCT31 + 50)], OCT31 + 1)
    t._samples = _CkptProxy(t._samples, mode)
    res = await t.ingest("15", [arr(9, expected=OCT31 + 86400 + 5, scheduled=OCT31 + 50, trip="n")], OCT31 + 86400 + 1)
    t.close()
    assert res.samples_written == 1 and "checkpoint" in caplog.text
