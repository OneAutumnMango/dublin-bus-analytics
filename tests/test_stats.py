import json
import math

import pytest
from hypothesis import given, strategies as st
from scipy.stats import t as student_t

from app.stats import delay_summary, on_time_pct, reliability_score

delays_st = st.lists(st.integers(-10_000, 10_000), min_size=0, max_size=60)


def _no_bad(d):
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in d.values())
    json.dumps(d)


def test_delay_summary_known_values():
    # mean 120; SS = 36000; sample var 9000; std = sqrt(9000) = 94.8683 (brief's "100" is wrong)
    s = delay_summary([0, 60, 120, 180, 240])
    assert s["n"] == 5 and s["mean"] == 120 and s["min"] == 0 and s["max"] == 240
    assert s["median"] == 120
    assert s["std"] == pytest.approx(math.sqrt(9000))
    half = 2.7764451 * math.sqrt(9000) / math.sqrt(5)  # t(0.975, 4) = 2.7764451
    assert half == pytest.approx(117.8, abs=0.05)
    assert s["ci_low"] == pytest.approx(120 - half, abs=1e-3)
    assert s["ci_high"] == pytest.approx(120 + half, abs=1e-3)
    assert s["ci_low"] == pytest.approx(120 - student_t.ppf(0.975, 4) * math.sqrt(9000 / 5))
    # linear-interp percentiles: p5 at index 0.2 -> 12, p95 at index 3.8 -> 228
    assert s["p_low"] == pytest.approx(12) and s["p_high"] == pytest.approx(228)


def test_summary_n0_all_none():
    s = delay_summary([])
    assert s["n"] == 0
    assert all(v is None for k, v in s.items() if k != "n")
    assert set(s) == {"n", "mean", "min", "max", "std", "ci_low", "ci_high", "p_low", "p_high", "median"}


def test_summary_n1_ci_none():
    s = delay_summary([42])
    assert s["n"] == 1 and s["mean"] == 42 and s["min"] == s["max"] == s["median"] == 42
    assert s["p_low"] == s["p_high"] == 42
    assert s["std"] is None and s["ci_low"] is None and s["ci_high"] is None


def test_summary_n2():
    s = delay_summary([0, 10])
    assert s["std"] == pytest.approx(math.sqrt(50))
    # t(0.975, 1) = 12.7062; half = 12.7062 * sqrt(50)/sqrt(2) = 63.531
    assert s["ci_high"] - 5 == pytest.approx(63.531, abs=1e-3)


def test_summary_identical_values_no_nan():
    s = delay_summary([7, 7, 7, 7])
    assert s["std"] == 0 and s["ci_low"] == s["ci_high"] == 7
    _no_bad(s)


def test_summary_negative_and_huge():
    s = delay_summary([-10**9, 10**9, 0])
    assert s["mean"] == 0 and s["min"] == -10**9 and s["max"] == 10**9
    _no_bad(s)


def test_summary_ci_level_widens():
    d = [0, 60, 120, 180, 240]
    w = lambda c: (lambda s: s["ci_high"] - s["ci_low"])(delay_summary(d, ci=c))
    assert w(0.5) < w(0.95) < w(0.99)
    assert delay_summary(d, ci=0.99)["ci_high"] - 120 == pytest.approx(
        4.604095 * math.sqrt(9000 / 5), abs=1e-3)


def test_summary_custom_pct():
    s = delay_summary([0, 60, 120, 180, 240], pct=(0.25, 0.75))
    assert s["p_low"] == 60 and s["p_high"] == 180


@pytest.mark.parametrize("pct", [(0.9, 0.1), (0.5, 0.5), (-0.1, 0.5), (0.5, 1.1)])
def test_summary_bad_pct(pct):
    with pytest.raises(ValueError):
        delay_summary([1, 2, 3], pct=pct)


@pytest.mark.parametrize("ci", [0, 1, -0.5, 1.5])
def test_summary_bad_ci(ci):
    with pytest.raises(ValueError):
        delay_summary([1, 2, 3], ci=ci)


def test_summary_pct_extremes_ok():
    s = delay_summary([1, 5, 9], pct=(0.0, 1.0))
    assert s["p_low"] == 1 and s["p_high"] == 9


@given(delays_st, st.sampled_from([0.5, 0.9, 0.95, 0.99]))
def test_summary_invariants(d, ci):
    s = delay_summary(d, ci=ci)
    _no_bad(s)
    if not d:
        return
    eps = 1e-6
    assert s["min"] <= s["p_low"] + eps and s["p_low"] <= s["median"] + eps
    assert s["median"] <= s["p_high"] + eps and s["p_high"] <= s["max"] + eps
    if len(d) > 1:
        assert s["ci_low"] <= s["mean"] + eps <= s["ci_high"] + 2 * eps
        assert s["std"] >= 0


# ---- on_time_pct / reliability_score ----

def test_on_time_window_edges_inclusive():
    assert on_time_pct([-60, 300]) == {"n": 2, "pct": 100.0}
    assert on_time_pct([-61, 301]) == {"n": 2, "pct": 0.0}
    assert on_time_pct([-61, 0, 300, 301]) == {"n": 4, "pct": 50.0}


def test_on_time_custom_window_and_degenerate():
    assert on_time_pct([5], lo_s=5, hi_s=5)["pct"] == 100.0  # lo == hi allowed
    with pytest.raises(ValueError):
        on_time_pct([1], lo_s=10, hi_s=0)


def test_on_time_empty_is_none():
    assert on_time_pct([]) == {"n": 0, "pct": None}


def test_reliability_none_when_empty():
    assert reliability_score([]) is None


def test_reliability_std_zero_equals_pct():
    assert reliability_score([0, 0, 0]) == 100.0
    assert reliability_score([1000]) == 0.0
    assert reliability_score([0]) == 100.0  # n=1: std unknown -> no penalty


def test_reliability_penalises_spread():
    tight = [0, 10, 20, 30]
    wide = [-60, 0, 150, 300]  # also 100% on time
    assert on_time_pct(tight)["pct"] == on_time_pct(wide)["pct"] == 100.0
    assert reliability_score(tight) > reliability_score(wide)
    # exact: [0,60,120,180,240] all on time, std sqrt(9000) -> 100/(1+94.868/300)
    assert reliability_score([0, 60, 120, 180, 240]) == pytest.approx(100 / (1 + math.sqrt(9000) / 300))


@given(delays_st, st.integers(-600, 0), st.integers(0, 900))
def test_on_time_invariants(d, lo, hi):
    r = on_time_pct(d, lo, hi)
    assert r["n"] == len(d)
    if d:
        assert 0 <= r["pct"] <= 100
        assert 0 <= reliability_score(d, lo, hi) <= r["pct"] + 1e-9
    else:
        assert r["pct"] is None
    json.dumps(r)
