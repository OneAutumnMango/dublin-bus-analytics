import json

import numpy as np
import pytest

from app.analysis.lab import (autocorr_by_lag, day_bootstrap, ecdf_points, omega_squared,
                              qq_points, recovery_slope, shape_stats)


def test_ecdf_downsamples_and_ends_at_one():
    e = ecdf_points(list(range(1000)))
    assert len(e) == 200
    assert e[-1][1] == 1.0
    xs, ps = zip(*e)
    assert list(xs) == sorted(xs) and list(ps) == sorted(ps)


def test_ecdf_small_sample_keeps_every_point():
    assert ecdf_points([3, 1, 2]) == [(1, 1 / 3), (2, 2 / 3), (3, 1.0)]
    assert ecdf_points([]) == []


def test_qq_normal_sample_lies_on_a_line():
    q = qq_points(np.random.default_rng(0).normal(size=5000))
    assert len(q) == 100
    z, s = np.array(q).T
    assert np.corrcoef(z, s)[0, 1] > 0.99
    assert (np.diff(z) > 0).all()
    assert qq_points([]) == []


def test_buffer_is_p95_minus_median():
    s = shape_stats(list(range(101)))
    assert (s["median"], s["p95"], s["buffer"]) == (50, 95, 45)


def test_shape_known_distributions():
    s = shape_stats(np.random.default_rng(1).exponential(size=200_000))
    assert abs(s["skew"] - 2) < 0.15
    assert abs(s["kurtosis"] - 6) < 1.5
    assert abs(shape_stats([-2, -1, 0, 1, 2])["skew"]) < 1e-9


def test_shape_degenerate_is_none_not_nan():
    e = shape_stats([])
    assert e["n"] == 0 and all(v is None for k, v in e.items() if k != "n")
    c = shape_stats([5] * 10)
    assert c["skew"] is None and c["kurtosis"] is None and c["buffer"] == 0
    assert shape_stats([1, 2])["skew"] is None
    assert shape_stats([1, 2, 3])["kurtosis"] is None
    for d in ([], [5] * 10, [1, 2], [1, 2, 3]):
        json.dumps(shape_stats(d), allow_nan=False)


def test_bad_k_raises():
    with pytest.raises(ValueError):
        qq_points([1, 2, 3], k=1)


def _mean_stat(days):
    return {"mean": float(np.concatenate(days).mean())}


def test_bootstrap_constant_data_gives_zero_width():
    days = [np.full(50, 5.0) for _ in range(10)]
    assert day_bootstrap(days, _mean_stat) == {"mean": (5.0, 5.0)}


def test_bootstrap_fewer_than_five_days_is_none():
    days = [np.full(50, 5.0) for _ in range(4)]
    assert day_bootstrap(days, _mean_stat) == {"mean": None}


def test_bootstrap_is_deterministic():
    days = [np.full(20, float(i)) for i in range(10)]
    assert day_bootstrap(days, _mean_stat) == day_bootstrap(days, _mean_stat)
    assert day_bootstrap(days, _mean_stat, seed=1) != day_bootstrap(days, _mean_stat, seed=2)


def test_bootstrap_reflects_day_level_variation():
    days = [np.full(100, float(i)) for i in range(10)]
    lo, hi = day_bootstrap(days, _mean_stat)["mean"]
    assert hi - lo > 2.0


def test_bootstrap_none_values_dropped():
    days = [np.full(5, float(i)) for i in range(10)]
    assert day_bootstrap(days, lambda d: {"mean": None}) == {"mean": None}
    calls = {"n": 0}

    def sometimes(d):
        calls["n"] += 1
        # first call is the real data; then None for ~1 in 4 resamples
        return {"mean": None if calls["n"] > 1 and calls["n"] % 4 == 0 else _mean_stat(d)["mean"]}

    assert day_bootstrap(days, sometimes)["mean"] is not None


def test_bootstrap_bad_level_raises():
    with pytest.raises(ValueError):
        day_bootstrap([np.zeros(3)] * 10, _mean_stat, level=1.0)


def _flaky(bad_value, bad_every):
    """stat_fn: real-days call is finite; resample k returns bad_value when bad_every(k)."""
    calls = {"n": 0}

    def fn(d):
        calls["n"] += 1
        k = calls["n"] - 1  # 0 is the real data
        return {"mean": bad_value if k > 0 and bad_every(k) else 1.0 + k % 3}

    return fn


_DAYS = [np.full(5, float(i)) for i in range(10)]


def test_bootstrap_all_nan_is_none():
    assert day_bootstrap(_DAYS, _flaky(float("nan"), lambda k: True)) == {"mean": None}


def test_bootstrap_inf_dropped_gives_finite_ci():
    ci = day_bootstrap(_DAYS, _flaky(float("inf"), lambda k: k % 10 == 0))
    json.dumps(ci, allow_nan=False)
    assert ci["mean"] is not None


def test_bootstrap_nan_for_over_half_is_none():
    # k = 1..200; NaN for k <= 101 leaves 99 valid (< 100 = half of 200)
    assert day_bootstrap(_DAYS, _flaky(float("nan"), lambda k: k <= 101)) == {"mean": None}


def test_autocorr_perfectly_persistent_trips():
    trips = [[(p, j * 10.0) for p in range(6)] for j in range(30)]
    ac = autocorr_by_lag(trips)
    assert ac[1]["n"] == 150
    for lag in range(1, 6):
        assert ac[lag]["r"] == pytest.approx(1.0)


def test_autocorr_independent_delays_near_zero():
    rng = np.random.default_rng(2)
    trips = [[(p, float(v)) for p, v in enumerate(rng.normal(size=6))] for _ in range(2000)]
    assert abs(autocorr_by_lag(trips)[1]["r"]) < 0.1


def test_autocorr_only_exact_position_gaps_pair():
    rng = np.random.default_rng(3)
    trips = [[(0, float(rng.normal())), (2, float(rng.normal())), (3, float(rng.normal()))]
             for _ in range(20)]
    ac = autocorr_by_lag(trips)
    assert ac[1]["n"] == ac[2]["n"] == ac[3]["n"] == 20
    assert ac[4]["n"] == 0 and ac[4]["r"] is None
    # lag 2 uses only (0,2): check against direct Pearson
    x = [t[0][1] for t in trips]; y = [t[1][1] for t in trips]
    assert ac[2]["r"] == pytest.approx(np.corrcoef(x, y)[0, 1])
    x = [t[0][1] for t in trips]; y = [t[2][1] for t in trips]
    assert ac[3]["r"] == pytest.approx(np.corrcoef(x, y)[0, 1])


def test_autocorr_degenerate():
    const = [[(p, 5.0) for p in range(4)] for _ in range(10)]
    assert autocorr_by_lag(const)[1]["r"] is None
    few = [[(0, 1.0), (1, 2.0)], [(0, 3.0), (1, 1.0)]]
    assert autocorr_by_lag(few)[1]["r"] is None
    with pytest.raises(ValueError):
        autocorr_by_lag(few, max_lag=0)


def test_recovery_slope():
    firsts = [1.0, 5.0, 9.0, 2.0, 7.0]
    half = [[(0, f), (1, 0.5 * f), (2, 0.5 * f)] for f in firsts]
    same = [[(0, f), (3, f)] for f in firsts]
    r = recovery_slope(half)
    assert r["slope"] == pytest.approx(0.5) and r["n"] == 10
    assert recovery_slope(same)["slope"] == pytest.approx(1.0)
    flat = [[(0, 4.0), (1, f)] for f in firsts]
    assert recovery_slope(flat)["slope"] is None
    assert recovery_slope([[(0, 1.0)]]) == {"slope": None, "n": 0}


def test_omega_separated_groups():
    rng = np.random.default_rng(4)
    codes = np.repeat(np.arange(5), 100)
    y = codes * 10 + rng.normal(size=500)
    assert omega_squared(codes, y) > 0.95


def test_omega_independent_near_zero():
    rng = np.random.default_rng(5)
    codes = rng.integers(0, 5, 1000)
    w = omega_squared(codes, rng.normal(size=1000))
    assert 0 <= w < 0.02


def test_omega_degenerate():
    y = np.arange(10.0)
    assert omega_squared(np.zeros(10, dtype=int), y) is None
    assert omega_squared(np.arange(10), y) is None
    assert omega_squared(np.arange(10) % 2, np.ones(10)) is None
    with pytest.raises(ValueError):
        omega_squared(np.zeros(3, dtype=int), np.zeros(4))
