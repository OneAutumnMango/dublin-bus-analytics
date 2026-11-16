# Result cache: measurements (2026-11-16)

## What was measured

The real backup from Nov 16: 6.1M samples, 347k arrivals, 1.6M forecast rows, 10 tracked routes, about 5 days of data. Measured on a dev machine that is faster than the Raspberry Pi, with the poller disabled so the data stayed fixed. Each endpoint was requested once with the cache off (live) and once with the cache on (a hit on the stored result).

| Endpoint | Live (cache off) | Stored result (cache hit) |
|---|---|---|
| `/api/all/routes` | 13.0 s | 2 ms |
| `/api/all/heatmap` | 0.4 s | 2 ms |
| `/api/all/forecast` | 1.2 s | 2 ms |
| `/api/map` | 2.9 s | 4 ms |
| `/api/map/shapes` | 0.3 s | 5 ms |
| `/api/routes/15/summary` | 0.2 s | 2 ms |
| `/api/routes/15/heatmap` | 0.2 s | 2 ms |
| `/api/routes/15/forecast` | 1.8 s | 2 ms |
| `/api/routes/15/accumulation` | 0.3 s | 2 ms |
| `/api/routes/7/summary` | 0.1 s | 2 ms |
| `/api/routes/E1/summary` | 0.3 s | 2 ms |
| `/api/all/lab/distribution` | 4.9 s | 3 ms |
| `/api/all/lab/variance` | 5.1 s | 2 ms |
| `/api/all/lab/persistence` | 2.4 s | 2 ms |
| `/api/routes/15/lab/distribution` | 0.7 s | 2 ms |
| `/api/routes/15/lab/variance` | 0.5 s | 2 ms |
| `/api/routes/15/lab/persistence` | 0.4 s | 2 ms |

A recompute (`fresh=1`, what the Refresh button and the hourly warmer do) runs the same live computation, so it costs what the Live column shows, give or take noise. The `x_cache` column in run-on.log is null only because the benchmark script read a capitalised header name from uvicorn's lowercase headers. The headers were confirmed separately: `x-cache: hit`, `x-cache: bypass`, `x-computed-at: <epoch>`, and no cache headers on uncached paths.

## Parity

All 17 responses were byte-identical between cache off and cache on (`cmp`), and every cache hit equalled the stored fresh result.

## Footprint

17 entries = 0.19 MB gzipped, largest body 77 KB, plus 10 persisted stop lists (about 2-5 KB each).

## Caveats

- The very first `/api/all/routes` in the cache-off run took 13.0 s. The cache-on run started from an identical copy and recomputed the same endpoint first in 0.4 s, so this is probably a one-off cold file-cache effect, not rollup building. It was not reproduced or explained further, so treat the 13.0 s as an unexplained outlier and do not read it as a typical cost.
- Each endpoint was timed once per mode, so single-run noise applies (for example `/api/map` 2.9 s live against 4.5 s on recompute, and route 15 forecast 1.8 s against 0.5 s).
- Timings are from a faster machine than the Pi, so absolute numbers on the Pi will be larger, but a hit remains a single indexed SQLite read.
