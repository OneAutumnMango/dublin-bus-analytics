import sqlite3
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


# ---- property test -------------------------------------------------------------------

KEYS = [("t1", "s1", 0), ("t1", "s1", 1), ("t1", "s2", 0), ("t2", "s1", 0)]


@st.composite
def scenarios(draw):
    ticks = []
    dues = {k: draw(st.integers(0, 8)) for k in KEYS}
    for _ in range(draw(st.integers(1, 12))):
        subset = draw(st.lists(st.sampled_from(KEYS), unique=True, max_size=4))
        if draw(st.booleans()):
            subset = draw(st.permutations(subset))
        al = []
        for k in subset:
            dues[k] = max(0, dues[k] - draw(st.integers(0, 2)))
            noise = draw(st.integers(-30, 30))
            if draw(st.booleans()):
                al.append((k, dues[k], noise, draw(st.booleans())))
            al.append((k, dues[k], noise, draw(st.booleans())))  # repeats / dup keys
        ticks.append(al)
    return ticks


@given(scenarios())
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_property_invariants(tmp_path_factory, ticks):
    d = tmp_path_factory.mktemp("prop")

    async def lookup(a):
        return int(a.scheduled - T0 - 300) // 3600

    t = Tracker(d, lookup)
    for i, al in enumerate(ticks):
        arrs = [arr(due, expected=T0 + due * 60 + noise, scheduled=T0 + 300 + k[2] * 3600,
                    trip=k[0], stop=k[1], realtime=rt) for k, due, noise, rt in al]
        await t.ingest("15", arrs, T0 + i * 30)
    t.close()
    m = sqlite3.connect(d / "meta.db")
    s = open_samples(d, T0)
    try:
        arrivals = m.execute("select trip_id,stop_id,seq,service_date,scheduled,actual,delay from arrivals").fetchall()
        assert len({a[:4] for a in arrivals}) == len(arrivals)
        for trip, stop, seq, _, sched, actual, delay in arrivals:
            exp = {r[0] for r in s.execute("select expected from sample where trip_id=? and stop_id=? and seq=?", (trip, stop, seq))}
            assert actual in exp
            assert delay == actual - sched
        for key in {(r[0], r[1], r[2]) for r in s.execute("select trip_id,stop_id,seq from sample")}:
            seq_rows = s.execute("select due_min,expected,realtime from sample where trip_id=? and stop_id=? and seq=? order by ts", key).fetchall()
            assert all(a != b for a, b in zip(seq_rows, seq_rows[1:]))
    finally:
        m.close()
        s.close()


async def test_dropped_key_reappearing_keeps_change_only_and_evicts(tr, tmp_path):
    await tr.ingest("15", [arr(5)], T0)
    assert (await tr.ingest("15", [], T0 + 30)).dropped == 1
    res = await tr.ingest("15", [arr(5)], T0 + 60)  # identical to last sample
    assert res.samples_written == 0
    await tr.ingest("15", [], T0 + 90)
    await tr.ingest("15", [], T0 + 90 + FINALISED_TTL_S + 1)
    assert tr._live == {}


async def test_seq_lookup_failure_leaves_state_untouched(tmp_path):
    async def bad(a):
        raise RuntimeError("lookup down")
    t = Tracker(tmp_path, bad)
    with pytest.raises(RuntimeError):
        await t.ingest("15", [arr(0)], T0)
    assert t._last_now == {} and t._live == {}
    t.close()
