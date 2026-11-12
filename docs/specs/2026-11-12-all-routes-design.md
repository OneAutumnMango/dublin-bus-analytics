# All routes aggregated view

Status: approved design, 2026-11-12.

## Scope
An "All routes" button at the top of the sidebar's Tracked routes list opens `#/all`, a page showing route-independent analytics pooled over every currently tracked route, plus a per-route comparison of mean and median lateness.

Non-goals:
- Stop-wise data on the aggregate page.
- Including untracked routes that still have stored history ("all" = the current tracked list).
- Weighting pooled numbers by route (pooling is per arrival; busy routes weigh more; the page says so).
- Forecast errors beyond +/-3600 s keep full weight in n/mean/MAE but are clamped to +/-3600 s for p5/p95.
- Optimising multi-route scan time unless Pi measurements show a problem.

## Page (`web/js/pages/all.js`, route name `all`)
Registered in `app.js` (`PAGES`, `NAV` -> routes). Reuses `filterBar`/`setup` from `pages/common.js`, so date range, on-time window and confidence apply as on the per-route analytics page. Each section loads independently via `region()`.

1. **Compare routes**
   - Chart: one row per route, mean and median as two markers on a shared early/late axis. Extend `rangeGeometry`/`renderRangeChart` with an optional `median` marker (ring) beside the mean diamond.
   - Table: route, n, mean, median, on-time %, reliability. Header buttons sort (`aria-sort`); default median descending (latest first). n < 10 greyed out. Routes with n = 0 listed as "no data" and omitted from the chart.
2. **Heatmap**: pooled weekday x hour grid via `renderHeatmap`. Weekday/hour filters stripped as in `heatQuery`.
3. **How accurate are the live predictions?**: pooled forecast horizons table. Percentile setting stripped as in `forecastQuery`.

Note under the title: "Pooled by arrival, not by route."

Empty state when no routes are tracked, pointing at the sidebar search.

## Sidebar
`sidebar.js` renders an "All routes" link (`#/all`) above the tracked list. `markActive` sets `aria-current` on it when the hash is `#/all`.

## API (`app/api/routes.py`)
All under the existing router; params via the existing `P` dependency.

| Endpoint | Response |
|---|---|
| `GET /all/routes` | `{filters, routes:[{route, n, low_n, mean, median, on_time_pct, reliability, p_low, p_high}]}` |
| `GET /all/heatmap` | `{filters, n, cells}` same shape as the per-route heatmap |
| `GET /all/forecast` | `{filters, horizons}` same shape as the per-route forecast |

Implementation: loop over `poller.tracked()` (in a thread) sequentially. Per route, call the existing `_heavy` (so `_cap` and the heavy gate apply per route, the 500k cap is per route, and other requests interleave between routes) and the existing per-route worker. Merging:
- routes: per-route `_summ` output, trimmed to the fields above.
- heatmap: merge cells by (n, sum) exactly as `_heatmap_db` does across chunks.
- forecast: mean/MAE from running (n, sum, abs-sum); p5/p95 from a merged 1-second-bin histogram per horizon (errors clamped to +/-3600 s), interpolated between neighbouring order statistics, so equal to `np.percentile` for errors inside that range.
- `filters.truncated_routes`: list of routes whose cap was hit; UI shows the existing truncation notice naming them.

Errors: no tracked routes -> 200 with empty data. Other failures follow the existing handling (inline error with Retry per region).

## Testing
Backend (pytest, one file per process, `ulimit -v 2500000`, `OPENBLAS_NUM_THREADS=1`):
- One tracked route: `/all/heatmap` and `/all/forecast` equal the per-route results.
- Two routes: pooled heatmap cells equal a hand-computed merge.
- `/all/routes` mean/median equal per-route summary values.
- `truncated_routes` reporting; zero tracked routes; untracked route data excluded.

Frontend (node tests): sort logic, sidebar button and `aria-current`, page render, contract test extended for the new endpoints.

## Rollout
No schema change, no migration. Purely additive endpoints and UI.
