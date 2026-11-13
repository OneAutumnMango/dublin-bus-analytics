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


def stop_changes(trip_rows, direction_lists) -> dict:
    """Per-stop change in delay: {stop_id: [n, sum]}.

    trip_rows: {trip occurrence: {stop_id: delay}}; a stop seen more than once on one occurrence
    (loop trips) is ambiguous and stored as None. direction_lists: {direction: [stop_id, ...]} in
    travel order. Rule per list position i: i > 0 -> delay(stop) - delay(previous stop), only when the
    trip has both. i == 0 -> the stop's own delay (baseline on time), but only when the trip also has
    the list's second stop, so a trip of another direction passing through a shared terminus does not
    count there. A stop repeated within one list counts once per trip (first position). A stop in
    several lists accumulates from each. Work is O(rows + matches): each trip's own stops are looked up
    in a precomputed stop -> [(list, position)] index.
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
