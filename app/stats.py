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
