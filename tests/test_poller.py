import asyncio
import logging
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.config import Settings
from app.db import open_meta
from app.models import Arrival
from app.poller import LazyTracker, Poller, seq_lookup
from app.serverclient import ServerClient
from app.tracker import IngestResult, Tracker
from tests.fake_server import FakeServer

T0 = 1_760_000_000


def ja(trip="t1", stop="s1", due=5, expected=None, sched=T0 + 300, route="15", rt=True):
    return {"tripId": trip, "routeShortName": route, "dueMinutes": due,
            "expectedEpochSeconds": T0 + due * 60 if expected is None else expected,
            "scheduledEpochSeconds": sched, "delaySeconds": 0, "isRealtime": rt}


class Env:
    def __init__(self, tmp_path, **kw):
        self.fs = FakeServer()
        self.cfg = Settings("http://fake", tmp_path, 30)
        self.client = ServerClient("http://fake", self.fs.client())
        self.tracker = Tracker(tmp_path, seq_lookup)
        self.poller = Poller(self.cfg, self.client, self.tracker, **kw)
        self.tmp = tmp_path

    def rows(self, q):
        c = open_meta(self.tmp)
        try:
            return c.execute(q).fetchall()
        finally:
            c.close()

    def arrivals(self):
        return self.rows("select route,trip_id,stop_id,seq,scheduled,actual,delay,confidence,realtime "
                         "from arrivals order by seq")

    def gaps(self):
        return self.rows("select start,end from gaps order by rowid")


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.tracker.close()


def one_route(env, route="15", due=5, trip="t1"):
    env.fs.set_arrivals(route, {"s1": [ja(trip=trip, due=due, route=route)]})
    env.poller.track(route)


# ---- brief tests ----
async def test_tick_polls_only_tracked_routes(env):
    env.fs.set_arrivals("15", {})
    env.fs.set_arrivals("46A", {})
    env.poller.track("15")
    await env.poller.tick(T0)
    assert env.fs.requests == ["/v1/routes/15/arrivals"]


async def test_unavailable_records_gap_and_no_arrivals(env):
    one_route(env)
    env.fs.set_down(True)
    await env.poller.tick(T0)
    assert env.gaps() == [(T0, T0)]
    assert env.arrivals() == []


async def test_gap_closed_on_recovery(env):
    one_route(env)
    env.fs.set_down(True)
    await env.poller.tick(T0)
    await env.poller.tick(T0 + 30)
    assert env.gaps() == [(T0, T0 + 30)]
    env.fs.set_down(False)
    await env.poller.tick(T0 + 60)
    assert env.gaps() == [(T0, T0 + 60)]
    env.fs.set_down(True)
    await env.poller.tick(T0 + 90)  # a later outage is a new row
    assert env.gaps() == [(T0, T0 + 60), (T0 + 90, T0 + 90)]


async def test_untrack_stops_polling(env):
    one_route(env)
    env.poller.untrack("15")
    await env.poller.tick(T0)
    assert env.fs.requests == []


def test_tracked_survives_restart(env, tmp_path):
    env.poller.track("15")
    p2 = Poller(env.cfg, env.client, env.tracker)
    assert p2.tracked() == ["15"]


# ---- gaps and failure paths ----
async def test_outage_one_gap_spanning_and_nothing_fabricated(env, monkeypatch):
    one_route(env)
    ingested = []
    real = env.tracker.ingest

    async def spy(route, arrivals, now):
        ingested.append((route, list(arrivals)))
        return await real(route, arrivals, now)
    monkeypatch.setattr(env.tracker, "ingest", spy)
    await env.poller.tick(T0)
    env.fs.set_down(True)
    for k in range(1, 6):
        await env.poller.tick(T0 + 30 * k)
    env.fs.set_down(False)
    await env.poller.tick(T0 + 180)
    assert env.gaps() == [(T0 + 30, T0 + 180)]
    assert len(ingested) == 2 and all(a for _, a in ingested)  # never ingest([]) on outage
    assert env.arrivals() == []


async def test_route_not_found_keeps_tracked_no_gap(env, caplog):
    env.poller.track("ghost")  # FakeServer 404s unknown routes
    with caplog.at_level(logging.WARNING):
        await env.poller.tick(T0)
    assert env.poller.tracked() == ["ghost"] and env.gaps() == []
    assert "not found" in caplog.text


async def test_route_not_found_does_not_close_open_gap_or_open_one(env):
    one_route(env)
    env.poller.track("ghost")
    env.fs.set_down(True)
    await env.poller.tick(T0)
    env.fs.set_down(False)
    await env.poller.tick(T0 + 30)  # 15 ok, ghost 404 -> still closes (real success seen)
    assert env.gaps() == [(T0, T0 + 30)]


async def test_only_not_found_does_not_close_gap(env):
    env.poller.track("ghost")
    env.poller._gap_open = None
    await env.poller.tick(T0)
    assert env.gaps() == []


async def test_mixed_tick_keeps_gap_open_and_extends(env):
    one_route(env)
    env.fs.set_arrivals("bad", {"s": [{"nonsense": 1}]})  # unexpected shape -> ServerUnavailable
    env.poller.track("bad")
    await env.poller.tick(T0)
    await env.poller.tick(T0 + 30)
    assert env.gaps() == [(T0, T0 + 30)]


async def test_exception_in_one_route_does_not_block_others(env, monkeypatch, caplog):
    one_route(env, "15")
    one_route(env, "46A", trip="t2")
    real = env.tracker.ingest

    async def boom(route, arrivals, now):
        if route == "15":
            raise RuntimeError("kaput")
        return await real(route, arrivals, now)
    monkeypatch.setattr(env.tracker, "ingest", boom)
    with caplog.at_level(logging.ERROR):
        await env.poller.tick(T0)
    assert "ingest failed for route 15" in caplog.text and "RuntimeError" in caplog.text
    assert env.rows("select count(*) from routes_tracked") == [(2,)]
    assert env.gaps() == []


async def test_fetch_exception_logged_others_continue(env, monkeypatch, caplog):
    one_route(env, "15")
    one_route(env, "46A", trip="t2")
    real = env.client.route_arrivals

    async def fetch(route):
        if route == "15":
            raise RuntimeError("bug")
        return await real(route)
    monkeypatch.setattr(env.client, "route_arrivals", fetch)
    seen = []
    t_ingest = env.tracker.ingest

    async def spy(route, a, now):
        seen.append(route)
        return await t_ingest(route, a, now)
    monkeypatch.setattr(env.tracker, "ingest", spy)
    with caplog.at_level(logging.ERROR):
        await env.poller.tick(T0)
    assert seen == ["46A"] and "poll failed for route 15" in caplog.text


async def test_skipped_and_stale_logged_as_warnings(env, monkeypatch, caplog):
    one_route(env)

    async def fake(route, a, now):
        return IngestResult(0, 0, 0, skipped=2, stale=True)
    monkeypatch.setattr(env.tracker, "ingest", fake)
    with caplog.at_level(logging.WARNING):
        await env.poller.tick(T0)
    assert "skipped=2 stale=True" in caplog.text


async def test_tick_with_no_tracked_routes(env):
    await env.poller.tick(T0)
    assert env.fs.requests == [] and env.gaps() == []


async def test_gap_not_resumed_after_restart(env):
    one_route(env)
    env.fs.set_down(True)
    await env.poller.tick(T0)
    p2 = Poller(env.cfg, env.client, env.tracker)  # restart mid-outage
    await p2.tick(T0 + 300)
    env.fs.set_down(False)
    await p2.tick(T0 + 330)
    assert env.gaps() == [(T0, T0), (T0 + 300, T0 + 330)]


# ---- end to end (realistic server: trip_calls is never consulted; seq == scheduled epoch) ----
async def countdown(env, mk, start=T0, step=60, dues=(5, 4, 3, 2, 1, 0)):
    for k, due in enumerate(dues):
        env.fs.set_arrivals("15", {"s1": mk(due, k)})
        await env.poller.tick(start + step * k)


async def test_end_to_end_one_finalised_row(env):
    env.poller.track("15")
    await countdown(env, lambda due, k: [ja(due=due, expected=T0 + 300 + 20 + (k > 3) * 10)])
    env.fs.set_arrivals("15", {})
    await env.poller.tick(T0 + 400)  # gone after finalising: no duplicate
    assert env.arrivals() == [("15", "t1", "s1", T0 + 300, T0 + 300, T0 + 330, 30, 1, 1)]
    assert not any("/v1/trips" in r for r in env.fs.requests)


async def test_scheduled_only_bus_stored_realtime_0(env):
    env.poller.track("15")
    await countdown(env, lambda due, k: [ja(due=due, expected=T0 + 300, rt=False)])
    rows = env.arrivals()
    assert len(rows) == 1 and rows[0][4] == T0 + 300 and rows[0][8] == 0


async def test_scheduled_only_then_live_is_one_row(env):
    env.poller.track("15")
    await countdown(env, lambda due, k: [ja(due=due, expected=T0 + 300 + 15, rt=k >= 2)])
    rows = env.arrivals()
    assert len(rows) == 1 and rows[0][8] == 1 and rows[0][4] == T0 + 300


async def test_same_trip_next_service_day_is_second_row(env):
    env.poller.track("15")
    await countdown(env, lambda due, k: [ja(due=due, expected=T0 + 300)])
    day = 86400
    await countdown(env, lambda due, k: [ja(due=due, expected=T0 + day + 300, sched=T0 + day + 300)],
                    start=T0 + day)
    assert [r[4] for r in env.arrivals()] == [T0 + 300, T0 + day + 300]


async def test_loop_trip_same_stop_twice_gives_two_rows(env):
    env.poller.track("15")
    env.fs.set_arrivals("15", {"s1": [ja(due=0, expected=T0 + 300, sched=T0 + 300),
                                      ja(due=0, expected=T0 + 900, sched=T0 + 900)]})
    await env.poller.tick(T0 + 300)
    assert sorted(r[3] for r in env.arrivals()) == [T0 + 300, T0 + 900]
    assert len(env.arrivals()) == 2


async def test_many_stops_one_trip_all_stored(env):
    env.poller.track("15")
    stops = [f"s{i}" for i in range(6)]
    env.fs.set_arrivals("15", {s: [ja(stop=s, due=0, expected=T0 + 300 + 60 * i, sched=T0 + 300 + 60 * i)]
                               for i, s in enumerate(stops)})
    await env.poller.tick(T0 + 300 + 360)
    assert sorted(r[2] for r in env.arrivals()) == stops


async def test_none_scheduled_skipped_counted_rest_stored(env, caplog):
    env.poller.track("15")
    env.fs.set_arrivals("15", {"s1": [ja(due=0, expected=T0 + 300),
                                      ja(trip="t2", due=0, expected=T0 + 300, sched=None)]})
    with caplog.at_level(logging.WARNING):
        await env.poller.tick(T0 + 300)
    assert [(r[1], r[3]) for r in env.arrivals()] == [("t1", T0 + 300)]
    assert "skipped=1" in caplog.text


async def test_seq_lookup_contract():
    assert await seq_lookup(arrival(sched=T0 + 5)) == T0 + 5
    with pytest.raises(ValueError):
        await seq_lookup(arrival(sched=None))


def arrival(trip="t1", stop="s1", sched=T0 + 300):
    return Arrival(stop, trip, "15", 1, T0, sched, 0, True, None, False)


# ---- track validation, threads ----
@pytest.mark.parametrize("bad", ["", "   ", "x" * 21, "a;b", "a'b", "a\nb", "é", "15/../x?", None, 15])
def test_track_rejects_invalid(env, bad):
    with pytest.raises(ValueError):
        env.poller.track(bad)
    assert env.poller.tracked() == []


def test_track_strips_dedups_sorts_and_boundaries(env):
    for r in ["46A", " 15 ", "15", "x" * 20, "A 1.2_3-/"]:
        env.poller.track(r)
    assert env.poller.tracked() == sorted(["15", "46A", "x" * 20, "A 1.2_3-/"])
    env.poller.untrack("nope")  # no-op
    env.poller.untrack("46A")
    env.poller.untrack("46A")
    assert "46A" not in env.poller.tracked()


def test_track_from_other_thread(env):
    errs = []

    def work():
        try:
            for i in range(20):
                env.poller.track(f"r{i}")
                env.poller.tracked()
                env.poller.untrack(f"r{i % 3}")
        except Exception as e:  # pragma: no cover
            errs.append(e)
    th = threading.Thread(target=work)
    th.start()
    th.join(10)
    assert not errs and not isinstance(errs, sqlite3.ProgrammingError)
    assert len(env.poller.tracked()) == 17


# ---- run loop ----
class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def mono(self):
        return self.t

    async def sleep(self, d):
        assert d >= 0
        self.sleeps.append(d)
        self.t += d
        await asyncio.sleep(0)


async def test_run_aligned_no_drift_and_clean_cancel(tmp_path):
    e = Env(tmp_path)
    ck = Clock()
    nows = iter([T0, T0 + 30, T0 + 29, T0 + 60] + [T0 + 90] * 100)  # clock going backwards once
    e.poller = Poller(e.cfg, e.client, e.tracker, now_fn=lambda: next(nows), mono_fn=ck.mono,
                      sleep_fn=ck.sleep)
    seen = []

    async def tick(now):
        seen.append(now)
        ck.t += 4  # work takes 4s -> sleep must be 26
        if len(seen) == 5:
            raise RuntimeError("tick blew up")  # loop must survive
    e.poller.tick = tick
    task = asyncio.create_task(e.poller.run())
    while len(seen) < 7:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert seen[:4] == [T0, T0 + 30, T0 + 30, T0 + 60]  # non-decreasing
    assert all(abs(s - 26) < 1e-9 for s in ck.sleeps)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
    e.tracker.close()


async def test_slow_tick_does_not_overlap_and_no_negative_sleep(tmp_path):
    e = Env(tmp_path)
    ck = Clock()
    active = maxactive = 0
    n = 0

    async def tick(now):
        nonlocal active, maxactive, n
        active += 1
        maxactive = max(maxactive, active)
        await asyncio.sleep(0)
        ck.t += 100  # 100s tick, 30s interval
        n += 1
        active -= 1
    e.poller = Poller(e.cfg, e.client, e.tracker, now_fn=lambda: T0, mono_fn=ck.mono, sleep_fn=ck.sleep)
    e.poller.tick = tick
    task = asyncio.create_task(e.poller.run())
    while n < 3:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert maxactive == 1 and ck.sleeps[:2] == [0, 0]
    e.tracker.close()


async def test_run_real_defaults_tick_once(env):
    one_route(env)
    p = Poller(env.cfg, env.client, env.tracker)  # real time.time / monotonic / sleep
    p.now_fn = lambda: T0
    task = asyncio.create_task(p.run())
    for _ in range(200):
        if env.fs.requests:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert env.fs.requests[0] == "/v1/routes/15/arrivals"


def test_default_now_fn_is_int_epoch(env):
    assert isinstance(env.poller.now_fn(), int) and env.poller.now_fn() > 1_700_000_000


# ---- property ----
@given(st.lists(st.booleans(), max_size=25))
@hsettings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_gap_invariants_random_up_down(tmp_path_factory, seq):
    async def go():
        tmp = tmp_path_factory.mktemp("p")
        e = Env(tmp)
        one_route(e)
        ingest_calls = 0
        real = e.tracker.ingest

        async def spy(route, a, now):
            nonlocal ingest_calls
            ingest_calls += 1
            return await real(route, a, now)
        e.tracker.ingest = spy
        ok = 0
        for k, up in enumerate(seq):
            e.fs.set_down(not up)
            ok += up
            await e.poller.tick(T0 + 30 * k)
        gaps = e.gaps()
        e.tracker.close()
        await e.client.aclose()
        return gaps, ingest_calls, ok
    gaps, ing, ok = asyncio.run(asyncio.wait_for(go(), 10))
    assert ing == ok
    for s, en in gaps:
        assert en >= s
    for (s1, e1), (s2, e2) in zip(gaps, gaps[1:]):
        assert e1 <= s2 and s1 < s2
    # one row per maximal run of failures
    runs = sum(1 for i, up in enumerate(seq) if not up and (i == 0 or seq[i - 1]))
    assert len(gaps) == runs


# ---- wiring ----
def test_import_main_does_no_io(tmp_path, monkeypatch):
    import subprocess
    import sys
    d = tmp_path / "nodata"
    r = subprocess.run([sys.executable, "-c", "import app.main"], env={"DATA_DIR": str(d), "PATH": ""},
                       capture_output=True, text=True, cwd=Path(__file__).parent.parent)
    assert r.returncode == 0, r.stderr
    assert not d.exists()


def test_create_app_state_and_no_background_by_default_for_tests(tmp_path):
    from app.main import create_app
    app = create_app(Settings("http://x", tmp_path / "d"), start_background=False)
    assert isinstance(app.state.poller, Poller) and isinstance(app.state.tracker, LazyTracker)
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"ok": True}
    assert not (tmp_path / "d").exists()  # nothing opened without ingest


def test_lifespan_runs_and_cancels_poller(tmp_path):
    from app.main import create_app
    app = create_app(Settings("http://x", tmp_path, 1))
    started = []

    async def fake_run():
        started.append(1)
        try:
            await asyncio.sleep(3600)
        finally:
            started.append("cancelled")
    app.state.poller.run = fake_run
    with TestClient(app):
        pass
    assert started == [1, "cancelled"]


async def test_lazy_tracker_opens_on_first_ingest_and_closes(tmp_path):
    lt = LazyTracker(tmp_path / "d", seq_lookup)
    assert not (tmp_path / "d").exists()
    lt.close()  # close before use is a no-op
    res = await lt.ingest("15", [], T0)
    assert res.samples_written == 0 and (tmp_path / "d" / "meta.db").exists()
    await lt.ingest("15", [], T0 + 30)  # reuses the same Tracker
    lt.close()
    assert lt._t is None
