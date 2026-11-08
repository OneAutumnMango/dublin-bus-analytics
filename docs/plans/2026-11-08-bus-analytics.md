# Dublin Bus Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A companion container that logs per-stop arrival data for chosen bus routes and serves delay analytics vs the scheduled timetable.
**Architecture:** FastAPI process with an in-process asyncio poller. It polls the existing server's `/v1/routes/{r}/arrivals` every 30s, writes change-only samples to monthly SQLite files, finalises (trip, stop) arrivals into `meta.db`, and serves a JSON API plus a static Chart.js UI. A small additive server change exposes the scheduled time.
**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, numpy, scipy, pytest(+pytest-asyncio), stdlib sqlite3; Chart.js (vendored, no build); Kotlin/Ktor (server change); Docker Compose.
**Spec:** `docs/specs/2026-11-08-bus-analytics-design.md`
**Assumptions:** Server arrivals list a bus at a stop until shortly after it passes ("possiblyPassed") — will NOT give exact actual times if the server drops buses earlier than ~1 poll before arrival (those rows get `confidence=0`). Single analytics process / single writer — will NOT work with multiple replicas. Timetable timezone is Europe/Dublin.

## Global Constraints
- Analytics polls only `SERVER_URL` (default `http://dublin-bus-server:8080`); never NTA directly.
- Poll interval 30s per tracked route, one `/v1/routes/{r}/arrivals` call per route per tick.
- Finalise (trip, stop) when dueMinutes==0, or when it vanishes after last due <= 2 min; vanish with last due > 2 min is dropped and counted.
- Actual = last `expectedEpochSeconds` seen with due <= 2 min; `confidence`=1 iff a due==0 sample was seen.
- Scheduled time comes from server field `scheduledEpochSeconds`, never `expected - delay`.
- Scheduled-only (isRealtime=false throughout) arrivals are stored with `realtime=0` and excluded from delay stats.
- Forecast horizons: 30/20/10/5/2 minutes. On-time window default -1..+5 min (configurable). Stats with n < 10 flagged.
- Samples: change-only, `WITHOUT ROWID`, integer epochs/ids, monthly files `samples-YYYY-MM.db`; closed months immutable then gzipped.
- UI/API bound to `127.0.0.1:${ANALYTICS_PORT:-8081}`; no auth.
- Testing standard (user requirement: rigorous): every task's tests cover happy path, boundaries (0, 1, exact thresholds), invalid input, and failure paths, not only the cases listed in the task. Pure logic (tracker, forecast, stats) also gets `hypothesis` property tests for invariants (e.g. no duplicate arrivals however samples are ordered/repeated, CI contains the mean, min <= p_low <= median <= p_high <= max, on-time % in [0,100]). Python 3.12; dev deps `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`; project line coverage >= 90% enforced with `--cov-fail-under=90` once Task 11 lands. Bugs found get a regression test first.
- Python is 3.12 (pyenv, `.python-version` set); use a project `.venv`, never system Python.

## Review Focus
- Trip loops through the same stop twice: two distinct arrivals, not merged (Task 4).
- Server unreachable for several ticks: a gap row, no fabricated arrivals, recovery on return (Task 6).
- Bus first seen already at due 0 / due 1: still finalises once, not twice (Task 4).
- Trip past midnight / service-date boundary: arrival keyed with correct service date (Task 4).
- Stop with zero or one arrival: stats return n and nulls, not a 500 (Tasks 7, 8).

## File Structure
- `dublin-bus-server/` (Task 0): `Dto.kt`, `ArrivalsService.kt`, `Routes.kt`, tests.
- `dublin-bus-analytics/app/config.py` — `Settings`
- `app/db.py` — schemas, connection helpers, month-file routing
- `app/models.py` — dataclasses `Arrival`, `ArrivalKey`
- `app/serverclient.py` — async HTTP client to the server
- `app/tracker.py` — change-only sampling + finalisation (pure logic over a DB handle)
- `app/forecast.py` — forecast-error extraction
- `app/poller.py` — loop, gaps, tracked routes
- `app/stats.py` — pure stats functions
- `app/api.py` — FastAPI routes, `app/main.py` — app + lifespan
- `app/backup.py` — snapshot, gzip of closed months
- `web/` — `index.html`, `app.js`, `chart.umd.min.js`, `style.css`
- `tests/` — one file per module plus `fake_server.py`
- `Dockerfile`, `pyproject.toml`, `README.md`; compose edit in `dublin-bus-server/docker-compose.yml`

### Task 0: Server exposes scheduled time

**Files:**
- Modify: `dublin-bus-server/src/main/kotlin/dev/dublinbustracker/server/Dto.kt`, `ArrivalsService.kt`, `Routes.kt` (response mappers)
- Test: existing server test dir (follow the `ArrivalsService` tests already there)

**Interfaces:**
- Consumes: nothing
- Produces: JSON field `scheduledEpochSeconds: Long?` on every arrival object (`/v1/stops/{s}/arrivals`, `/v1/routes/{r}/arrivals`) and on each `stopCalls[]` entry of `/v1/trips/{id}`. Null only when no timetable call matches.

**Security flag:** `none`
**Does NOT cover:** changing `expectedEpochSeconds`/`delaySeconds` semantics; Android app changes.

- [ ] **Step 1: Failing tests** in the existing `ArrivalsService` test file:
  - `direct realtime arrival carries scheduledEpochSeconds equal to the timetable call`
  - `cascaded arrival scheduled == timetable epoch (expected - delay)`
  - `static fallback arrival scheduled == expected`
  - `tripStopCalls include scheduledEpochSeconds`
- [ ] **Step 2:** Run `cd dublin-bus-server && ./scripts/test.sh`. Expected: FAIL (missing field).
- [ ] **Step 3:** Add `scheduledEpochSeconds: Long? = null` to `Arrival`, `ArrivalResponse`, `TripStopCall` and its response; populate where the timetable epoch is computed (`closestScheduledEpoch`/`scheduledEpochs`/`scheduledArrivalsForStop`); direct arrivals look up the closest timetable epoch to `expected - delay` (reuse `closestScheduledEpoch`).
- [ ] **Step 4:** Run `./scripts/test.sh`. Expected: PASS.
- [ ] **Step 5:** Commit in `dublin-bus-server`: `git commit -m "Expose scheduledEpochSeconds on arrivals and trip stop calls"`.

### Task 1: Scaffold analytics service

**Files:**
- Create: `dublin-bus-analytics/pyproject.toml`, `Dockerfile`, `app/__init__.py`, `app/config.py`, `app/main.py`, `tests/test_health.py`, `.gitignore`

**Interfaces:**
- Consumes: nothing
- Produces: `Settings(server_url: str, data_dir: Path, poll_seconds: int = 30, port: int = 8081)` with `Settings.from_env()`; `create_app(settings: Settings | None = None) -> FastAPI` with `GET /api/health` -> `{"ok": true}`.

**Security flag:** `none`
**Does NOT cover:** poller, DB.

- [x] **Step 1:** Test `test_health_returns_ok` using FastAPI `TestClient`: `GET /api/health` is 200 and `{"ok": true}`.
- [x] **Step 2:** `cd dublin-bus-analytics && python -m pytest tests/test_health.py` -> FAIL (no module).
- [x] **Step 3:** Implement `config.py` (env: `SERVER_URL`, `DATA_DIR=/data`, `POLL_SECONDS`, `PORT`), `main.py` `create_app`, pyproject deps, Dockerfile (python:3.12-slim, `uvicorn app.main:app --host 0.0.0.0 --port 8081`).
- [x] **Step 4:** pytest -> PASS.
- [x] **Step 5:** Commit `"Scaffold analytics service"`.

### Task 2: Storage schema

**Files:** Create `app/db.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: nothing
- Produces: `open_meta(data_dir: Path) -> sqlite3.Connection` (WAL, creates tables `routes_tracked(route TEXT PK, added INT)`, `arrivals(id INTEGER PK, trip_id TEXT, stop_id TEXT, seq INT, service_date TEXT, scheduled INT, actual INT, delay INT, confidence INT, realtime INT, route TEXT, UNIQUE(trip_id,stop_id,seq,service_date))`, `forecast(arrival_id INT, horizon_min INT, predicted INT, error_s INT, PRIMARY KEY(arrival_id,horizon_min))`, `gaps(start INT, end INT)`, `meta_kv(k TEXT PK, v TEXT)`); `open_samples(data_dir: Path, ts: int) -> sqlite3.Connection` for the month of `ts` (UTC), table `sample(ts INT, trip_id TEXT, stop_id TEXT, seq INT, due_min INT, expected INT, realtime INT, PRIMARY KEY(trip_id,stop_id,seq,ts)) WITHOUT ROWID`; `samples_path(data_dir, ts) -> Path`.

**Security flag:** `none`
**Does NOT cover:** gzipping, backup (Task 9).

- [ ] **Step 1:** Tests: `test_meta_schema_has_tables`, `test_samples_file_named_by_month` (ts 2026-11-08 -> `samples-2026-11.db`), `test_arrival_unique_key_rejects_duplicate`, `test_wal_enabled`.
- [ ] **Step 2:** pytest -> FAIL. **Step 3:** implement. **Step 4:** pytest -> PASS. **Step 5:** Commit `"Add storage schema"`.

### Task 3: Server client + fake server

**Files:** Create `app/models.py`, `app/serverclient.py`, `tests/fake_server.py`, `tests/test_serverclient.py`

**Interfaces:**
- Consumes: nothing
- Produces: `@dataclass Arrival(stop_id: str, trip_id: str, route: str, due_min: int, expected: int, scheduled: int | None, delay: int | None, realtime: bool, vehicle_id: str | None, possibly_passed: bool)`; `class ServerClient: __init__(base_url: str, client: httpx.AsyncClient | None = None)`, `async route_arrivals(route: str) -> list[Arrival]` (flattens by-stop map), `async search_routes(query: str) -> list[dict]`, `async route_stops(route: str) -> dict[str, list[dict]]` (by direction), `async trip_calls(trip_id: str) -> list[dict]` (stopSequence, stopId, scheduledEpochSeconds), `async status() -> dict`. Raises `ServerUnavailable` on connection/5xx errors. `tests/fake_server.py` exposes a mutable in-memory ASGI app (`FakeServer.set_arrivals(route, {stop: [...]})`) used by later tests.

**Security flag:** `none`
**Does NOT cover:** retry/backoff (Task 6).

- [ ] **Step 1:** Tests: `test_route_arrivals_flattens_by_stop`, `test_missing_scheduled_is_none`, `test_server_500_raises_unavailable`, `test_connection_refused_raises_unavailable`.
- [ ] **Step 2-4:** fail, implement with httpx, pass. **Step 5:** Commit `"Add server client"`.

### Task 4: Tracker (change-only samples + finalisation)

**Files:** Create `app/tracker.py`, `tests/test_tracker.py`

**Interfaces:**
- Consumes: `Arrival`, `open_meta`, `open_samples`, `ServerClient.trip_calls`
- Produces: `class Tracker: __init__(data_dir: Path, seq_lookup: Callable[[Arrival], Awaitable[int]])`, `async ingest(route: str, arrivals: list[Arrival], now: int) -> IngestResult(samples_written: int, finalised: int, dropped: int)`. State per live key `(trip_id, stop_id, seq)`: last sample, last due, saw_zero. Key's seq via `seq_lookup` = rank of `scheduled` among that trip's calls at that stop (cache per trip). `service_date` = Europe/Dublin local date of `scheduled` (or `expected` if None).

**Security flag:** `none`
**Does NOT cover:** forecast rows (Task 5 hooks in at finalise), polling schedule.

- [ ] **Step 1:** Tests (hand-built `Arrival` sequences, tmp_path DB):
  - `test_unchanged_sample_not_rewritten`
  - `test_due_zero_finalises_with_confidence_1_and_delay_actual_minus_scheduled`
  - `test_vanish_after_due_2_finalises_confidence_0_actual_last_expected`
  - `test_vanish_with_due_5_dropped_not_stored`
  - `test_first_seen_at_due_0_finalises_once` (re-ingest does not duplicate)
  - `test_loop_trip_two_seq_two_arrivals`
  - `test_scheduled_only_stored_realtime_0`
  - `test_service_date_across_midnight` (scheduled 00:10 Dublin on 9th with 25:10 timetable semantics uses scheduled epoch's local date)
- [ ] **Step 2-4:** fail, implement, pass. **Step 5:** Commit `"Add tracker"`.

### Task 5: Forecast extraction

**Files:** Create `app/forecast.py`, modify `app/tracker.py` (call at finalise), `tests/test_forecast.py`

**Interfaces:**
- Consumes: `open_samples`, `arrivals` row (id, trip_id, stop_id, seq, actual)
- Produces: `extract_forecasts(samples_conn, trip_id, stop_id, seq, actual: int) -> list[tuple[int, int, int]]` of `(horizon_min, predicted_epoch, error_s)` where, for each horizon H in 30/20/10/5/2, the sample used is the latest one with `ts <= actual - H*60`; `error_s = actual - predicted`; horizons with no sample omitted. Tracker writes them into `forecast` on finalise.

**Security flag:** `none`
**Does NOT cover:** cross-month sample reads beyond current and previous month.

- [ ] **Step 1:** Tests: `test_picks_latest_sample_before_horizon`, `test_missing_horizon_omitted`, `test_error_sign_positive_when_late`, `test_reads_previous_month_file_at_boundary`.
- [ ] **Step 2-4:** fail, implement, pass. **Step 5:** Commit `"Add forecast extraction"`.

### Task 6: Poller loop, tracked routes, gaps

**Files:** Create `app/poller.py`, `tests/test_poller.py`; modify `app/main.py` (lifespan starts it)

**Interfaces:**
- Consumes: `ServerClient`, `Tracker`, `open_meta`
- Produces: `class Poller: __init__(settings, client, tracker)`, `async tick(now: int) -> None` (one pass over tracked routes), `async run() -> None` (loop every `poll_seconds`), `track(route: str)`, `untrack(route: str)`, `tracked() -> list[str]`. On `ServerUnavailable` it writes/extends a `gaps` row and keeps going; the first successful tick after closes the gap.

**Security flag:** `none`
**Does NOT cover:** concurrent writers.

- [ ] **Step 1:** Tests against `FakeServer`: `test_tick_polls_only_tracked_routes`, `test_unavailable_records_gap_and_no_arrivals`, `test_gap_closed_on_recovery`, `test_untrack_stops_polling`, `test_tracked_survives_restart` (persisted in `routes_tracked`).
- [ ] **Step 2-4:** fail, implement, pass. **Step 5:** Commit `"Add poller"`.

### Task 7: Stats functions

**Files:** Create `app/stats.py`, `tests/test_stats.py`

**Interfaces:**
- Consumes: delay lists / arrival rows (dicts with `delay`, `actual`, `scheduled`, `confidence`, `realtime`, `stop_id`, `seq`)
- Produces (all pure, n=0/1 return None fields, never raise):
  - `delay_summary(delays: list[int], ci: float = 0.95, pct: tuple[float, float] = (0.05, 0.95)) -> dict(n, mean, min, max, std, ci_low, ci_high, p_low, p_high, median)` (t-interval on mean; empirical percentiles)
  - `on_time_pct(delays, lo_s=-60, hi_s=300) -> dict(n, pct)`; `reliability_score(delays, lo_s=-60, hi_s=300) -> float | None` = on_time share penalised by spread: `on_time_pct * (1 / (1 + std/300))`
  - `heatmap(rows) -> dict[(weekday, hour)] -> dict(n, mean_delay)` (Europe/Dublin local time of scheduled)
  - `accumulation(rows_by_trip_ordered) -> list[dict(stop_id, seq, mean_delay, segment_delta)]` mean over trips of delay change between consecutive stops
  - `forecast_accuracy(forecast_rows) -> dict[horizon_min] -> dict(n, mean_error, mae, p_low, p_high)`
  - `filter_rows(rows, min_confidence=0, realtime_only=True, since=None, until=None, weekdays=None, hours=None) -> list`

**Security flag:** `none`
**Does NOT cover:** bootstrap CIs (YAGNI per spec).

- [ ] **Step 1:** Tests with hand-computed fixtures: `test_delay_summary_known_values` (delays [0,60,120,180,240]: mean 120, std 100 (sample), ci via t(0.975,4)=2.776 -> 120±124.2), `test_summary_n0_all_none`, `test_summary_n1_ci_none`, `test_on_time_window_edges_inclusive`, `test_reliability_penalises_spread`, `test_heatmap_buckets_dublin_local_time` (DST case), `test_accumulation_segment_delta`, `test_forecast_accuracy_by_horizon`, `test_filter_excludes_scheduled_only_and_low_confidence`.
- [ ] **Step 2-4:** fail, implement with numpy/scipy.stats.t, pass. **Step 5:** Commit `"Add stats"`.

### Task 8: JSON API

**Files:** Create `app/api.py`, `tests/test_api.py`; modify `app/main.py` (include router)

**Interfaces:**
- Consumes: `stats.*`, `Poller.track/untrack/tracked`, `ServerClient`, `open_meta`, `open_samples`
- Produces (all under `/api`): `GET routes?q=`, `GET/PUT/DELETE routes/tracked[/{route}]`, `GET routes/{r}/stops`, `GET routes/{r}/summary?ci=&pct_lo=&pct_hi=&since=&until=&weekdays=&hours=&min_confidence=`, `GET stops/{s}/stats?route=&ci=&window_lo=&window_hi=` (+ arrival list), `GET trips/{id}/series?stop=` (samples; reads gz months transparently via Task 9 helper once it exists), `GET routes/{r}/heatmap`, `GET routes/{r}/accumulation?direction=`, `GET routes/{r}/forecast`. `ci` outside (0,1) or bad ints -> 422; unknown route/stop -> 404; no data -> 200 with `n: 0`.

**Security flag:** `security` (query-param validation; all SQL parameterised)
**Does NOT cover:** auth.

- [ ] **Step 1:** Tests seeded via direct DB inserts: `test_track_untrack_roundtrip`, `test_route_summary_per_stop_in_route_order`, `test_stop_stats_ci_param_changes_interval`, `test_invalid_ci_422`, `test_stop_with_no_arrivals_returns_n0_not_500`, `test_trip_series_returns_due_reaching_zero`, `test_sql_injection_in_route_param_harmless`.
- [ ] **Step 2-4:** fail, implement, pass. **Step 5:** Commit `"Add API"`.

### Task 9: Backup + monthly compaction

**Files:** Create `app/backup.py`, `tests/test_backup.py`; modify `app/main.py` (nightly task, `POST /api/backup`, `GET /api/backup/download`); modify `app/db.py` samples opener to read `.db.gz` by decompressing to a temp file

**Interfaces:**
- Consumes: `open_meta`, `samples_path`
- Produces: `snapshot(data_dir: Path) -> Path` (consistent copies of `meta.db` and every `samples-*.db` via `sqlite3.Connection.backup`, tarred into `/data/backups/analytics-<UTC stamp>.tar`; retains last 7); `compact_closed_months(data_dir: Path, now: int) -> list[Path]` (gzip + remove months strictly before the current UTC month; idempotent).

**Security flag:** `none`
**Does NOT cover:** off-host backup (README says to copy the volume).

- [ ] **Step 1:** Tests: `test_snapshot_restorable_and_consistent_while_writing`, `test_snapshot_retention_keeps_7`, `test_compact_gzips_only_closed_months`, `test_compact_idempotent`, `test_series_reads_gzipped_month`.
- [ ] **Step 2-4:** fail, implement, pass. **Step 5:** Commit `"Add backup and compaction"`.

### Task 10: Web UI

**Files:** Create `web/index.html`, `web/app.js`, `web/style.css`, `web/chart.umd.min.js` (vendored Chart.js, pinned version); modify `app/main.py` to mount `/`

**Interfaces:**
- Consumes: Task 8/9 API
- Produces: pages (hash-routed): Routes (search/track/status + backup button), Route (stop list, mean delay bars with min/max whiskers, percentile band, controls for ci/percentiles/window/hours/weekdays), Stop (histogram, CI, on-time %, reliability, trips), Trip (due-minutes series), Analytics (heatmap, accumulation, forecast accuracy). n<10 values greyed; empty states shown.

**Security flag:** `none` (render API text with `textContent`, never `innerHTML`)
**Does NOT cover:** mobile layout polish beyond responsive basics; build tooling.

Follow `superpowers-optimized:frontend-design` for visual quality and accessibility (labels, contrast, keyboard).

- [ ] **Step 1:** Add `tests/test_web.py::test_index_served_and_assets_200` (GET `/`, `/app.js`, `/style.css` all 200) -> FAIL.
- [ ] **Step 2:** Implement pages; manually verify against a seeded DB by running `uvicorn` and exercising every page; capture one screenshot per page.
- [ ] **Step 3:** pytest -> PASS. **Step 4:** Commit `"Add web UI"`.

### Task 11: Compose wiring, README, end-to-end smoke

**Files:** Modify `dublin-bus-server/docker-compose.yml`; create `dublin-bus-analytics/README.md`, `scripts/smoke.sh`

**Interfaces:** Consumes everything. Produces: compose service:
```yaml
  dublin-bus-analytics:
    build: ../dublin-bus-analytics
    container_name: dublin-bus-analytics
    restart: unless-stopped
    depends_on: [dublin-bus-server]
    ports: ["127.0.0.1:${ANALYTICS_PORT:-8081}:8081"]
    environment:
      SERVER_URL: http://dublin-bus-server:8080
    volumes: [analytics-data:/data]
```
plus `analytics-data:` under `volumes:`.

**Security flag:** `none`
**Does NOT cover:** publishing an image to ghcr.

- [ ] **Step 1:** README: what it is, compose usage, config table, storage layout, backup/restore, "never expose publicly".
- [ ] **Step 2:** `scripts/smoke.sh`: `docker compose up -d --build`, wait for `/api/health`, track one route via `PUT /api/routes/tracked/15`, wait 90s, assert `/api/routes/15/summary` shows `n >= 1` for some stop.
- [ ] **Step 3:** Run `cd dublin-bus-server && docker compose config` (valid) then `../dublin-bus-analytics/scripts/smoke.sh` (needs `GTFSR_API_KEY` in `server.env`). Expected: exits 0.
- [ ] **Step 4:** Verify `scheduledEpochSeconds` against live API on a known stop vs the published timetable once (spot check, record result in README "Verification").
- [ ] **Step 5:** Commit in both repos.
