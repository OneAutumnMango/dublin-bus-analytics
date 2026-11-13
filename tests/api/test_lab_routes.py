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
    y = np.array([r[2] for r in rows], float)
    loc = [stats._local(r[3]) for r in rows]
    want = {"hour": [t.hour for t in loc], "weekday": [t.weekday() for t in loc],
            "stop": [r[1] for r in rows], "date": [r[0] for r in rows]}
    b = env.get(VAR).json()
    for x in b["factors"]:
        exp = lab.omega_squared(np.unique(want[x["factor"]], return_inverse=True)[1], y)
        assert exp is not None
        assert x["omega2"] == pytest.approx(exp, rel=1e-9)
        assert x["ci_low"] is not None
