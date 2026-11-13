# Stats lab, all routes (addendum to the Stats lab design)

Extends `docs/specs/2026-11-13-stats-lab-design.md`. Everything there still applies (pure stats in `app/analysis/lab.py`, day-block bootstrap, `low_n`, `enough_days`, explanations, finite JSON). This addendum only covers pooling every tracked route.

## Goal

A page `#/lab` (no route) showing the same three Stats lab sections pooled across all tracked routes, with the same filter bar.

## User decisions (2026-11-13)

- Persistence is computed per route with that route's own stop order, and pooled. A route whose stop order is unavailable is skipped and the page says how many routes were left out.
- The variance chart gets a fifth factor, `route`, in the all-routes view only.
- Reachable from the sidebar (a "Stats lab (all routes)" link under "All routes") and from the existing All routes page.

## API

`GET /api/all/lab/distribution`, `/api/all/lab/variance`, `/api/all/lab/persistence`. Same query parameters (`P`), same response shapes as the per-route endpoints with these differences:

- No `route` key. Add `routes` (number of tracked routes with data in the result) and `filters.truncated_routes` is not used; `filters.truncated` / `since` behave as for per-route (see Pooling).
- Variance: `factors` has five entries in order `hour, weekday, stop, date, route`. `route.levels` = number of routes with data. A stop id shared by several routes is one `stop` level (pooled by stop id).
- Persistence: add `routes_used` (list of route ids) and `routes_skipped` (list of route ids whose stop order was unavailable or which had no matched trips). `order_source` is `"server"` when at least one route was used, else `"none"` (and the usual empty result).

## Pooling

- Distribution and variance run one pooled scan with `where route in (<tracked routes>)` through the existing `_heavy`/`_cap`/`_scan_arrivals` machinery. The row cap therefore applies to the pooled set (the most recent `MAX_ROWS` arrivals, with `filters.truncated`/`since` echoed as usual), which keeps the bootstrap cost the same as one large route.
- A bootstrap "day" is a Dublin service date pooled across routes (resampling whole days, as before).
- Persistence uses `_each_tracked` (per route, each capped on its own), builds per-day additive sums per route with the existing `autocorr_sums`/`recovery_sums`, and adds the vectors per service date across routes. `n_trips` is the total over used routes.
- No tracked routes: HTTP 200, `n: 0`, empty arrays (distribution/variance), `routes: 0`, `routes_used: []`.

## Frontend

- `#/lab` with no route renders the same three sections from the `/all/lab/*` endpoints, heading `Stats lab for all routes`, with a one-line note of how many routes are pooled; the variance chart shows five bars; the persistence section shows `routes_skipped` ("N routes left out: stop order unavailable") when non-empty.
- Sidebar link under "All routes" and a link on the All routes page; breadcrumb back to `#/all`.
- Explanations: the `variance` popover gains a sentence on the route factor (all-routes view only).

## Non-goals

Per-route breakdown tables inside the pooled page; weighting routes equally (every arrival counts once, so busy routes dominate); cross-route stop-order alignment.

## Failure modes

- Busy routes dominate the pooled figures (every arrival counts once): stated in the page note, accepted.
- The same physical stop shared by routes is one `stop` level: accepted.
- Persistence for many routes means many server stop-order calls: `_directions` already caches (`STOPS_TTL`); routes are fetched with the existing `_each_tracked` concurrency.
- Pooled `MAX_ROWS` truncation drops older days across all routes: the existing truncated notice covers it.
