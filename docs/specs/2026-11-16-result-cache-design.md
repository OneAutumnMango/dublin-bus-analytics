# Precomputed result cache (instant page loads)

Date: 2026-11-16. Status: draft for user review.

## Problem

Measured on the user's real data (Nov 16 backup: 6.1M samples, 347k arrivals, 10 routes, ~5 days), on a
dev machine faster than the Pi: all-routes lab 2.7-6.6 s, /map 1.6 s, /map/shapes 4.5 s cold,
/all/forecast 1.2 s, single-route calls 0.2-1.0 s each (4-6 per page). Every click and tab switch
recomputes from raw rows; only /all/{routes,heatmap,forecast} use rollups. Stop lists (server call) are cached
5 min only, and the server answered in 6-15 s per route on the Pi.

## Goal

Opening any data page or switching tabs returns a stored result at once. Raw data is kept untouched
(new analyses stay easy). Default staleness: up to 1 hour. A "Refresh" button recomputes with the newest data.

## Non-goals

- Exact results for new filter combinations without computing (they compute live, then get cached).
- Backup changes (separate spec, after this).
- Extending rollups to more endpoints (revisit at ~10-100x data).
- Cross-process / multi-instance cache coherence (single app process).

## Design

**Generic GET response cache** (`app/cache.py`), table `result_cache` in meta.db:
`key TEXT PK, body BLOB (gzip JSON), computed_at INT, last_used INT, ms INT`.

- Applies to GET under `/api/` for: `/all/*`, `/map`, `/map/shapes`, `/routes/{r}/{summary,heatmap,forecast,accumulation}`,
  `/routes/{r}/lab/*`, `/stops/{s}/stats`, `/trips/{id}/series`. Not tracked/health/search/data/backup/export.
  An explicit allow-list of path patterns, so a new endpoint is not cached by accident.
- **Key** = path + query sorted, with `since` and `until` rounded down to the hour (the UI sends a rolling
  `since`; unrounded keys would never hit). A hit may therefore reflect a window up to 1 h older than requested;
  `filters.since` in the payload shows what was actually used.
- **Hit:** return stored body with headers `X-Computed-At` (epoch) and `X-Cache: hit`; update `last_used`.
- **Miss:** compute live through the existing handler (existing gate/caps unchanged), store, return `X-Cache: miss`.
  Only HTTP 200 is stored. Concurrent identical misses share one computation (per-key lock).
- **Refresh now:** `?fresh=1` skips the lookup, recomputes, overwrites. (`fresh` is excluded from the key.)
- **Warmer** (`BackupScheduler`-style asyncio task started in the app lifespan): at startup + 60 s, then hourly,
  recompute every entry with `last_used` in the last 2 days (the UI's default start date moves at UTC midnight, so older keys are dead), oldest `computed_at` first, one at a time, through
  the app's own ASGI app (in-process), behind the existing `heavy` gate. No hard-coded list: whatever the user opens
  gets pre-warmed from then on, so new analyses are covered automatically. Skips entries computed < 50 min ago
  (restart safety). A failed recompute keeps the old body.
- **Invalidation:** clear the whole cache when data changes in a way the hourly refresh must not mask:
  `remove_day` (next to `_drop_rollups`), tracked-routes add/remove, restore from backup/import.
  Normal polling does NOT invalidate (that is the point; the hourly warm picks it up).
- **Eviction:** delete entries unused for 14 days; hard cap 500 entries / ~100 MB body total, least recently used
  first. Entries are one stored blob each, so size is bounded by pages used, not by history.
- **Stop lists / shapes:** raise `STOPS_TTL` 300 s -> 24 h and persist route stop lists in the same cache table
  (key `srv:stops:{r}`), so a cold Pi page no longer waits on the server (static GTFS). Failures stay uncached.
  Shapes TTL 1 h -> 24 h.
- **UI** (`web/js/pages/common.js`, shared get helper): read `X-Computed-At`; show "Computed 14:05 · Refresh"
  beside the filters on each data page. Refresh calls the same request with `fresh=1` and re-renders.
  While the first-ever compute runs the existing loading state is used.

## Interfaces

- `cache.lookup(key) -> (body, computed_at) | None`, `cache.store(key, body)`, `cache.clear()`, `cache.key(path, query)`.
- Middleware wraps allowed GETs; handlers unchanged.
- Settings: `RESULT_CACHE` (default true), `RESULT_CACHE_REFRESH_SECONDS` (default 3600).

## Error handling

Cache read/write failure (locked/corrupt) is logged and the request falls back to live compute; never a 5xx
from the cache layer. Warmer errors are logged per entry and do not stop the loop.

## Testing

1. Hit/miss/fresh: second identical request returns identical body with `X-Cache: hit` and no handler call.
2. Key rounding: two `since` values in the same hour share a key; different hours do not.
3. Equality on real data: for each allowed endpoint, cached payload == live payload at store time
   (run against the Nov 16 backup copy; skipped in CI if absent).
4. `remove_day` / track change clears cache; polling write does not.
5. Warmer refreshes only recently used entries and keeps the old body on failure.
6. Eviction by age and by count.
7. Timing table (before/after) on the backup copy, recorded in the PR.

## Failure modes checked

- Cold start after upgrade: first load of each page computes live (as today); entries persist across restarts. Minor.
- Stale after deletion/untracking: handled by invalidation hooks. Critical, covered by test 4.
- Warmer load on the Pi: serial, behind the heavy gate, hourly, only used entries. If it overruns the hour it
  simply starts the next pass late. Minor.
- Key mismatch from rounding shows slightly older window than requested: accepted, visible in `filters`/timestamp.

## Rollout

Additive migration (`create table if not exists result_cache`). Bump to 0.5.0. Disable with `RESULT_CACHE=false`.
