# Audit fixes: design

Date: 2026-11-12. Source: ponytail audit of this repo. Findings 1-8 are in scope; finding 9 (drop scipy) is skipped by decision.

## Scope and non-goals

In scope: one-process note, `service_date` index, leaner backups with an archive copy of old months, bounded concurrent map fetches, optional Host allow-list, dead-code removal, forecast `filters` echo, export join.

Non-goals: app-level authentication, removing `PORT`, replacing scipy, changing the tracker or stats maths, protecting a public tunnel beyond documenting that Cloudflare Access (login) must sit in front of it.

## A. One process only (finding 1)

README section and a comment above the Dockerfile `CMD`: run exactly one process, never `--workers N`. The poller, export gate, SQLite lock, backup lock and removal jobs are per process. No code change.

## B. Index (finding 2)

Add `create index if not exists ix_arr_date on arrivals(service_date, route);` to `META_SCHEMA` in `app/core/db.py`. Existing databases get it on the next open. The one-time build on a multi-million-row table can pause polling for a few seconds (the tracker opens the DB on the event loop); the README notes this.

## C. Backups (finding 3)

- `snapshot()` tars `meta.db` and the plain `samples-*.db` files only. `.gz` months are no longer added.
- New setting `BACKUP_ARCHIVE_DIR`. Default `<backup dir>/archive`. An empty string disables the archive copy.
- After a successful snapshot, each `samples-YYYY-MM.db.gz` in the data dir that is not in the archive dir is copied there: write to a temp file in the archive dir, fsync, rename into place (never overwrite). If a same-named file already exists with a different size, log a warning and leave it. Archive files are never pruned.
- An archive failure (disk full, permissions) is logged and cleaned up (temp file removed). It never fails the snapshot or the scheduler run.
- Runs under the existing backup `_lock`.
- Restore = latest tar plus the archive folder. README documents this and the new variable.
- The manual `/api/backup` endpoint and the scheduler both call `snapshot()`, so both get the archive copy.

## D. Concurrent map fetches (finding 4)

`/api/map` and `/api/map/shapes` fetch per-route stop lists and shapes with `asyncio.gather`, limited by a semaphore of `settings.server_concurrency` (default 2). Results keep the order of the tracked-routes list. Failure handling stays per route, as today (unavailable routes are listed, shape failures give no shape).

## E. Optional Host allow-list (finding 5)

- New env `ALLOWED_HOSTS`: comma list of host names. Wildcards like `*.example.com` are supported (Starlette `TrustedHostMiddleware`).
- Unset or empty: no check, every Host is accepted (today's behaviour).
- Set: only the listed hosts plus `localhost`, `127.0.0.1` and `[::1]` are accepted, so the Docker healthcheck keeps working. Anything else gets Starlette's plain 400 "Invalid host header" (it does not name the host; the app log does not either, so check the `Host` your proxy sends with `curl -v`).
- Added in `create_app` only when set. Parsed in `Settings.from_env` like the other variables.
- README, `analytics.env.example` and `docker-compose.example.yml` get a commented example with a LAN name and a tunnel host name. README states the check only protects once the variable is set, and that a public tunnel needs Cloudflare Access in front.

## F. Cleanup (finding 6)

Delete `stats.accumulation`, `stats.forecast_accuracy`, `stats.heatmap_to_json`, `ServerClient.status`, the `meta_kv` table (schema and the mention in the `day.py` docstring), and `Arrival.vehicle_id` and `possibly_passed` (and their parsing in `ServerClient.route_arrivals`), together with tests that cover only them. Existing databases keep an empty unused `meta_kv` table; it is harmless. `Settings.port` / `PORT` stays.

## G. Forecast filters echo (finding 7)

`/routes/{r}/forecast` and `/all/forecast` omit `pct_lo` and `pct_hi` from `filters`, because their `p_low`/`p_high` are always the 5th/95th percentile. Parameters are still accepted and validated, so existing callers do not break.

## H. Export join (finding 8)

`iter_export` fetches forecasts for each batch of arrivals with one query (`arrival_id in (...)`, ordered by arrival then horizon) instead of one query per arrival. The JSON output is byte-identical.

## Error handling

Archive copy and Host check fail safe as described. Map fetch errors stay per route. No new failure reaches the poller or tracker.

## Testing

- B: query-plan test shows `ix_arr_date` used for `service_date` lookups with and without route.
- C: tar excludes `.gz`; archive gets each gz once; second run copies nothing; differing existing file is kept and warned; copy failure leaves no temp file and the snapshot still succeeds; empty setting disables the copy; config parsing tests.
- D: all routes fetched, concurrency never exceeds the cap, output order and per-route failure behaviour unchanged.
- E: unset allows any Host; set allows listed, wildcard and localhost hosts; others get 400; healthcheck path works.
- F: existing suite still passes after removal.
- G: forecast responses lack the two keys; other endpoints unchanged.
- H: export output equals the previous output on seeded data.
- Final: full `pytest` (run heavy files one at a time with `ulimit -v 2500000`) and `node --test web/test/`.

## Failure modes

- Locked out by the Host check (forgotten `ALLOWED_HOSTS` entry, changed LAN IP): minor. It fails closed with a clear 400. Wildcards and `.local` names are documented.
- Half-written archive file: handled by temp file plus rename and cleanup.
- Too many simultaneous requests to the bus server: handled by the `SERVER_CONCURRENCY` cap.
- Cloudflare tunnel Host header differs from the public name: verify with a sample request; the 400 message names the Host seen.
- Index build pause on a large existing DB: minor, one time, documented.
