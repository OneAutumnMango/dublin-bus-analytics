# dublin-bus-analytics

Polls a `dublin-bus-server` for arrivals on the routes you track, stores change-only samples in SQLite, and serves delay and forecast-accuracy analytics with a small web UI (FastAPI plus plain ES modules, no build step).

**Warning: there is no authentication. Bind to 127.0.0.1 and never expose it publicly.**

## Run the website without the container (dev environment)

Prerequisites: git, Python 3.12 (`python --version`; with pyenv: `pyenv install 3.12 && pyenv local 3.12` inside the repo), and Node 18+ (`node --version`, only for the JS tests).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The UI is served from the repo's `web/` directory automatically (override with `WEB_DIR`); `--reload` picks up code changes.

### Option A: offline demo (no NTA key)

```bash
python scripts/seed_demo.py ./data/demo --routes 15,46A --days 14 --fake-server 8765
```

This writes 14 days of synthetic data for routes 15 and 46A into `./data/demo` and keeps a tiny fake bus server running on 127.0.0.1:8765 (leave it running; Ctrl-C stops it). Seeding is deterministic and safe to repeat. In a second terminal (venv active):

```bash
SERVER_URL=http://127.0.0.1:8765 DATA_DIR=./data/demo uvicorn app.main:app --reload --port 8081
```

Open http://127.0.0.1:8081. The pages (hash routes):

- **Sidebar** (on every page): **Find a route** (searches as you type, exact matches first; Enter on an exact route number starts tracking it) above the list of tracked routes. Click a tracked route to open its analytics; **Untrack** stops polling (stored data is kept).
- **Routes** (`#/`): welcome text and a Backup card.
- **Route**: per-stop delay statistics in route order, with links to each stop and to analytics.
- **Stop**: delay summary and histogram for one stop, and its most recent arrivals.
- **Trip**: how the predictions for one trip at one stop evolved over time.
- **Analytics**: mean delay by weekday and hour, where time is lost along the route, and how accurate the 30/20/10/5/2-minute forecasts were.

The fake server returns no live arrivals, so only the seeded history appears.

### Option B: live data

Run the real server (needs a `GTFSR_API_KEY` from the NTA developer portal; see the sibling server README for setup: `docker compose up -d` publishes 127.0.0.1:8080, or `scripts/run-server.sh` without Docker), then:

```bash
SERVER_URL=http://127.0.0.1:8080 DATA_DIR=./data/live uvicorn app.main:app --reload --port 8081
```

Track a route on the Routes page and wait a few minutes (each tracked route is polled every 30 s). Statistics need days of data: `n` is the number of observations behind a figure, and anything with n < 10 is flagged low-n and should not be trusted. Stop order comes from the server's route data; if it is unavailable the route view reports `order_source: none`.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SERVER_URL` | `http://dublin-bus-server:8080` | Base URL of dublin-bus-server |
| `DATA_DIR` | `/data` | Directory for `meta.db`, `samples-*.db`, `backups/` |
| `POLL_SECONDS` | `30` | Seconds between polls (positive integer) |
| `SERVER_TIMEOUT_SECONDS` | `45` | Read timeout for server calls in seconds (positive integer) |
| `SERVER_CONCURRENCY` | `2` | Routes fetched at once (positive integer, lower is gentler on a Raspberry Pi) |
| `PORT` | `8081` | Parsed into settings only; the listen port is uvicorn's `--port` |
| `BACKUP_ENABLED` | `true` | Nightly backup on/off (`true/false/1/0/yes/no`) |
| `BACKUP_TIME` | `03:00` | `HH:MM`, Europe/Dublin local time |
| `BACKUP_KEEP` | `7` | Backups to keep (positive integer) |
| `BACKUP_DIR` | `<DATA_DIR>/backups` | Backup directory (a relative path is relative to the working directory) |
| `WEB_DIR` | `web/` next to `app/` | Directory of the static UI (read by `app/api/static.py`) |

Invalid values stop the app at startup with a message naming the variable. See `analytics.env.example`.

### Troubleshooting

- `ModuleNotFoundError` (for example `httpx`): the venv is not active; run `source .venv/bin/activate`.
- `Address already in use`: pick another `--port`, or find the process with `ss -ltnp | grep 8081`.
- Permission or read-only errors on `DATA_DIR`: locally it must be writable by your user; the container runs as uid 1000.
- Empty UI: nothing tracked (press Track), nothing seeded, or `SERVER_URL` wrong (the Routes search will show an error). The API alone: `curl -s http://127.0.0.1:8081/api/health`.
- `order_source: none`: the server's stop list for the route is unavailable (wrong `SERVER_URL`, or the server is down).
- Blank page with a 404: `web/` not found; set `WEB_DIR` to the directory containing `index.html`.

### How delays are measured

`delay = actual - scheduled`. From version 0.3.0, `actual` is the time the bus left the stop's arrival list (the midpoint between the last poll that listed it and the first that did not), once it has shown as due now. If the bus stays listed for more than 10 minutes after first showing due now (some feeds keep passed stops), `actual` falls back to the feed's predicted time at that first due-now reading. The same 10-minute window also bounds the leave-time estimate after a gap in polling. A bus that vanishes without ever showing due now is stored (confidence 0) with its last predicted time only if that prediction was within 2 minutes of arrival; otherwise it is dropped. A feed that drops a bus for one poll right after due now gives an early `actual`. Rows stored before this change used the predicted time only.

## Run the tests

```bash
pytest -q                                  # whole suite; several minutes
pytest -q -k "forecast or stats"           # subset by keyword
pytest -q tests/core/test_db.py tests/test_docs.py   # subset by file
pytest --cov=app --cov-branch --cov-report=term-missing
node --test web/test/                      # JS unit tests (no server needed)
BASE_URL=http://127.0.0.1:8081 node --test web/test/contract.test.mjs   # against a running app
```

On a low-RAM machine run Python with a cap, for example `ulimit -v 2500000; pytest -q`, and run the heavy files one at a time: `tests/api/test_routes.py`, `tests/collect/test_tracker.py`, `tests/backup/test_backup.py`. The contract test is skipped unless `BASE_URL` is set; optional `ROUTE` and `STOP` pick the route and stop to check (with the demo data: `ROUTE=15 STOP=15-S02`).

## Build and run the container

```bash
docker build -t dublin-bus-analytics .
docker run --rm -p 127.0.0.1:8081:8081 -v analytics-data:/data \
  -e SERVER_URL=http://host.docker.internal:8080 dublin-bus-analytics
```

Or both services with compose (create `server.env` with `GTFSR_API_KEY` first, as in the server README):

```bash
docker compose -f docker-compose.example.yml up -d --build
```

The image serves the UI as well as the API (`WEB_DIR=/srv/web`) and has a healthcheck.

### Run the released image

Tagging `X.Y.Z` (matching `version` in `pyproject.toml`) runs the tests and publishes `ghcr.io/oneautumnmango/dublin-bus-analytics:X.Y.Z` (also `X.Y`, `X` and `latest`, for amd64 and arm64). `docker-compose.release.yml` runs that image on an existing `caddy_net` network next to a running `dublin-bus-server`; the UI has no authentication, so keep it off the public internet.

```bash
docker compose -f docker-compose.release.yml up -d
```

### Run with the server compose file

The sibling `dublin-bus-server` repo's `docker-compose.yml` also runs this service, behind the opt-in `analytics` profile (a plain `docker compose up -d` there starts only the server):

```bash
cd ../dublin-bus-server && docker compose --profile analytics up -d --build
```

Server on `127.0.0.1:8080`, analytics on `127.0.0.1:8081`; override with `ANALYTICS_PORT`, `ANALYTICS_POLL_SECONDS`, `ANALYTICS_BACKUP_ENABLED`, `ANALYTICS_BACKUP_TIME`, `ANALYTICS_BACKUP_KEEP`, `ANALYTICS_BACKUP_DIR`. `scripts/smoke.sh` runs this end to end (needs an API key); what is still unverified is listed in [docs/verification.md](docs/verification.md).

## Data and backups

Under `DATA_DIR`: `meta.db` (tracked routes, stops, trips, arrivals; kept forever), `samples-YYYY-MM.db` (raw prediction samples, one file per month, stored only when a prediction changes), and `backups/`. A month's file is gzipped (`.db.gz`, still readable) once it has been closed for 2 days.

A scheduled backup runs daily at `BACKUP_TIME` (default 03:00 Europe/Dublin), writes `analytics-<UTC timestamp>.tar` of all databases into the backup directory, keeps the newest `BACKUP_KEEP` (7), then gzips closed months. If none is under 24 h old at startup, one runs straight away.

Manual: the Backup card on the Routes page, or

```bash
curl -X POST http://127.0.0.1:8081/api/backup
curl -OJ http://127.0.0.1:8081/api/backup/download
```

Restore:

1. Stop the app.
2. Delete any stale `meta.db-wal`, `meta.db-shm`, `samples-*.db-wal` and `samples-*.db-shm` in `DATA_DIR` (a crash-leftover WAL would otherwise be replayed over the restored database).
3. A local `samples-YYYY-MM.db.gz` takes precedence over an extracted plain `samples-YYYY-MM.db`: delete the `.gz` for every month you want restored from the backup.
4. Check the tar contents with `tar -tf`, extract with `tar -xf analytics-<stamp>.tar -C "$DATA_DIR"`, and start the app.

Copies kept off the host are your responsibility. `scripts/seed_demo.py` must never be pointed at a live `DATA_DIR` (it is for throwaway demo directories).

### JSON export

`GET /api/export.json` streams the stored data as one JSON document (the tar backups remain the restore path; there is no JSON import).

Query parameters (all optional): `route` (one route), `since` and `until` (Unix seconds, inclusive; they filter `arrivals.scheduled` and `sample.ts`), `gzip=1` (gzip-compressed download, `.json.gz`). The filename is `analytics-export-YYYYMMDD[-route].json[.gz]`.

```
curl -OJ "http://127.0.0.1:8081/api/export.json?route=15&since=1790000000"
```

Document keys, in order: `version` (1), `exported_at`, `filters` (`route`, `since`, `until`), `routes_tracked`, `gaps`, `arrivals` (each with a nested `forecast` list of `horizon_min`, `predicted`, `error_s`), `samples` (`ts`, `trip_id`, `stop_id`, `seq`, `due_min`, `expected`, `realtime`), then `"complete":true` as the last key. The response is streamed, so a dropped connection leaves a truncated file: a download is whole only if it parses and `complete` is `true`.

Notes:

- One export at a time: a concurrent request gets `429` with a `Retry-After` header.
- Memory stays flat (rows are read in batches); samples are read one month at a time, and a gzipped month is decompressed to a temporary copy that is removed before the next month.
- A very long export holds a read transaction on each file, which can delay the WAL checkpoint until it finishes; the WAL file may grow meanwhile.
- `since`/`until` are not range-checked: very large values just yield a truncated stream, which the `complete` key reveals. An export abandoned before its first chunk frees the one-at-a-time slot only when its 3600 s lease expires.

In the UI, the Backup card on the Routes page has a route select, a "compressed" checkbox and a Download JSON button.

### Remove a day of data

Deletes everything recorded for one Europe/Dublin calendar day (for example data from an older version). There is no undo: take a backup first (`POST /api/backup`).

```bash
curl "http://127.0.0.1:8081/api/data/day?date=2026-11-11"                       # preview: counts only
curl -X POST "http://127.0.0.1:8081/api/data/day/remove?date=2026-11-11&confirm=2026-11-11"
```

Add `route=15` to limit it to one route. `confirm` must equal `date`. The preview returns `arrivals`, `forecasts`, `samples`, `gaps`, the month files that would be edited (`months`) and gzipped months that overlap the day and cannot be edited (`skipped_months`); the remove response has the same shape with what was actually deleted.

In the web UI this is the "Remove a day of data" section of the Backup card on the Routes page: pick a date and route, press Preview, then "Remove these rows" (shown only for the previewed date and route).

- Deleted: arrivals of that `service_date`, their forecasts, raw samples in the day's local window (23 or 25 hours on DST days) and gaps starting in it. `routes_tracked` and `meta_kv` are kept; nothing is vacuumed.
- With `route`, only samples belonging to that route's arrivals of the day go; samples without an arrival (buses dropped before arriving) and gaps cannot be attributed to a route and stay.
- A gzipped (closed) month is immutable and is skipped and reported; extract it by hand if you must clean it.
- Rows are removed in chunks of 2000, each its own short transaction, so the collector keeps writing. If a file stays locked the call returns `409` saying what was done; repeat it to finish. Only one remove, backup or compaction runs at a time (`409` when busy).
- Today and tomorrow are accepted, but the collector keeps live state in memory: after deleting today's samples, buses still in flight get incomplete forecast rows when they finalise, and a bus held at "due now" can re-create an arrival row. Prefer completed days, or run it right after deploying a new version; a preview taken while the collector runs is not guaranteed to be all zero afterwards.
- Windows differ by table: arrivals are matched by `service_date`, samples by recording time. In route mode, samples of a day-D arrival recorded before local midnight stay behind (they cannot be attributed), and a trip id that crosses midnight can lose the previous day's early-morning samples. In all-routes mode, after-midnight samples of late D-1 buses are deleted too (their arrival rows and forecasts survive; only the raw trip detail is truncated).
- A `409` partial result can mean samples were deleted while arrivals remain; rerun to finish. A non-lock database error gives `503` with the same `done` counts.
- Security: the same-origin check relies on the `Origin` header (browsers send it on cross-origin POSTs; non-browser clients and DNS rebinding are not covered) and `confirm` is not CSRF protection. The app has no authentication and must stay off the public internet.
- The collector remembers finalised arrivals in memory for about 6 h so they are not re-inserted, but a bus held at "due now" may still finalise after the removal and recreate a row for that day: run the removal again if so.

## Repo layout

- `app/` FastAPI app, grouped by responsibility: `core/` (config, models, db), `collect/` (server client, poller, tracker, forecast), `analysis/` (stats), `api/` (JSON routes, static UI), `backup/` (backup, scheduler, backup router), `maintenance/` (remove a day of data); entrypoint `app.main:app`
- `web/` static UI and its Node tests (`web/test/`)
- `scripts/` `seed_demo.py` (demo data and fake server), `smoke.sh` (full-stack check)
- `tests/` pytest suite mirroring the `app/` layout (`tests/support/` holds the fake server)
- `docs/verification.md` what is verified and what is still owed manually
- `docs/specs/` design spec, `docs/plans/` implementation plan
- `Dockerfile`, `docker-compose.example.yml`, `analytics.env.example`
