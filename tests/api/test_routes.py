import asyncio
import contextlib
import json
import math
import time
import types
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.core.config import Settings
from app.core.db import open_meta
from app.main import create_app
from app.collect.serverclient import ServerClient
from tests.support.fake_server import FakeServer

T0 = 1_760_000_000  # a Tuesday-ish base epoch; tests that need weekdays compute them


class FakePoller:
    def __init__(self):
        self._t = set()

    def track(self, r):
        self._t.add(r)

    def untrack(self, r):
        self._t.discard(r)

    def tracked(self):
        return set(self._t)


class Env:
    def __init__(self, tmp_path, real_poller=False):
        self.s = Settings(server_url="http://fake", data_dir=tmp_path)
        self.fake = FakeServer()
        self.app = create_app(self.s, start_background=False,
                              client=ServerClient("http://fake", self.fake.client()))
        if not real_poller:
            self.app.state.poller = FakePoller()
        self.c = TestClient(self.app)
        self.n = 0

    def add(self, route="15", stop="s1", seq=None, delay=0, scheduled=T0, confidence=1, realtime=1,
            trip=None, date="2026-01-01"):
        """seq defaults to `scheduled`: in real data seq is the per-visit key, not a position."""
        self.n += 1
        seq = scheduled if seq is None else seq
        db = open_meta(self.s.data_dir)
        with db:
            cur = db.execute(
                "insert into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,"
                "confidence,realtime,route) values(?,?,?,?,?,?,?,?,?,?)",
                (trip or f"t{self.n}", stop, seq, date, scheduled, None if delay is None else scheduled + delay,
                 delay, confidence, realtime, route))
        db.close()
        return cur.lastrowid

    def trip(self, trip, calls, route="15", t0=T0, step=60):
        """One row per (trip, stop): calls = [(stop_id, delay)], scheduled = t0 + i*step."""
        for i, (stop, d) in enumerate(calls):
            self.add(route=route, stop=stop, delay=d, scheduled=t0 + i * step, trip=trip)

    def fc(self, arrival_id, horizon, err):
        db = open_meta(self.s.data_dir)
        with db:
            db.execute("insert into forecast values(?,?,?,?)", (arrival_id, horizon, 0, err))
        db.close()

    keep_cache = False

    def get(self, url, **q):
        if not self.keep_cache and hasattr(self.app.state, "stops_cache"):
            del self.app.state.stops_cache  # server-state changes between requests must be visible
        return self.c.get(url, params=q)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def finite(o):
    json.dumps(o, allow_nan=False)


def seed5(env, **kw):
    for d in (0, 60, 120, 180, 240):
        env.add(delay=d, **kw)


# ---------- tracking ----------

def test_track_untrack_roundtrip(env):
    assert env.get("/api/routes/tracked").json() == {"routes": []}
    assert env.c.put("/api/routes/tracked/15").json() == {"routes": ["15"]}
    assert env.c.put("/api/routes/tracked/N4").json() == {"routes": ["15", "N4"]}
    assert env.c.put("/api/routes/tracked/15").json() == {"routes": ["15", "N4"]}  # idempotent
    assert env.app.state.poller.tracked() == {"15", "N4"}
    assert env.get("/api/routes/tracked").json() == {"routes": ["15", "N4"]}
    assert env.c.delete("/api/routes/tracked/15").json() == {"routes": ["N4"]}
    assert env.c.delete("/api/routes/tracked/15").json() == {"routes": ["N4"]}  # absent: ok


@pytest.mark.parametrize("bad", ["%20", "a" * 21, "a" * 25, "a;b", "a'b", "a%00b", "<b>", "caf%C3%A9",
                                 "%D9%A3", "a%0Ab"])
def test_track_invalid_route_422_real_poller(tmp_path, bad):
    env = Env(tmp_path, real_poller=True)
    assert env.c.put(f"/api/routes/tracked/{bad}").status_code == 422
    assert env.c.delete(f"/api/routes/tracked/{bad}").status_code == 422
    assert env.get("/api/routes/tracked").json() == {"routes": []}


def test_real_poller_strip_roundtrip(tmp_path):
    env = Env(tmp_path, real_poller=True)
    assert env.c.put("/api/routes/tracked/16%20").json() == {"routes": ["16"]}
    assert env.c.put("/api/routes/tracked/%2016").json() == {"routes": ["16"]}
    assert env.c.delete("/api/routes/tracked/16%20").json() == {"routes": []}
    assert env.app.state.poller.tracked() == []


def test_real_poller_valid_special_names(tmp_path):
    env = Env(tmp_path, real_poller=True)
    for r in ("N4", "15/A", "15 A", "X_1.2-3", "a" * 20):
        assert env.c.put("/api/routes/tracked/" + quote(r, safe="")).status_code == 200, r
    assert env.get("/api/routes/tracked").json()["routes"] == sorted(
        ["N4", "15/A", "15 A", "X_1.2-3", "a" * 20])


def test_poller_value_error_maps_to_422(env):
    class Raising(FakePoller):
        def track(self, r):
            raise ValueError("nope")

    env.app.state.poller = Raising()
    r = env.c.put("/api/routes/tracked/15")
    assert r.status_code == 422 and r.json()["detail"] == "nope"


def test_track_special_route_names(env):
    assert env.c.put("/api/routes/tracked/" + quote("15 A", safe="")).json() == {"routes": ["15 A"]}


def test_slash_route_encoded(env):
    r = env.c.put("/api/routes/tracked/" + quote("15/A", safe=""))
    assert r.status_code == 200 and r.json() == {"routes": ["15/A"]}
    env.add(route="15/A", delay=60)
    r = env.get("/api/routes/" + quote("15/A", safe="") + "/summary")
    assert r.status_code == 200 and r.json()["route"] == "15/A" and r.json()["n"] == 1


# ---------- proxies ----------

def test_search_routes_proxy(env):
    env.fake.set_routes([{"shortName": "15"}, {"shortName": "150"}, {"shortName": "N4"}])
    assert [x["shortName"] for x in env.get("/api/routes", q="15").json()] == ["15", "150"]
    assert len(env.get("/api/routes").json()) == 3
    assert env.get("/api/routes", q="zzz").json() == []
    assert env.get("/api/routes", q="x" * 65).status_code == 422


def test_route_stops_proxy_and_errors(env):
    env.fake.set_route_stops("15", {"0": [{"id": "s1"}]})
    assert env.get("/api/routes/15/stops").json() == {"route": "15", "directions": {"0": [{"id": "s1"}]}}
    assert env.get("/api/routes/nope/stops").status_code == 404
    env.fake.set_status(503)
    assert env.get("/api/routes/15/stops").status_code == 502
    assert env.get("/api/routes").status_code == 502
    env.fake.set_status(None)
    env.fake.set_down(True)
    r = env.get("/api/routes/15/stops")
    assert r.status_code == 502 and "unavailable" in r.json()["detail"]


# ---------- summary ----------

def test_route_summary_per_stop_in_route_order(env):
    # reviewer's repro: server order zz, mm, aa (neither alphabetical nor seq-sorted); seq == scheduled
    env.fake.set_route_stops("15", {"out": [{"id": "zz"}, {"id": "mm"}, {"id": "aa"}]})
    for i, t in enumerate(("t1", "t2", "t3")):
        env.trip(t, [("zz", 0), ("mm", 60), ("aa", 180)], t0=T0 + i * 3600)
    env.add(route="other", stop="qq", delay=5)
    j = env.get("/api/routes/15/summary").json()
    assert j["order_source"] == "server"
    assert [s["stop_id"] for s in j["stops"]] == ["zz", "mm", "aa"]
    assert [s["position"] for s in j["stops"]] == [0, 1, 2]
    assert [s["mean"] for s in j["stops"]] == [0, 60, 180]
    assert [s["n"] for s in j["stops"]] == [3, 3, 3] and j["n"] == 9 and j["route"] == "15"
    assert "seq" not in j["stops"][0]


def test_summary_unlisted_stops_last_by_stop_id(env):
    env.fake.set_route_stops("15", {"out": [{"id": "b"}, {"id": "a"}]})
    env.trip("t1", [("z", 1), ("a", 1), ("y", 1), ("b", 1)])
    s = env.get("/api/routes/15/summary").json()["stops"]
    assert [(x["stop_id"], x["position"]) for x in s] == [("b", 0), ("a", 1), ("y", None), ("z", None)]
    assert [x["directions"] for x in s] == [["out"], ["out"], [], []]


def test_summary_two_directions_and_loop_stop(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}, {"id": "a"}],
                                    "in": [{"id": "c"}, {"id": "b"}]})
    env.trip("t1", [("c", 1), ("b", 1), ("a", 1)])
    s = env.get("/api/routes/15/summary").json()["stops"]
    # first listing wins: a@0 (out), b@1 (out), c@0 (in, later direction)
    # loop stop a appears twice but in one direction; b is listed by both directions
    assert [(x["stop_id"], x["position"], x["direction"], x["directions"]) for x in s] == [
        ("a", 0, "out", ["out"]), ("b", 1, None, ["out", "in"]), ("c", 0, "in", ["in"])]


def test_summary_stop_in_several_directions_has_null_direction(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "s"}], "in": [{"id": "s"}, {"id": "z"}]})
    env.trip("t1", [("a", 1), ("s", 2), ("z", 3)])
    s = {x["stop_id"]: x for x in env.get("/api/routes/15/summary").json()["stops"]}
    assert (s["s"]["direction"], s["s"]["directions"], s["s"]["position"]) == (None, ["out", "in"], 1)
    assert (s["a"]["direction"], s["a"]["directions"]) == ("out", ["out"])
    assert (s["z"]["direction"], s["z"]["directions"]) == ("in", ["in"])


@pytest.mark.parametrize("mode", ["down", "404", "malformed"])
def test_summary_order_none_fallback(env, mode):
    env.trip("t1", [("sb", 1), ("sa", 2)])
    if mode == "down":
        env.fake.set_route_stops("15", {"out": [{"id": "sb"}]})
        env.fake.set_down(True)
    elif mode == "malformed":
        env.fake.set_route_stops("15", {"out": ["sb", 5]})
    j = env.get("/api/routes/15/summary").json()
    assert j["order_source"] == "none"
    assert [(x["stop_id"], x["position"], x["direction"], x["directions"]) for x in j["stops"]] == [
        ("sa", None, None, []), ("sb", None, None, [])]
    assert j["n"] == 2


def test_summary_exact_numbers(env):
    seed5(env)
    s = env.get("/api/routes/15/summary").json()["stops"][0]
    assert s["n"] == 5 and s["mean"] == 120 and s["median"] == 120
    assert s["std"] == pytest.approx(94.8683, abs=1e-3)
    assert s["ci_low"] == pytest.approx(120 - 117.8, abs=0.05)
    assert s["ci_high"] == pytest.approx(120 + 117.8, abs=0.05)
    assert (s["min"], s["max"]) == (0, 240)
    assert s["on_time_pct"] == 100.0  # all within -60..300
    assert s["reliability"] == pytest.approx(100 / (1 + 94.8683 / 300), abs=1e-3)
    assert s["low_n"] is True


def test_ci_param_changes_interval_summary(env):
    seed5(env)
    w = lambda ci: (lambda s: s["ci_high"] - s["ci_low"])(env.get("/api/routes/15/summary", ci=ci).json()["stops"][0])
    assert w(0.5) < w(0.95) < w(0.99)


@pytest.mark.parametrize("n,low", [(9, True), (10, False)])
def test_low_n_boundary(env, n, low):
    for i in range(n):
        env.add(delay=i)
    j = env.get("/api/routes/15/summary").json()
    assert j["stops"][0]["low_n"] is low and j["low_n"] is low
    assert env.get("/api/stops/s1/stats").json()["low_n"] is low


def test_summary_filters_echoed_and_defaults(env):
    f = env.get("/api/routes/15/summary").json()["filters"]
    assert f == {"ci": 0.95, "pct_lo": 0.05, "pct_hi": 0.95, "window_lo": -60, "window_hi": 300,
                 "since": None, "until": None, "weekdays": None, "hours": None,
                 "min_confidence": 1, "truncated": False, "realtime_only": True}
    f = env.get("/api/routes/15/summary", weekdays="0, 2,2", hours="5", since="1", until="9", ci="0.9",
                min_confidence="0").json()["filters"]
    assert f["weekdays"] == [0, 2] and f["hours"] == [5] and (f["since"], f["until"]) == (1, 9)
    assert f["ci"] == 0.9 and f["min_confidence"] == 0


def test_default_excludes_realtime0_and_confidence0(env):
    env.add(delay=10)
    env.add(delay=20, confidence=0)
    env.add(delay=30, realtime=0)
    env.add(delay=40, realtime=0, confidence=0)
    assert env.get("/api/routes/15/summary").json()["n"] == 1
    j = env.get("/api/routes/15/summary", min_confidence=0).json()
    assert j["n"] == 2 and j["stops"][0]["mean"] == 15  # realtime=0 still excluded


def test_null_delay_rows_ignored(env):
    env.add(delay=None)
    env.add(delay=60)
    assert env.get("/api/routes/15/summary").json()["n"] == 1


def test_since_until_inclusive(env):
    for i in range(5):
        env.add(scheduled=T0 + i * 100, delay=i)
    n = lambda **q: env.get("/api/routes/15/summary", **q).json()["n"]
    assert n(since=T0 + 100, until=T0 + 300) == 3
    assert n(since=T0 + 101) == 3
    assert n(until=T0 - 1) == 0
    assert n(since=T0 + 400, until=T0 + 400) == 1


def test_weekday_hour_filters_dublin_local(env):
    tz = ZoneInfo("Europe/Dublin")
    mon9 = int(datetime(2026, 1, 5, 9, 30, tzinfo=tz).timestamp())  # Monday winter
    wed14 = int(datetime(2026, 1, 7, 14, 0, tzinfo=tz).timestamp())
    sun_late = int(datetime(2026, 7, 5, 23, 59, tzinfo=tz).timestamp())  # Sunday, BST
    for s in (mon9, wed14, sun_late):
        env.add(scheduled=s, delay=10)
    n = lambda **q: env.get("/api/routes/15/summary", **q).json()["n"]
    assert n(weekdays="0") == 1 and n(weekdays="0,2") == 2 and n(weekdays="6") == 1
    assert n(hours="9") == 1 and n(hours="14,23") == 2 and n(hours="0") == 0
    assert n(weekdays="0", hours="14") == 0


# ---------- stop stats ----------

def test_stop_stats_exact(env):
    seed5(env)
    j = env.get("/api/stops/s1/stats").json()
    assert j["n"] == 5 and j["delay"]["mean"] == 120
    assert j["delay"]["std"] == pytest.approx(94.868, abs=1e-3)
    assert j["delay"]["ci_low"] == pytest.approx(2.2, abs=0.05)
    assert j["delay"]["ci_high"] == pytest.approx(237.8, abs=0.05)
    assert j["route"] is None and j["stop_id"] == "s1"
    assert len(j["recent"]) == 5 and [r["delay"] for r in j["recent"]] == [240, 180, 120, 60, 0]
    finite(j)


def test_stop_stats_ci_param_changes_interval(env):
    seed5(env)
    w = lambda ci: (lambda d: d["ci_high"] - d["ci_low"])(env.get("/api/stops/s1/stats", ci=ci).json()["delay"])
    assert w(0.5) < w(0.95) < w(0.99)
    assert env.get("/api/stops/s1/stats", ci="0.5").json()["filters"]["ci"] == 0.5


def test_on_time_window_changes_pct(env):
    seed5(env)  # delays 0..240
    pct = lambda **q: env.get("/api/stops/s1/stats", **q).json()["on_time_pct"]
    assert pct() == 100.0
    assert pct(window_lo=0, window_hi=120) == 60.0
    assert pct(window_lo=121, window_hi=300) == 40.0
    assert pct(window_lo=0, window_hi=0) == 20.0  # inclusive edges


def test_stop_stats_route_filter(env):
    env.add(route="15", delay=60)
    env.add(route="16", delay=120)
    assert env.get("/api/stops/s1/stats").json()["n"] == 2
    j = env.get("/api/stops/s1/stats", route="16").json()
    assert j["n"] == 1 and j["delay"]["mean"] == 120 and j["route"] == "16"


def test_stop_recent_order_and_cap(env):
    for i in range(25):
        env.add(scheduled=T0 + i, delay=i)
    r = env.get("/api/stops/s1/stats").json()["recent"]
    assert len(r) == 20 and [x["delay"] for x in r] == list(range(24, 4, -1))
    assert set(r[0]) == {"trip_id", "route", "seq", "service_date", "scheduled", "actual", "delay", "confidence"}


def test_histogram_bins(env):
    for d in (-700, -600, -541, 0, 59, 60, 1799, 1800, 5000):
        env.add(delay=d)
    h = env.get("/api/stops/s1/stats").json()["histogram"]
    assert len(h) == 42 and h[0] == {"lo": None, "hi": -600, "count": 1}
    assert h[-1] == {"lo": 1800, "hi": None, "count": 2}
    assert sum(b["count"] for b in h) == 9
    by_lo = {b["lo"]: b["count"] for b in h}
    assert by_lo[-600] == 2 and by_lo[0] == 2 and by_lo[60] == 1 and by_lo[1740] == 1
    assert [b["lo"] for b in h[1:3]] == [-600, -540] and h[1]["hi"] == -540


# ---------- heatmap ----------

def test_heatmap_dst_day(env):
    # 2026-03-29: clocks go forward at 01:00 UTC. 00:30 UTC = 00:30 GMT, 01:30 UTC = 02:30 IST.
    a = int(datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc).timestamp())
    b = int(datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc).timestamp())
    env.add(scheduled=a, delay=60)
    env.add(scheduled=a + 60, delay=120)
    env.add(scheduled=b, delay=300)
    j = env.get("/api/routes/15/heatmap").json()
    assert j["cells"] == [
        {"weekday": 6, "hour": 0, "n": 2, "low_n": True, "mean_delay": 90.0},
        {"weekday": 6, "hour": 2, "n": 1, "low_n": True, "mean_delay": 300.0}]
    assert j["n"] == 3


def test_heatmap_filters_and_sorted(env):
    tz = ZoneInfo("Europe/Dublin")
    for day, hr in ((7, 10), (5, 11), (5, 9)):
        env.add(scheduled=int(datetime(2026, 1, day, hr, 0, tzinfo=tz).timestamp()), delay=1)
    env.add(scheduled=int(datetime(2026, 1, 6, 8, 0, tzinfo=tz).timestamp()), delay=1, confidence=0)
    c = env.get("/api/routes/15/heatmap").json()["cells"]
    assert [(x["weekday"], x["hour"]) for x in c] == [(0, 9), (0, 11), (2, 10)]
    assert len(env.get("/api/routes/15/heatmap", min_confidence=0).json()["cells"]) == 4
    assert len(env.get("/api/routes/15/heatmap", weekdays="0").json()["cells"]) == 2


# ---------- accumulation ----------

def acc(env, **q):
    return env.get("/api/routes/15/accumulation", **q).json()


def test_accumulation_reviewer_repro_realistic_seq(env):
    env.fake.set_route_stops("15", {"out": [{"id": "zz"}, {"id": "mm"}, {"id": "aa"}]})
    for i, t in enumerate(("t1", "t2", "t3")):
        env.trip(t, [("zz", 0), ("mm", 60), ("aa", 180)], t0=T0 + i * 3600)
    j = acc(env)
    assert j["order_source"] == "server" and len(j["groups"]) == 1
    g = j["groups"][0]
    assert (g["direction"], g["n"], g["n_delta"], g["low_n"]) == ("out", 3, 3, True)
    assert [x["stop_id"] for x in g["stops"]] == ["zz", "mm", "aa"]
    assert [x["position"] for x in g["stops"]] == [0, 1, 2]
    assert [x["mean_delay"] for x in g["stops"]] == [0, 60, 180]
    assert [x["segment_delta"] for x in g["stops"]] == [None, 60, 120]
    assert [(x["n"], x["n_delta"], x["low_n"], x["low_n_delta"]) for x in g["stops"]][1] == (3, 3, True, True)
    finite(j)


def test_accumulation_two_directions_shared_stops(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                                    "in": [{"id": "c"}, {"id": "b"}, {"id": "x"}]})
    env.trip("o1", [("a", 0), ("b", 60), ("c", 180)])
    env.trip("o2", [("a", 0), ("b", 120), ("c", 240)], t0=T0 + 5000)
    env.trip("i1", [("c", 10), ("b", 40), ("x", 100)], t0=T0 + 9000)
    g = {x["direction"]: x for x in acc(env)["groups"]}
    assert list(g) == ["out", "in"]  # server direction order
    assert [x["mean_delay"] for x in g["out"]["stops"]] == [0, 90, 210]
    assert [x["segment_delta"] for x in g["out"]["stops"]] == [None, 90, 120]
    assert g["out"]["n"] == 2 and g["in"]["n"] == 1
    assert [(x["stop_id"], x["position"], x["segment_delta"]) for x in g["in"]["stops"]] == [
        ("c", 0, None), ("b", 1, 30), ("x", 2, 60)]


def test_accumulation_loop_trip(env):
    # loop: a b c a ; second visit of "a" must map to position 3, not 0
    env.fake.set_route_stops("15", {"loop": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "a"}]})
    env.trip("t1", [("a", 0), ("b", 30), ("c", 90), ("a", 100)])
    g = acc(env)["groups"][0]
    assert [(x["stop_id"], x["position"], x["mean_delay"]) for x in g["stops"]] == [
        ("a", 0, 0), ("b", 1, 30), ("c", 2, 90), ("a", 3, 100)]
    assert [x["segment_delta"] for x in g["stops"]] == [None, 30, 60, 10]


def test_accumulation_tie_and_nomatch_unknown(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}], "in": [{"id": "a"}, {"id": "c"}]})
    env.trip("tie", [("a", 5)])  # a is first in both: tie
    env.trip("none", [("q", 5)], t0=T0 + 5000)  # matches nothing
    g = acc(env)["groups"]
    assert [(x["direction"], x["fallback"], x["n"], x["n_delta"], x["stops"]) for x in g] == [
        (None, True, 2, 0, [])]


def test_server_direction_named_unknown_does_not_collide_with_fallback(env):
    env.fake.set_route_stops("15", {"unknown": [{"id": "a"}, {"id": "b"}]})
    env.trip("real", [("a", 0), ("b", 60)])
    env.trip("nomatch", [("q", 5)], t0=T0 + 5000)
    g = acc(env)["groups"]
    assert [(x["direction"], x["fallback"], x["n"], len(x["stops"])) for x in g] == [
        ("unknown", False, 1, 2), (None, True, 1, 0)]
    assert [x["direction"] for x in acc(env, direction="unknown")["groups"]] == ["unknown"]
    assert [x["fallback"] for x in acc(env, direction="__unknown__")["groups"]] == [True]


def test_server_direction_literally_named_dunder_unknown_is_ordinary(env):
    env.fake.set_route_stops("15", {"__unknown__": [{"id": "a"}, {"id": "b"}], "out": [{"id": "c"}]})
    env.trip("T1", [("a", 0), ("b", 60)])
    env.trip("T2", [("zz", 5)], t0=T0 + 5000)
    g = acc(env)["groups"]
    assert [(x["direction"], x["fallback"], x["n"], len(x["stops"])) for x in g] == [
        ("__unknown__", False, 1, 2), (None, True, 1, 0)]
    assert [x["direction"] for x in acc(env, direction="__unknown__")["groups"]] == ["__unknown__"]
    assert [x["stop_id"] for x in acc(env, direction="__unknown__")["groups"][0]["stops"]] == ["a", "b"]
    assert [x["fallback"] for x in acc(env, direction="out")["groups"]] == []
    assert env.get("/api/routes/15/summary").json()["stops"][0]["direction"] == "__unknown__"


def test_accumulation_same_trip_id_on_two_service_dates_not_merged(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    for date, t0, ds in (("2026-01-01", T0, (0, 60, 180)), ("2026-01-02", T0 + 86400, (0, 120, 240))):
        for i, (sid, d) in enumerate(zip("abc", ds)):
            env.add(trip="T", stop=sid, delay=d, scheduled=t0 + i * 60, date=date)
    g = acc(env)["groups"]
    assert len(g) == 1 and g[0]["n"] == 2  # merged by trip_id alone it would be 1 trip / 3 matches
    assert [(x["n"], x["mean_delay"], x["segment_delta"]) for x in g[0]["stops"]] == [
        (2, 0, None), (2, 90, 90), (2, 210, 120)]


def test_accumulation_identical_scheduled_rows_not_dropped(env):
    # reviewer repro: rows c, b, a inserted in that order, all with the same scheduled
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    for sid, d in (("c", 30), ("b", 20), ("a", 10)):
        env.add(trip="t1", stop=sid, delay=d, scheduled=T0)
    g = acc(env)["groups"]
    assert [(x["stop_id"], x["position"], x["mean_delay"]) for x in g[0]["stops"]] == [
        ("a", 0, 10), ("b", 1, 20), ("c", 2, 30)]
    assert [x["segment_delta"] for x in g[0]["stops"]] == [None, 10, 10]


def test_accumulation_partial_trip_best_match(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                                    "in": [{"id": "c"}, {"id": "b"}, {"id": "a"}]})
    env.trip("t1", [("b", 10), ("c", 50)])  # out: 2 matches; in: b@1 then c needs >1: only 1
    g = acc(env)["groups"]
    assert [x["direction"] for x in g] == ["out"]
    assert [x["position"] for x in g[0]["stops"]] == [1, 2]


def test_accumulation_order_follows_scheduled_not_seq(env):
    # seq deliberately disagrees with scheduled order: b has seq 1, a has seq 2, yet a is scheduled first
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}]})
    env.add(trip="t1", stop="b", delay=60, scheduled=T0 + 60, seq=1)
    env.add(trip="t1", stop="a", delay=0, scheduled=T0, seq=2)
    g = acc(env)["groups"][0]
    assert [(x["stop_id"], x["position"], x["segment_delta"]) for x in g["stops"]] == [
        ("a", 0, None), ("b", 1, 60)]


def test_accumulation_filters_apply(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}]})
    env.trip("t1", [("a", 0), ("b", 60)])
    env.trip("t2", [("a", 0), ("b", 600)], t0=T0 + 5000)
    j = acc(env, until=T0 + 1000)
    assert j["groups"][0]["n"] == 1 and j["groups"][0]["stops"][1]["mean_delay"] == 60
    assert acc(env, since=T0 + 4000)["groups"][0]["stops"][1]["mean_delay"] == 600


def test_accumulation_direction_param(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}], "in": [{"id": "x"}, {"id": "y"}]})
    env.trip("t1", [("a", 0), ("b", 60)])
    env.trip("t2", [("x", 0), ("y", 60)], t0=T0 + 5000)
    assert [g["direction"] for g in acc(env, direction="in")["groups"]] == ["in"]
    assert acc(env, direction="__unknown__")["groups"] == []
    assert [g["fallback"] for g in acc(env)["groups"]] == [False, False]
    assert env.get("/api/routes/15/accumulation", direction="sideways").status_code == 404


def test_accumulation_without_server_order_none(env):
    env.trip("t1", [("a", 0), ("b", 60)])
    env.fake.set_route_stops("15", {"out": [{"id": "a"}]})
    env.fake.set_down(True)
    j = acc(env)
    assert j["order_source"] == "none" and j["groups"] == []
    assert env.get("/api/routes/15/accumulation", direction="out").status_code == 502


def test_accumulation_unknown_route_on_server(env):
    env.add(delay=1)
    assert acc(env) == {"route": "15", "filters": acc(env)["filters"], "order_source": "none", "groups": []}
    assert env.get("/api/routes/15/accumulation", direction="out").status_code == 404


@pytest.mark.parametrize("bad", [{"out": [5]}, {"out": "x"}, {"out": [{"id": 5}]}, {"out": [{}]}, []])
def test_malformed_shapes_never_500(env, bad):
    env.fake.set_route_stops("15", bad)
    env.add(delay=1)
    assert env.get("/api/routes/15/stops").status_code == 502
    assert env.get("/api/routes/15/summary").json()["order_source"] == "none"
    assert acc(env)["order_source"] == "none"
    assert env.get("/api/routes/15/accumulation", direction="out").status_code == 502


def test_missing_directions_key_is_502(env):
    # FakeServer always wraps in "directions"; emulate a server answering without it
    import httpx
    from app.collect.serverclient import ServerClient as SC
    h = lambda req: httpx.Response(200, json={"routeId": "15"})
    env.app.state.client = SC("http://x", httpx.AsyncClient(transport=httpx.MockTransport(h)))
    env.add(delay=1)
    assert env.get("/api/routes/15/stops").status_code == 502
    assert env.get("/api/routes/15/summary").json()["order_source"] == "none"
    assert acc(env)["groups"] == []


def test_accumulation_stopid_key_variant(env):
    env.fake.set_route_stops("15", {"out": [{"stopId": "a"}]})
    env.add(stop="a", delay=3)
    assert acc(env)["groups"][0]["direction"] == "out"


def test_accumulation_skips_null_delay_and_scheduled(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}]})
    env.add(trip="t1", stop="a", delay=None, scheduled=T0)
    env.add(trip="t1", stop="b", delay=60, scheduled=T0 + 60)
    db = open_meta(env.s.data_dir)
    with db:
        db.execute("insert into arrivals(trip_id,stop_id,seq,service_date,scheduled,delay,confidence,"
                   "realtime,route) values('t1','a',99,'d',NULL,5,1,1,'15')")
    db.close()
    g = acc(env)["groups"][0]
    assert [(x["stop_id"], x["segment_delta"]) for x in g["stops"]] == [("b", None)]


# ---------- forecast ----------

def test_forecast_horizons_always_present(env):
    j = env.get("/api/routes/15/forecast").json()
    assert [h["horizon_min"] for h in j["horizons"]] == [30, 20, 10, 5, 2]
    assert all(h["n"] == 0 and h["mean_error"] is None and h["low_n"] for h in j["horizons"])


def test_forecast_exact(env):
    a = env.add(delay=0)
    b = env.add(delay=0)
    for aid, e in ((a, 60), (b, -120)):
        env.fc(aid, 10, e)
    env.fc(a, 2, 30)
    env.fc(a, 7, 99)  # non-standard horizon ignored
    env.add(route="other", delay=0)
    env.fc(env.add(route="other", delay=0), 10, 1000)
    hs = {h["horizon_min"]: h for h in env.get("/api/routes/15/forecast").json()["horizons"]}
    assert hs[10]["n"] == 2 and hs[10]["mean_error"] == -30 and hs[10]["mae"] == 90
    assert hs[2]["n"] == 1 and hs[2]["mean_error"] == 30 and hs[30]["n"] == 0
    assert 7 not in hs


def test_forecast_filters(env):
    a = env.add(delay=0, confidence=0)
    b = env.add(delay=0, realtime=0)
    c = env.add(delay=0)
    for x in (a, b, c):
        env.fc(x, 5, 10)
    n = lambda **q: env.get("/api/routes/15/forecast", **q).json()["horizons"][3]["n"]
    assert n() == 1 and n(min_confidence=0) == 2


# ---------- empty database ----------

def assert_empty(url, j):
    """The exact empty-result structure of each endpoint (n 0 / no data), never a vacuous check."""
    finite(j)
    if "/stats" in url:
        assert j["n"] == 0 and j["low_n"] is True and j["delay"]["mean"] is None
        assert j["on_time_pct"] is None and j["reliability"] is None and j["recent"] == []
        assert sum(b["count"] for b in j["histogram"]) == 0
    elif url.endswith("/summary"):
        assert j["n"] == 0 and j["low_n"] is True and j["stops"] == []
    elif url.endswith("/heatmap"):
        assert j["n"] == 0 and j["cells"] == []
    elif url.endswith("/forecast"):
        assert [h["horizon_min"] for h in j["horizons"]] == [30, 20, 10, 5, 2]
        assert all(h["n"] == 0 and h["mean_error"] is None for h in j["horizons"])
    else:
        assert url.endswith("/accumulation") and j["groups"] == [] and j["order_source"] == "none"


EMPTY_URLS = ["/api/routes/{}/summary", "/api/stops/{}/stats", "/api/routes/{}/heatmap",
              "/api/routes/{}/forecast", "/api/routes/{}/accumulation"]


@pytest.mark.parametrize("url", EMPTY_URLS)
def test_empty_db_200_n0(env, url):
    url = url.format("zz")
    r = env.get(url)
    assert r.status_code == 200
    assert_empty(url, r.json())


def test_stop_with_no_arrivals_returns_n0_not_500(env):
    env.add(stop="other", delay=5)
    r = env.get("/api/stops/ghost/stats")
    assert r.status_code == 200 and r.json()["n"] == 0


def test_single_row_has_null_ci(env):
    env.add(delay=60)
    s = env.get("/api/routes/15/summary").json()["stops"][0]
    assert s["n"] == 1 and s["mean"] == 60 and s["std"] is None and s["ci_low"] is None
    assert s["reliability"] is None and s["on_time_pct"] == 100.0


# ---------- validation ----------

BAD = [("ci", "0"), ("ci", "1"), ("ci", "-0.1"), ("ci", "1.5"), ("ci", "abc"), ("ci", "nan"),
       ("ci", "inf"), ("ci", ""), ("pct_lo", "0.95"), ("pct_lo", "x"), ("pct_lo", "-0.1"),
       ("pct_hi", "1.1"), ("pct_hi", "0.01"), ("pct_hi", "nan"), ("window_lo", "301"),
       ("window_lo", "1.5"), ("window_lo", "abc"), ("window_lo", "99999999999999"),
       ("window_hi", "-61"), ("window_hi", "1e3"), ("since", "abc"), ("since", "1.0"),
       ("since", "1 "), ("since", ""), ("since", "9" * 30), ("until", "-"), ("until", "0x10"),
       ("weekdays", "7"), ("weekdays", "-1"), ("weekdays", "a"), ("weekdays", ""), ("weekdays", "1,,2"),
       ("weekdays", "1.5"), ("hours", "24"), ("hours", "-1"), ("hours", "x"), ("hours", "1;2"),
       ("min_confidence", "2"), ("min_confidence", "-1"), ("min_confidence", "x"),
       ("ci", "٠.٥"), ("ci", "1_0"), ("ci", "5e-1"), ("ci", "0.5\n"), ("ci", "+0.5"), ("window_lo", "٣"),
       ("since", "٣"), ("until", "5\n"), ("weekdays", "٣"), ("hours", "١"), ("min_confidence", "١"),
       ("ci", "9" * 65), ("route", "x" * 65), ("direction", "x" * 65)]
URLS = ["/api/routes/15/summary", "/api/stops/s1/stats", "/api/routes/15/heatmap",
        "/api/routes/15/accumulation", "/api/routes/15/forecast"]


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("k,v", BAD)
def test_invalid_params_422(env, url, k, v):
    if k == "route" and "stats" not in url or k == "direction" and "accumulation" not in url:
        pytest.skip("param not accepted by this endpoint")
    r = env.get(url, **{k: v})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]


def test_invalid_ci_422(env):
    r = env.get("/api/routes/15/summary", ci="1.5")
    assert r.status_code == 422 and "ci" in r.json()["detail"]


def test_since_after_until_422(env):
    assert env.get("/api/routes/15/summary", since=10, until=9).status_code == 422
    assert env.get("/api/routes/15/summary", since=9, until=9).status_code == 200


@pytest.mark.parametrize("k,v", [("ci", "0.001"), ("ci", "0.999"), ("pct_lo", "0"), ("pct_hi", "1"), ("ci", ".5"), ("ci", "0.5"), ("pct_lo", "0."),
                                 ("window_lo", "-99999"), ("window_lo", "300"), ("since", "0"),
                                 ("since", "-5"), ("hours", "0,23"), ("weekdays", "0,1,2,3,4,5,6")])
def test_boundary_values_ok(env, k, v):
    seed5(env)
    r = env.get("/api/routes/15/summary", **{k: v})
    assert r.status_code == 200
    finite(r.json())


def test_path_param_length_limits(env):
    assert env.get("/api/routes/" + "r" * 32 + "/summary").status_code == 200
    assert env.get("/api/routes/" + "r" * 33 + "/summary").status_code == 422
    assert env.get("/api/stops/" + "s" * 64 + "/stats").status_code == 200
    assert env.get("/api/stops/" + "s" * 65 + "/stats").status_code == 422
    assert env.get("/api/routes/" + "r" * 5000 + "/summary").status_code == 422


# ---------- injection ----------

INJ = ["15' OR '1'='1", "15'; DROP TABLE arrivals;--", "x\" or 1=1 --", "%' union select 1 --",
       "15); delete from arrivals; --"]


@pytest.mark.parametrize("evil", INJ)
def test_sql_injection_in_route_param_harmless(env, evil):
    env.add(delay=10)
    q = quote(evil, safe="")
    for u in EMPTY_URLS:
        url = u.format(q)
        r = env.get(url)
        assert r.status_code == 200, (url, r.text)
        assert_empty(url, r.json())
    r = env.get("/api/stops/s1/stats", route=evil)
    assert r.status_code == 200 and r.json()["n"] == 0
    assert env.c.put(f"/api/routes/tracked/{q}").status_code == 422
    assert env.get("/api/routes/15/summary").json()["n"] == 1  # table intact
    db = open_meta(env.s.data_dir)
    assert db.execute("select count(*) from arrivals").fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize("evil", INJ)
def test_sql_injection_in_query_params_rejected_or_harmless(env, evil):
    env.add(delay=10)
    for k in ("ci", "since", "weekdays", "hours", "min_confidence", "window_lo"):
        assert env.get("/api/routes/15/summary", **{k: evil}).status_code == 422
    env.get("/api/routes", q=evil)
    assert env.get("/api/routes/15/summary").json()["n"] == 1


def test_unicode_and_space_route_names(env):
    env.add(route="N 4é", delay=5)
    r = env.get("/api/routes/" + quote("N 4é", safe="") + "/summary")
    assert r.json()["n"] == 1


# ---------- leaks and finiteness ----------

def test_no_connection_leak_under_errors(env, recwarn):
    import gc
    seed5(env)
    for _ in range(30):
        env.get("/api/routes/15/summary")
        env.get("/api/routes/15/summary", ci="2")
        env.get("/api/stops/s1/stats")
        env.get("/api/routes/15/forecast")
    gc.collect()
    assert not [w for w in recwarn if issubclass(w.category, ResourceWarning)]
    # a leaked write-lock/handle would also block a fresh exclusive writer
    db = open_meta(env.s.data_dir)
    db.execute("begin immediate")
    db.rollback()
    db.close()


def test_connection_closed_on_db_error(env, monkeypatch):
    import app.api.routes as api
    opened = []
    real = api.open_meta

    def spy(d):
        c = real(d)
        opened.append(c)
        return c

    monkeypatch.setattr(api, "open_meta", spy)
    monkeypatch.setattr(api, "_summ", lambda *a, **k: 1 / 0)
    env.add(delay=1)
    with pytest.raises(ZeroDivisionError):
        TestClient(env.app).get("/api/routes/15/summary")
    assert opened
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("select 1")


def test_all_responses_json_strict_with_extreme_delays(env):
    for d in (-10**9, 10**9, 0, 0):
        env.add(delay=d)
    for url in URLS:
        finite(env.get(url).json())


# ---------- property ----------

@hsettings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.tuples(st.sampled_from("abc"), st.integers(1, 3), st.integers(-3000, 3000),
                          st.integers(T0, T0 + 10**6), st.integers(0, 1), st.integers(0, 1)), max_size=40),
       st.integers(0, 1), st.one_of(st.none(), st.integers(T0, T0 + 10**6)))
def test_summary_n_matches_filtered_rows(tmp_path_factory, rows, minc, since):
    env = Env(tmp_path_factory.mktemp("p"))
    for stop, seq, d, sched, conf, rt in rows:
        env.add(stop=stop, seq=seq, delay=d, scheduled=sched, confidence=conf, realtime=rt)
    q = {"min_confidence": minc}
    if since is not None:
        q["since"] = since
    j = env.get("/api/routes/15/summary", **q).json()
    expect = sum(1 for _, _, _, s, c, rt in rows if rt and c >= minc and (since is None or s >= since))
    assert j["n"] == sum(s["n"] for s in j["stops"]) == expect
    finite(j)
    for s in j["stops"]:
        if s["n"] > 1:
            assert s["ci_low"] <= s["mean"] <= s["ci_high"]
            assert s["min"] <= s["p_low"] <= s["median"] <= s["p_high"] <= s["max"]
        assert s["on_time_pct"] is None or 0 <= s["on_time_pct"] <= 100
        assert all(not (isinstance(v, float) and not math.isfinite(v)) for v in s.values())
    assert sum(b["count"] for b in env.get("/api/stops/a/stats", **q).json()["histogram"]) == \
        sum(s["n"] for s in j["stops"] if s["stop_id"] == "a")


# ---------- SQL pushdown (scale) ----------

@pytest.fixture
def spy(monkeypatch):
    """Records executed statements (expanded) and, per bulk scan, the number of rows fetched."""
    import app.api.routes as api
    st = types.SimpleNamespace(sql=[], fetched=[])
    real_open, real_scan = api.open_meta, api._scan

    def open_spy(d):
        c = real_open(d)
        c.set_trace_callback(st.sql.append)
        return c

    def scan_spy(request, sql, args, fn, *a, **k):
        n = [0]

        def counting(rows):
            n[0] += len(rows)
            return fn(rows)

        try:
            return real_scan(request, sql, args, counting, *a, **k)
        finally:
            st.fetched.append(n[0])

    monkeypatch.setattr(api, "open_meta", open_spy)
    monkeypatch.setattr(api, "_scan", scan_spy)
    return st


def bulk(env, n, route="15", step=60, batch=50_000):
    """Seed n arrivals in batches (never holds all rows in memory)."""
    db = open_meta(env.s.data_dir)
    for lo in range(0, n, batch):
        with db:
            db.executemany(
                "insert into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,"
                "realtime,route) values(?,?,?,?,?,?,?,?,?,?)",
                [(f"{route}t{i}", f"s{i % 20}", T0 + i * step, "d", T0 + i * step, T0 + i * step + i % 100,
                  i % 100, 0 if i % 10 == 0 else 1, 1, route) for i in range(lo, min(n, lo + batch))])
    db.close()


WEEK = 7 * 86400


def test_window_fetches_only_window_rows(env, spy):
    bulk(env, 50_000)
    bulk(env, 500, route="other")
    since = T0 + 10 * 86400
    until = since + WEEK - 1
    exp = [i for i in range(50_000) if since <= T0 + i * 60 <= until and i % 10 != 0]
    j = env.get("/api/routes/15/summary", since=since, until=until).json()
    assert spy.fetched == [len(exp)] and j["n"] == len(exp) and len(exp) < 10080
    assert sum(s["n"] for s in j["stops"]) == len(exp)
    spy.fetched.clear()
    h = env.get("/api/routes/15/heatmap", since=since, until=until).json()
    assert spy.fetched == [len(exp)] and h["n"] == len(exp)
    spy.fetched.clear()
    env.get("/api/routes/15/summary", since=since, until=until, min_confidence=0)
    assert spy.fetched == [10080]  # confidence filter off: whole window, still not the table
    spy.fetched.clear()
    j = env.get("/api/stops/s3/stats", since=since, until=until).json()
    assert spy.fetched == [len([i for i in exp if i % 20 == 3])] and j["n"] == spy.fetched[0]


def test_weekday_filter_stays_python_but_window_is_sql(env, spy):
    bulk(env, 20_000)
    since = T0 + 3 * 86400
    j = env.get("/api/routes/15/summary", since=since, until=since + WEEK - 1, weekdays="0").json()
    assert spy.fetched[0] > j["n"] > 0


def plan(sql, data_dir):
    db = open_meta(data_dir)
    try:
        return " ".join(r[3] for r in db.execute("explain query plan " + sql))
    finally:
        db.close()


@pytest.mark.parametrize("url,idx", [("/api/routes/15/summary", "ix_arr_route_sched"),
                                     ("/api/routes/15/heatmap", "ix_arr_route_sched"),
                                     ("/api/routes/15/forecast", "ix_arr_route_sched"),
                                     ("/api/routes/15/accumulation", "ix_arr_route_sched"),
                                     ("/api/stops/s1/stats", "ix_arr_stop_sched")])
@pytest.mark.parametrize("q", [{}, {"since": T0, "until": T0 + 99}])
def test_statements_use_the_index(env, spy, url, idx, q):
    bulk(env, 200)
    env.fake.set_route_stops("15", {"out": [{"id": "s1"}]})
    env.get(url, **q)
    stmts = [x for x in spy.sql if "from arrivals" in x.lower() and not x.lower().startswith("insert")]
    assert stmts, spy.sql
    for x in stmts:
        assert idx in plan(x, env.s.data_dir), (x, plan(x, env.s.data_dir))
    assert not any("scan arrivals" in plan(x, env.s.data_dir).lower() for x in stmts)


def test_forecast_pushdown_and_values(env, spy):
    bulk(env, 5000)
    db = open_meta(env.s.data_dir)
    with db:
        db.executemany("insert into forecast values(?,?,?,?)",
                       [(i, h, 0, h) for i in range(1, 5001) for h in (30, 10, 7)])
    db.close()
    since = T0 + 1000 * 60
    j = env.get("/api/routes/15/forecast", since=since, until=since + 99 * 60).json()
    n = len([i for i in range(1000, 1100) if i % 10 != 0])
    assert spy.fetched == [2 * n]  # horizons 30 and 10 only (7 filtered in SQL), window only
    assert {h["horizon_min"]: h["n"] for h in j["horizons"]} == {30: n, 20: 0, 10: n, 5: 0, 2: 0}
    assert j["horizons"][0]["mean_error"] == 30


# ---------- event loop not blocked ----------

@pytest.mark.parametrize("what", ["summary", "accumulation", "tracked", "track"])
async def test_event_loop_not_blocked(tmp_path, monkeypatch, what):
    import app.api.routes as api
    env = Env(tmp_path)
    env.fake.set_route_stops("15", {"out": [{"id": "s1"}]})
    if what == "summary":
        monkeypatch.setattr(api, "_summary_db", lambda *a: (time.sleep(0.3), {})[1])
        req = ("GET", "/api/routes/15/summary")
    elif what == "accumulation":
        monkeypatch.setattr(api, "_accumulation_db", lambda *a: (time.sleep(0.3), ([], a[2]))[1])
        req = ("GET", "/api/routes/15/accumulation")
    elif what == "tracked":
        env.app.state.poller.tracked = lambda: (time.sleep(0.3), [])[1]
        req = ("GET", "/api/routes/tracked")
    else:
        env.app.state.poller.track = lambda r: time.sleep(0.3)
        req = ("PUT", "/api/routes/tracked/15")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t") as c:
        t = time.perf_counter()  # a blocked loop delays the sleep below as well as the health call
        task = asyncio.create_task(c.request(*req))
        await asyncio.sleep(0.05)
        assert (await c.get("/api/health")).json() == {"ok": True}
        assert time.perf_counter() - t < 0.15
        assert (await task).status_code == 200


def test_search_routes_404_and_502(env):
    env.fake.set_status(404)
    assert env.get("/api/routes").status_code == 404


# ---------- fix round 2: N3 ci upper bound + sanitizer ----------

@pytest.mark.parametrize("url", ["/api/routes/15/summary", "/api/stops/s1/stats", "/api/routes/15/heatmap",
                                 "/api/routes/15/accumulation", "/api/routes/15/forecast"])
@pytest.mark.parametrize("ci", ["0.9999999999999999", "0.99991", "0.99999999"])
def test_ci_too_close_to_one_is_422(env, url, ci):
    seed5(env)
    r = env.get(url, ci=ci)
    assert r.status_code == 422 and "0.9999" in r.json()["detail"]


@pytest.mark.parametrize("url", ["/api/routes/15/summary", "/api/stops/s1/stats"])
def test_ci_max_allowed_is_finite(env, url):
    seed5(env)
    r = env.get(url, ci="0.9999")
    assert r.status_code == 200
    j = r.json()
    finite(j)
    d = j["delay"] if "delay" in j else j["stops"][0]
    assert d["ci_low"] < 120 < d["ci_high"]


def test_clean_replaces_non_finite_recursively():
    import app.api.routes as api
    nan, inf = float("nan"), float("inf")
    src = {"a": [nan, inf, 1.0, {"b": -inf, "c": 2}], "d": "x", "e": None, "f": (1, nan)}
    out = api._clean(src)
    assert out == {"a": [None, None, 1.0, {"b": None, "c": 2}], "d": "x", "e": None, "f": [1, None]}
    json.dumps(out, allow_nan=False)


def test_non_finite_in_stats_becomes_null_not_500(env, monkeypatch):
    import app.api.routes as api
    seed5(env)
    real = api.stats.delay_summary

    def bad(*a, **k):
        return {**real(*a, **k), "ci_high": float("inf"), "mean": float("nan")}

    monkeypatch.setattr(api.stats, "delay_summary", bad)
    for url in ("/api/routes/15/summary", "/api/stops/s1/stats"):
        r = env.get(url)
        assert r.status_code == 200
        j = r.json()
        d = j["delay"] if "delay" in j else j["stops"][0]
        assert d["ci_high"] is None and d["mean"] is None


# ---------- fix round 2: N8 route_stops cache + timeout ----------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def stops_hits(env, route="15"):
    return env.fake.requests.count(f"/v1/routes/{route}/stops")


def test_route_stops_cached_between_requests(env):
    import app.api.routes as api
    clock = Clock()
    env.app.state.stops_cache = api._TTLCache(clock=clock)
    env.keep_cache = True
    env.fake.set_route_stops("15", {"out": [{"id": "a"}]})
    env.add(stop="a", delay=1)
    for url in ("/api/routes/15/summary", "/api/routes/15/accumulation", "/api/routes/15/stops"):
        assert env.get(url).status_code == 200
    assert stops_hits(env) == 1
    env.get("/api/routes/16/summary")  # other route: separate entry (404 on server, not cached)
    assert stops_hits(env, "16") == 1
    env.get("/api/routes/16/summary")
    assert stops_hits(env, "16") == 2


def test_route_stops_cache_expires_after_5_min(env):
    import app.api.routes as api
    clock = Clock()
    env.app.state.stops_cache = api._TTLCache(clock=clock)
    env.keep_cache = True
    env.fake.set_route_stops("15", {"out": [{"id": "a"}]})
    env.get("/api/routes/15/stops")
    clock.t += 299
    env.get("/api/routes/15/stops")
    assert stops_hits(env) == 1
    env.fake.set_route_stops("15", {"out": [{"id": "b"}]})
    clock.t += 2  # 301 s old
    assert env.get("/api/routes/15/stops").json()["directions"] == {"out": [{"id": "b"}]}
    assert stops_hits(env) == 2


def test_route_stops_failure_not_cached(env):
    env.keep_cache = True
    env.add(stop="a", delay=1)
    env.fake.set_route_stops("15", {"out": [{"id": "a"}]})
    env.fake.set_down(True)
    assert env.get("/api/routes/15/summary").json()["order_source"] == "none"
    env.fake.set_down(False)
    assert env.get("/api/routes/15/summary").json()["order_source"] == "server"
    env.fake.set_route_stops("15", {"out": ["bad"]})  # malformed: not cached either
    env.app.state.stops_cache.clear()
    assert env.get("/api/routes/15/summary").json()["order_source"] == "none"
    env.fake.set_route_stops("15", {"out": [{"id": "a"}]})
    assert env.get("/api/routes/15/summary").json()["order_source"] == "server"


def test_ttl_cache_unit_bounds_and_eviction():
    import app.api.routes as api
    clock = Clock()
    c = api._TTLCache(ttl=10, maxsize=3, clock=clock)
    for k in "abc":
        c.put(k, k.upper())
    c.put("d", "D")  # full: oldest ("a") evicted
    assert [c.get(k) for k in "abcd"] == [None, "B", "C", "D"]
    clock.t += 11
    assert c.get("b") is None  # expired
    c.put("e", "E")  # c, d expired but not yet purged: cache not full yet
    assert len(c._d) == 3
    c.put("f", "F")  # full: expired entries purged first, nothing live evicted
    assert sorted(c._d) == ["e", "f"]
    c.clear()
    assert c.get("e") is None


def test_ttl_cache_thread_safe():
    import threading
    import app.api.routes as api
    c = api._TTLCache(maxsize=50)

    def work(i):
        for j in range(500):
            c.put(f"{i}-{j}", j)
            c.get(f"{i}-{j - 1}")

    ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(c._d) <= 50


def test_hung_server_times_out_fast(env, monkeypatch):
    import app.api.routes as api

    class Hung:
        async def route_stops(self, r):
            await asyncio.sleep(5)

    monkeypatch.setattr(api, "STOPS_TIMEOUT", 0.05)
    env.app.state.client = Hung()
    env.add(delay=1)
    t = time.perf_counter()
    assert env.get("/api/routes/15/summary").json()["order_source"] == "none"
    assert env.get("/api/routes/15/stops").status_code == 502
    assert time.perf_counter() - t < 1.5


# ---------- fix round 2: N2 serialised sqlite reads ----------

BURST = ["/api/routes/15/summary", "/api/stops/s3/stats", "/api/routes/15/heatmap",
         "/api/routes/15/forecast", "/api/routes/15/accumulation"]


async def test_concurrent_requests_bounded_by_serialised_time(tmp_path):
    env = Env(tmp_path)
    bulk(env, 6000)
    env.fake.set_route_stops("15", {"out": [{"id": f"s{i}"} for i in range(20)]})
    db = open_meta(env.s.data_dir)
    with db:
        db.executemany("insert into forecast values(?,?,?,?)", [(i, 10, 0, i % 50) for i in range(1, 6001)])
    db.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t") as c:
        for u in BURST:  # warm-up (imports, route_stops cache)
            assert (await c.get(u)).status_code == 200
        serial = []
        for _ in range(3):
            t = time.perf_counter()
            for u in BURST:
                assert (await c.get(u)).status_code == 200
            serial.append(time.perf_counter() - t)
        conc = []
        for _ in range(3):
            t = time.perf_counter()
            rs = await asyncio.gather(*(c.get(u) for u in BURST))
            conc.append(time.perf_counter() - t)
            assert all(r.status_code == 200 for r in rs)
    assert min(conc) < 3 * min(serial), (conc, serial)


def test_lock_released_after_error_inside_locked_section(env, monkeypatch):
    import app.api.routes as api
    env.add(delay=1)
    real = api.open_meta
    calls = []

    def boom(d):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk on fire")
        return real(d)

    monkeypatch.setattr(api, "open_meta", boom)
    with pytest.raises(RuntimeError):
        env.get("/api/stops/s1/stats")
    assert api._DB_LOCK.acquire(timeout=1)  # not left held
    api._DB_LOCK.release()
    assert env.get("/api/stops/s1/stats").json()["n"] == 1


def test_lock_released_after_sql_error(env, monkeypatch):
    import app.api.routes as api
    env.add(delay=1)
    monkeypatch.setattr(api, "_cond", lambda p, a="": ("nosuchcolumn = 1", []))
    with pytest.raises(sqlite3.OperationalError):
        env.get("/api/routes/15/heatmap")
    monkeypatch.undo()
    assert api._DB_LOCK.acquire(timeout=1)
    api._DB_LOCK.release()
    assert env.get("/api/routes/15/heatmap").json()["n"] == 1


def test_db_work_is_mutually_exclusive(env, monkeypatch):
    import threading
    import app.api.routes as api
    env.add(delay=1)
    inside, worst = [0], [0]
    real = api.open_meta

    def spy(d):
        inside[0] += 1
        worst[0] = max(worst[0], inside[0])
        time.sleep(0.01)
        try:
            return real(d)
        finally:
            inside[0] -= 1

    monkeypatch.setattr(api, "open_meta", spy)
    ts = [threading.Thread(target=lambda: env.c.get("/api/stops/s1/stats")) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert worst[0] == 1


# ---------- fix round 2: N1 bounded memory (cap, chunking, equivalence, peak RSS) ----------

def test_scan_callback_runs_outside_lock_in_chunks(env, monkeypatch):
    import app.api.routes as api
    bulk(env, 100)
    monkeypatch.setattr(api, "CHUNK", 7)
    sizes = []

    def fn(rows):
        assert not api._DB_LOCK.locked()  # lock is per fetch chunk, released while the caller works
        sizes.append(len(rows))

    api._scan(types.SimpleNamespace(app=env.app), "select scheduled from arrivals", [], fn)
    assert sizes == [7] * 14 + [2]
    assert not api._DB_LOCK.locked()


def test_row_cap_truncates_oldest_rows_and_says_so(env, monkeypatch):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_ROWS", 100)
    for i in range(250):
        env.add(scheduled=T0 + i * 60, delay=i)
    j = env.get("/api/routes/15/summary", min_confidence=0).json()
    assert j["n"] == 100
    assert j["filters"]["truncated"] is True and j["filters"]["since"] == T0 + 150 * 60
    assert j["stops"][0]["min"] == 150  # oldest 150 rows dropped, newest kept
    # an explicit narrower since is respected (not truncated: only 40 rows fit)
    j = env.get("/api/routes/15/summary", since=T0 + 210 * 60, min_confidence=0).json()
    assert j["n"] == 40 and j["filters"]["truncated"] is False and j["filters"]["since"] == T0 + 210 * 60


def test_row_cap_exact_size_not_truncated(env, monkeypatch):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_ROWS", 100)
    for i in range(100):
        env.add(scheduled=T0 + i * 60, delay=i)
    j = env.get("/api/routes/15/summary", min_confidence=0).json()
    assert j["n"] == 100 and j["filters"]["truncated"] is False and j["filters"]["since"] is None


@pytest.mark.parametrize("url", ["/api/routes/15/heatmap", "/api/stops/s1/stats", "/api/routes/15/forecast",
                                 "/api/routes/15/accumulation"])
def test_row_cap_applies_to_every_endpoint(env, monkeypatch, url):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_ROWS", 20)
    env.fake.set_route_stops("15", {"out": [{"id": "s1"}]})
    for i in range(60):
        a = env.add(scheduled=T0 + i * 60, delay=i, trip=f"t{i}")
        env.fc(a, 10, i)
    j = env.get(url).json()
    assert j["filters"]["truncated"] is True and j["filters"]["since"] == T0 + 40 * 60
    if "horizons" in j:
        assert j["horizons"][2]["n"] == 20
    elif "cells" in j or "delay" in j:
        assert j["n"] == 20
    else:
        assert j["groups"][0]["n"] == 20


def test_filters_truncated_false_by_default(env):
    assert env.get("/api/routes/15/forecast").json()["filters"]["truncated"] is False


def test_forecast_matches_reference_computation(env):
    import random
    rnd = random.Random(7)
    rows = []
    for i in range(300):
        a = env.add(scheduled=T0 + i * 77, delay=0, trip=f"t{i}", confidence=rnd.choice([0, 1, 1]))
        for h in (30, 20, 10, 5, 2):
            if rnd.random() < 0.7:
                e = rnd.randint(-400, 900)
                env.fc(a, h, e)
                rows.append((h, e, i))
    got = {h["horizon_min"]: h for h in env.get("/api/routes/15/forecast").json()["horizons"]}
    db = open_meta(env.s.data_dir)
    ok = {r[0] for r in db.execute("select id from arrivals where confidence >= 1")}
    db.close()
    import app.analysis.stats as st
    ref = st.forecast_accuracy([{"horizon_min": h, "error_s": e} for h, e, i in rows if i + 1 in ok])
    for h in (30, 20, 10, 5, 2):
        assert got[h]["n"] == ref[h]["n"]
        for k in ("mean_error", "mae", "p_low", "p_high"):
            assert got[h][k] == pytest.approx(ref[h][k])


def test_accumulation_matches_reference_computation(env):
    import random
    import app.analysis.stats as st
    rnd = random.Random(11)
    ids = [f"s{i}" for i in range(12)]
    env.fake.set_route_stops("15", {"out": [{"id": x} for x in ids]})
    trips = []
    for t in range(60):
        pos = sorted(rnd.sample(range(12), rnd.randint(2, 12)))
        calls, rows = [], []
        for k, ps in enumerate(pos):
            d = rnd.randint(-100, 600)
            calls.append((ids[ps], d))
            rows.append({"stop_id": ids[ps], "seq": ps, "delay": d})
        env.trip(f"t{t}", calls, t0=T0 + t * 10_000)
        trips.append(rows)
    g = acc(env)["groups"]
    assert len(g) == 1 and g[0]["n"] == 60
    ref = {a["seq"]: a for a in st.accumulation(trips)}
    assert [x["position"] for x in g[0]["stops"]] == sorted(ref)
    for x in g[0]["stops"]:
        r = ref[x["position"]]
        assert (x["stop_id"], x["n"], x["n_delta"]) == (r["stop_id"], r["n"], r["n_delta"])
        assert x["mean_delay"] == pytest.approx(r["mean_delay"])
        if r["segment_delta"] is None:
            assert x["segment_delta"] is None
        else:
            assert x["segment_delta"] == pytest.approx(r["segment_delta"])
    assert g[0]["n_delta"] == max(a["n_delta"] for a in ref.values())


MEM_SCRIPT = r"""
import json, resource, sys, time
resource.setrlimit(resource.RLIMIT_AS, (2_500_000 * 1024,) * 2)
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.main import create_app
from app.collect.serverclient import ServerClient
from tests.support.fake_server import FakeServer
fake = FakeServer()
fake.set_route_stops("15", {"out": [{"id": f"s{i}"} for i in range(50)]})
app = create_app(Settings("http://f", Path(sys.argv[2])), start_background=False,
                 client=ServerClient("http://f", fake.client()))
tc = TestClient(app)
tc.get("/api/routes/zz/summary"); tc.get("/api/routes/zz/forecast"); tc.get("/api/routes/zz/accumulation")
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
t = time.perf_counter()
r = tc.get(sys.argv[3])
secs = time.perf_counter() - t
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"status": r.status_code, "delta_mb": (peak - base) / 1024, "secs": secs, "body": r.json()}))
"""


@pytest.fixture(scope="module")
def big_db(tmp_path_factory):
    d = tmp_path_factory.mktemp("big")
    env = Env(d)
    bulk(env, 300_000, batch=20_000)  # 50 stops cycle -> 6000 trips of 50 stops
    db = open_meta(d)
    for lo in range(0, 300_000, 25_000):
        with db:
            db.executemany("insert into forecast values(?,?,?,?)",
                           [(i, h, 0, (i * 7) % 600 - 100) for i in range(lo + 1, lo + 25_001) for h in (30, 10)])
    db.close()
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("url,field", [("/api/routes/15/forecast", "horizons"),
                                       ("/api/routes/15/accumulation", "groups"),
                                       ("/api/routes/15/summary", "stops")])
def test_unwindowed_peak_memory_bounded_300k_rows(big_db, url, field):
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parent.parent.parent)
    out = subprocess.run([sys.executable, "-c", MEM_SCRIPT, root, str(big_db), url], capture_output=True,
                         text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["status"] == 200 and res["body"][field]
    print(url, f"{res['delta_mb']:.0f} MB above baseline, {res['secs']:.1f}s")
    assert res["delta_mb"] < 150, res["delta_mb"]  # old code: ~200+ MB at 300k rows


def test_open_failure_inside_scan_releases_lock_and_connection(env, monkeypatch):
    import app.api.routes as api
    env.add(delay=1)
    real, calls = api.open_meta, []

    def boom(d):
        calls.append(1)
        if len(calls) == 2:  # 1st = cap probe (_fetch), 2nd = the bulk _scan
            raise RuntimeError("cannot open")
        return real(d)

    monkeypatch.setattr(api, "open_meta", boom)
    with pytest.raises(RuntimeError):
        env.get("/api/stops/s1/stats")
    assert not api._DB_LOCK.locked()
    assert env.get("/api/stops/s1/stats").json()["n"] == 1


# ---------- H3: previously surviving mutants ----------

def test_heatmap_multi_chunk_equals_unchunked(env, monkeypatch):
    import app.api.routes as api
    for i in range(30):  # 10-minute steps: every hour cell gets 6 rows spread across several chunks
        env.add(delay=i * 10, scheduled=T0 + i * 600)
    whole = env.get("/api/routes/15/heatmap").json()
    assert whole["n"] == 30 and sum(c["n"] for c in whole["cells"]) == 30
    monkeypatch.setattr(api, "CHUNK", 7)
    assert env.get("/api/routes/15/heatmap").json() == whole
    exp = {}
    for i in range(30):
        d = datetime.fromtimestamp(T0 + i * 600, ZoneInfo("Europe/Dublin"))
        exp.setdefault((d.weekday(), d.hour), []).append(i * 10)
    assert {(c["weekday"], c["hour"]): (c["n"], c["mean_delay"]) for c in whole["cells"]} == {
        k: (len(v), sum(v) / len(v)) for k, v in exp.items()}


def _utc(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp())


def test_forecast_weekday_hour_filters_incl_dst_edge(env):
    # Dublin clocks go 01:00Z -> 02:00 local on Sun 2026-03-29
    rows = {"sat23": _utc(2026, 3, 28, 23, 30),   # Sat 23h
            "sun0": _utc(2026, 3, 29, 0, 30),     # Sun 0h (GMT)
            "sun2": _utc(2026, 3, 29, 1, 30),     # Sun 2h (BST; hour 1 does not exist)
            "mon0": _utc(2026, 3, 29, 23, 30)}    # Mon 0h (BST)
    for sch in rows.values():
        env.fc(env.add(delay=0, scheduled=sch), 5, 10)
    n = lambda **q: env.get("/api/routes/15/forecast", **q).json()["horizons"][3]["n"]
    assert n() == 4
    assert n(hours="2") == 1 and n(hours="1") == 0 and n(hours="0") == 2 and n(hours="23") == 1
    assert n(weekdays="6") == 2 and n(weekdays="0") == 1 and n(weekdays="5") == 1 and n(weekdays="1") == 0
    assert n(weekdays="6", hours="0") == 1 and n(weekdays="0,5", hours="0,23") == 2


def test_route_stops_failure_not_cached_counts_refetch(env):
    env.keep_cache = True
    env.fake.set_route_stops("15", {"out": [{"id": "a"}]})
    env.fake.set_down(True)
    assert env.get("/api/routes/15/stops").status_code == 502
    after_fail = stops_hits(env)
    assert after_fail == 0  # the fake records nothing while down
    env.fake.set_down(False)
    assert env.get("/api/routes/15/stops").json()["directions"] == {"out": [{"id": "a"}]}
    assert stops_hits(env) == after_fail + 1  # failure was not cached: refetched
    env.get("/api/routes/15/stops")
    assert stops_hits(env) == after_fail + 1  # success is cached


def test_cap_probe_ignores_null_delay_rows(env, monkeypatch):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_ROWS", 5)
    for i in range(12):
        env.add(delay=None, scheduled=T0 + i)
    for i in range(3):
        env.add(delay=i * 10, scheduled=T0 + 100 + i)
    j = env.get("/api/routes/15/heatmap").json()
    assert j["filters"]["truncated"] is False and j["n"] == 3
    s = env.get("/api/routes/15/summary").json()
    assert s["filters"]["truncated"] is False and s["n"] == 3


# ---------- H2: heavy-scan gate (asyncio, waits on the event loop) ----------

def _heavy_env(env, monkeypatch, slots=1):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_HEAVY_SCANS", slots)  # the gate is created lazily per event loop
    monkeypatch.setattr(api, "HEAVY_ROWS", 3)
    for i in range(5):  # route 15 is heavy (5 > 3 rows); route 16 is light
        env.add(delay=i, scheduled=T0 + i)
    env.add(route="16", delay=1, scheduled=T0)
    return api


def _limiter(env, n):
    """Set the anyio default thread-limiter tokens of the client's (shared) loop; returns the old value."""
    import anyio.to_thread

    async def set_(n):
        lim = anyio.to_thread.current_default_thread_limiter()
        old, lim.total_tokens = lim.total_tokens, n
        return old
    return env.c.portal.call(set_, n)


def _gate_value(env, name="heavy"):
    import app.api.routes as api

    async def val():
        return api._gate(name)._value
    return env.c.portal.call(val)


def _run(fn, *a):
    out = []
    t = threading.Thread(target=lambda: out.append(fn(*a)), daemon=True)
    t.start()
    return t, out


@contextlib.contextmanager
def _shared_loop(env):
    """`with env.c:` (one event loop for all client threads) that re-raises a body failure only AFTER the
    portal has shut down cleanly: raising through the TestClient exit can hang on teardown."""
    err = []
    with env.c:
        try:
            yield
        except BaseException as e:  # noqa: BLE001 - re-raised below
            err.append(e)
    if err:
        raise err[0]


def test_gates_are_created_lazily_per_event_loop(monkeypatch):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_HEAVY_SCANS", 3)

    async def gates():
        return api._gate("heavy"), api._gate("heavy"), api._gate("gz")

    a1, a2, g1 = asyncio.run(gates())
    b1, _, g2 = asyncio.run(gates())
    assert a1 is a2 and a1 is not b1 and g1 is not g2 and a1 is not g1  # per loop, per name
    assert (a1._value, g1._value) == (3, 1)  # sizes: MAX_HEAVY_SCANS and 1


def _get(fn, *a, timeout=5):
    """Call fn in a helper thread; returns None when it does not finish in time (fail fast, no hang). Callers
    assert after the `with _shared_loop(env):` block: raising through the TestClient portal exit can hang on teardown."""
    t, o = _run(fn, *a)
    t.join(timeout)
    return None if t.is_alive() else o[0]  # None = starved; asserted by the caller AFTER leaving `with env.c`


def test_heavy_requests_are_serialised_by_the_gate(env, monkeypatch):
    api = _heavy_env(env, monkeypatch, slots=1)
    log, gate, first_in = [], threading.Event(), threading.Event()
    orig = api._scan

    def spy(request, sql, args, fn, size=None):
        log.append("start")
        first_in.set()
        if len(log) == 1:
            assert gate.wait(10)
        orig(request, sql, args, fn, size)
        log.append("end")

    monkeypatch.setattr(api, "_scan", spy)
    with _shared_loop(env):
        t1, o1 = _run(env.c.get, "/api/routes/15/heatmap")
        assert first_in.wait(10)
        t2, o2 = _run(env.c.get, "/api/routes/15/forecast")
        time.sleep(0.5)
        parked = list(log)
        gate.set()
        t1.join(15), t2.join(15)
    assert parked == ["start"]  # second heavy request is parked on the gate
    assert log == ["start", "end", "start", "end"]
    assert o1[0].status_code == 200 and o2[0].status_code == 200


def test_queued_heavy_requests_do_not_starve_light_ones_or_health(env, monkeypatch):
    """Reviewer probe: gate 2, only 4 worker threads, 6 slow heavy requests. Waiting requests must hold no
    thread (a thread-based wait would leave nothing for the light request until a heavy one finishes)."""
    api = _heavy_env(env, monkeypatch, slots=2)
    gate, inside = threading.Event(), threading.Semaphore(0)
    orig = api._scan

    def spy(request, sql, args, fn, size=None):
        if args[0] == "15":
            inside.release()
            assert gate.wait(15)
        orig(request, sql, args, fn, size)

    monkeypatch.setattr(api, "_scan", spy)
    with _shared_loop(env):
        old = _limiter(env, 4)
        try:
            runs = [_run(env.c.get, "/api/routes/15/heatmap") for _ in range(6)]
            assert inside.acquire(timeout=10) and inside.acquire(timeout=10)  # both permits taken
            time.sleep(0.3)  # the other four are queued
            t = time.perf_counter()
            light = _get(env.c.get, "/api/routes/16/heatmap")
            health = _get(env.c.get, "/api/health")
            took = time.perf_counter() - t
            third = inside.acquire(timeout=0.01)  # must stay False: only two running
            gate.set()
            for th, _ in runs:
                th.join(20)
        finally:
            gate.set()
            _limiter(env, old)
    assert light is not None and health is not None, "light request or health starved"
    assert light.status_code == 200 and light.json()["n"] == 1 and health.json() == {"ok": True}
    assert took < 1.0, took
    assert not third
    assert [o[0].status_code for _, o in runs] == [200] * 6 and all(o[0].json()["n"] == 5 for _, o in runs)


def test_health_is_a_coroutine_and_answers_with_all_workers_busy(env, monkeypatch):
    import inspect
    api = _heavy_env(env, monkeypatch, slots=2)
    ep = next(r.endpoint for r in env.app.routes if getattr(r, "path", "") == "/api/health")
    assert inspect.iscoroutinefunction(ep)
    gate, inside = threading.Event(), threading.Semaphore(0)
    orig = api._scan

    def spy(request, sql, args, fn, size=None):
        inside.release()
        assert gate.wait(15)
        orig(request, sql, args, fn, size)

    monkeypatch.setattr(api, "_scan", spy)
    with _shared_loop(env):
        old = _limiter(env, 2)  # exactly the two running heavy requests
        try:
            runs = [_run(env.c.get, "/api/routes/15/heatmap") for _ in range(2)]
            assert inside.acquire(timeout=10) and inside.acquire(timeout=10)
            health = _get(env.c.get, "/api/health")  # a sync health would starve
        finally:
            gate.set()
            for th, _ in runs:
                th.join(20)
            _limiter(env, old)
    assert health is not None and health.json() == {"ok": True}


def test_heavy_slot_released_on_error(env, monkeypatch):
    api = _heavy_env(env, monkeypatch, slots=1)
    orig = api._scan
    calls = []

    def boom(request, sql, args, fn, size=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("scan failed")
        orig(request, sql, args, fn, size)

    monkeypatch.setattr(api, "_scan", boom)
    with _shared_loop(env):
        with pytest.raises(RuntimeError):
            env.c.get("/api/routes/15/heatmap")
        assert _gate_value(env) == 1  # permit back
        assert env.c.get("/api/routes/15/heatmap").status_code == 200
        assert _gate_value(env) == 1


async def test_heavy_cancelled_while_waiting_leaks_no_permit(env, monkeypatch):
    import app.api.routes as api
    _heavy_env(env, monkeypatch, slots=1)
    req = types.SimpleNamespace(app=env.app)
    running, release = threading.Event(), threading.Event()

    def work(p):
        running.set()
        assert release.wait(10)
        return "done"

    t1 = asyncio.create_task(api._heavy(req, api._params(), "route = ?", ["15"], work))
    while not running.is_set():
        await asyncio.sleep(0.01)
    t2 = asyncio.create_task(api._heavy(req, api._params(), "route = ?", ["15"], lambda p: "never"))
    await asyncio.sleep(0.1)  # t2 waits on the gate
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2
    assert api._gate("heavy")._value == 0  # t1 still holds its permit; t2 took none and released none
    release.set()
    assert await t1 == "done"
    assert api._gate("heavy")._value == 1
    assert await api._heavy(req, api._params(), "route = ?", ["15"], lambda p: "again") == "again"
    assert api._gate("heavy")._value == 1
    assert await api._heavy(req, api._params(), "route = ?", ["16"], lambda p: p["truncated"]) is False  # light


# ---------- trip series ----------

S = T0  # 2025-10-09 08:53:20 UTC
SERIES_KEYS = {"trip_id", "stop_id", "truncated", "visits"}
VISIT_KEYS = {"seq", "scheduled", "arrival", "last_seen_ts", "points"}
ARRIVAL_KEYS = {"actual", "delay", "confidence", "realtime", "service_date"}
POINT_KEYS = {"ts", "due_min", "expected", "realtime"}


def put_samples(env, rows, trip="T", stop="s1"):
    """rows = (seq, ts, due_min, expected, realtime); each goes to the UTC month file of its ts."""
    from app.core.db import open_samples
    for seq, ts, due, exp, rt in rows:
        c = open_samples(env.s.data_dir, ts)
        with c:
            c.execute("insert into sample values(?,?,?,?,?,?,?)", (ts, trip, stop, seq, due, exp, rt))
        c.close()


def series(env, trip="T", stop="s1", **q):
    return env.get(f"/api/trips/{quote(trip, safe='')}/series", **({"stop": stop} | q))


def check_shape(j):
    finite(j)
    assert set(j) == SERIES_KEYS
    for v in j["visits"]:
        assert set(v) == VISIT_KEYS
        assert v["arrival"] is None or set(v["arrival"]) == ARRIVAL_KEYS
        assert all(set(p) == POINT_KEYS for p in v["points"])
        assert [p["ts"] for p in v["points"]] == sorted(p["ts"] for p in v["points"])
    assert [v["seq"] for v in j["visits"]] == sorted(v["seq"] for v in j["visits"])


@pytest.fixture
def tmp_private(tmp_path, monkeypatch):
    import tempfile
    d = tmp_path / "tmpdir"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


COUNTDOWN = [(S, S - 1500, 25, S, 1), (S, S - 600, 10, S + 30, 1), (S, S + 40, 0, S + 40, 1)]


def seed_countdown(env):
    env.add(trip="T", stop="s1", scheduled=S, delay=40)
    put_samples(env, COUNTDOWN)


@pytest.mark.filterwarnings("error::ResourceWarning")
def test_trip_series_returns_due_reaching_zero(env, tmp_private):
    seed_countdown(env)
    r = series(env)
    assert r.status_code == 200
    j = r.json()
    check_shape(j)
    assert j == {"trip_id": "T", "stop_id": "s1", "truncated": False, "visits": [{
        "seq": S, "scheduled": S, "last_seen_ts": S + 40,
        "arrival": {"actual": S + 40, "delay": 40, "confidence": 1, "realtime": 1, "service_date": "2026-01-01"},
        "points": [{"ts": t, "due_min": d, "expected": e, "realtime": rt} for _, t, d, e, rt in COUNTDOWN]}]}
    for p in j["visits"][0]["points"]:  # the documented reconstruction reproduces the stored due
        assert max(0, (p["expected"] - p["ts"]) // 60) == p["due_min"]
    assert list(tmp_private.iterdir()) == []


def test_trip_series_loop_trip_has_two_visits(env):
    env.add(trip="T", stop="s1", scheduled=S, delay=0)
    env.add(trip="T", stop="s1", scheduled=S + 1800, delay=60)
    put_samples(env, [(S + 1800, S + 1000, 13, S + 1800, 1), (S, S - 60, 1, S + 10, 1), (S, S + 5, 0, S + 5, 1)])
    j = series(env).json()
    check_shape(j)
    assert [(v["seq"], len(v["points"]), v["arrival"]["delay"]) for v in j["visits"]] == [(S, 2, 0), (S + 1800, 1, 60)]


def test_trip_series_same_trip_two_service_dates_and_filters(env):
    d2 = S + 86400
    env.add(trip="T", stop="s1", scheduled=S, delay=0, date="2026-01-01")
    env.add(trip="T", stop="s1", scheduled=d2, delay=5, date="2026-01-02")
    put_samples(env, [(S, S - 60, 1, S, 1), (d2, d2 - 60, 2, d2 + 60, 1)])
    both = series(env).json()
    assert [v["seq"] for v in both["visits"]] == [S, d2]
    assert [v["arrival"]["service_date"] for v in both["visits"]] == ["2026-01-01", "2026-01-02"]
    for q, expect in (({"seq": str(d2)}, [d2]), ({"seq": str(S)}, [S]), ({"since": str(S + 1)}, [d2]),
                      ({"until": str(S)}, [S]), ({"since": str(S), "until": str(d2)}, [S, d2])):
        j = series(env, **q).json()
        assert [v["seq"] for v in j["visits"]] == expect, q
        assert all(len(v["points"]) == 1 for v in j["visits"])
    assert series(env, seq=str(S + 1)).status_code == 404  # nothing matched
    assert series(env, since=str(d2 + 1)).status_code == 404


@pytest.mark.filterwarnings("error::ResourceWarning")
def test_trip_series_reads_gzipped_month(env, tmp_private):
    from app.backup.backup import compact_closed_months
    seed_countdown(env)
    assert compact_closed_months(env.s.data_dir, S + 90 * 86400)
    assert not list(env.s.data_dir.glob("samples-*.db")) and list(env.s.data_dir.glob("samples-*.db.gz"))
    j = series(env).json()
    assert [p["ts"] for p in j["visits"][0]["points"]] == [S - 1500, S - 600, S + 40]
    assert list(tmp_private.iterdir()) == []  # temp decompressed copy removed
    assert not list(env.s.data_dir.glob("samples-*.db"))  # and never created in the data dir


@pytest.mark.filterwarnings("error::ResourceWarning")
def test_trip_series_spans_month_boundary(env, tmp_private):
    b = _utc(2025, 10, 1)  # visit scheduled just after midnight UTC
    env.add(trip="T", stop="s1", scheduled=b + 600, delay=30)
    put_samples(env, [(b + 600, b - 900, 25, b + 600, 1), (b + 600, b + 100, 8, b + 620, 1),
                      (b + 600, b + 630, 0, b + 630, 1)])
    assert {p.name for p in env.s.data_dir.glob("samples-*.db")} == {"samples-2025-09.db", "samples-2025-10.db"}
    j = series(env).json()
    check_shape(j)
    assert [p["ts"] for p in j["visits"][0]["points"]] == [b - 900, b + 100, b + 630]
    assert j["visits"][0]["last_seen_ts"] == b + 630 and j["truncated"] is False
    assert list(tmp_private.iterdir()) == []


def test_trip_series_missing_month_file_skipped(env, monkeypatch):
    import app.api.routes as api
    env.add(trip="T", stop="s1", scheduled=S, delay=0)  # arrival, no samples file at all
    j = series(env).json()
    assert j["visits"][0]["points"] == [] and j["visits"][0]["last_seen_ts"] is None
    put_samples(env, COUNTDOWN)

    def gone(*a):  # file vanished between the directory scan and the open
        raise FileNotFoundError("x")
    monkeypatch.setattr(api, "open_samples_readonly", gone)
    assert series(env).json()["visits"][0]["points"] == []


@pytest.mark.parametrize("kind", ["plain", "gz", "truncated_gz"])
@pytest.mark.filterwarnings("error::ResourceWarning")
def test_trip_series_corrupt_month_skipped_with_warning(env, tmp_private, caplog, kind):
    import gzip
    b = _utc(2025, 10, 1)
    env.add(trip="T", stop="s1", scheduled=b + 600, delay=30)
    put_samples(env, [(b + 600, b + 100, 8, b + 620, 1), (b + 600, b + 630, 0, b + 630, 1)])
    d = env.s.data_dir
    if kind == "plain":
        (d / "samples-2025-09.db").write_bytes(b"garbage" * 500)
    elif kind == "gz":
        (d / "samples-2025-09.db.gz").write_bytes(b"not gzip at all")
    else:
        good = gzip.compress(b"x" * 100_000)
        (d / "samples-2025-09.db.gz").write_bytes(good[: len(good) // 2])
    with caplog.at_level("WARNING", logger="app.api.routes"):
        r = series(env)
    assert r.status_code == 200
    assert [p["ts"] for p in r.json()["visits"][0]["points"]] == [b + 100, b + 630]
    assert any("samples month 2025-09" in m for m in caplog.messages)
    assert list(tmp_private.iterdir()) == []


def test_trip_series_unknown_and_validation(env):
    seed_countdown(env)
    assert series(env, trip="nope").status_code == 404
    assert series(env, stop="nope").status_code == 404
    assert env.get("/api/trips/T/series").status_code == 422  # stop missing
    assert series(env, stop="").status_code == 422
    assert series(env, stop="s" * 65).status_code == 422
    assert series(env, stop="s" * 64).status_code == 404  # exact limit passes validation
    assert env.get("/api/trips/" + "t" * 65 + "/series", stop="s1").status_code == 422
    assert series(env, seq="abc").status_code == 422
    assert series(env, seq="1.5").status_code == 422
    assert series(env, since="x").status_code == 422
    assert series(env, since="10", until="9").status_code == 422
    assert series(env, seq=str(S)).status_code == 200


@pytest.mark.parametrize("bad", ["x' OR '1'='1", "'; drop table arrivals;--", "T\" or 1=1 --", "%", "_", "a/b"])
def test_trip_series_sql_injection_strings(env, bad):
    seed_countdown(env)
    assert series(env, trip=bad).status_code == 404
    assert series(env, stop=bad).status_code == 404
    for q in ({"seq": bad}, {"since": bad}, {"until": bad}):
        assert series(env, **q).status_code == 422
    db = open_meta(env.s.data_dir)
    assert db.execute("select count(*) from arrivals").fetchone() == (1,)
    db.close()
    assert len(series(env).json()["visits"][0]["points"]) == 3


def test_trip_series_slash_in_trip_id(env):
    env.add(trip="a/b", stop="s1", scheduled=S, delay=0)
    put_samples(env, [(S, S, 0, S, 1)], trip="a/b")
    assert len(series(env, trip="a/b").json()["visits"][0]["points"]) == 1


def test_trip_series_dropped_bus_has_no_arrival_and_last_seen(env):
    put_samples(env, [(S, S - 1500, 25, S, 1), (S, S - 900, 20, S + 120, 0)])  # vanished at due 20: no arrivals row
    j = series(env).json()
    check_shape(j)
    v = j["visits"][0]
    assert v["arrival"] is None and v["scheduled"] == S and v["last_seen_ts"] == S - 900
    assert [p["realtime"] for p in v["points"]] == [1, 0]


def test_trip_series_without_arrivals_uses_recent_months_or_since_until(env):
    for m, (y, mo) in enumerate([(2025, 7), (2025, 8), (2025, 9)]):
        t = _utc(y, mo, 15)
        put_samples(env, [(t, t - 60, 1, t, 1)])
    j = series(env).json()  # no since/until: the two most recent months on disk
    assert [v["seq"] for v in j["visits"]] == [_utc(2025, 8, 15), _utc(2025, 9, 15)] and j["truncated"] is True
    j = series(env, until=str(_utc(2025, 7, 20))).json()
    assert [v["seq"] for v in j["visits"]] == [_utc(2025, 7, 15)] and j["truncated"] is False
    j = series(env, since=str(_utc(2025, 8, 1)), until=str(_utc(2025, 8, 31))).json()
    assert [v["seq"] for v in j["visits"]] == [_utc(2025, 8, 15)]
    assert series(env, since=str(_utc(2025, 12, 1))).status_code == 404


def test_trip_series_ignores_foreign_files_in_data_dir(env):
    seed_countdown(env)
    for n in ("samples-2025-13.db", "samples-2025-1.db", "samples-2025-10.db.bak", "xsamples-2025-10.db",
              "samples-2025-10.db.gz.tmp"):
        (env.s.data_dir / n).write_bytes(b"junk")
    assert len(series(env).json()["visits"][0]["points"]) == 3


def test_trip_series_truncation_caps(env, monkeypatch):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_VISITS", 2)
    monkeypatch.setattr(api, "MAX_POINTS", 3)
    for i in range(3):
        env.add(trip="T", stop="s1", scheduled=S + i * 600, delay=0)
    put_samples(env, [(S + 600, S - 100 + k, 1, S, 1) for k in range(5)])
    j = series(env).json()
    assert j["truncated"] is True and [v["seq"] for v in j["visits"]] == [S + 600, S + 1200]  # most recent
    v = j["visits"][0]
    assert [p["ts"] for p in v["points"]] == [S - 100, S - 99, S - 98] and v["last_seen_ts"] == S - 96  # earliest kept
    # a newer sample-only visit evicts the oldest kept one
    put_samples(env, [(S + 5000, S + 4000, 3, S + 5000, 1)])
    assert series(env, seq=str(S + 5000)).json()["visits"][0]["seq"] == S + 5000  # alone it fits
    j = series(env).json()
    assert j["truncated"] is True and [v["seq"] for v in j["visits"]] == [S + 1200, S + 5000]
    monkeypatch.undo()
    j = series(env, seq=str(S + 600)).json()
    assert j["truncated"] is False and len(j["visits"][0]["points"]) == 5


def test_trip_series_sample_only_visit_over_cap_flagged(env, monkeypatch):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_VISITS", 2)
    put_samples(env, [(S + i, S + i, 1, S + i, 1) for i in range(3)])
    j = series(env).json()
    assert j["truncated"] is True and [v["seq"] for v in j["visits"]] == [S + 1, S + 2]
    # an OLDER visit first seen in a later month file, once the visits are full, is dropped
    P = _utc(2025, 9, 15)
    put_samples(env, [(P + 1, P + 1, 1, P, 1), (P + 2, P + 2, 1, P, 1), (P - 5, _utc(2025, 10, 2), 1, P, 1)],
                trip="U")
    j = series(env, trip="U").json()
    assert j["truncated"] is True and [v["seq"] for v in j["visits"]] == [P + 1, P + 2]


def test_trip_series_month_cap(env, monkeypatch):
    import app.api.routes as api
    monkeypatch.setattr(api, "MAX_SERIES_MONTHS", 1)
    b = _utc(2025, 10, 1)
    env.add(trip="T", stop="s1", scheduled=b + 600, delay=0)  # candidate months: Sep and Oct
    put_samples(env, [(b + 600, b - 900, 25, b + 600, 1), (b + 600, b + 100, 8, b + 620, 1)])
    j = series(env).json()
    assert j["truncated"] is True and [p["ts"] for p in j["visits"][0]["points"]] == [b + 100]


def test_trip_series_other_trip_and_stop_samples_not_mixed(env):
    seed_countdown(env)
    put_samples(env, [(S, S, 9, S, 1)], trip="T2")
    put_samples(env, [(S, S, 9, S, 1)], stop="s2")
    assert len(series(env).json()["visits"][0]["points"]) == 3


def test_scan_open_failure_propagates_without_close(env, monkeypatch):
    import app.api.routes as api

    def boom(_):
        raise sqlite3.OperationalError("cannot open")
    monkeypatch.setattr(api, "open_meta", boom)
    req = types.SimpleNamespace(app=env.app)
    with pytest.raises(sqlite3.OperationalError):
        api._scan(req, "select 1", [], lambda rows: None)
    assert api._DB_LOCK.acquire(blocking=False)  # lock released
    api._DB_LOCK.release()


# ---------- fix round 1: series selection, clamping, gz gate ----------

def put_many(env, rows, trip="T", stop="s1"):
    from app.core.db import open_samples, samples_path
    by_month = {}
    for r in rows:
        by_month.setdefault(samples_path(env.s.data_dir, r[1]), []).append(r)
    for path, rs in by_month.items():
        c = open_samples(env.s.data_dir, rs[0][1])
        with c:
            c.executemany("insert into sample values(?,?,?,?,?,?,?)",
                          [(ts, trip, stop, sq, d, e, rt) for sq, ts, d, e, rt in rs])
        c.close()


def daily(env, start, days, hour=8):
    """One arrival + 2 samples per day for the same trip at s1; returns the seqs."""
    seqs = []
    for i in range(days):
        sq = start + i * 86400 + hour * 3600
        env.add(trip="T", stop="s1", scheduled=sq, delay=0)
        put_many(env, [(sq, sq - 600, 10, sq, 1), (sq, sq + 10, 0, sq + 10, 1)])
        seqs.append(sq)
    return seqs


def test_series_default_returns_most_recent_visits_with_points(env):
    seqs = daily(env, _utc(2026, 1, 15), 60)  # 15 Jan .. 15 Mar, 3 month files
    j = series(env).json()
    check_shape(j)
    got = [v["seq"] for v in j["visits"]]
    assert got == [q for q in seqs if q >= _utc(2026, 2, 1)] and len(got) == 43  # Feb + Mar fit; January dropped
    assert got[-1] == seqs[-1]  # the newest is present
    assert all(len(v["points"]) == 2 for v in j["visits"])  # none empty merely for an unread month
    assert j["truncated"] is True


def test_series_arrivals_in_three_months_keeps_newest_two_months(env):
    seqs = [_utc(2026, m, 15, 8) for m in (1, 2, 3)]
    for q in seqs:
        env.add(trip="T", stop="s1", scheduled=q, delay=0)
        put_many(env, [(q, q, 0, q, 1)])
    j = series(env).json()
    assert [v["seq"] for v in j["visits"]] == seqs[1:] and j["truncated"] is True
    assert all(len(v["points"]) == 1 for v in j["visits"])
    assert [v["seq"] for v in series(env, seq=str(seqs[0])).json()["visits"]] == seqs[:1]  # still reachable


def test_series_sample_only_visit_sorts_before_later_arrivals_visit(env):
    seed_countdown(env)
    put_many(env, [(S - 7200, S - 7500, 5, S - 7200, 1)])  # dropped bus, earlier, same month, no arrivals row
    j = series(env).json()
    check_shape(j)
    assert [(v["seq"], v["arrival"] is None) for v in j["visits"]] == [(S - 7200, True), (S, False)]


def test_series_exact_caps_not_truncated_but_one_more_is(env):
    for i in range(50):
        env.add(trip="T", stop="s1", scheduled=S + i, delay=0)
    j = series(env).json()
    assert len(j["visits"]) == 50 and j["truncated"] is False
    env.add(trip="T", stop="s1", scheduled=S + 50, delay=0)
    j = series(env).json()
    assert [v["seq"] for v in j["visits"]] == [S + i for i in range(1, 51)] and j["truncated"] is True


def test_series_exact_point_cap_and_points_only_truncation(env):
    put_many(env, [(S, S - 5000 + i, 1, S, 1) for i in range(2000)])
    j = series(env).json()
    assert len(j["visits"][0]["points"]) == 2000 and j["truncated"] is False
    put_many(env, [(S, S - 5000 + 2000, 1, S, 1)])
    j = series(env).json()  # 2001 rows: one visit, only the points are cut
    assert len(j["visits"]) == 1 and len(j["visits"][0]["points"]) == 2000
    assert j["truncated"] is True and j["visits"][0]["last_seen_ts"] == S - 3000


def test_series_exact_seq_finds_old_sample_only_visit_beyond_two_newer_months(env):
    feb = _utc(2026, 2, 10, 8)
    put_many(env, [(feb, feb, 5, feb, 1)])
    for m in (3, 4):
        q = _utc(2026, m, 10, 8)
        put_many(env, [(q, q, 5, q, 1)])
    assert [v["seq"] for v in series(env).json()["visits"]] == [_utc(2026, 3, 10, 8), _utc(2026, 4, 10, 8)]
    j = series(env, seq=str(feb)).json()
    assert [v["seq"] for v in j["visits"]] == [feb] and len(j["visits"][0]["points"]) == 1


@pytest.mark.parametrize("q", [{"since": "999999999999"}, {"until": "-999999999999"}, {"since": "-999999999999"},
                               {"until": "999999999999"}, {"seq": "999999999999"}, {"seq": "0"},
                               {"seq": "253402300800"}, {"seq": "-62135596801"}, {"seq": "-999999999999"},
                               {"since": "-62135596801", "until": "253402300800"}])
@pytest.mark.parametrize("with_data", [False, True])
def test_series_out_of_range_epochs_never_500(env, q, with_data):
    if with_data:
        seed_countdown(env)
    r = series(env, **q)
    assert r.status_code in (200, 404), (q, r.text)
    if r.status_code == 200:
        check_shape(r.json())


def test_series_out_of_range_db_values_do_not_crash(env):
    env.add(trip="T", stop="s1", scheduled=10**12, delay=0, seq=10**12)  # nonsense epoch already in the db
    assert series(env).status_code == 200


def gz_two_months(env):
    """Oct 2025 gzipped (closed), Nov 2025 plain; returns (oct seq, nov seq)."""
    from app.backup.backup import compact_closed_months
    o, n = _utc(2025, 10, 10, 8), _utc(2025, 11, 10, 8)
    for q, trip in ((o, "G"), (n, "P")):
        env.add(trip=trip, stop="s1", scheduled=q, delay=0)
        put_many(env, [(q, q - 60, 1, q, 1)], trip=trip)
    assert len(compact_closed_months(env.s.data_dir, _utc(2025, 11, 5))) == 1
    assert (env.s.data_dir / "samples-2025-11.db").exists()
    return o, n


@pytest.mark.filterwarnings("error::ResourceWarning")
def test_series_gz_reads_are_serialised_and_leave_no_temp(env, monkeypatch, tmp_private):
    import app.api.routes as api
    gz_two_months(env)
    log, gate, first_in = [], threading.Event(), threading.Event()
    orig = api._read_month

    def spy(data_dir, m, *a):
        if m != 2025 * 12 + 9:  # only the gz month (October); the newest plain month is read too
            return orig(data_dir, m, *a)
        log.append("start")
        first_in.set()
        if len(log) == 1:
            assert gate.wait(10)
        orig(data_dir, m, *a)
        log.append("end")

    monkeypatch.setattr(api, "_read_month", spy)
    with _shared_loop(env):
        t1, o1 = _run(series, env, "G")
        assert first_in.wait(10)
        t2, o2 = _run(series, env, "G")
        time.sleep(0.5)
        parked = list(log)
        gate.set()
        t1.join(15), t2.join(15)
    assert parked == ["start"]  # second gz read waits on the loop
    assert log == ["start", "end", "start", "end"]
    assert [o[0].status_code for o in (o1, o2)] == [200, 200]
    assert all(len(o[0].json()["visits"][0]["points"]) == 1 for o in (o1, o2))
    assert list(tmp_private.iterdir()) == []


def test_series_plain_month_not_blocked_by_gz_read(env, monkeypatch):
    import app.api.routes as api
    gz_two_months(env)
    gate, first_in = threading.Event(), threading.Event()
    orig = api._read_month

    def spy(data_dir, m, *a):
        if m == 2025 * 12 + 9:  # October, the gz month
            first_in.set()
            assert gate.wait(10)
        orig(data_dir, m, *a)

    monkeypatch.setattr(api, "_read_month", spy)
    with _shared_loop(env):
        t1, o1 = _run(series, env, "G")
        assert first_in.wait(10)
        t = time.perf_counter()
        r = _get(series, env, "P")
        took, still_running = time.perf_counter() - t, t1.is_alive()
        gate.set()
        t1.join(15)
    assert r is not None, "plain-month series blocked by a gz read"
    assert r.status_code == 200 and len(r.json()["visits"][0]["points"]) == 1
    assert took < 1.0 and still_running
    assert o1[0].status_code == 200


# ---------- fix round 2 ----------

def test_series_newest_sample_only_visit_in_newer_month_is_returned(env):
    jan, mar = _utc(2026, 1, 10, 8), _utc(2026, 3, 10, 8)
    env.add(trip="T", stop="s1", scheduled=jan, delay=0)
    put_many(env, [(jan, jan, 0, jan, 1), (mar, mar - 600, 12, mar, 1)])  # March: dropped bus, no arrivals row
    j = series(env).json()
    assert [v["seq"] for v in j["visits"]] == [jan, mar] and j["truncated"] is True  # newest month may hide more
    assert j["visits"][1]["arrival"] is None and len(j["visits"][1]["points"]) == 1


def test_series_newest_disk_month_selected_is_not_truncated(env):
    q = _utc(2026, 3, 10, 8)
    env.add(trip="T", stop="s1", scheduled=q, delay=0)
    put_many(env, [(q, q, 0, q, 1)])
    put_many(env, [(q, q, 0, q, 1)], trip="other")
    assert series(env).json()["truncated"] is False
    assert series(env, seq=str(q)).json()["truncated"] is False


def test_series_newer_month_pinned_by_filters_is_not_flagged(env):
    jan = _utc(2026, 1, 10, 8)
    env.add(trip="T", stop="s1", scheduled=jan, delay=0)
    put_many(env, [(jan, jan, 0, jan, 1), (_utc(2026, 3, 10), _utc(2026, 3, 10), 0, 0, 1)], trip="other")
    assert series(env, since=str(jan - 1)).json()["truncated"] is False


def test_series_newest_month_not_added_when_visit_needs_two_months(env):
    b = _utc(2025, 10, 1)  # visit spans Sep+Oct; a Dec file exists too: no room for it
    env.add(trip="T", stop="s1", scheduled=b + 600, delay=0)
    put_many(env, [(b + 600, b - 900, 25, b + 600, 1), (b + 600, b + 100, 8, b + 620, 1)])
    put_many(env, [(1, _utc(2025, 12, 5), 0, 1, 1)], trip="other")
    j = series(env).json()
    assert [len(v["points"]) for v in j["visits"]] == [2] and j["truncated"] is True


@pytest.mark.parametrize("name", ["samples-0000-01.db", "samples-9999-12.db", "samples-1969-12.db",
                                  "samples-0000-01.db.gz"])
def test_series_ignores_out_of_range_year_files(env, name):
    seed_countdown(env)
    (env.s.data_dir / name).write_bytes(b"junk")
    r = series(env)
    assert r.status_code == 200 and len(r.json()["visits"][0]["points"]) == 3


def test_series_upper_margin_pulls_in_next_month(env):
    q = _utc(2025, 10, 31, 23, 55)  # arrival without an actual: only scheduled + 6 h reaches November
    env.add(trip="T", stop="s1", scheduled=q, delay=None)
    put_many(env, [(q, q - 900, 12, q, 1), (q, q + 1800, 0, q + 1800, 1)])  # second sample is in the Nov file
    put_many(env, [(1, _utc(2025, 12, 5), 0, 1, 1)], trip="other")  # newer month: Nov is not "the newest"
    assert {p.name for p in env.s.data_dir.glob("samples-*.db")} == {
        "samples-2025-10.db", "samples-2025-11.db", "samples-2025-12.db"}
    j = series(env).json()
    assert [p["ts"] for p in j["visits"][0]["points"]] == [q - 900, q + 1800]


async def test_no_deadlock_under_mixed_concurrency(env, monkeypatch):
    import anyio.to_thread
    api = _heavy_env(env, monkeypatch, slots=2)
    urls = ["/api/routes/15/heatmap", "/api/routes/16/heatmap", "/api/routes/15/forecast",
            "/api/routes/15/summary", "/api/stops/s1/stats", "/api/routes/16/forecast",
            "/api/routes/15/accumulation", "/api/trips/x/series?stop=s1"]
    env.fake.set_route_stops("15", {"out": [{"id": "s1"}]})
    lim = anyio.to_thread.current_default_thread_limiter()
    old, lim.total_tokens = lim.total_tokens, 8
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t") as c:
            rs = await asyncio.wait_for(asyncio.gather(*(c.get(urls[i % len(urls)]) for i in range(20))), 60)
    finally:
        lim.total_tokens = old
    assert [r.status_code for r in rs] == [404 if "series" in urls[i % len(urls)] else 200 for i in range(20)]
    assert api._gate("heavy")._value == 2


# ---------- map ----------

def _stop(sid, name, lat, lon, code="1"):
    return {"id": sid, "code": code, "name": name, "lat": lat, "lon": lon}


def _map_env(env):
    env.c.put("/api/routes/tracked/15")
    env.c.put("/api/routes/tracked/46A")
    env.fake.set_route_stops("15", {"out": [_stop("a", "Alpha", 53.3, -6.2, "2051"), _stop("b", "Beta", 53.31, -6.21), {"id": "nocoords"}]})
    env.fake.set_route_stops("46A", {"out": [_stop("b", "Beta", 53.31, -6.21), _stop("c", "Gamma", 53.32, -6.22)]})


def test_map_merges_routes_and_weights_mean(env):
    _map_env(env)
    for d in (60, 120):
        env.add(route="15", stop="a", delay=d, scheduled=T0 + d)
    env.add(route="15", stop="b", delay=0, scheduled=T0)
    for d in (300, 300, 300):
        env.add(route="46A", stop="b", delay=d, scheduled=T0 + d)
    env.add(route="15", stop="ghost", delay=5)  # not in any stop list: ignored
    j = env.get("/api/map").json()
    finite(j)
    assert [r["route"] for r in j["routes"]] == ["15", "46A"] and j["unavailable"] == []
    by = {s["stop_id"]: s for s in j["stops"]}
    assert set(by) == {"a", "b", "c"}  # b listed by two routes appears once; stop without coordinates skipped
    assert by["a"]["code"] == "2051" and by["a"]["name"] == "Alpha" and by["a"]["lat"] == 53.3
    assert by["a"]["n"] == 2 and by["a"]["mean_delay"] == 90
    assert by["b"]["n"] == 4 and by["b"]["mean_delay"] == (0 + 900) / 4
    assert {x["route"]: (x["n"], x["mean_delay"]) for x in by["b"]["routes"]} == {"15": (1, 0), "46A": (3, 300)}
    assert by["c"]["n"] == 0 and by["c"]["mean_delay"] is None and by["c"]["routes"] == []
    assert j["n"] == 6 and j["minutes"] is None and "minutes" not in j["filters"]


def test_map_minute_window_wraps_and_validates(env):
    _map_env(env)
    base = int(datetime(2026, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())  # winter: UTC == Dublin
    for hh, mm, d in ((6, 0, 10), (10, 30, 20), (12, 0, 30), (23, 30, 40), (0, 30, 50)):
        env.add(route="15", stop="a", delay=d, scheduled=base + hh * 3600 + mm * 60)
    j = env.get("/api/map", minutes_from=360, minutes_to=630).json()
    assert j["minutes"] == [360, 630] and j["filters"]["minutes"] == [360, 630]
    assert {s["stop_id"]: s["n"] for s in j["stops"]}["a"] == 2
    wrap = env.get("/api/map", minutes_from=1380, minutes_to=60).json()
    assert {s["stop_id"]: s["n"] for s in wrap["stops"]}["a"] == 2  # 23:30 and 00:30
    single = env.get("/api/map", minutes_from=720, minutes_to=720).json()
    assert {s["stop_id"]: s["n"] for s in single["stops"]}["a"] == 1
    for bad in ({"minutes_from": 10}, {"minutes_to": 10}, {"minutes_from": -1, "minutes_to": 5},
                {"minutes_from": 0, "minutes_to": 1440}, {"minutes_from": "x", "minutes_to": 5}):
        assert env.get("/api/map", **bad).status_code == 422, bad


def test_map_server_down_for_route_is_reported_not_fatal(env):
    env.c.put("/api/routes/tracked/15")
    env.c.put("/api/routes/tracked/99")
    env.fake.set_route_stops("15", {"out": [_stop("a", "Alpha", 53.3, -6.2)]})  # 99 unknown to the server
    env.add(route="15", stop="a", delay=60)
    j = env.get("/api/map").json()
    assert j["unavailable"] == ["99"] and [r["route"] for r in j["routes"]] == ["15"]
    assert j["stops"][0]["mean_delay"] == 60
    assert env.get("/api/map").status_code == 200


def test_map_with_nothing_tracked(env):
    j = env.get("/api/map").json()
    assert j["routes"] == [] and j["stops"] == [] and j["n"] == 0


# ---------- map shapes ----------

def test_map_shapes_real_geometry_thinned_cached_and_missing_directions_absent(env):
    env.c.put("/api/routes/tracked/15")
    env.fake.set_route_stops("15", {"0": [_stop("a", "A", 53.3, -6.2)], "1": [_stop("b", "B", 53.31, -6.21)]})
    line = [[53.30000, -6.20000], [53.300005, -6.200005], [53.30100, -6.20100], [53.30100, -6.20100], [53.30200, -6.20200], ["x", 1], [53.3, float("nan")]]
    env.fake.set_shape("15", "0", line)  # direction "1" has no shape on the server
    j = env.get("/api/map/shapes").json()
    finite(j)
    assert set(j["shapes"]["15"]) == {"0"}
    pts = j["shapes"]["15"]["0"]
    assert pts[0] == [-6.2, 53.3] and pts[-1] == [-6.202, 53.302]  # [lon, lat]; the final point is kept
    assert len(pts) == 3, pts  # the 0.7 m neighbour and the duplicate are dropped; malformed points skipped
    calls = env.fake.shape_calls
    env.keep_cache = True
    assert env.get("/api/map/shapes").json() == j
    assert env.fake.shape_calls == calls + 1  # direction "0" cached; only the missing direction is asked again


def test_map_shapes_nothing_tracked_and_unknown_route(env):
    assert env.get("/api/map/shapes").json() == {"shapes": {}}
    env.c.put("/api/routes/tracked/99")  # server knows nothing about it
    assert env.get("/api/map/shapes").json() == {"shapes": {}}


# ---------- map: per-stop change in delay ----------

def _chg_env(env, stops=("s1", "s2", "s3"), route="15"):
    env.c.put(f"/api/routes/tracked/{route}")
    env.fake.set_route_stops(route, {"out": [_stop(s, s.upper(), 53.3 + i / 100, -6.2) for i, s in enumerate(stops)]})


def _chg(j):
    return {s["stop_id"]: s for s in j["stops"]}


def test_map_change_example_and_two_trips(env):
    _chg_env(env)
    env.trip("A", [("s1", 60), ("s2", 120), ("s3", 120)])
    by = _chg(env.get("/api/map").json())
    assert [(by[s]["mean_change"], by[s]["n_change"]) for s in ("s1", "s2", "s3")] == [(60, 1), (60, 1), (0, 1)]
    assert [(by[s]["n"], by[s]["mean_delay"]) for s in ("s1", "s2", "s3")] == [(1, 60), (1, 120), (1, 120)]
    env.trip("B", [("s1", 0), ("s2", 60), ("s3", 180)], t0=T0 + 10)
    by = _chg(env.get("/api/map").json())
    assert [by[s]["mean_change"] for s in ("s1", "s2", "s3")] == [30, 60, 60]
    assert [by[s]["n_change"] for s in ("s1", "s2", "s3")] == [2, 2, 2]
    assert by["s3"]["routes"][0]["mean_change"] == 60 and by["s3"]["routes"][0]["n_change"] == 2


def test_map_change_missing_previous_stop(env):
    _chg_env(env)
    env.trip("A", [("s1", 60), ("s3", 200)])
    env.add(stop="s2", delay=90, scheduled=T0 + 500, trip="C")
    by = _chg(env.get("/api/map").json())
    assert (by["s1"]["mean_change"], by["s1"]["n_change"]) == (60, 1)
    assert (by["s2"]["mean_change"], by["s2"]["n_change"]) == (None, 0)
    assert (by["s3"]["mean_change"], by["s3"]["n_change"]) == (None, 0)
    assert (by["s2"]["n"], by["s2"]["mean_delay"]) == (1, 90)
    assert (by["s3"]["n"], by["s3"]["mean_delay"]) == (1, 200)


def test_map_change_duplicate_stop_in_trip_skipped(env):
    _chg_env(env)
    env.trip("L", [("s1", 10), ("s2", 20), ("s1", 30), ("s3", 50)])
    by = _chg(env.get("/api/map").json())
    assert by["s1"]["n_change"] == 0 and by["s1"]["mean_change"] is None
    assert by["s2"]["n_change"] == 0  # previous stop ambiguous
    assert by["s3"]["n_change"] == 1 and by["s3"]["mean_change"] == 50 - 20  # s2 is unambiguous
    assert by["s1"]["n"] == 2 and by["s1"]["mean_delay"] == 20


def test_map_change_two_routes_share_stop(env):
    _chg_env(env, ("s1", "s2"), "15")
    _chg_env(env, ("s1", "s2"), "46A")
    env.trip("A", [("s1", 0), ("s2", 60)], route="15")
    env.trip("B", [("s1", 0), ("s2", 60)], route="15", t0=T0 + 5)
    env.trip("C", [("s1", 0), ("s2", 300)], route="46A", t0=T0 + 10)
    s2 = _chg(env.get("/api/map").json())["s2"]
    assert s2["n_change"] == 3 and s2["mean_change"] == (60 + 60 + 300) / 3
    assert {r["route"]: (r["n_change"], r["mean_change"]) for r in s2["routes"]} == {"15": (2, 60), "46A": (1, 300)}


def test_map_change_filters_apply(env):
    _chg_env(env)
    env.add(stop="s1", delay=100, scheduled=T0, trip="A", confidence=0)
    env.add(stop="s2", delay=160, scheduled=T0 + 60, trip="A")
    env.add(stop="s3", delay=160, scheduled=T0 + 120, trip="A")
    by = _chg(env.get("/api/map", min_confidence=0).json())
    assert by["s2"]["mean_change"] == 60 and by["s2"]["n_change"] == 1
    by = _chg(env.get("/api/map").json())  # s1 row dropped: s2 loses previous, s3 still has s2
    assert by["s2"]["n_change"] == 0 and by["s2"]["mean_change"] is None
    assert by["s3"]["mean_change"] == 0 and by["s3"]["n_change"] == 1


def test_map_change_minutes_window(env):
    _chg_env(env)
    base = int(datetime(2026, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    env.add(stop="s1", delay=10, scheduled=base + 3 * 3600, trip="A", date="2026-01-12")
    env.add(stop="s2", delay=40, scheduled=base + 7 * 3600, trip="A", date="2026-01-12")
    by = _chg(env.get("/api/map", minutes_from=360, minutes_to=480).json())
    assert by["s2"]["n_change"] == 0 and by["s2"]["n"] == 1


def test_map_change_no_data_is_null(env):
    _chg_env(env)
    by = _chg(env.get("/api/map").json())
    assert by["s1"]["mean_change"] is None and by["s1"]["n_change"] == 0
