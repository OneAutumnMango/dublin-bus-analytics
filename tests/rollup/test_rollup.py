import json
import random
from datetime import date

import numpy as np
import pytest

from app import rollup
from app.analysis import stats
from app.core.db import open_meta

T0 = 1_760_000_000


def _fill(c, rng, days, route="15", n=300, spread=500):
    """Random realtime arrivals per service day (+ some forecasts, some NULL delays, some non-realtime)."""
    for d in days:
        base = T0 + (date.fromisoformat(d) - date(2025, 10, 9)).days * 86400
        for i in range(n):
            sched = base + rng.randrange(0, 86400)
            delay = None if rng.random() < 0.05 else int(rng.gauss(30, spread)) if rng.random() < .97 else rng.choice([-9000, 9000])
            cur = c.execute(
                "insert into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,realtime,route)"
                " values(?,?,?,?,?,?,?,?,?,?)",
                (f"{d}-{i}", "s", sched, d, sched, None if delay is None else sched + delay, delay,
                 rng.choice([0, 1, 1]), int(rng.random() > .1), route))
            for h in (30, 10, 2):
                if rng.random() < .8:
                    c.execute("insert into forecast values(?,?,?,?)", (cur.lastrowid, h, 0, int(rng.gauss(0, 200))))
    c.commit()


@pytest.fixture
def db(tmp_path):
    c = open_meta(tmp_path)
    yield c
    c.close()


def test_closed_cutoff_is_yesterday():
    assert rollup.closed_before(date(2026, 11, 14)) == "2026-11-13"


def test_ensure_builds_only_closed_days_and_is_idempotent(db):
    _fill(db, random.Random(1), ["2026-01-01", "2026-01-02", "2026-11-13", "2026-11-14"])
    rollup.ensure_route(db, "15", date(2026, 11, 14))
    got = [r[0] for r in db.execute("select service_date from ru_done order by 1")]
    assert got == ["2026-01-01", "2026-01-02"]  # 11-13 and 11-14 are within the one-day margin
    before = db.execute("select count(*) from ru_delay").fetchone()
    rollup.ensure_route(db, "15", date(2026, 11, 14))
    assert db.execute("select count(*) from ru_delay").fetchone() == before


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("mc", [0, 1])
def test_summary_matches_raw_summary(db, seed, mc):
    rng = random.Random(seed)
    days = ["2026-01-01", "2026-01-02", "2026-01-03"]
    _fill(db, rng, days)
    rollup.ensure_route(db, "15", date(2026, 11, 14))
    p = {"window_lo": -60, "window_hi": 300, "ci": .95, "pct_lo": .05, "pct_hi": .95}
    # clamp-free comparison: drop the +/-9000 outliers from the raw side by checking they don't hit a percentile
    acc = rollup.read_delay(db, "15", json.dumps(days), mc)
    rows = [r[0] for r in db.execute("select delay from arrivals where realtime=1 and scheduled is not null and delay is not null and confidence>=?", (mc,))]
    exp = stats.delay_summary(rows, .95, (.05, .95))
    s = acc.summary(p)
    assert s["n"] == len(rows)
    assert s["mean"] == pytest.approx(exp["mean"])
    assert (s["median"], s["p_low"], s["p_high"]) == pytest.approx((exp["median"], exp["p_low"], exp["p_high"]))
    assert s["on_time_pct"] == pytest.approx(stats.on_time_pct(rows, -60, 300)["pct"])
    assert s["reliability"] == pytest.approx(stats.reliability_score(rows, -60, 300))


def test_acc_empty_and_single():
    a = rollup.Acc()
    s = a.summary({"window_lo": -60, "window_hi": 300})
    assert s["n"] == 0 and s["mean"] is None and s["on_time_pct"] is None and s["reliability"] is None
    a.add_delays(np.array([42]))
    s = a.summary({"window_lo": -60, "window_hi": 300})
    assert (s["n"], s["mean"], s["median"], s["reliability"]) == (1, 42, 42, None)


def test_add_delays_plus_blobs_equals_all_raw():
    a, b = rollup.Acc(), rollup.Acc()
    xs = np.array([-5000, -61, 0, 10, 300, 301, 7000])
    a.add_delays(xs)
    b.add_delays(xs[:3]); b.add_delays(xs[3:])
    assert (a.n, a.s, a.s2) == (b.n, b.s, b.s2) and (a.hist == b.hist).all()
    assert a.s == int(xs.sum())  # sums are NOT clamped


def test_heat_and_forecast_parts_match_raw(db):
    days = ["2026-01-01", "2026-01-02"]
    _fill(db, random.Random(7), days)
    rollup.ensure_route(db, "15", date(2026, 11, 14))
    js = json.dumps(days)
    heat = rollup.read_heat(db, "15", js, 1)
    raw = {}
    for sched, delay in db.execute("select scheduled, delay from arrivals where realtime=1 and confidence>=1 and scheduled is not null and delay is not null"):
        k = stats._local(sched)
        n, t = raw.get((k.weekday(), k.hour), (0, 0))
        raw[(k.weekday(), k.hour)] = (n + 1, t + delay)
    assert heat == raw
    fc = rollup.read_fc(db, "15", js, 1, (30, 10, 2))
    for h in (30, 10, 2):
        errs = [e for (e,) in db.execute("select f.error_s from arrivals a join forecast f on f.arrival_id=a.id where a.realtime=1 and a.confidence>=1 and a.scheduled is not null and f.horizon_min=?", (h,))]
        n, s, ab, counts = fc[h]
        assert (n, s, ab) == (len(errs), sum(errs), sum(abs(e) for e in errs))
        assert counts.sum() == n


def test_rolled_days_respects_since_until_and_drop(db):
    days = ["2026-01-01", "2026-01-02", "2026-01-03"]
    _fill(db, random.Random(3), days)
    rollup.ensure_route(db, "15", date(2026, 11, 14))
    assert rollup.rolled_days(db, "15", None, None) == days
    lo, hi = db.execute("select min_s, max_s from ru_done where service_date='2026-01-02'").fetchone()
    assert rollup.rolled_days(db, "15", lo, hi) == ["2026-01-02"]  # neighbours are not fully inside
    assert rollup.rolled_days(db, "15", lo + 1, None) == ["2026-01-03"]
    rollup.drop_days(db, ["2026-01-02"])
    assert rollup.rolled_days(db, "15", None, None) == ["2026-01-01", "2026-01-03"]
    assert db.execute("select count(*) from ru_delay where service_date='2026-01-02'").fetchone() == (0,)
    rollup.drop_days(db, ["2026-01-01"], route="99")
    assert "2026-01-01" in rollup.rolled_days(db, "15", None, None)


def test_read_heat_weekday_hour_filters(db):
    _fill(db, random.Random(11), ["2026-01-01", "2026-01-02"])
    rollup.ensure_route(db, "15", date(2026, 11, 14))
    js = json.dumps(["2026-01-01", "2026-01-02"])
    full = rollup.read_heat(db, "15", js, 0)
    wd = sorted({k[0] for k in full})[:2]
    sub = rollup.read_heat(db, "15", js, 0, weekdays=wd, hours=[8, 9])
    assert sub == {k: v for k, v in full.items() if k[0] in wd and k[1] in (8, 9)} and sub
