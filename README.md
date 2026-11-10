# dublin-bus-analytics

Polls a [dublin-bus-server](../dublin-bus-server/README.md) for arrivals on the routes you track, stores change-only samples in SQLite, and serves delay and forecast-accuracy analytics with a small web UI (FastAPI plus plain ES modules, no build step).

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

- **Routes** (`#/`): tracked routes with status, a search box over the server's routes, a Backup card. Press **Track** on a route to start polling it, **Untrack** to stop (stored data is kept).
- **Route**: per-stop delay statistics in route order, with links to each stop and to analytics.
- **Stop**: delay summary and histogram for one stop, and its most recent arrivals.
- **Trip**: how the predictions for one trip at one stop evolved over time.
- **Analytics**: mean delay by weekday and hour, where time is lost along the route, and how accurate the 30/20/10/5/2-minute forecasts were.

The fake server returns no live arrivals, so only the seeded history appears.

### Option B: live data

Run the real server (needs a `GTFSR_API_KEY` from the NTA developer portal; see its [README](../dublin-bus-server/README.md): `docker compose up -d` publishes 127.0.0.1:8080, or `scripts/run-server.sh` without Docker), then:

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
| `PORT` | `8081` | Parsed into settings only; the listen port is uvicorn's `--port` |
| `BACKUP_ENABLED` | `true` | Nightly backup on/off (`true/false/1/0/yes/no`) |
| `BACKUP_TIME` | `03:00` | `HH:MM`, Europe/Dublin local time |
| `BACKUP_KEEP` | `7` | Backups to keep (positive integer) |
| `BACKUP_DIR` | `<DATA_DIR>/backups` | Backup directory (a relative path is relative to the working directory) |
| `WEB_DIR` | `web/` next to `app/` | Directory of the static UI (read by `app/static.py`) |

Invalid values stop the app at startup with a message naming the variable. See `analytics.env.example`.

### Troubleshooting

- `ModuleNotFoundError` (for example `httpx`): the venv is not active; run `source .venv/bin/activate`.
- `Address already in use`: pick another `--port`, or find the process with `ss -ltnp | grep 8081`.
- Permission or read-only errors on `DATA_DIR`: locally it must be writable by your user; the container runs as uid 10001.
- Empty UI: nothing tracked (press Track), nothing seeded, or `SERVER_URL` wrong (the Routes search will show an error). The API alone: `curl -s http://127.0.0.1:8081/api/health`.
- `order_source: none`: the server's stop list for the route is unavailable (wrong `SERVER_URL`, or the server is down).
- Blank page with a 404: `web/` not found; set `WEB_DIR` to the directory containing `index.html`.

## Run the tests

```bash
pytest -q                                  # whole suite; several minutes
pytest -q -k "forecast or stats"           # subset by keyword
pytest -q tests/test_db.py tests/test_docs.py   # subset by file
pytest --cov=app --cov-branch --cov-report=term-missing
node --test web/test/                      # JS unit tests (no server needed)
BASE_URL=http://127.0.0.1:8081 node --test web/test/contract.test.mjs   # against a running app
```

On a low-RAM machine run Python with a cap, for example `ulimit -v 2500000; pytest -q`, and run the heavy files one at a time: `tests/test_api.py`, `tests/test_tracker.py`, `tests/test_backup.py`. The contract test is skipped unless `BASE_URL` is set; optional `ROUTE` and `STOP` pick the route and stop to check (with the demo data: `ROUTE=15 STOP=15-S02`).

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

### Run with the server compose file

The sibling `dublin-bus-server` repo's `docker-compose.yml` also runs this service:

```bash
cd ../dublin-bus-server && docker compose up -d --build
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

Restore: stop the app, extract the tar into `DATA_DIR` (`tar -xf analytics-<stamp>.tar -C "$DATA_DIR"`; check the tar contents with `tar -tf` first), start the app. Copies kept off the host are your responsibility.

## Repo layout

- `app/` FastAPI app: poller, tracker, stats, forecast, API, backup
- `web/` static UI and its Node tests (`web/test/`)
- `scripts/` `seed_demo.py` (demo data and fake server)
- `tests/` pytest suite
- `docs/specs/` design spec, `docs/plans/` implementation plan
- `Dockerfile`, `docker-compose.example.yml`, `analytics.env.example`
