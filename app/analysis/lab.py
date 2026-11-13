"""Pure distribution statistics for the stats lab. No I/O, no db/model imports.

Delays are seconds (actual - scheduled). Invalid *parameters* raise ValueError;
empty/degenerate *data* yields None fields, never NaN/inf.
"""
from __future__ import annotations

import math

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
        vals = [d[k] for d in draws if d.get(k) is not None and np.isfinite(d[k])]
        if len(vals) * 2 < B:
            out[k] = None
        else:
            lo, hi = np.percentile(vals, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
            out[k] = (float(lo), float(hi))
    return out


def _pearson(a: np.ndarray, b: np.ndarray):
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return None
    r = float(np.corrcoef(a, b)[0, 1])
    return r if np.isfinite(r) else None


def autocorr_by_lag(trips, max_lag: int = 5) -> dict[int, dict]:
    """Pearson r of delays `lag` positions apart, pooled over trips. trips: [[(pos, delay)]]."""
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")
    pairs: dict[int, tuple[list, list]] = {g: ([], []) for g in range(1, max_lag + 1)}
    for t in trips:
        for i, (p, d) in enumerate(t):
            for q, e in t[i + 1:]:
                if q - p > max_lag:
                    break
                pairs[q - p][0].append(d)
                pairs[q - p][1].append(e)
    return {g: {"r": _pearson(np.array(a, float), np.array(b, float)), "n": len(a)}
            for g, (a, b) in pairs.items()}


def recovery_slope(trips) -> dict:
    """Least-squares slope of later delays on the trip's first-position delay."""
    xs, ys = [], []
    for t in trips:
        for _, d in t[1:]:
            xs.append(t[0][1])
            ys.append(d)
    n = len(xs)
    x, y = np.array(xs, float), np.array(ys, float)
    if n < 3 or x.std() == 0:
        return {"slope": None, "n": n}
    slope = float(((x - x.mean()) * (y - y.mean())).sum() / ((x - x.mean()) ** 2).sum())
    return {"slope": slope if np.isfinite(slope) else None, "n": n}


def autocorr_sums(trips, max_lag: int = 5) -> np.ndarray:
    """Additive sufficient statistics for autocorr_by_lag: per lag 1..max_lag
    [n, sum a, sum b, sum a^2, sum b^2, sum ab] (a earlier, b later delay), flattened to one int64 vector.
    Delays are integer seconds (truncated with int()), so the sums are exact; per-day vectors just add."""
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")
    v = [[0] * 6 for _ in range(max_lag)]
    for t in trips:
        for i, (p, d) in enumerate(t):
            d = int(d)
            for q, e in t[i + 1:]:
                if q - p > max_lag:
                    break
                e, a = int(e), v[q - p - 1]
                a[0] += 1
                a[1] += d
                a[2] += e
                a[3] += d * d
                a[4] += e * e
                a[5] += d * e
    return np.array(v, dtype=np.int64).ravel()


def recovery_sums(trips) -> np.ndarray:
    """Additive sufficient statistics for recovery_slope: [n, sum x, sum y, sum x^2, sum xy] (int64, exact)."""
    n = sx = sy = sxx = sxy = 0
    for t in trips:
        x = int(t[0][1])
        for _, y in t[1:]:
            y = int(y)
            n += 1
            sx += x
            sy += y
            sxx += x * x
            sxy += x * y
    return np.array([n, sx, sy, sxx, sxy], dtype=np.int64)


def autocorr_from_sums(vec, max_lag: int = 5) -> dict[int, dict]:
    """Same result as autocorr_by_lag, from (summed) autocorr_sums vectors. Python ints: no overflow."""
    out = {}
    for g in range(1, max_lag + 1):
        n, sa, sb, saa, sbb, sab = (int(x) for x in vec[(g - 1) * 6:g * 6])
        va, vb, cov = n * saa - sa * sa, n * sbb - sb * sb, n * sab - sa * sb
        r = None
        if n >= 3 and va > 0 and vb > 0:
            r = min(1.0, max(-1.0, cov / math.sqrt(va * vb)))
        out[g] = {"r": r, "n": n}
    return out


def recovery_from_sums(vec) -> dict:
    """Same result as recovery_slope, from (summed) recovery_sums vectors."""
    n, sx, sy, sxx, sxy = (int(x) for x in vec)
    vx = n * sxx - sx * sx
    if n < 3 or vx == 0:
        return {"slope": None, "n": n}
    return {"slope": (n * sxy - sx * sy) / vx, "n": n}


def omega_squared(codes, y, levels: int | None = None) -> float | None:
    """Adjusted one-way ANOVA effect size, clamped at 0. `levels`: codes are ints in [0, levels); skips np.unique."""
    codes, y = np.asarray(codes), np.asarray(y, float)
    if len(codes) != len(y):
        raise ValueError("codes and y must have the same length")
    n = len(y)
    if levels is None:
        _, inv = np.unique(codes, return_inverse=True)
        cnt = np.bincount(inv)
    else:
        if n and (codes.min() < 0 or codes.max() >= levels):
            raise ValueError("codes must be in [0, levels)")
        inv = codes
        cnt = np.bincount(inv, minlength=levels)
    k = int(np.count_nonzero(cnt))
    if k < 2 or n - k < 1:
        return None
    sst = float(((y - y.mean()) ** 2).sum())
    if sst == 0:
        return None
    nz = cnt > 0
    means = np.bincount(inv, weights=y, minlength=len(cnt))[nz] / cnt[nz]
    ssb = float((cnt[nz] * (means - y.mean()) ** 2).sum())
    msw = (sst - ssb) / (n - k)
    w = (ssb - (k - 1) * msw) / (sst + msw)
    return max(0.0, float(w)) if np.isfinite(w) else None
