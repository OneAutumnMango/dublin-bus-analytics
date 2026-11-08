# Dublin Bus Analytics — Design

## Goal
A companion webapp in the server's docker compose. User picks bus routes; it logs every stop's
arrival data over time, stores it compactly and permanently, and shows delay analytics vs the
scheduled timetable.

## Scope / non-goals
In: route selection, 30s polling logger, storage + backup, route/stop/trip views, confidence
intervals, on-time %, hour x weekday heatmap, delay accumulation along route, forecast accuracy.
Out: auth (localhost-only like the server), multi-user, alerting, mobile app integration,
sub-30s resolution, any direct NTA access.

## 0. Server change (dublin-bus-server)
Static timetable lives in the server (`ArrivalsService.scheduledEpochs`) but is not exposed.
Add `scheduledEpochSeconds: Long?` to `ArrivalResponse` and `TripStopCall`/its response
(null only when the timetable has no matching call). Backward-compatible additive field.
Server tests updated. Analytics uses this as the schedule source; it never derives
`expected - delay`.

## 1. Architecture
- New repo `dublin-bus-analytics`, one container `analytics`, added to
  `dublin-bus-server/docker-compose.yml` (depends_on server, volume `analytics-data:/data`,
  port `127.0.0.1:${ANALYTICS_PORT:-8081}:8081`, env `SERVER_URL=http://dublin-bus-server:8080`).
- Python 3.12, FastAPI + uvicorn, stdlib sqlite3, numpy/scipy for stats. UI: static
  HTML/JS + Chart.js served by FastAPI, no build step.
- Layout: `app/{poller,store,finalise,stats,api}.py`, `web/`, `tests/`, `Dockerfile`, README.
- Poller runs as an asyncio task in the same process; one process, one writer.

## 2. Logging
- Tracked routes are chosen in the UI (search via `/v1/routes`); stored in `meta.db`.
- Every 30s, per tracked route: `GET /v1/routes/{r}/arrivals` (all stops, one call).
- Per (trip, stop) the latest sample is compared with the previous; a sample row is written
  only when dueMinutes, expected, or realtime flag changed (change-only logging).
- Finalise a (trip, stop) arrival when dueMinutes reaches 0, or when the pair disappears
  from the list after its last sample was due <= 2 min. Writes one `arrivals` row.
  - actual = last `expectedEpochSeconds` seen with due <= 2 min.
  - `confidence`: 1 if a due==0 sample was seen, else 0 (vanished). Stats can filter.
  - Scheduled-only trips (isRealtime=false throughout) are logged with `realtime=0` and
    excluded from delay stats.
- Pairs that vanish with last due > 2 min are dropped (not arrivals), counted in a metric.
- Server errors: log, retry next tick, record a gap row (start/end) so stats can ignore it.

## 3. Storage (permanent, compact, backupable)
All under `/data`, SQLite WAL, integer ids and epoch seconds.
- `meta.db` (small, forever): `routes_tracked`, `stops`, `trips`, `arrivals`
  (trip_id, stop_id, seq, scheduled, actual, delay, confidence, realtime),
  `forecast` (arrival_id, horizon_min, predicted_epoch, error_s) for horizons 30/20/10/5/2,
  computed at finalisation from raw samples, `gaps`.
- `samples-YYYY-MM.db` (raw time series): `sample(ts, trip_id, stop_id, seq, due_min, expected,
  realtime)` WITHOUT ROWID, keyed (trip_id, stop_id, seq, ts). `seq` holds the scheduled epoch
  of that call (the per-visit discriminator: distinguishes loops and recycled trip ids; every arrival carries it from
  the server's `scheduledEpochSeconds`; arrivals without one are skipped). REVISED 2026-11-08: the original
  plan derived an occurrence index from `/v1/trips/{id}`, but those stop calls come from the live feed only. Finalisation reads the current
  month (and previous month at a boundary).
- Closed months are immutable; a nightly job gzips them. Gzipped months are readable via
  decompress-on-demand for trip drill-down.
- Backup: nightly + on demand (`POST /api/backup`, UI button) writes a consistent snapshot of
  all DBs via SQLite online backup API to `/data/backups/`; `GET /api/backup/download`
  returns a tar. Restore = replace `/data`. README documents it.
- Analytics queries use only `meta.db`; raw samples are only needed for trip drill-down.

## 4. API (JSON, `/api`)
`routes`, `routes/tracked` (GET/PUT/DELETE), `routes/{r}/stops`, `routes/{r}/summary`,
`stops/{s}/arrivals`, `stops/{s}/stats?ci=0.95&route=&window=`, `trips/{id}/series`,
`routes/{r}/heatmap`, `routes/{r}/accumulation`, `routes/{r}/forecast`, `backup`.
Common filters: date range, direction, hour/weekday, min confidence.

## 5. UI
- Routes page: search, track/untrack, per-route status (last poll, samples, gaps).
- Route view: stops in order; per stop mean delay with min/max whiskers and a configurable
  percentile band vs scheduled (0 line); toggles for CI level, window, hours.
- Stop view: delay distribution histogram, CI of actual arrival relative to scheduled,
  on-time % (window default -1..+5 min, configurable), reliability score, trip list; trip
  click shows due-minutes time series (reaching 0 at arrival).
- Analytics: hour x weekday heatmap; delay accumulation along route (segment gain/loss);
  forecast accuracy (error by horizon).
- CI method: t-interval on mean delay; empirical percentile interval of arrival delay;
  bootstrap not used (YAGNI). Minimum n shown on every statistic; n < 10 greyed.

## 6. Error handling
Server unreachable -> backoff, gap recorded, UI banner. Corrupt/locked DB -> fail loudly.
Unknown route/stop -> 404. Invalid CI/window params -> 422.

## 7. Testing
- Unit: finaliser (due==0, vanish, <=2 min rule, loops, scheduled-only), change-only logger,
  forecast extraction, stats (CI, on-time, accumulation) against hand-computed fixtures.
- Integration: fake server fixture -> poller -> DB -> API.
- Server: test for new `scheduledEpochSeconds`.
- Compose smoke: bring up both, track a route, check rows appear.

## 8. Failure modes
Critical (handled above): bus vanishing before due 0 biases delay -> confidence flag;
scheduled-only trips -> excluded. Minor / accepted: +-30s time resolution; gaps during
outages; loops-back trips share a stop (keyed by seq); trip ids may recycle across days
(arrivals also keyed by service date); no auth.
