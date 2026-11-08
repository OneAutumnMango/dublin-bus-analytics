import json
import math
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.config import Settings
from app.db import open_meta
from app.main import create_app
from app.serverclient import ServerClient
from tests.fake_server import FakeServer

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
    def __init__(self, tmp_path):
        self.s = Settings(server_url="http://fake", data_dir=tmp_path)
        self.fake = FakeServer()
        self.app = create_app(self.s, start_background=False)
        self.app.state.poller = FakePoller()
        self.app.state.client = ServerClient("http://fake", self.fake.client())
        self.c = TestClient(self.app)
        self.n = 0

    def add(self, route="15", stop="s1", seq=1, delay=0, scheduled=T0, confidence=1, realtime=1,
            trip=None, date="2026-01-01"):
        self.n += 1
        db = open_meta(self.s.data_dir)
        with db:
            cur = db.execute(
                "insert into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,"
                "confidence,realtime,route) values(?,?,?,?,?,?,?,?,?,?)",
                (trip or f"t{self.n}", stop, seq, date, scheduled, None if delay is None else scheduled + delay,
                 delay, confidence, realtime, route))
        db.close()
        return cur.lastrowid

    def fc(self, arrival_id, horizon, err):
        db = open_meta(self.s.data_dir)
        with db:
            db.execute("insert into forecast values(?,?,?,?)", (arrival_id, horizon, 0, err))
        db.close()

    def get(self, url, **q):
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


def test_track_awaits_async_poller(env):
    class AP(FakePoller):
        async def track(self, r):
            self._t.add(r)

    env.app.state.poller = AP()
    assert env.c.put("/api/routes/tracked/15").json() == {"routes": ["15"]}


@pytest.mark.parametrize("bad", ["%20", "a" * 33, "-x", "a;b", "a'b", "a%00b", ".x", "<b>"])
def test_track_invalid_route_422(env, bad):
    assert env.c.put(f"/api/routes/tracked/{bad}").status_code == 422
    assert env.c.delete(f"/api/routes/tracked/{bad}").status_code == 422
    assert env.app.state.poller.tracked() == set()


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
    for stop, seq in (("sC", 3), ("sA", 1), ("sB", 2)):
        env.add(stop=stop, seq=seq, delay=seq * 60)
    env.add(stop="sB", seq=2, delay=60)
    env.add(route="other", stop="zz", seq=0, delay=5)
    j = env.get("/api/routes/15/summary").json()
    assert [s["stop_id"] for s in j["stops"]] == ["sA", "sB", "sC"]
    assert [s["seq"] for s in j["stops"]] == [1, 2, 3]
    assert [s["n"] for s in j["stops"]] == [1, 2, 1]
    assert j["n"] == 4 and j["low_n"] is True and j["route"] == "15"


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
                 "min_confidence": 1, "realtime_only": True}
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


def test_summary_nonexistent_stop_seq_none_sorted_last(env):
    env.add(stop="a", seq=None, delay=1)
    env.add(stop="b", seq=5, delay=1)
    assert [s["stop_id"] for s in env.get("/api/routes/15/summary").json()["stops"]] == ["b", "a"]


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

def seed_trips(env):
    env.fake.set_route_stops("15", {"out": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                                    "in": [{"id": "x"}, {"id": "y"}]})
    for t, ds in (("t1", (0, 60, 180)), ("t2", (0, 120, 240))):
        for i, (sid, d) in enumerate(zip("abc", ds), 1):
            env.add(trip=t, stop=sid, seq=i, delay=d)
    for i, (sid, d) in enumerate(zip("xy", (10, 40)), 1):
        env.add(trip="t3", stop=sid, seq=i, delay=d)
    env.add(trip="t4", stop="q", seq=1, delay=5)  # matches no direction


def test_accumulation_grouped_by_direction(env):
    seed_trips(env)
    j = env.get("/api/routes/15/accumulation").json()
    assert j["direction_source"] == "server"
    d = {g["direction"]: g for g in j["directions"]}
    assert set(d) == {"out", "in", "unknown"}
    assert d["out"]["n_trips"] == 2 and d["in"]["n_trips"] == 1 and d["unknown"]["n_trips"] == 1
    out = d["out"]["stops"]
    assert [s["stop_id"] for s in out] == ["a", "b", "c"]
    assert [s["mean_delay"] for s in out] == [0, 90, 210]
    assert [s["segment_delta"] for s in out] == [None, 90, 120]
    assert [(s["n"], s["n_delta"], s["low_n"], s["low_n_delta"]) for s in out][1] == (2, 2, True, True)
    assert d["in"]["stops"][1]["segment_delta"] == 30
    finite(j)


def test_accumulation_direction_param(env):
    seed_trips(env)
    j = env.get("/api/routes/15/accumulation", direction="in").json()
    assert [g["direction"] for g in j["directions"]] == ["in"]
    assert env.get("/api/routes/15/accumulation", direction="sideways").status_code == 404


def test_accumulation_without_server(env):
    seed_trips(env)
    env.fake.set_down(True)
    j = env.get("/api/routes/15/accumulation").json()
    assert j["direction_source"] == "none" and [g["direction"] for g in j["directions"]] == ["all"]
    assert j["directions"][0]["n_trips"] == 4
    assert env.get("/api/routes/15/accumulation", direction="out").status_code == 502


def test_accumulation_direction_with_unknown_route_404(env):
    env.add(delay=1)
    assert env.get("/api/routes/15/accumulation", direction="out").status_code == 404
    assert env.get("/api/routes/15/accumulation").json()["directions"][0]["direction"] == "all"


def test_accumulation_stopid_key_variant(env):
    env.fake.set_route_stops("15", {"out": [{"stopId": "a"}]})
    env.add(stop="a", seq=1, delay=3)
    assert env.get("/api/routes/15/accumulation").json()["directions"][0]["direction"] == "out"


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

@pytest.mark.parametrize("url", ["/api/routes/zz/summary", "/api/stops/zz/stats", "/api/routes/zz/heatmap",
                                 "/api/routes/zz/forecast"])
def test_empty_db_200_n0(env, url):
    r = env.get(url)
    assert r.status_code == 200
    j = r.json()
    finite(j)
    assert j.get("n", 0) == 0
    if "stats" in url:
        assert j["n"] == 0 and j["low_n"] is True and j["delay"]["mean"] is None
        assert j["on_time_pct"] is None and j["reliability"] is None and j["recent"] == []
        assert sum(b["count"] for b in j["histogram"]) == 0
    if "summary" in url:
        assert j["stops"] == []
    if "heatmap" in url:
        assert j["cells"] == []


def test_stop_with_no_arrivals_returns_n0_not_500(env):
    env.add(stop="other", delay=5)
    r = env.get("/api/stops/ghost/stats")
    assert r.status_code == 200 and r.json()["n"] == 0


def test_empty_accumulation(env):
    assert env.get("/api/routes/zz/accumulation").json()["directions"] == []


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


@pytest.mark.parametrize("k,v", [("ci", "0.001"), ("ci", "0.999"), ("pct_lo", "0"), ("pct_hi", "1"),
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
    for url in (f"/api/routes/{q}/summary", f"/api/routes/{q}/heatmap", f"/api/routes/{q}/forecast",
                f"/api/routes/{q}/accumulation", f"/api/stops/{q}/stats"):
        r = env.get(url)
        assert r.status_code == 200 and r.json().get("n", 0) == 0, (url, r.text)
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
    import app.api as api
    opened = []
    real = api.open_meta

    def spy(d):
        c = real(d)
        opened.append(c)
        return c

    monkeypatch.setattr(api, "open_meta", spy)
    monkeypatch.setattr(api.stats, "filter_rows", lambda *a, **k: 1 / 0)
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
