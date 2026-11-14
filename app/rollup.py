"""Per-day rollups of closed service days, so overview endpoints stop rescanning all history.

A day is closed once its service_date is before (Dublin today - 1): arrivals are inserted once at
finalisation (at most ~6 h late), so a closed day only changes through remove_day, which calls drop_days.
Rollups are derived data and built lazily (ensure_route). Callers hold the API's DB lock and pass a
connection; nothing here opens files.

Delay and forecast-error histograms are 1 s bins clamped to +/-CLAMP (percentiles inside the clamp are
exact; sums and sums of squares stay unclamped and exact).
"""
import json
import zlib
from datetime import date, datetime, timedelta

import numpy as np

from app.analysis.stats import DUBLIN, _local

CLAMP = 3600
SIZE = 2 * CLAMP + 1
HORIZONS = (30, 20, 10, 5, 2)


def closed_before(today: date | None = None) -> str:
    return ((today or datetime.now(DUBLIN).date()) - timedelta(days=1)).isoformat()


def _pack(counts) -> bytes:
    return zlib.compress(np.asarray(counts, dtype="<i4").tobytes(), 1)


def _unpack(b) -> np.ndarray:
    return np.frombuffer(zlib.decompress(b), dtype="<i4").astype(np.int64)


def _hist(values) -> np.ndarray:
    return np.bincount(np.clip(np.asarray(values, dtype=np.int64), -CLAMP, CLAMP) + CLAMP, minlength=SIZE)


def percentile(counts, n, q) -> float:
    """Percentile q (0..1) from a histogram of n values, linear interpolation like np.percentile."""
    rank = q * (n - 1)
    lo, hi = int(np.floor(rank)), int(np.ceil(rank))
    cum = np.cumsum(counts)
    v_lo = int(np.searchsorted(cum, lo + 1)) - CLAMP
    v_hi = int(np.searchsorted(cum, hi + 1)) - CLAMP
    return float(v_lo + (v_hi - v_lo) * (rank - lo))


class Acc:
    """Running delay summary: n, sum, sum of squares (exact Python ints) and the clamped histogram."""
    def __init__(self):
        self.n = self.s = self.s2 = 0
        self.hist = np.zeros(SIZE, dtype=np.int64)

    def add_delays(self, delays) -> None:
        a = np.asarray(delays, dtype=np.int64)
        if len(a):
            self.n += len(a)
            self.s += int(a.sum())
            self.s2 += int((a * a).sum())
            self.hist += _hist(a)

    def add_row(self, n, s, s2, blob) -> None:
        self.n += n
        self.s += s
        self.s2 += s2
        self.hist += _unpack(blob)

    def summary(self, p) -> dict:
        """n, mean, median, p_low, p_high, on_time_pct, reliability; needs window_lo/hi strictly inside +/-CLAMP."""
        out = dict.fromkeys(("mean", "median", "p_low", "p_high", "on_time_pct", "reliability"))
        out["n"] = n = self.n
        if n == 0:
            return out
        out["mean"] = self.s / n
        out["median"] = percentile(self.hist, n, .5)
        out["p_low"] = percentile(self.hist, n, p.get("pct_lo", .05))
        out["p_high"] = percentile(self.hist, n, p.get("pct_hi", .95))
        lo, hi = p["window_lo"], p["window_hi"]
        out["on_time_pct"] = 100.0 * int(self.hist[lo + CLAMP:hi + CLAMP + 1].sum()) / n
        if n > 1:
            std = (max(self.s2 * n - self.s * self.s, 0) / (n * (n - 1))) ** 0.5
            out["reliability"] = out["on_time_pct"] / (1 + std / 300)
        return out


def build_day(c, route: str, day: str) -> None:
    """(Re)build one route-day inside the caller's transaction."""
    drop_days(c, [day], route)
    lo, hi = c.execute("select min(scheduled), max(scheduled) from arrivals where route=? and service_date=? "
                       "and realtime=1 and scheduled is not null", (route, day)).fetchone()
    if lo is None:
        return
    c.execute("insert into ru_done values(?,?,?,?)", (route, day, lo, hi))
    rows = c.execute("select scheduled, delay, confidence from arrivals where route=? and service_date=? "
                     "and realtime=1 and scheduled is not null and delay is not null", (route, day)).fetchall()
    heat = {}
    for conf in (0, 1):
        ds = [d for _, d, cf in rows if cf == conf]
        if ds:
            a = Acc()
            a.add_delays(ds)
            c.execute("insert into ru_delay values(?,?,?,?,?,?,?)", (route, day, conf, a.n, a.s, a.s2, _pack(a.hist)))
    for sched, d, cf in rows:
        k = _local(sched)
        h = heat.setdefault((k.weekday(), k.hour, cf), [0, 0])
        h[0] += 1
        h[1] += d
    c.executemany("insert into ru_heat values(?,?,?,?,?,?,?)",
                  [(route, day, wd, hr, cf, n, s) for (wd, hr, cf), (n, s) in heat.items()])
    marks = ",".join("?" * len(HORIZONS))
    by = {}
    for cf, h, e in c.execute(
            "select a.confidence, f.horizon_min, f.error_s from arrivals a join forecast f on f.arrival_id=a.id "
            f"where a.route=? and a.service_date=? and a.realtime=1 and a.scheduled is not null "
            f"and f.error_s is not null and f.horizon_min in ({marks})", (route, day, *HORIZONS)):
        by.setdefault((cf, h), []).append(e)
    for (cf, h), es in by.items():
        a = np.asarray(es, dtype=np.int64)
        c.execute("insert into ru_fc values(?,?,?,?,?,?,?,?)",
                  (route, day, cf, h, len(a), int(a.sum()), int(np.abs(a).sum()), _pack(_hist(a))))


def ensure_route(c, route: str, today: date | None = None) -> None:
    """Build every closed day of `route` that has arrivals and no rollup yet (one transaction per day)."""
    cut = date.fromisoformat(closed_before(today))
    first = c.execute("select min(service_date) from arrivals where route=?", (route,)).fetchone()[0]
    if first is None:
        return
    done = {r[0] for r in c.execute("select service_date from ru_done where route=?", (route,))}
    d = date.fromisoformat(first)
    while d < cut:
        day = d.isoformat()
        if day not in done and c.execute("select 1 from arrivals where service_date=? and route=? limit 1",
                                         (day, route)).fetchone():
            with c:
                build_day(c, route, day)
        d += timedelta(days=1)


def rolled_days(c, route: str, since, until) -> list[str]:
    """Rolled days lying entirely inside [since, until] (None = unbounded), ascending."""
    return [r[0] for r in c.execute(
        "select service_date from ru_done where route=? and (? is null or min_s >= ?) and (? is null or max_s <= ?) "
        "order by 1", (route, since, since, until, until))]


def drop_days(c, days, route: str | None = None) -> None:
    """Forget rollups of these service days (all routes, or one). Does not commit."""
    for t in ("ru_done", "ru_delay", "ru_heat", "ru_fc"):
        for day in days:
            c.execute(f"delete from {t} where service_date=?" + (" and route=?" if route else ""),
                      (day, route) if route else (day,))


def read_delay(c, route: str, days_json: str, mc: int) -> Acc:
    a = Acc()
    for n, s, s2, b in c.execute("select n, sum_d, sum_d2, hist from ru_delay where route=? and conf>=? "
                                 "and service_date in (select value from json_each(?))", (route, mc, days_json)):
        a.add_row(n, s, s2, b)
    return a


def read_heat(c, route: str, days_json: str, mc: int) -> dict:
    return {(wd, hr): (n, s) for wd, hr, n, s in c.execute(
        "select wd, hr, sum(n), sum(sum_d) from ru_heat where route=? and conf>=? "
        "and service_date in (select value from json_each(?)) group by wd, hr", (route, mc, days_json))}


def read_fc(c, route: str, days_json: str, mc: int, horizons=HORIZONS) -> dict:
    """{horizon: [n, sum, abs_sum, counts]}, counts indexed by clamp(error)+CLAMP."""
    acc = {h: [0, 0, 0, np.zeros(SIZE, dtype=np.int64)] for h in horizons}
    for h, n, s, ab, b in c.execute("select horizon, n, sum_e, abs_e, hist from ru_fc where route=? and conf>=? "
                                    "and service_date in (select value from json_each(?))", (route, mc, days_json)):
        if h in acc:
            a = acc[h]
            a[0] += n
            a[1] += s
            a[2] += ab
            a[3] += _unpack(b)
    return acc
