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
