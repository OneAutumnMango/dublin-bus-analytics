# Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply audit findings 1-8 to dublin-bus-analytics: one-process note, `service_date` index, leaner backups with an archive of old months, bounded concurrent map fetches, optional Host allow-list, dead-code removal, forecast `filters` echo, one-query export.
**Architecture:** Small independent edits in `app/`. Backups gain an archive-copy step called from the one `snapshot()` function; the Host check is Starlette's `TrustedHostMiddleware` added only when configured; everything else is local to one module.
**Tech Stack:** Python 3.12, FastAPI/Starlette, SQLite, pytest (+pytest-asyncio), Node tests for the JS (untouched).
**Spec:** `docs/specs/2026-11-12-audit-fixes-design.md`
**Assumptions:** Assumes one process, as today. Assumes `ALLOWED_HOSTS` unset means no check. Will NOT work if someone runs several workers (finding 1 only documents it).

## Global Constraints

- Models: sonnet or cheaper only, never opus. At most 2 agents at once. Run Python with `ulimit -v 2500000`. Run `tests/api/test_routes.py`, `tests/collect/test_tracker.py`, `tests/backup/test_backup.py` one file at a time.
- Do not commit unless the user asks (the repo has uncommitted docs; commit steps below are skipped unless asked).
- `PORT` env and `Settings.port` stay.
- scipy stays.
- Env parsing follows `app/core/config.py` style: invalid values raise `ValueError` naming the variable.
- Run tests from the repo root with the venv: `source .venv/bin/activate`.

## Review Focus

- Archive copy with a half-written file left behind by a crash: next run must not treat it as a finished copy (temp name, rename only when complete).
- `BACKUP_ARCHIVE_DIR` set to an empty string: copy is off; unset: default folder is used.
- Host header with a port (`localhost:8081`) and with different case must pass when `ALLOWED_HOSTS` is set.
- Map with a route whose stops call fails: that route is `unavailable`, others still load, order is unchanged.
- Export of an arrival with no forecast rows: `"forecast":[]` as before.

---

## File Structure

- `app/core/db.py` (B, F): index added, `meta_kv` removed.
- `app/core/config.py` (C, E): `backup_archive_dir`, `backup_archive_enabled`, `archive_path`, `allowed_hosts`.
- `app/backup/backup.py` (C): `snapshot` drops `.gz`, gains archive step.
- `app/backup/scheduler.py`, `app/backup/api.py` (C): pass the archive dir.
- `app/api/routes.py` (D, G): concurrent map fetches, forecast `filters`.
- `app/main.py` (E): add the middleware.
- `app/analysis/stats.py`, `app/collect/serverclient.py`, `app/core/models.py`, `app/maintenance/day.py` (F).
- `app/export/stream.py` (H).
- `README.md`, `Dockerfile`, `analytics.env.example`, `docker-compose.example.yml` (A, C, E docs).

---

### Task 1: Service-date index (finding 2)

**Files:**
- Modify: `app/core/db.py`
- Test: `tests/core/test_db.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing

**Security flag:** `none`

- [ ] **Step 1: Write failing test** in `tests/core/test_db.py`:

```python
def test_service_date_lookups_use_the_date_index(tmp_path):
    c = open_meta(tmp_path)
    for sql, args in (("select id from arrivals where service_date=? and route=?", ("2026-01-01", "15")),
                      ("select count(*) from arrivals where service_date=?", ("2026-01-01",))):
        plan = " ".join(r[3] for r in c.execute("explain query plan " + sql, args))
        assert "ix_arr_date" in plan
```

- [ ] **Step 2: Run:** `pytest -q tests/core/test_db.py -k service_date` — Expected: FAIL (plan lacks `ix_arr_date`).
- [ ] **Step 3: Implement:** add `create index if not exists ix_arr_date on arrivals(service_date, route);` to `META_SCHEMA` after the other two indexes. If the second query still chooses the unique autoindex, make the index `(service_date, route)` the covering choice by keeping it as is and adjusting only the test to the query the code really runs (`_meta_counts` and `_scan_months` in `app/maintenance/day.py`); both queries in `day.py` filter on `service_date` first.
- [ ] **Step 4: Run:** `pytest -q tests/core/test_db.py tests/maintenance/test_day.py` — Expected: PASS.

---

### Task 2: Dead-code removal (finding 6)

**Files:**
- Modify: `app/analysis/stats.py`, `app/collect/serverclient.py`, `app/core/models.py`, `app/core/db.py`, `app/maintenance/day.py` (docstring line 4), `scripts/seed_demo.py` if it builds `Arrival`
- Test: `tests/analysis/test_stats.py`, `tests/collect/test_serverclient.py`, `tests/collect/test_tracker.py`, `tests/core/test_db.py`, `tests/maintenance/test_day.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Arrival(stop_id, trip_id, route, due_min, expected, scheduled, delay, realtime)` — the dataclass loses `vehicle_id` and `possibly_passed` (task 1's test file is untouched by this).

**Security flag:** `none`

- [ ] **Step 1:** Delete `accumulation`, `forecast_accuracy`, `heatmap_to_json` from `app/analysis/stats.py`; delete their tests (`test_accumulation_*`, `test_forecast_accuracy_*`, heatmap_to_json tests) from `tests/analysis/test_stats.py`.
- [ ] **Step 2:** Delete `ServerClient.status` and its tests (`tests/collect/test_serverclient.py` lines around 90-115).
- [ ] **Step 3:** Remove the `meta_kv` table from `META_SCHEMA`; fix `tests/core/test_db.py` (the name set at line 43 and the insert/duplicate-key test around line 94) and `tests/maintenance/test_day.py` (lines 41 and 80, and the `meta_kv` mention in the `day.py` module docstring).
- [ ] **Step 4:** Remove `vehicle_id` and `possibly_passed` from `Arrival` and from `ServerClient.route_arrivals`; remove them from every `Arrival(...)` call in tests and in `scripts/seed_demo.py` (find with `grep -rn "vehicle_id\|possibly_passed" app tests scripts`).
- [ ] **Step 5: Verify:** `grep -rn "vehicle_id\|possibly_passed\|meta_kv\|heatmap_to_json\|forecast_accuracy" app tests scripts` returns nothing, then `pytest -q tests/analysis tests/collect/test_serverclient.py tests/core tests/maintenance tests/test_seed_demo.py` — Expected: PASS. Then run `tests/collect/test_tracker.py` alone — Expected: PASS.

---

### Task 3: Backup archive (finding 3)

**Files:**
- Modify: `app/core/config.py`, `app/backup/backup.py`, `app/backup/scheduler.py`, `app/backup/api.py`
- Test: `tests/core/test_config.py`, `tests/backup/test_backup.py`, `tests/backup/test_scheduler.py`, `tests/backup/test_api.py` (create if no backup API test file exists)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Settings.backup_archive_dir: Path | None = None`, `Settings.backup_archive_enabled: bool = True`, property `Settings.archive_path -> Path | None` (None when disabled, else `backup_archive_dir` or `backup_path / "archive"`).
  - `backup.snapshot(data_dir, backup_dir=None, keep=7, archive_dir=None) -> Path` (tar has `meta.db` and plain `samples-*.db` only).
  - `backup.archive_months(data_dir: Path, archive_dir: Path) -> list[Path]` (paths newly copied; never raises on per-file errors).

**Security flag:** `none`

**Does NOT cover:** pruning the archive; verifying archived files with a checksum; restoring.

- [ ] **Step 1: Config tests** in `tests/core/test_config.py`: unset gives `archive_path == backup_path / "archive"`; `BACKUP_ARCHIVE_DIR=/x` gives `Path("/x")`; `BACKUP_ARCHIVE_DIR=""` (or spaces) gives `archive_path is None`. Add `BACKUP_ARCHIVE_DIR` to the `BENV` list so the env fixture clears it. Run: `pytest -q tests/core/test_config.py` — Expected: FAIL.
- [ ] **Step 2: Implement** the two fields and `archive_path` in `Settings`; `from_env` reads `BACKUP_ARCHIVE_DIR` (None → `(None, True)`; blank → `(None, False)`; else `(Path(raw), True)`). Run the config tests — Expected: PASS.
- [ ] **Step 3: Backup tests** in `tests/backup/test_backup.py`. Replace `test_snapshot_includes_gz_as_is_and_safe_names` with `test_snapshot_excludes_gz_months` (tar names contain `meta.db` and plain months, no `.gz`). Add:
  - `test_archive_copies_each_gz_once`: after `snapshot(..., archive_dir=a)` the archive holds every `samples-*.db.gz` byte-identical; a second call copies nothing (`archive_months` returns `[]`).
  - `test_archive_never_overwrites_a_different_file`: pre-existing archive file with other bytes stays unchanged and a warning is logged.
  - `test_archive_failure_does_not_fail_snapshot_and_leaves_no_temp`: make the archive dir unwritable (monkeypatch `os.replace` to raise `OSError`); snapshot still returns the tar; no `*.tmp` remains in the archive dir.
  - `test_archive_disabled_when_none`: `archive_dir=None` creates no folder.
  Run: `pytest -q tests/backup/test_backup.py -k "archive or excludes_gz"` — Expected: FAIL.
- [ ] **Step 4: Implement** in `app/backup/backup.py`: stop adding `.gz` files in `snapshot` (keep the file filter regex so `.db` and `meta.db` only); add `archive_months`: for each `data_dir/samples-*.db.gz`, skip if the same name exists in `archive_dir` (log a warning when sizes differ); else copy to `<name>.tmp` in `archive_dir`, `fsync`, `os.replace` to the final name, remove the temp on any error and log. `snapshot` calls it after the tar is placed and retention has run, inside the existing `_lock`, wrapped so any exception is logged and swallowed. Create `archive_dir` with `mkdir(parents=True, exist_ok=True)` inside that guard. `_clean_stale` should also remove `samples-*.db.gz.tmp` in the archive dir older than `STALE_S`.
- [ ] **Step 5: Wire callers:** `BackupScheduler.run_once` passes `archive_dir=s.archive_path` as a keyword to `snapshot_fn`; `create_backup` in `app/backup/api.py` passes `archive_dir=getattr(s, "archive_path", None)`. Update the scheduler test's fake `snapshot_fn` signature to accept `archive_dir` and assert it receives `settings.archive_path`; add one API test that `POST /api/backup` creates the archive copy.
- [ ] **Step 6: Run** `pytest -q tests/backup/test_scheduler.py tests/core/test_config.py`, then `tests/backup/test_backup.py` alone — Expected: PASS.

---

### Task 4: Concurrent map fetches (finding 4)

**Files:**
- Modify: `app/api/routes.py`
- Test: `tests/api/test_routes.py`

**Interfaces:**
- Consumes: `Settings.server_concurrency` (exists)
- Produces: `async def _bounded_map(request, items: list, fn) -> list` (awaits `fn(item)` for all items with at most `server_concurrency` in flight, results in input order; exceptions are returned as values, not raised).

**Security flag:** `none`

**Does NOT cover:** the database scans in `/api/map` (`_heavy` calls stay sequential, they are gated already).

- [ ] **Step 1: Tests** in `tests/api/test_routes.py` using the file's existing fake-client fixture style: 
  - `test_map_fetches_routes_concurrently_within_cap`: 6 tracked routes, fake `route_stops` that records the number running at once and sleeps briefly; peak is `> 1` and `<= server_concurrency`.
  - `test_map_one_route_unavailable_others_load`: one route's `route_stops` raises `ServerUnavailable`; response lists it in `unavailable`, others present, `routes` order equals sorted tracked order.
  - `test_map_shapes_concurrent_and_ordered`: same idea for `/api/map/shapes`; result dict keys are the tracked routes with their directions.
  Run `pytest -q tests/api/test_routes.py -k "map and (concurrent or unavailable)"` — Expected: the concurrency tests FAIL (peak is 1).
- [ ] **Step 2: Implement** `_bounded_map` with an `asyncio.Semaphore(getattr(request.app.state.settings, "server_concurrency", 2))` and `asyncio.gather(..., return_exceptions=True)`. In `map_data`, first run `_bounded_map(request, routes, lambda r: _order(request, r))` and `_directions` raw lists the same way (they hit the 5-minute cache after the first call), then keep the existing sequential loop that merges results and runs `_heavy`. In `map_shapes`, collect all `(route, direction)` pairs from the orders and fetch `_shape` through `_bounded_map`, then rebuild `out` in route order, omitting empty results exactly as before. Non-cancellation exceptions from `_shape` are already handled inside `_shape`; for `_order` the existing `_NoOrder` handling stays inside `_order`.
- [ ] **Step 3: Run** `tests/api/test_routes.py` alone — Expected: PASS.

---

### Task 5: Optional Host allow-list (finding 5)

**Files:**
- Modify: `app/core/config.py`, `app/main.py`
- Test: `tests/core/test_config.py`, `tests/test_main.py` (create if absent; if the repo's app-wiring tests live elsewhere, e.g. `tests/api/test_static.py`, add there)

**Interfaces:**
- Consumes: `Settings`, `create_app` (exist)
- Produces: `Settings.allowed_hosts: tuple[str, ...] = ()`

**Security flag:** `security`

**Does NOT cover:** authentication; Origin checks; blocking requests when `ALLOWED_HOSTS` is unset (by design nothing is blocked).

- [ ] **Step 1: Tests:**
  - Config: `ALLOWED_HOSTS` unset gives `()`; `"a.com, *.b.com ,"` gives `("a.com", "*.b.com")` (trimmed, empties dropped).
  - App (`create_app(Settings(...), start_background=False)` with `TestClient`): with no hosts set, `GET /api/health` with `Host: evil.example` is 200; with `allowed_hosts=("tunnel.example.com",)`: `Host: tunnel.example.com` 200, `Host: TUNNEL.example.com:443` 200, `Host: localhost:8081` 200, `Host: 127.0.0.1` 200, `Host: evil.example` 400. Also one wildcard case: `("*.example.com",)` accepts `a.example.com`.
  Run: `pytest -q tests/core/test_config.py tests/test_main.py` — Expected: FAIL.
- [ ] **Step 2: Implement:** `Settings.from_env` parses `ALLOWED_HOSTS` (comma split, strip, drop empty). In `create_app`, if `settings.allowed_hosts`: `app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*settings.allowed_hosts, "localhost", "127.0.0.1"])`. Check that `Host: [::1]:8081` is accepted by trying both `"[::1]"` and `"::1"` in a quick test against the installed Starlette (`.venv/lib/python3.12/site-packages/starlette/middleware/trustedhost.py` uses `parse_host_header`); put the form that passes into the list and add one test for it.
- [ ] **Step 3: Run** the same tests plus `pytest -q tests/api/test_static.py` — Expected: PASS.

---

### Task 6: Forecast `filters` echo (finding 7)

**Files:**
- Modify: `app/api/routes.py`
- Test: `tests/api/test_routes.py`

**Interfaces:**
- Consumes: `_filters(p)` (exists)
- Produces: `_forecast_filters(p) -> dict` (`_filters` minus `pct_lo` and `pct_hi`)

**Security flag:** `none`

- [ ] **Step 1: Tests:** `test_forecast_filters_omit_percentile_params` (per-route) and `test_all_forecast_filters_omit_percentile_params`: `filters` has no `pct_lo`/`pct_hi`; `ci`, `window_lo`, `since` etc. still present; sending `pct_lo=0.1&pct_hi=0.9` is still accepted (200). Update existing forecast tests that assert the full `filters` dict (search `test_forecast_filters` at ~line 625 and the `all_forecast` tests). Run: `pytest -q tests/api/test_routes.py -k forecast` — Expected: new tests FAIL.
- [ ] **Step 2: Implement** `_forecast_filters` and use it in `_forecast_db` and `all_forecast` (including the docstring of `route_forecast` stating `p_low`/`p_high` are always the 5th/95th percentile). Check `web/js` does not read `filters.pct_lo` from forecast responses (`grep -n "pct_lo" web/js/pages/analytics.js web/js/pages/all.js`; `all.js:57` reads the heatmap/route filters, not forecast).
- [ ] **Step 3: Run** `tests/api/test_routes.py` alone and `node --test web/test/` — Expected: PASS.

---

### Task 7: Export in one query per batch (finding 8)

**Files:**
- Modify: `app/export/stream.py`
- Test: `tests/export/test_stream.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing

**Security flag:** `none`

- [ ] **Step 1: Pin the old output.** In `tests/export/test_stream.py` add `test_export_output_matches_golden_with_forecasts`: seed arrivals including one with no forecast rows and one with several; compare `"".join(iter_export(...))` to a golden string captured now (run the current code once, paste the result into the test). Run — Expected: PASS before any change.
- [ ] **Step 2: Add a query-count test** `test_export_runs_one_forecast_query_per_batch`: count `select ... from forecast` statements with the connection trace callback or by wrapping `sqlite3.connect`; for 5 arrivals expect 1.
- [ ] **Step 3: Implement** `add_forecast(rows)` in `iter_export`: one `select arrival_id, horizon_min, predicted, error_s from forecast where arrival_id in (...) order by arrival_id, horizon_min` per batch (batch is at most 1000 ids, under SQLite's variable limit), grouped into a dict by `arrival_id`; each row gets `r["forecast"] = grouped.get(r["id"], [])` as the same `{"horizon_min","predicted","error_s"}` dicts in the same key order.
- [ ] **Step 4: Run** `pytest -q tests/export` — Expected: PASS (golden test unchanged, query-count test passes).

---

### Task 8: Documentation (findings 1, 2, 3, 5)

**Files:**
- Modify: `README.md`, `Dockerfile`, `analytics.env.example` (edit with the Edit tool; do not `cat` it, a hook blocks reading `.env*` files with cat), `docker-compose.example.yml`
- Test: `tests/test_docs.py`

**Interfaces:**
- Consumes: the variable names from tasks 3 and 5
- Produces: nothing

**Security flag:** `none`

- [ ] **Step 1:** Read `tests/test_docs.py` to see what it checks (it likely compares documented env vars with `Settings`); add the two new variables to whatever it expects. Run it — Expected: FAIL until the README lists them.
- [ ] **Step 2: README.** Add rows to the environment table: `BACKUP_ARCHIVE_DIR` (default `<BACKUP_DIR>/archive`; one copy of each finished month; empty string turns it off) and `ALLOWED_HOSTS` (comma list; unset means any Host is accepted; when set `localhost` and `127.0.0.1` are also allowed). Add: a "One process only" note (never `--workers`); a "Backups and restore" note (tar holds `meta.db` and the current month; old months are in the archive folder; restore with the latest tar plus the archive files); a note that the first start after upgrading builds the `service_date` index and can pause polling for a few seconds on a large database; a "Tunnels" warning: the app has no login, a public tunnel needs Cloudflare Access in front, and `ALLOWED_HOSTS` only protects once set (example: `ALLOWED_HOSTS=192.168.1.20,mypi.local,analytics.example.com`).
- [ ] **Step 3:** Dockerfile: comment above `CMD`: `# one process only: the poller and locks are per process; never add --workers`. `analytics.env.example` and `docker-compose.example.yml`: commented `ALLOWED_HOSTS` and `BACKUP_ARCHIVE_DIR` lines with the example values; compose passes `ALLOWED_HOSTS: "${ANALYTICS_ALLOWED_HOSTS:-}"` (empty string means unset behaviour, so make `from_env` treat blank as unset: add that case to Task 5's config test and parser).
- [ ] **Step 4: Run** `pytest -q tests/test_docs.py` — Expected: PASS.

---

### Task 9: Whole-suite verification

**Files:** none

**Interfaces:**
- Consumes: all tasks
- Produces: nothing

**Security flag:** `none`

- [ ] **Step 1:** `ulimit -v 2500000; pytest -q --ignore=tests/api/test_routes.py --ignore=tests/collect/test_tracker.py --ignore=tests/backup/test_backup.py` — Expected: PASS.
- [ ] **Step 2:** Run each heavy file alone: `pytest -q tests/api/test_routes.py`, then `tests/collect/test_tracker.py`, then `tests/backup/test_backup.py` — Expected: PASS each.
- [ ] **Step 3:** `node --test web/test/` — Expected: PASS. `pytest --cov=app --cov-branch -q` meets the 90% floor in `pyproject.toml` (run only if RAM allows).
- [ ] **Step 4:** Report results to the user. Do not commit unless asked.
