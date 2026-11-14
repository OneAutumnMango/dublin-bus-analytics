# Overview rollups (speed-up of `/all/routes`, `/all/heatmap`, `/all/forecast`)

Approved by the user (afk, "approve all specs and plans"). Branch `perf/rollups`.

## Problem (measured on a 3-day real copy: 239k arrivals, 1.12M forecast rows, 10 routes)
| Endpoint | Alone | Cause |
|---|---|---|
| `/all/routes` | 0.74 s | rescans every arrival |
| `/all/heatmap` | 0.74 s | rescans the same arrivals |
| `/all/forecast` | 1.97 s | 1.5 s fetching ~670k joined rows |
| the 3 at once (the page) | 6.4 s | `_DB_LOCK` + GIL contention: slower than serial (3.5 s) |

Every request is O(all history). The user expects 10-100x more data, so the cost must stop growing with history.

## Goal and non-goals
- Overview endpoints cost O(days x routes) for closed days plus a raw scan of only the recent/partial days.
- Responses stay **numerically identical** to today for every filter combination (parity-tested), except nothing else changes in the JSON.
- Non-goals: per-route pages, Stats lab, map (still raw scans; same rollups can serve them later); a monthly second tier (needed only beyond ~100 routes x 1 year); background pre-building (lazy build on first request).

## Design
**Closed day** = `service_date` earlier than (Dublin today - 1 day). Rows are inserted once at finalisation (`insert or ignore`), at most ~6 h late, so a closed day is immutable except `remove_day`.

**Tables** (in `meta.db`, `create table if not exists`, so existing databases upgrade in place):
- `ru_done(route, service_date, min_s, max_s)`: marker that the day is rolled; min/max `scheduled` of its realtime arrivals.
- `ru_delay(route, service_date, conf, n, sum_d, sum_d2, hist)`: arrivals with realtime=1, scheduled and delay not null. `hist` = zlib of int32 counts of delay clamped to +/-3600 at 1 s (7201 bins).
- `ru_heat(route, service_date, wd, hr, conf, n, sum_d)`: exact per Dublin-local weekday/hour.
- `ru_fc(route, service_date, conf, horizon, n, sum_e, abs_e, hist)`: forecast errors (same clamp), arrivals as above but delay may be null.
- `conf` is the arrival's confidence (0 or 1); a request with `min_confidence=m` sums `conf >= m`.

**Request path** (`/all/*` only), per tracked route:
1. Eligible if no `minutes` filter, and (routes/forecast) no weekdays/hours filter, and (routes) window within +/-3600. Otherwise the existing raw path runs unchanged.
2. Build missing closed-day rollups for the route (one transaction per route-day, lazy, under `_DB_LOCK`).
3. Rolled days used = in `ru_done` with `since <= min_s` and `max_s <= until` (partial days stay raw).
4. Raw scan (existing code, existing cap/heavy gate) with `service_date not in (rolled days)` merged with the rollup parts.
5. Summary computed from (n, sum, sum2, hist): mean/std/CI exact; median/percentiles/on-time from the histogram, equal to numpy on integer data inside the clamp. The existing forecast helper `_hist_percentile` is reused.

**Contention**: `_each_tracked` takes a single-permit gate so the three overview scans run one after another (first table appears sooner, total time lower).

**remove_day** deletes the matching rollup rows (same transaction scope as arrivals) so a removed day is never resurrected from a rollup.

## Failure-mode check
- *Rollup drifts from raw (critical)*: parity tests compare rollup path against the raw path on random data for random filters and mixed rolled/raw days; also checked on the real copy.
- *Late insert into a rolled day*: closed-day margin of one full extra day exceeds the tracker's ~6 h hold; a repeated `remove_day` also clears rollups. Residual risk accepted (non-goal: fingerprinting).
- *Year of 100 routes*: ~180k forecast blobs per request (~10 s); documented, second tier is the follow-up.
- *Clamp*: delays beyond +/-1 h are clamped for percentiles only (sum/sum2 stay exact); same precedent as the forecast endpoint.
