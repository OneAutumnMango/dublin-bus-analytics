import asyncio
import copy
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.db import open_meta, open_samples
from app.models import Arrival
from app.tracker import FINALISED_TTL_S, Tracker

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
        return 0 if a.scheduled is None else (a.scheduled - T0 - 300) // 3600
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
    assert [r.samples_written for r in (r1, r2, r3, r4, r5)] == [1, 0, 1, 1, 1]
    assert [s[0] for s in samples(tmp_path)] == [T0, T0 + 60, T0 + 90, T0 + 120]


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
    await tr.ingest("15", [arr(0, scheduled=s1, expected=s1, trip="a"),
                           arr(0, scheduled=s2, expected=s2, trip="b")], s1 - 600)
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
    await tr.ingest("15", [arr(5)], end_oct)
    await tr.ingest("15", [arr(4, expected=T0)], end_oct + 60)
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
    assert (res.skipped, res.finalised, res.dropped) == (0, 0, 0)  # observed with the live key's seq


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
    await asyncio.gather(t.ingest("15", [arr(5)], end_oct),
                         t.ingest("16", [arr(5, route="16", trip="z")], end_oct + 20))
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
            assert all(x != y for x, y in zip(seq_rows, seq_rows[1:]))  # change-only
    finally:
        sc.close()
    assert {(r[0], r[1], r[2]) for r in arrivals} == {k for k, v in model.items() if v["done"]}
    for trip, stop, seq, actual, delay, sched, conf in arrivals:
        mk = model[(trip, stop, seq)]
        assert delay == actual - sched
        assert actual == mk["actual"]  # last expected seen with due <= 2
        assert conf == int(mk["zero"])


# ---- fix round 2 -----------------------------------------------------------------------
import app.tracker as trk  # noqa: E402


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
    await t.ingest("15", [arr(5, trip="x")], E)
    await t.ingest("16", [arr(5, trip="y", route="16")], E)
    before = (copy.deepcopy((t._live, t._finalised, t._seq, t._last_now)))
    fail["on"] = True
    with pytest.raises(OSError):
        await t.ingest("15", [arr(4, trip="x", expected=T0)], E + 30)
    assert before == copy.deepcopy((t._live, t._finalised, t._seq, t._last_now))
    fail["on"] = False
    res = await t.ingest("15", [arr(4, trip="x", expected=T0)], E + 30)  # same now: not stale
    assert res.stale is False and res.samples_written == 1
    assert (await t.ingest("16", [arr(5, trip="y", route="16")], E + 40)).stale is False
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


async def test_persistent_lookup_failure_for_live_bus_keeps_old_seq(tmp_path):
    fail = {"on": False}

    async def lookup(a):
        if fail["on"]:
            raise RuntimeError
        return 0
    t = Tracker(tmp_path, lookup)
    await t.ingest("15", [arr(2, expected=T0 + 400, scheduled=T0 + 300)], T0)
    fail["on"] = True
    r1 = await t.ingest("15", [arr(1, expected=T0 + 410, scheduled=T0 + 310)], T0 + 70)
    r2 = await t.ingest("15", [arr(0, expected=T0 + 420, scheduled=T0 + 320)], T0 + 140)
    assert (r1.skipped, r2.skipped, r2.finalised) == (0, 0, 1)
    (r,) = rows(tmp_path)
    assert (r[6], r[8]) == (T0 + 420, 1)
    t.close()


async def test_persistent_failure_then_vanish_uses_latest_expected(tmp_path):
    fail = {"on": False}

    async def lookup(a):
        if fail["on"]:
            raise RuntimeError
        return 0
    t = Tracker(tmp_path, lookup)
    await t.ingest("15", [arr(2, expected=T0 + 400, scheduled=T0 + 300)], T0)
    fail["on"] = True
    await t.ingest("15", [arr(1, expected=T0 + 410, scheduled=T0 + 310)], T0 + 70)
    await t.ingest("15", [], T0 + 140)
    assert rows(tmp_path)[0][6] == T0 + 410
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
