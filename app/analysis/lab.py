"""Pure distribution statistics for the stats lab. No I/O, no db/model imports.

Delays are seconds (actual - scheduled). Invalid *parameters* raise ValueError;
empty/degenerate *data* yields None fields, never NaN/inf.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

LOW_N = 10
MIN_DAYS = 5
BOOT_B = 200
BOOT_SEED = 20261113


def _clean(delays) -> np.ndarray:
    return np.sort(np.array([d for d in delays if d is not None], dtype=float))


def ecdf_points(delays, k: int = 200) -> list[tuple[float, float]]:
    x = _clean(delays)
    n = len(x)
    if n == 0:
        return []
    idx = np.arange(n) if n <= k else np.unique(np.linspace(0, n - 1, k).round().astype(int))
    return [(float(x[i]), float((i + 1) / n)) for i in idx]


def qq_points(delays, k: int = 100) -> list[tuple[float, float]]:
    if k < 2:
        raise ValueError("k must be >= 2")
    x = _clean(delays)
    if len(x) == 0:
        return []
    p = (np.arange(k) + 0.5) / k
    return [(float(z), float(q)) for z, q in zip(norm.ppf(p), np.quantile(x, p))]


def shape_stats(delays) -> dict:
    x = _clean(delays)
    n = len(x)
    if n == 0:
        return dict(n=0, median=None, p95=None, buffer=None, skew=None, kurtosis=None)
    med, p95 = float(np.median(x)), float(np.percentile(x, 95))
    sd = x.std()
    z = (x - x.mean()) / sd if sd > 0 else None
    return dict(
        n=n, median=med, p95=p95, buffer=p95 - med,
        skew=float((z**3).mean()) if z is not None and n >= 3 else None,
        kurtosis=float((z**4).mean() - 3) if z is not None and n >= 4 else None,
    )


def day_bootstrap(days, stat_fn, level: float = 0.95, B: int = BOOT_B,
                  seed: int = BOOT_SEED, min_days: int = MIN_DAYS):
    """Percentile CIs from resampling whole days with replacement.

    stat_fn(list_of_days) -> {key: float | None}. A key maps to None when there are
    fewer than min_days days or under half the resamples produced a value.
    """
    if not 0 < level < 1:
        raise ValueError("level must be in (0, 1)")
    keys = list(stat_fn(days))
    n = len(days)
    if n < min_days:
        return {k: None for k in keys}
    rng = np.random.default_rng(seed)
    draws = [stat_fn([days[i] for i in rng.integers(0, n, n)]) for _ in range(B)]
    out = {}
    for k in keys:
        vals = [d[k] for d in draws if d.get(k) is not None]
        if len(vals) * 2 < B:
            out[k] = None
        else:
            lo, hi = np.percentile(vals, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
            out[k] = (float(lo), float(hi))
    return out
