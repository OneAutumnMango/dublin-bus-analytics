"""Pure statistics over plain dicts/lists. No I/O, no db/model imports.

Delays are seconds (actual - scheduled). Invalid *parameters* raise ValueError;
empty/degenerate *data* yields None fields, never NaN/inf.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import t as student_t

DUBLIN = ZoneInfo("Europe/Dublin")


def _f(x):
    """numpy scalar -> plain float (JSON-safe)."""
    return float(x)


def delay_summary(delays, ci: float = 0.95, pct: tuple[float, float] = (0.05, 0.95)) -> dict:
    if not 0 < ci < 1:
        raise ValueError("ci must be in (0, 1)")
    lo, hi = pct
    if not (0 <= lo < hi <= 1):
        raise ValueError("pct must satisfy 0 <= lo < hi <= 1")
    n = len(delays)
    out = dict.fromkeys(
        ["mean", "min", "max", "std", "ci_low", "ci_high", "p_low", "p_high", "median"])
    out["n"] = n
    if n == 0:
        return out
    a = np.asarray(delays, dtype=float)
    mean = _f(a.mean())
    out.update(mean=mean, min=_f(a.min()), max=_f(a.max()), median=_f(np.median(a)),
               p_low=_f(np.percentile(a, lo * 100)), p_high=_f(np.percentile(a, hi * 100)))
    if n > 1:
        std = _f(a.std(ddof=1))
        half = _f(student_t.ppf((1 + ci) / 2, n - 1)) * std / n**0.5
        out.update(std=std, ci_low=mean - half, ci_high=mean + half)
    return out


def on_time_pct(delays, lo_s: int = -60, hi_s: int = 300) -> dict:
    """Share (0-100) of delays within [lo_s, hi_s], both edges inclusive."""
    if lo_s > hi_s:
        raise ValueError("lo_s must be <= hi_s")
    n = len(delays)
    if n == 0:
        return {"n": 0, "pct": None}
    return {"n": n, "pct": 100.0 * sum(lo_s <= d <= hi_s for d in delays) / n}


def reliability_score(delays, lo_s: int = -60, hi_s: int = 300) -> float | None:
    """on_time_pct / (1 + std/300); std of a single sample is treated as 0."""
    pct = on_time_pct(delays, lo_s, hi_s)["pct"]
    if pct is None:
        return None
    std = np.asarray(delays, dtype=float).std(ddof=1) if len(delays) > 1 else 0.0
    return pct / (1 + _f(std) / 300)


def _local(epoch):
    """Epoch seconds -> Europe/Dublin local datetime (DST-aware)."""
    return datetime.fromtimestamp(epoch, DUBLIN)


def heatmap(rows) -> dict:
    """{(weekday, hour): {n, mean_delay}} by Dublin-local time of `scheduled`.

    weekday: 0=Monday .. 6=Sunday. Rows lacking delay or scheduled are skipped.
    Keys are tuples (not JSON-serialisable as-is; callers convert).
    """
    acc = defaultdict(list)
    for r in rows:
        if r.get("delay") is None or r.get("scheduled") is None:
            continue
        d = _local(r["scheduled"])
        acc[(d.weekday(), d.hour)].append(r["delay"])
    return {k: {"n": len(v), "mean_delay": _f(np.mean(v))} for k, v in acc.items()}


def filter_rows(rows, min_confidence=0, realtime_only=True, since=None, until=None,
                weekdays=None, hours=None) -> list:
    """Keep rows passing all filters. since/until: inclusive epoch bounds on `scheduled`;
    weekdays (0=Mon)/hours are Dublin-local. A row without `scheduled` fails any time filter."""
    timed = not (since is None and until is None and weekdays is None and hours is None)
    out = []
    for r in rows:
        if r.get("confidence", 0) < min_confidence or (realtime_only and not r.get("realtime")):
            continue
        if timed:
            s = r.get("scheduled")
            if s is None or (since is not None and s < since) or (until is not None and s > until):
                continue
            d = _local(s)
            if (weekdays is not None and d.weekday() not in weekdays) or (
                    hours is not None and d.hour not in hours):
                continue
        out.append(r)
    return out


def accumulation(rows_by_trip_ordered) -> list[dict]:
    """Delay build-up along a route.

    Input: list of trips (or dict trip_id -> trip); each trip is a list of rows with
    keys stop_id, seq, delay (sorted by seq here, so order is not required).
    Output per seq: mean_delay over trips, and segment_delta = mean over trips of
    delay(seq) - delay(seq-1) for trips that have both *adjacent* seqs (None if none).
    """
    trips = rows_by_trip_ordered.values() if isinstance(rows_by_trip_ordered, dict) \
        else rows_by_trip_ordered
    delays, deltas, ids = defaultdict(list), defaultdict(list), {}
    for trip in trips:
        prev = None
        for r in sorted(trip, key=lambda r: r["seq"]):
            if r.get("delay") is None:
                prev = None
                continue
            s = r["seq"]
            ids[s] = r["stop_id"]
            delays[s].append(r["delay"])
            if prev is not None and prev[0] == s - 1:
                deltas[s].append(r["delay"] - prev[1])
            prev = (s, r["delay"])
    return [{"stop_id": ids[s], "seq": s, "mean_delay": _f(np.mean(delays[s])),
             "segment_delta": _f(np.mean(deltas[s])) if deltas[s] else None}
            for s in sorted(delays)]


def forecast_accuracy(forecast_rows) -> dict:
    """{horizon_min: {n, mean_error, mae, p_low, p_high}} from rows with keys
    horizon_min and error (seconds, forecast minus actual). p_low/p_high = 5th/95th pct."""
    acc = defaultdict(list)
    for r in forecast_rows:
        if r.get("horizon_min") is not None and r.get("error") is not None:
            acc[r["horizon_min"]].append(r["error"])
    out = {}
    for h, errs in acc.items():
        a = np.asarray(errs, dtype=float)
        out[h] = {"n": len(errs), "mean_error": _f(a.mean()), "mae": _f(np.abs(a).mean()),
                  "p_low": _f(np.percentile(a, 5)), "p_high": _f(np.percentile(a, 95))}
    return out
