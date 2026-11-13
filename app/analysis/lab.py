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
