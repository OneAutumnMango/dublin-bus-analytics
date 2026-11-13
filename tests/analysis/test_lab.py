import json

import numpy as np
import pytest

from app.analysis.lab import day_bootstrap, ecdf_points, qq_points, shape_stats


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
