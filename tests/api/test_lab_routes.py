import numpy as np

from tests.api.test_routes import T0, Env, finite  # noqa: F401  (env fixture)
from tests.api.test_routes import env  # noqa: F401

URL = "/api/routes/15/lab/distribution"


def seed(env, days, per=20, delay=lambda d, i: i * 10 + d * 5, route="15"):
    out = []
    for d in range(days):
        for i in range(per):
            x = delay(d, i)
            out.append(x)
            env.add(route=route, delay=x, scheduled=T0 + d * 86400 + i * 60, date=f"2026-01-{d + 1:02d}")
    return out


def test_distribution_basic(env):
    delays = seed(env, 6)
    r = env.get(URL)
    assert r.status_code == 200
    b = r.json()
    finite(b)
    assert b["n"] == 120 and b["days"] == 6 and b["enough_days"] is True
    assert b["ecdf"][-1]["p"] == 1.0
    m = b["tiles"]["median"]
    assert m["value"] == float(np.median(delays))
    assert m["ci_low"] <= m["value"] <= m["ci_high"]


def test_distribution_three_days_has_no_ci(env):
    seed(env, 3)
    b = env.get(URL).json()
    assert b["enough_days"] is False and b["days"] == 3
    for t in b["tiles"].values():
        assert t["ci_low"] is None and t["ci_high"] is None
    assert b["tiles"]["median"]["value"] is not None


def test_distribution_filters_apply(env):
    seed(env, 7)
    full = env.get(URL).json()
    b = env.get(URL, weekdays="0,1,2").json()
    assert 0 < b["n"] < full["n"]
    assert b["filters"]["weekdays"] == [0, 1, 2]


def test_distribution_empty_route(env):
    b = env.get("/api/routes/99/lab/distribution").json()
    finite(b)
    assert b["n"] == 0 and b["ecdf"] == [] and b["qq"] == []
    assert all(t["value"] is None for t in b["tiles"].values())


def test_distribution_constant_delays(env):
    seed(env, 10, delay=lambda d, i: 60)
    b = env.get(URL).json()
    finite(b)
    assert b["tiles"]["skew"]["value"] is None and b["tiles"]["kurtosis"]["value"] is None
    assert b["tiles"]["buffer"]["value"] == 0


def test_distribution_is_deterministic(env):
    seed(env, 6)
    assert env.get(URL).json() == env.get(URL).json()


def test_distribution_bad_param_422(env):
    assert env.get(URL, ci="abc").status_code == 422


def test_distribution_tiles_match_shape_stats(env):
    import pytest
    from app.analysis import lab
    delays = seed(env, 6, per=15, delay=lambda d, i: i * i + d)
    t = env.get(URL).json()["tiles"]
    want = lab.shape_stats(delays)
    for k in ("median", "p95", "buffer", "skew", "kurtosis"):
        assert want[k] is not None
        assert t[k]["value"] == pytest.approx(want[k], rel=1e-9)


def test_distribution_small_n_thresholds(env):
    seed(env, 1, per=2)
    t = env.get(URL).json()["tiles"]
    assert t["skew"]["value"] is None and t["kurtosis"]["value"] is None


def test_distribution_n3_skew_only(env):
    seed(env, 1, per=3, delay=lambda d, i: i * i)
    t = env.get(URL).json()["tiles"]
    assert t["skew"]["value"] is not None and t["kurtosis"]["value"] is None


def test_distribution_n4_constant_no_shape(env):
    seed(env, 1, per=4, delay=lambda d, i: 30)
    t = env.get(URL).json()["tiles"]
    assert t["skew"]["value"] is None and t["kurtosis"]["value"] is None


VAR = "/api/routes/15/lab/variance"


def _seed_hours(env, days, route="15", stops=("S1",), delay=lambda h: 600 if h == 8 else 0):
    # two distinct local hours per day (T0 is not hour-aligned, but +h*3600 keeps each in one hour)
    for d in range(days):
        for h in (8, 9):
            for i in range(4):
                env.add(route=route, delay=delay(h), stop=stops[i % len(stops)],
                        scheduled=T0 + d * 86400 + h * 3600 + i * 60, date=f"2026-01-{d + 1:02d}")


def test_variance_hour_dominates(env):
    _seed_hours(env, 8)
    b = env.get(VAR).json()
    finite(b)
    f = {x["factor"]: x for x in b["factors"]}
    assert [x["factor"] for x in b["factors"]] == ["hour", "weekday", "stop", "date"]
    assert f["hour"]["omega2"] > 0.9 and f["weekday"]["omega2"] < 0.05 and f["stop"]["omega2"] is None


def test_variance_levels_counted(env):
    _seed_hours(env, 8)
    f = {x["factor"]: x for x in env.get(VAR).json()["factors"]}
    assert f["hour"]["levels"] == 2 and f["date"]["levels"] == 8 and f["stop"]["levels"] == 1


def test_variance_three_days_no_ci(env):
    _seed_hours(env, 3)
    b = env.get(VAR).json()
    assert b["enough_days"] is False
    assert all(x["ci_low"] is None and x["ci_high"] is None for x in b["factors"])
    assert b["factors"][0]["omega2"] is not None


def test_variance_empty_and_constant(env):
    b = env.get("/api/routes/99/lab/variance").json()
    finite(b)
    assert b["n"] == 0 and all(x["omega2"] is None for x in b["factors"])
    _seed_hours(env, 6, delay=lambda h: 60)
    b = env.get(VAR).json()
    finite(b)
    assert all(x["omega2"] is None for x in b["factors"])


def test_variance_matches_omega_squared(env):
    import pytest
    from app.analysis import lab, stats
    rows = []
    for d in range(7):
        for i in range(24):
            s = T0 + d * 86400 + i * 3000
            rows.append((f"2026-01-{d + 1:02d}", f"S{i % 3}", (i * 7 + d * 13) % 50 + (i % 3) * 20, s))
            env.add(route="15", delay=rows[-1][2], stop=rows[-1][1], scheduled=s, date=rows[-1][0])
    late = T0 - (8 * 3600 + 53 * 60 + 20) + 22 * 3600 + 1800  # 23:30 Dublin (BST): hour 23 must be a level
    rows.append(("2026-01-01", "S1", 33, late))
    env.add(route="15", delay=33, stop="S1", scheduled=late, date="2026-01-01")
    y = np.array([r[2] for r in rows], float)
    loc = [stats._local(r[3]) for r in rows]
    want = {"hour": [t.hour for t in loc], "weekday": [t.weekday() for t in loc],
            "stop": [r[1] for r in rows], "date": [r[0] for r in rows]}
    assert 23 in want["hour"]
    b = env.get(VAR).json()
    for x in b["factors"]:
        exp = lab.omega_squared(np.unique(want[x["factor"]], return_inverse=True)[1], y)
        assert exp is not None
        assert x["omega2"] == pytest.approx(exp, rel=1e-9)
        assert x["ci_low"] is not None


PER = "/api/routes/15/lab/persistence"
STOPS = [f"s{i}" for i in range(6)]


def _seed_dated(env, days, per, delays):
    env.fake.set_route_stops("15", {"out": [{"id": s} for s in STOPS]})
    out = []
    for d in range(days):
        for k in range(per):
            ds = delays(d, k)
            for i, (s, x) in enumerate(zip(STOPS, ds)):
                env.add(trip=f"t{d}-{k}", stop=s, delay=x, scheduled=T0 + d * 86400 + k * 3600 + i * 60,
                        date=f"2026-01-{d + 1:02d}")
            out.append(list(enumerate(ds)))
    return out


def test_persistence_persistent_trips(env):
    trips = _seed_dated(env, 10, 4, lambda d, k: [60 + 37 * (d * 4 + k) % 300] * 6)
    b = env.get(PER).json()
    finite(b)
    assert b["n_trips"] == len(trips) == 40 and b["order_source"] == "server"
    assert [x["lag"] for x in b["lags"]] == [1, 2, 3, 4, 5]
    assert all(x["r"] > 0.9 for x in b["lags"])
    assert abs(b["recovery"]["slope"] - 1.0) < 0.1


def test_persistence_recovering_trips(env):
    _seed_dated(env, 10, 4, lambda d, k: [x := 2 * (20 + 29 * (d * 4 + k) % 150)] + [x // 2] * 5)
    b = env.get(PER).json()
    assert abs(b["recovery"]["slope"] - 0.5) < 0.1


def test_persistence_loop_route(env):
    env.fake.set_route_stops("15", {"loop": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "a"}]})
    env.trip("t1", [("a", 0), ("b", 30), ("c", 90), ("a", 100)])
    b = env.get(PER).json()
    assert b["n_trips"] == 1 and b["lags"][0]["n"] == 3 and b["lags"][2]["n"] == 1


def test_persistence_three_days_no_ci(env):
    _seed_dated(env, 3, 4, lambda d, k: [10 * k + i * (d + 1) for i in range(6)])
    b = env.get(PER).json()
    assert b["enough_days"] is False and b["days"] == 3
    assert all(x["ci_low"] is None and x["ci_high"] is None for x in b["lags"])
    assert b["recovery"]["ci_low"] is None and b["recovery"]["ci_high"] is None
    assert b["lags"][0]["r"] is not None and b["recovery"]["slope"] is not None


def test_persistence_server_unavailable(env):
    env.trip("t1", [("a", 0), ("b", 60)])
    env.fake.set_down(True)
    r = env.get(PER)
    b = r.json()
    assert r.status_code == 200 and b["order_source"] == "none" and b["lags"] == [] and b["recovery"] is None
    assert b["n_trips"] == 0 and b["low_n"] is True and b["enough_days"] is False


def test_persistence_empty(env):
    env.fake.set_route_stops("15", {"out": [{"id": s} for s in STOPS]})
    r = env.get(PER)
    b = r.json()
    finite(b)
    assert r.status_code == 200 and b["n_trips"] == 0
    assert len(b["lags"]) == 5 and all(x["r"] is None for x in b["lags"])
    assert b["recovery"]["slope"] is None and b["recovery"]["n"] == 0 and b["recovery"]["low_n"] is True
    assert b["recovery"]["ci_low"] is None and b["recovery"]["ci_high"] is None


def test_persistence_excludes_fallback_direction_trip(env):
    env.fake.set_route_stops("15", {"out": [{"id": s} for s in STOPS]})
    env.trip("good", [(s, 10 * i) for i, s in enumerate(STOPS)])
    env.trip("odd", [(f"x{i}", 500 + i) for i in range(6)])  # no stop on any server direction
    b = env.get(PER).json()
    assert b["n_trips"] == 1
    assert [x["n"] for x in b["lags"]] == [5, 4, 3, 2, 1]
    assert b["recovery"]["n"] == 5


def test_persistence_matches_lab_functions(env):
    import pytest
    from app.analysis import lab
    trips = _seed_dated(env, 7, 3, lambda d, k: [(d * 7 + k * 13 + i * i * (k + 1)) % 90 for i in range(6)])
    b = env.get(PER).json()
    ac, rec = lab.autocorr_by_lag(trips), lab.recovery_slope(trips)
    for x in b["lags"]:
        assert ac[x["lag"]]["r"] is not None
        assert x["r"] == pytest.approx(ac[x["lag"]]["r"]) and x["n"] == ac[x["lag"]]["n"]
        assert x["ci_low"] is not None
    assert b["recovery"]["slope"] == pytest.approx(rec["slope"]) and b["recovery"]["n"] == rec["n"]
    assert b == env.get(PER).json()


# ---- all routes (pooled) ----

ALL = "/api/all/lab/"
ROUTES = ("15", "16", "N4")


def _track(env, routes=ROUTES):
    for r in routes:
        env.app.state.poller.track(r)


def _seed_all(env, days=6, per=10):
    _track(env)
    out = []
    for k, r in enumerate(ROUTES):
        out += seed(env, days, per=per, delay=lambda d, i, k=k: (i * 7 + d * 3 + k * 40) % 90 + k, route=r)
    return out


def test_all_distribution_matches_pooled_rows(env):
    import pytest
    from app.analysis import lab
    delays = _seed_all(env)
    b = env.get(ALL + "distribution").json()
    finite(b)
    assert b["routes"] == 3 and b["n"] == len(delays) == 180 and "route" not in b
    want = lab.shape_stats(delays)
    for k in ("median", "p95", "buffer", "skew", "kurtosis"):
        assert b["tiles"][k]["value"] == pytest.approx(want[k], abs=1e-9)


def test_all_distribution_no_tracked_routes(env):
    r = env.get(ALL + "distribution")
    b = r.json()
    finite(b)
    assert r.status_code == 200 and b["n"] == 0 and b["ecdf"] == [] and b["routes"] == 0


def test_all_distribution_ignores_untracked(env):
    _seed_all(env, days=2, per=2)
    seed(env, 2, per=2, route="99")
    assert env.get(ALL + "distribution").json()["n"] == 12


def _seed_var(env):
    _track(env)
    rows = []
    for k, r in enumerate(ROUTES):
        for d in range(7):
            for h in (8, 9, 10):
                for i in range(3):
                    s = T0 + d * 86400 + h * 3600 + i * 60
                    rows.append((r, f"2026-01-{d + 1:02d}", f"S{(i + k) % 4}", 100 * k + (i + d) % 2, s))
                    env.add(route=r, delay=rows[-1][3], stop=rows[-1][2], scheduled=s, date=rows[-1][1])
    return rows


def test_all_variance_has_route_factor(env):
    import pytest
    from app.analysis import lab, stats
    rows = _seed_var(env)
    b = env.get(ALL + "variance").json()
    finite(b)
    assert [x["factor"] for x in b["factors"]] == ["hour", "weekday", "stop", "date", "route"]
    assert b["routes"] == 3 and "route" not in b
    f = {x["factor"]: x for x in b["factors"]}
    assert f["route"]["omega2"] > 0.9 and f["route"]["levels"] == 3
    y = np.array([r[3] for r in rows], float)
    loc = [stats._local(r[4]) for r in rows]
    want = {"hour": [t.hour for t in loc], "weekday": [t.weekday() for t in loc],
            "stop": [r[2] for r in rows], "date": [r[1] for r in rows], "route": [r[0] for r in rows]}
    for x in b["factors"]:
        exp = lab.omega_squared(np.unique(want[x["factor"]], return_inverse=True)[1], y)
        assert exp is not None
        assert x["omega2"] == pytest.approx(exp, abs=1e-9)


def test_all_variance_shared_stop_is_one_level(env):
    _track(env, ("15", "16"))
    for r in ("15", "16"):
        _seed_hours(env, 4, route=r, stops=("SHARED",))
    f = {x["factor"]: x for x in env.get(ALL + "variance").json()["factors"]}
    assert f["stop"]["levels"] == 1 and f["route"]["levels"] == 2


def test_all_variance_no_tracked_routes(env):
    b = env.get(ALL + "variance").json()
    finite(b)
    assert b["n"] == 0 and b["routes"] == 0 and len(b["factors"]) == 5
    assert all(x["omega2"] is None for x in b["factors"])


def _seed_two_routes(env, days=7, per=3):
    _track(env, ("15", "16"))
    out = []
    for k, r in enumerate(("15", "16")):
        stops = [f"{r}s{i}" for i in range(6)]
        env.fake.set_route_stops(r, {"out": [{"id": s} for s in stops]})
        for d in range(days):
            for j in range(per):
                ds = [(d * 7 + j * 13 + i * i * (j + 1) + k * 11) % 90 for i in range(6)]
                for i, (s, x) in enumerate(zip(stops, ds)):
                    env.add(route=r, trip=f"{r}t{d}-{j}", stop=s, delay=x,
                            scheduled=T0 + d * 86400 + j * 3600 + i * 60, date=f"2026-01-{d + 1:02d}")
                out.append(list(enumerate(ds)))
    return out


def test_all_persistence_pools_routes(env):
    import pytest
    from app.analysis import lab
    trips = _seed_two_routes(env)
    b = env.get(ALL + "persistence").json()
    finite(b)
    assert b["routes"] == 2 and b["routes_used"] == ["15", "16"] and b["routes_skipped"] == []
    assert b["order_source"] == "server" and b["n_trips"] == len(trips) and "route" not in b
    ac, rec = lab.autocorr_by_lag(trips), lab.recovery_slope(trips)
    for x in b["lags"]:
        assert x["r"] == pytest.approx(ac[x["lag"]]["r"]) and x["n"] == ac[x["lag"]]["n"]
        assert x["ci_low"] is not None
    assert b["recovery"]["slope"] == pytest.approx(rec["slope"]) and b["recovery"]["n"] == rec["n"]


def test_all_persistence_skips_unavailable_route(env):
    _seed_two_routes(env)
    del env.fake.route_stops["16"]  # server 404 for route 16 -> its stop order is unavailable
    b = env.get(ALL + "persistence").json()
    assert b["routes_used"] == ["15"] and b["routes_skipped"] == ["16"] and b["order_source"] == "server"


def test_all_persistence_all_unavailable(env):
    _seed_two_routes(env)
    env.fake.set_down(True)
    r = env.get(ALL + "persistence")
    b = r.json()
    finite(b)
    assert r.status_code == 200 and b["order_source"] == "none" and b["routes_used"] == []
    assert b["routes_skipped"] == ["15", "16"] and b["lags"] == [] and b["recovery"] is None


def test_all_persistence_no_tracked_routes(env):
    b = env.get(ALL + "persistence").json()
    assert b["routes"] == 0 and b["routes_used"] == [] and b["n_trips"] == 0 and b["order_source"] == "none"


def test_all_endpoints_filters_echoed(env):
    _seed_all(env, days=7)
    for ep in ("distribution", "variance"):
        full = env.get(ALL + ep).json()
        b = env.get(ALL + ep, weekdays="0,1,2").json()
        assert 0 < b["n"] < full["n"] and b["filters"]["weekdays"] == [0, 1, 2]
