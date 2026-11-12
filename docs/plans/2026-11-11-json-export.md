# JSON Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user download all recorded analytics data (or one route's) as a streamed JSON file from the Routes page.
**Architecture:** A new `app/export/` package: `stream.py` yields the JSON document in chunks straight from SQLite (meta.db plus monthly sample files), `gate.py` allows one export at a time, `api.py` serves `GET /api/export.json`. The web Backup card gets a route select, a compressed checkbox and a "Download JSON" link.
**Tech Stack:** Python 3.12, FastAPI/Starlette, sqlite3, pytest; vanilla JS ES modules with `node --test`.
**Spec:** `/home/david/dbus/dublin-bus-analytics/docs/specs/2026-11-11-json-export-design.md` (plan conflicts resolve against the spec).
**Assumptions:**
- Assumes samples live in `samples-YYYY-MM.db` or `samples-YYYY-MM.db.gz` (UTC months) beside `meta.db`, as `app/core/db.py` writes them. Will NOT work for any other layout.
- Assumes one process owns the data dir, so a module-level in-process gate is enough. Will NOT serialise exports across several app processes.

## Global Constraints
- Models: use **sonnet or cheaper only** for every subagent, never opus. At most **2 agents at once**.
- RAM: WSL has about 7.7 GB and crashes when exhausted. Always `ulimit -v 2500000;` and `/home/david/dbus/.venv/bin/python -m pytest <named files> -q --no-cov -p no:cacheprovider`; never run the whole suite; test databases at most 500k rows (this plan uses 200k). Run `node --test` only on the named web test files.
- Document keys, in order: `version` (1), `exported_at`, `filters` (`route`, `since`, `until`), `routes_tracked`, `gaps`, `arrivals` (each with nested `forecast` list of `horizon_min`, `predicted`, `error_s`), `samples` (`ts`, `trip_id`, `stop_id`, `seq`, `due_min`, `expected`, `realtime`), then `"complete":true` last.
- Streamed row by row with `fetchmany`; memory stays flat. Samples are read one month at a time; a `.gz` month is decompressed via `open_samples_readonly` and its temp file removed before the next month.
- `since`/`until` filter `arrivals.scheduled` and `sample.ts` (inclusive). A route filter selects `arrivals.route` and, for samples, joins on `(trip_id, stop_id, seq)` through `meta.db` attached read-only.
- One export at a time: a concurrent request gets 429 with `Retry-After`. The export never takes `_DB_LOCK`. Gate lease expires after 3600 s so a leaked hold self-heals.
- Filename: `analytics-export-YYYYMMDD[-route].json[.gz]` (route sanitised to `[A-Za-z0-9_-]`). No change to the CSP; the control is a same-origin link with `download`.
- No import/restore from JSON; tar backups stay the restore path.
- Commit on a new branch `feat/json-export` created from `main` in `/home/david/dbus/dublin-bus-analytics`; the two spec/plan docs stay untracked unless the user asks otherwise.

## Review Focus
- Starlette runs a sync generator's `next()` on worker threads, and sqlite connections are thread-bound: every export connection must be opened with `check_same_thread=False`. Pinned in Task 1 (test consumes the stream via `TestClient` in Task 4).
- Missing `meta.db`, a missing/empty month, or a data dir with only a `.gz` month: valid JSON with empty arrays and `complete:true`. Pinned in Tasks 1 and 2.
- A stream abandoned before the first chunk leaves the gate held; the lease expiry must free it. Pinned in Task 3.
- Route ids with `/` (e.g. `15/A`) must survive query-string use and filename sanitising. Pinned in Task 4.
- `until < since` is a 422, not an empty export. Pinned in Task 4.

---

### Task 1: Export stream: metadata, arrivals, forecast

**Files:**
- Create: `app/export/__init__.py` (empty), `app/export/stream.py`
- Modify: `app/core/db.py` (`open_samples_readonly` gains `check_same_thread: bool = True`, passed to both `sqlite3.connect` calls)
- Test: `tests/export/__init__.py` (empty), `tests/export/test_stream.py`

**Interfaces:**
- Consumes: `app.core.db.open_meta` schema (tables `routes_tracked(route, added)`, `arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route)`, `forecast(arrival_id, horizon_min, predicted, error_s)`, `gaps(start, end)`).
- Produces: `def iter_export(data_dir: Path, *, route: str | None = None, since: int | None = None, until: int | None = None, exported_at: int | None = None) -> Iterator[str]` yielding JSON text chunks whose concatenation is the document; `samples` is an empty array until Task 2.

**Security flag:** `none`
**Does NOT cover:** samples (Task 2); compression (Task 3); HTTP (Task 4). With a route filter, `routes_tracked` and `gaps` are still exported in full.

- [ ] **Step 1: Write failing tests** in `tests/export/test_stream.py` (helper builds `meta.db` in `tmp_path` with `open_meta` and inserts rows):
  - `test_document_shape_and_key_order`: `json.loads("".join(iter_export(tmp_path, exported_at=123)))` has keys in the Global Constraints order, `version == 1`, `exported_at == 123`, `complete is True`.
  - `test_arrivals_with_nested_forecast`: two arrivals, one with two forecast rows; assert columns and `forecast == [{"horizon_min":..,"predicted":..,"error_s":..}, ...]` ordered by `horizon_min`, empty list when none.
  - `test_filters_route_since_until`: arrivals on routes "15" and "7" at several `scheduled` values; route-only, since-only, until-only and combined (inclusive bounds) give exactly the expected ids; `filters` echoes the values.
  - `test_missing_meta_db_gives_empty_valid_document`: empty `tmp_path` -> all arrays empty, `complete` true.
  - `test_chunks_are_bounded`: 5000 arrivals; every yielded chunk is under 256 KiB and there are more than 1 chunks.
  - `test_connections_usable_across_threads`: consume the generator by calling `next()` from alternating `threading.Thread`s; no `sqlite3.ProgrammingError`.
- [ ] **Step 2: Run**: `ulimit -v 2500000; /home/david/dbus/.venv/bin/python -m pytest tests/export/test_stream.py -q --no-cov -p no:cacheprovider`. Expected: FAIL (module not found).
- [ ] **Step 3: Implement** `iter_export` in `stream.py`: open meta.db with `sqlite3.connect(<uri>?mode=ro, uri=True, check_same_thread=False)` and read everything inside one read transaction (`isolation_level=None`, explicit `begin`), emit chunks by batching up to 1000 rows of `json.dumps(row, separators=(",", ":"))`; load each arrival's forecast with a primary-key lookup (`where arrival_id=? order by horizon_min`); close connections in `finally`. `exported_at` defaults to `int(time.time())`. Add the `check_same_thread` keyword to `open_samples_readonly` (default `True`, no behaviour change).
- [ ] **Step 4: Run** the same command plus `tests/core/test_db.py`. Expected: PASS.
- [ ] **Step 5: Commit**: `git add app/export app/core/db.py tests/export && git commit -m "Add streamed JSON export of metadata, arrivals and forecasts"`.

---

### Task 2: Export stream: samples across months

**Files:**
- Modify: `app/export/stream.py`
- Test: `tests/export/test_stream.py`

**Interfaces:**
- Consumes: `iter_export` (Task 1), `open_samples_readonly(data_dir, ts, check_same_thread=False)`, `samples_path`, `SAMPLES_SCHEMA` (`sample(ts, trip_id, stop_id, seq, due_min, expected, realtime)`).
- Produces: `iter_export` now streams the `samples` array across every month file in order, with the filters applied; helper `def sample_months(data_dir: Path) -> list[int]` returning one UTC timestamp (first second of the month) per distinct month found as `samples-YYYY-MM.db` or `.db.gz`, ascending.

**Security flag:** `none`
**Does NOT cover:** consistency across months (each file is read in its own transaction, same guarantee as backups).

- [ ] **Step 1: Write failing tests**:
  - `test_samples_span_months_in_order`: two plain months with rows; output samples are ordered by month then primary key and the count matches.
  - `test_gz_month_is_included_and_temp_removed`: one month gzipped via the same helper `app.core.db` uses for compaction (or `gzip` of a valid db), assert its rows appear and no `samples-ro-*.db` remains in `tempfile.gettempdir()` afterwards (compare listings before/after).
  - `test_samples_filtered_by_ts`: `since`/`until` inclusive on `ts`, and months wholly outside the range are not opened (assert by monkeypatching `open_samples_readonly` with a recorder).
  - `test_route_filter_joins_through_arrivals`: samples for trips on routes "15" and "7"; `route="15"` returns only samples whose `(trip_id, stop_id, seq)` is in `arrivals` with `route == "15"`, and the same holds for a `.gz` month.
  - `test_no_sample_files_gives_empty_samples`.
- [ ] **Step 2: Run**: `ulimit -v 2500000; /home/david/dbus/.venv/bin/python -m pytest tests/export/test_stream.py -q --no-cov -p no:cacheprovider -k "samples or sample_"`. Expected: FAIL.
- [ ] **Step 3: Implement**: for each month, open via `open_samples_readonly(..., check_same_thread=False)`; with a route filter run `attach database ? as m` using the meta.db read-only URI (connections are opened with `uri=True`) and select with `exists (select 1 from m.arrivals a where a.route=? and a.trip_id=s.trip_id and a.stop_id=s.stop_id and a.seq=s.seq)`; stream with `fetchmany(1000)`; `close()` each month in `finally` before opening the next (this removes the temp copy). Skip months whose bounds lie outside `[since, until]`. When meta.db is missing and a route filter is set, emit no samples.
- [ ] **Step 4: Run** the whole file. Expected: PASS.
- [ ] **Step 5: Commit**: `git add app/export tests/export && git commit -m "Stream samples month by month, including gzipped months"`.

---

### Task 3: Gzip wrapper and export gate

**Files:**
- Create: `app/export/gate.py`
- Modify: `app/export/stream.py`
- Test: `tests/export/test_gate.py`, `tests/export/test_stream.py`

**Interfaces:**
- Consumes: `iter_export` (Tasks 1-2).
- Produces: `def gzip_chunks(chunks: Iterable[str]) -> Iterator[bytes]` in `stream.py` (a valid gzip stream, flushed per chunk, trailer written at the end); in `gate.py`: `class ExportGate` with `__init__(self, lease_s: float = 3600, clock: Callable[[], float] = time.monotonic)`, `try_acquire(self) -> object | None`, `release(self, token: object) -> None` (idempotent, ignores unknown tokens).

**Security flag:** `none`
**Does NOT cover:** HTTP wiring (Task 4); the gate is per process.

- [ ] **Step 1: Write failing tests**:
  - `test_gzip_round_trip`: `gzip.decompress(b"".join(gzip_chunks(iter_export(tmp_path))))` equals the plain output for the same `exported_at`.
  - `test_gate_allows_one_holder`: second `try_acquire` is `None` until `release(token)`; releasing twice or with a stale token does nothing.
  - `test_gate_lease_expires`: with a fake clock, a never-released token stops blocking after `lease_s`; the old token's `release` no longer frees the new holder.
- [ ] **Step 2: Run**: `ulimit -v 2500000; /home/david/dbus/.venv/bin/python -m pytest tests/export -q --no-cov -p no:cacheprovider`. Expected: FAIL.
- [ ] **Step 3: Implement** both (`zlib.compressobj(wbits=31)` with `Z_SYNC_FLUSH` per chunk; gate guarded by a `threading.Lock`).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit**: `git add app/export tests/export && git commit -m "Add gzip streaming and a single-export gate with lease"`.

---

### Task 4: HTTP endpoint

**Files:**
- Create: `app/export/api.py`
- Modify: `app/main.py` (include the router before the static mount, next to the backup router)
- Test: `tests/export/test_api.py`

**Interfaces:**
- Consumes: `iter_export`, `gzip_chunks`, `ExportGate` (Tasks 1-3), `app.collect.poller.ROUTE_RE`, `request.app.state.settings.data_dir`.
- Produces: `router` with `GET /api/export.json?route=&since=&until=&gzip=`; `app.state.export_gate` (an `ExportGate`) set in `create_app`. 200 streams `application/json` (or `application/gzip` for `gzip=1`) with `Content-Disposition: attachment; filename="..."`; invalid `route` (not matching `ROUTE_RE`), non-integer `since`/`until`, or `until < since` -> 422; gate busy -> 429 with `Retry-After: 30`.

**Security flag:** `security` (a new endpoint exposing all stored data and building a filename from user input; the app is unauthenticated by design, so the check is input validation and header safety).
**Does NOT cover:** authentication (the app is never exposed publicly, see project constraints); CSRF protection (GET, read-only).

- [ ] **Step 1: Write failing tests** with `TestClient(create_app(Settings("http://fake", tmp_path, 30), start_background=False))` and fixture data from the Task 1/2 helpers:
  - `test_export_returns_valid_json_with_attachment_header`: body parses, `complete` true, header filename matches `analytics-export-\d{8}\.json`.
  - `test_export_route_filter_and_filename`: `route=15/A` (query-encoded) -> filename `analytics-export-YYYYMMDD-15_A.json`, rows limited to that route.
  - `test_export_gzip`: `gzip=1` -> media type `application/gzip`, filename ends `.json.gz`, `gzip.decompress` parses.
  - `test_export_invalid_params_422`: bad route chars, `since=abc`, `until=1&since=2`.
  - `test_export_busy_returns_429`: pre-acquire `app.state.export_gate`, request -> 429 and `Retry-After`; release, request -> 200.
  - `test_gate_released_after_stream_consumed`: after a full 200 response the gate is free again.
  - `test_header_injection_safe`: `route` containing `"` or newline is rejected (422) and never reaches a header.
- [ ] **Step 2: Run**: `ulimit -v 2500000; /home/david/dbus/.venv/bin/python -m pytest tests/export/test_api.py -q --no-cov -p no:cacheprovider`. Expected: FAIL.
- [ ] **Step 3: Implement** the handler as a plain `def` (not async) so Starlette streams from the threadpool. Acquire the gate before building the response; wrap the generator so a `finally` releases the token, and also pass `background=BackgroundTask(gate.release, token)` to `StreamingResponse`. Reuse the validation style of `app/backup/api.py`.
- [ ] **Step 4: Run** `tests/export tests/test_main.py tests/backup`. Expected: PASS.
- [ ] **Step 5: Commit**: `git add app/export app/main.py tests/export && git commit -m "Serve the export at GET /api/export.json"`.

---

### Task 5: Web UI

**Files:**
- Modify: `web/js/api.js`, `web/js/pages/routes.js`
- Test: `web/test/api.test.mjs`, `web/test/pages.test.mjs`

**Interfaces:**
- Consumes: `qs`, `api.tracked(o)` (list of tracked routes), `h`/`link` helpers already imported in `routes.js`, `GET /api/export.json` (Task 4).
- Produces: `export const exportUrl = ({ route, gzip } = {}) => string` in `api.js` (`/api/export.json` plus a query with only the set parameters; `gzip` serialises as `gzip=1`).

**Security flag:** `none`
**Does NOT cover:** progress display (a browser download); date filters in the UI (API-only).

- [ ] **Step 1: Write failing tests**:
  - `api.test.mjs` `exportUrl builds the query`: `exportUrl()` is `/api/export.json`; `exportUrl({route:'15/A'})` is `/api/export.json?route=15%2FA`; `exportUrl({gzip:true})` is `/api/export.json?gzip=1`; both together contain both.
  - `pages.test.mjs` (follow the existing Routes page tests with `fakedom.mjs`): the Backup card contains a "Download JSON" link whose href is `/api/export.json` by default; choosing a tracked route in the select updates it to include `route=`; ticking "compressed (.gz)" adds `gzip=1`; the select lists "All routes" first then the tracked routes returned by a stubbed `api.tracked`.
- [ ] **Step 2: Run**: `node --test web/test/api.test.mjs web/test/pages.test.mjs`. Expected: FAIL.
- [ ] **Step 3: Implement** `exportUrl` in `api.js`; in `routes.js` add, inside the existing Backup card, a labelled `<select>` (All routes + tracked), a labelled checkbox "compressed (.gz)" and `link(href, 'Download JSON', { class: 'btn btn-quiet', download: '' })` whose `href` is recomputed on each change. Reuse the existing `h`/`put` helpers and the page's `signal` for the `api.tracked` call; on a tracked-routes error, keep just "All routes".
- [ ] **Step 4: Run** the same command plus `node --test web/test/security.test.mjs web/test/contract.test.mjs`. Expected: PASS.
- [ ] **Step 5: Commit**: `git add web && git commit -m "Add Download JSON controls to the Backup card"`.

---

### Task 6: Scale tests and docs

**Files:**
- Test: `tests/export/test_scale.py`
- Modify: `README.md`, `docs/verification.md`

**Interfaces:**
- Consumes: `iter_export` (Tasks 1-2).
- Produces: nothing

**Security flag:** `none`
**Does NOT cover:** exports above 500k rows.

- [ ] **Step 1: Write the tests**:
  - `test_export_memory_is_bounded`: one month with 200k `sample` rows (`executemany`) and 20k arrivals; consume `iter_export` while discarding chunks under `tracemalloc`; assert peak under 25 MiB.
  - `test_route_filtered_samples_are_fast_enough`: same data, `route` set; total time under 5 s.
- [ ] **Step 2: Run**: `ulimit -v 2500000; /home/david/dbus/.venv/bin/python -m pytest tests/export/test_scale.py -q --no-cov -p no:cacheprovider`. Expected: PASS. If the memory test fails, fix the streaming (chunks must not be accumulated) rather than raising the limit; if the timing test fails, report the measured seconds to the user instead of editing the threshold.
- [ ] **Step 3: Docs**: README: document `GET /api/export.json` (parameters, document keys, the `complete` check, one export at a time, truncated-download note, WAL checkpoint caveat for long exports) next to the backup description; `docs/verification.md`: add manual checks (download from the Routes page, open in a JSON viewer, `complete` true, gz variant, route filter, a second tab gets 429 while one export runs).
- [ ] **Step 4: Run** `tests/test_docs.py tests/export -q`. Expected: PASS.
- [ ] **Step 5: Commit**: `git add tests/export README.md docs/verification.md && git commit -m "Add export scale tests and docs"`.
