import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, strategies as st
from scipy.stats import t as student_t

from app.analysis.stats import (delay_summary, filter_rows, heatmap,
                       on_time_pct, reliability_score)

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
    assert reliability_score([1000, 1000]) == 0.0
    assert reliability_score([0]) is None  # n<2: spread unknown -> no score
    assert reliability_score([None, 0]) is None


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
        rel = reliability_score(d, lo, hi)
        assert (rel is None) == (len(d) < 2)
        assert rel is None or 0 <= rel <= r["pct"] + 1e-9
    else:
        assert r["pct"] is None
    json.dumps(r)


# ---- heatmap / filter_rows ----
DUB = ZoneInfo("Europe/Dublin")


def local_epoch(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=DUB).timestamp())


def utc_epoch(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


def row(delay=0, scheduled=0, confidence=1, realtime=1, **kw):
    return {"delay": delay, "actual": None if delay is None else scheduled + delay, "scheduled": scheduled,
            "confidence": confidence, "realtime": realtime, "stop_id": 1, "seq": 1, **kw}


def test_heatmap_buckets_dublin_local_time():
    # 2026-07-01 is a Wednesday (weekday 2), IST = UTC+1: 23:30 UTC -> 00:30 Thu (3, 0)
    rows = [row(60, utc_epoch(2026, 7, 1, 10, 0)),      # 11:00 IST Wed
            row(120, utc_epoch(2026, 7, 1, 10, 59)),    # 11:59 IST Wed, same bucket
            row(30, utc_epoch(2026, 7, 1, 23, 30)),     # 00:30 IST Thu
            row(-10, utc_epoch(2026, 1, 7, 23, 30))]    # winter GMT: 23:30 Wed (2, 23)
    h = heatmap(rows)
    assert h[(2, 11)] == {"n": 2, "mean_delay": 90.0}
    assert h[(3, 0)] == {"n": 1, "mean_delay": 30.0}
    assert h[(2, 23)] == {"n": 1, "mean_delay": -10.0}
    assert len(h) == 3


def test_heatmap_monday_is_zero_and_midnight_boundary():
    assert (0, 0) in heatmap([row(1, local_epoch(2026, 7, 6, 0, 0))])      # Mon 00:00
    h = heatmap([row(1, local_epoch(2026, 7, 5, 23, 59)), row(2, local_epoch(2026, 7, 6, 0, 0))])
    assert set(h) == {(6, 23), (0, 0)}                                     # Sun 23:59 vs Mon 00:00


def test_heatmap_dst_spring_forward():
    # 2026-03-29: 01:00 UTC clocks jump GMT->IST. 00:59 UTC = 00:59 local; 01:00 UTC = 02:00 local.
    h = heatmap([row(1, utc_epoch(2026, 3, 29, 0, 59)), row(2, utc_epoch(2026, 3, 29, 1, 0))])
    assert set(h) == {(6, 0), (6, 2)}  # local hour 1 never exists; Sunday = 6


def test_heatmap_dst_fall_back():
    # 2026-10-25: 01:00 UTC clocks go IST->GMT; local hour 1 occurs twice (00:30 UTC and 01:30 UTC)
    h = heatmap([row(10, utc_epoch(2026, 10, 25, 0, 30)), row(20, utc_epoch(2026, 10, 25, 1, 30))])
    assert h == {(6, 1): {"n": 2, "mean_delay": 15.0}}


def test_heatmap_skips_unusable_rows_and_empty():
    assert heatmap([]) == {}
    assert heatmap([row(None, 5), {"delay": 5, "scheduled": None}, {"delay": 5}]) == {}


def test_filter_excludes_scheduled_only_and_low_confidence():
    rows = [row(1, confidence=1, realtime=1), row(2, confidence=0, realtime=1),
            row(3, confidence=1, realtime=0), row(4, confidence=0, realtime=0)]
    assert [r["delay"] for r in filter_rows(rows, min_confidence=1)] == [1]
    assert [r["delay"] for r in filter_rows(rows)] == [1, 2]  # realtime only, any confidence
    assert [r["delay"] for r in filter_rows(rows, realtime_only=False)] == [1, 2, 3, 4]
    assert filter_rows([]) == []


def test_filter_since_until_inclusive_and_missing_scheduled():
    rows = [row(1, 100), row(2, 200), row(3, 300), {"delay": 9, "realtime": 1, "confidence": 1}]
    assert [r["delay"] for r in filter_rows(rows, since=200, until=300)] == [2, 3]
    assert [r["delay"] for r in filter_rows(rows, since=201)] == [3]
    assert len(filter_rows(rows)) == 4          # no time filter: missing scheduled kept
    assert filter_rows(rows, since=301) == []   # time filter: missing scheduled dropped


def test_filter_weekdays_hours_local_time():
    mon9 = row(1, local_epoch(2026, 7, 6, 9, 0))
    tue9 = row(2, local_epoch(2026, 7, 7, 9, 0))
    mon17 = row(3, local_epoch(2026, 7, 6, 17, 0))
    rows = [mon9, tue9, mon17]
    assert filter_rows(rows, weekdays=[0]) == [mon9, mon17]
    assert filter_rows(rows, hours=[9]) == [mon9, tue9]
    assert filter_rows(rows, weekdays={0}, hours={9}) == [mon9]
    assert filter_rows(rows, weekdays=[]) == []


def _rows_st():
    return st.lists(st.builds(
        row, delay=st.integers(-500, 500), scheduled=st.integers(1_700_000_000, 1_800_000_000),
        confidence=st.integers(0, 1), realtime=st.integers(0, 1)), max_size=40)


@given(_rows_st(), st.integers(0, 1), st.booleans(),
       st.one_of(st.none(), st.integers(1_700_000_000, 1_800_000_000)),
       st.one_of(st.none(), st.integers(1_700_000_000, 1_800_000_000)),
       st.one_of(st.none(), st.sets(st.integers(0, 6))),
       st.one_of(st.none(), st.sets(st.integers(0, 23))))
def test_filter_never_violates_and_idempotent(rows, mc, rt, since, until, wd, hrs):
    kw = dict(min_confidence=mc, realtime_only=rt, since=since, until=until, weekdays=wd, hours=hrs)
    out = filter_rows(rows, **kw)
    for r in out:
        assert r["confidence"] >= mc
        assert not rt or r["realtime"]
        assert since is None or r["scheduled"] >= since
        assert until is None or r["scheduled"] <= until
        loc = datetime.fromtimestamp(r["scheduled"], DUB)
        assert wd is None or loc.weekday() in wd
        assert hrs is None or loc.hour in hrs
    assert filter_rows(out, **kw) == out


@given(_rows_st())
def test_heatmap_total_n_equals_rows(rows):
    h = heatmap(rows)
    assert sum(v["n"] for v in h.values()) == len(rows)
    for (wd, hr), v in h.items():
        assert 0 <= wd <= 6 and 0 <= hr <= 23
    assert h == {} or all(math.isfinite(v["mean_delay"]) for v in h.values())


# ---- round 2: None handling, error_s, n counts ----

def test_none_values_dropped():
    s = delay_summary([None, 1, 2])
    assert s["n"] == 2 and s["mean"] == 1.5
    assert delay_summary([None]) == delay_summary([])
    assert on_time_pct([None]) == {"n": 0, "pct": None}
    assert on_time_pct([None, 0]) == {"n": 1, "pct": 100.0}
    assert reliability_score([None]) is None


def test_filter_null_confidence():
    r = row(1)
    r["confidence"] = None
    assert filter_rows([r]) == [r]
    assert filter_rows([r], min_confidence=1) == []


mixed_st = st.lists(st.one_of(st.none(), st.integers(-10**6, 10**6)), max_size=30)


@given(mixed_st)
def test_mixed_none_never_raises_and_finite(d):
    outs = [delay_summary(d), on_time_pct(d), {"r": reliability_score(d)},
            {"h": [list(heatmap([row(x, 1_750_000_000) for x in d]).values())]}]
    for o in outs:
        json.dumps(o, allow_nan=False)


def _at(h, m):
    """Epoch of a winter (UTC == Dublin) time of day."""
    return int(datetime(2026, 1, 12, h, m, tzinfo=timezone.utc).timestamp())


def test_filter_minutes_inclusive_wrap_and_single_minute():
    rows = [{"scheduled": _at(h, m), "confidence": 1, "realtime": 1, "k": f"{h:02}:{m:02}"}
            for h, m in ((5, 59), (6, 0), (10, 30), (10, 31), (23, 59), (0, 0), (1, 59), (2, 0))]
    keys = lambda **kw: [r["k"] for r in filter_rows(rows, **kw)]  # noqa: E731
    assert keys(minutes=(360, 630)) == ["06:00", "10:30"]
    assert keys(minutes=(1320, 119)) == ["23:59", "00:00", "01:59"]  # wraps midnight, inclusive both ends
    assert keys(minutes=(630, 630)) == ["10:30"]  # equal bounds = that single minute, not all day
    assert len(keys(minutes=(0, 1439))) == len(rows)
    assert filter_rows([{"confidence": 1, "realtime": 1}], minutes=(0, 1439)) == []  # no `scheduled` fails


def test_stop_changes_pure():
    from app.analysis.stats import stop_changes
    trips = {("A", "d"): {"s1": 60, "s2": 120, "s3": 120}, ("B", "d"): {"s1": 0, "s3": 180},
             ("C", "d"): {"s2": 50}}
    got = stop_changes(trips, {"out": ["s1", "s2", "s3"], "back": ["s3", "s1"]})
    # out: s1 (first, needs s2) -> A 60; s2 -> A 60; s3 -> A 0 (B lacks s2)
    # back: s3 first needs s1: A 120, B 180; s1 -> A 60-120, B 0-180
    assert got["s1"] == [3, 60 - 60 - 180]
    assert got["s2"] == [1, 60]
    assert got["s3"] == [3, 0 + 120 + 180]


def test_stop_changes_shared_terminus_not_polluted():
    from app.analysis.stats import stop_changes
    lists = {"out": ["s1", "s2", "s3"], "back": ["s3", "s4", "s5"]}
    trips = {("A", "d"): {"s1": 60, "s2": 120, "s3": 120},
             ("B", "d"): {"s3": 30, "s4": 50, "s5": 50}}
    got = stop_changes(trips, lists)
    assert got["s3"] == [2, 0 + 30]  # A at s3 as out-last (0); only B as back-first (own delay 30)
    assert got["s1"] == [1, 60] and got["s2"] == [1, 60]
    assert got["s4"] == [1, 20] and got["s5"] == [1, 0]


def test_stop_changes_loop_list_does_not_double_count():
    from app.analysis.stats import stop_changes
    got = stop_changes({("A", "d"): {"a": 5, "b": 9}}, {"out": ["a", "b", "a"]})
    assert got == {"a": [1, 5], "b": [1, 4]}


def _stop_changes_ref(trips, lists):
    acc = {}
    for stops in lists.values():
        seen = set()
        for i, sid in enumerate(stops):
            if sid in seen:
                continue
            seen.add(sid)
            for t in trips.values():
                d = t.get(sid)
                if d is None:
                    continue
                if i > 0:
                    pr = t.get(stops[i - 1])
                    if pr is None:
                        continue
                    d -= pr
                elif len(stops) > 1 and t.get(stops[1]) is None:
                    continue
                a = acc.setdefault(sid, [0, 0])
                a[0] += 1
                a[1] += d
    return acc


def test_stop_changes_matches_reference_randomised():
    import random
    from app.analysis.stats import stop_changes
    rnd = random.Random(7)
    ids = list("abcdefg")
    for _ in range(300):
        lists = {f"d{k}": [rnd.choice(ids) for _ in range(rnd.randint(1, 6))] for k in range(rnd.randint(1, 3))}
        trips = {}
        for n in range(rnd.randint(0, 6)):
            trips[(n, "d")] = {sid: rnd.choice([None, rnd.randint(-5, 9)]) for sid in ids if rnd.random() < .7}
        assert stop_changes(trips, lists) == _stop_changes_ref(trips, lists)


def test_stop_changes_scales():
    import time
    from app.analysis.stats import stop_changes
    stops = [f"s{i}" for i in range(120)]
    trips = {(n, "d"): {s: n + i for i, s in enumerate(stops)} for n in range(2000)}
    t0 = time.perf_counter()
    got = stop_changes(trips, {"out": stops, "back": stops[::-1]})
    assert got["s5"][0] == 4000 and time.perf_counter() - t0 < 5


def test_stop_changes_ambiguous_stop_skipped():
    from app.analysis.stats import stop_changes
    # None marks a stop seen twice on one trip occurrence
    trips = {("A", "d"): {"s1": 10, "s2": None, "s3": 50}}
    assert stop_changes(trips, {"out": ["s1", "s2", "s3"]}) == {}  # s1 first needs s2 (ambiguous); s2 skipped; s3 needs s2
