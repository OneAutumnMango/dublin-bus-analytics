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
    delays = [d for d in delays if d is not None]
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
    delays = [d for d in delays if d is not None]
    n = len(delays)
    if n == 0:
        return {"n": 0, "pct": None}
    return {"n": n, "pct": 100.0 * sum(lo_s <= d <= hi_s for d in delays) / n}


def reliability_score(delays, lo_s: int = -60, hi_s: int = 300) -> float | None:
    """on_time_pct / (1 + std/300). None for n < 2 (spread is undefined)."""
    delays = [d for d in delays if d is not None]
    r = on_time_pct(delays, lo_s, hi_s)
    if r["n"] < 2:
        return None
    return r["pct"] / (1 + _f(np.asarray(delays, dtype=float).std(ddof=1)) / 300)


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
                weekdays=None, hours=None, minutes=None) -> list:
    """Keep rows passing all filters. since/until: inclusive epoch bounds on `scheduled`;
    weekdays (0=Mon)/hours are Dublin-local. minutes=(lo, hi): Dublin-local minute of day, inclusive;
    lo > hi wraps midnight. A row without `scheduled` fails any time filter."""
    timed = not (since is None and until is None and weekdays is None and hours is None and minutes is None)
    out = []
    for r in rows:
        if (r.get("confidence") or 0) < min_confidence or (realtime_only and not r.get("realtime")):
            continue
        if timed:
            s = r.get("scheduled")
            if s is None or (since is not None and s < since) or (until is not None and s > until):
                continue
            d = _local(s)
            if (weekdays is not None and d.weekday() not in weekdays) or (
                    hours is not None and d.hour not in hours):
                continue
            if minutes is not None:
                m, lo, hi = d.hour * 60 + d.minute, minutes[0], minutes[1]
                if not (lo <= m <= hi if lo <= hi else (m >= lo or m <= hi)):
                    continue
        out.append(r)
    return out


def accumulation(rows_by_trip_ordered) -> list[dict]:
    """Delay build-up along a route.

    Input: list of trips (or dict trip_id -> trip); each trip is a list of rows with
    keys stop_id, seq, delay (sorted by seq here, so order is not required).
    Output per seq: mean_delay over trips, and segment_delta = mean over trips of
    delay(seq) - delay(seq-1) for trips that have both *adjacent* seqs (None if none).
    n = trips with a delay at that seq; n_delta = trips contributing to segment_delta
    (use these for the n<10 flag). mean_delay is over all trips present at that seq, so it is
    NOT the cumulative sum of segment_deltas (different trip sets); group by route variant.
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
             "segment_delta": _f(np.mean(deltas[s])) if deltas[s] else None,
             "n": len(delays[s]), "n_delta": len(deltas[s])}
            for s in sorted(delays)]


def stop_changes(trip_rows, direction_lists) -> dict:
    """Per-stop change in delay: {stop_id: [n, sum]}.

    trip_rows: {trip occurrence: {stop_id: delay}}; a stop seen more than once on one occurrence
    (loop trips) is ambiguous and stored as None. direction_lists: {direction: [stop_id, ...]} in
    travel order. Rule per list position i: i > 0 -> delay(stop) - delay(previous stop), only when the
    trip has both. i == 0 -> the stop's own delay (baseline on time), but only when the trip also has
    the list's second stop, so a trip of another direction passing through a shared terminus does not
    count there. A stop repeated within one list counts once per trip (first position). A stop in
    several lists accumulates from each. Work is O(rows + matches): each trip's own stops are looked up
    in a precomputed stop -> [(list, position)] index. Does not touch segment_delta/accumulation.
    """
    lists = list(direction_lists.values())
    index: dict[str, list] = {}
    for k, stops in enumerate(lists):
        seen = set()
        for i, sid in enumerate(stops):
            if sid not in seen:
                seen.add(sid)
                index.setdefault(sid, []).append((k, i))
    acc: dict[str, list] = {}
    for t in trip_rows.values():
        for sid, d in t.items():
            if d is None:
                continue
            for k, i in index.get(sid, ()):
                stops = lists[k]
                if i > 0:
                    p = t.get(stops[i - 1])
                    if p is None:
                        continue
                    c = d - p
                else:
                    if len(stops) > 1 and t.get(stops[1]) is None:
                        continue
                    c = d
                a = acc.setdefault(sid, [0, 0])
                a[0] += 1
                a[1] += c
    return acc


def forecast_accuracy(forecast_rows) -> dict:
    """{horizon_min: {n, mean_error, mae, p_low, p_high}} from rows with keys
    horizon_min and error_s (or error; error_s preferred) (seconds, actual minus predicted; positive = bus later than predicted). p_low/p_high = 5th/95th pct."""
    acc = defaultdict(list)
    for r in forecast_rows:
        e = r.get("error_s")
        if e is None:
            e = r.get("error")
        if r.get("horizon_min") is not None and e is not None:
            acc[r["horizon_min"]].append(e)
    out = {}
    for h, errs in acc.items():
        a = np.asarray(errs, dtype=float)
        out[h] = {"n": len(errs), "mean_error": _f(a.mean()), "mae": _f(np.abs(a).mean()),
                  "p_low": _f(np.percentile(a, 5)), "p_high": _f(np.percentile(a, 95))}
    return out


def heatmap_to_json(h: dict) -> list[dict]:
    """Flatten heatmap() output to JSON-friendly rows sorted by (weekday, hour)."""
    return [{"weekday": wd, "hour": hr, **v} for (wd, hr), v in sorted(h.items())]
