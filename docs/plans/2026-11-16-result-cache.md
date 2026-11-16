# Precomputed Result Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Data pages open instantly from stored results that refresh hourly (or on a Refresh button), with raw data untouched.
**Architecture:** A generic HTTP response cache (Starlette middleware) in front of an allow-list of heavy GET endpoints, stored in a `result_cache` table in `meta.db`; an hourly in-process warmer recomputes recently used entries; route stop lists persist in the same table. A shared UI bar shows "computed at" and a Refresh button.
**Tech Stack:** Python 3 / FastAPI / Starlette / sqlite3 / httpx (tests: pytest, `TestClient`); vanilla ES modules (tests: `node --test` in `web/`).
**Spec:** `docs/specs/2026-11-16-result-cache-design.md` (approved by the user 2026-11-16; plan conflicts resolve against it).
**Assumptions:** Assumes one app process (cache coherence is per-process, in-process locks) - will NOT work if several workers run. Assumes `meta.db` is writable by the app - on a read-only DB every request falls back to live compute. Assumes the UI's `since`/`until` are epoch seconds.

## Global Constraints

- Work on branch `feat/result-cache` (create from `main` in Task 0). Commit at the end of every task; end commit messages with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Python only via `/home/david/dbus/.venv/bin/python`; run pytest/uvicorn under `ulimit -v 2500000` (host has ~7.9 GB, WSL has crashed). Max 2 subagents at once; subagent models sonnet or cheaper.
- Never touch the user's original backup `C:\Users\david\Downloads\analytics-20261116T133604Z.tar`. The scratchpad copies (`<scratchpad>/bk`, `<scratchpad>/run`) must not be deleted; benchmark on a new copy of `bk`.
- Cache layer must never turn a request into a 5xx: any sqlite/cache error is logged and the request computes live.
- Only HTTP 200 JSON responses are stored. Raw `samples-*.db` and `arrivals`/`forecast` tables are never modified.
- Cache-key rounding: `since`/`until` floored to a multiple of 3600; `fresh` is excluded from the key.
- Defaults: `RESULT_CACHE=true`, `RESULT_CACHE_REFRESH_SECONDS=3600`; entries unused 14 days are evicted; caps 500 entries and 100 MiB of body; warmer covers entries used in the last 7 days and skips entries computed < 3000 s ago; stop-list TTL 24 h (was 300 s); shape TTL 24 h (was 3600 s).

## Review Focus

- **Degraded 200 responses:** `/map` with the server down returns 200 with non-empty `unavailable`, and route pages return `order_source: "none"`. Storing those would show a broken page for an hour. Expected: not stored (pinned in Task 2).
- **Invalidate-during-compute race:** a request already computing when `remove_day`/untrack clears the cache must not store its stale result afterwards. Expected: store is refused (generation check, Task 1/2).
- **Warmer behind host allow-list:** in-process warm requests must use a loopback host or `TrustedHostMiddleware` rejects them when `ALLOWED_HOSTS` is set (Task 3).
- **Duplicate/odd query strings:** repeated params and param order must map to one key (Task 1).

---

## File Structure

- `app/cache.py` (new): storage + keying, no HTTP. One responsibility: the `result_cache` table.
- `app/cache_http.py` (new): `install(app)` middleware (allow-list, hit/miss/fresh, locks, headers).
- `app/cache_warm.py` (new): `CacheWarmer` hourly refresh loop.
- Modify: `app/core/config.py`, `app/core/db.py` (schema), `app/main.py` (wire), `app/api/routes.py` (`_change`, `_directions`, shape TTL), `app/maintenance/day.py` (invalidate), `web/js/api.js`, `web/js/app.js`, `web/index.html`, `web/css/*` (bar style), `README.md`, `pyproject.toml` (version).
- Tests: `tests/cache/test_cache.py`, `tests/cache/test_http.py`, `tests/cache/test_warm.py`, `tests/api/test_stops_cache.py`, `web/test/api.test.mjs`.

### Task 0: Branch, settings, schema

**Files:** Modify `app/core/config.py`, `app/core/db.py`; Test `tests/cache/test_cache.py` (create `tests/cache/__init__.py`).

**Interfaces:**
- Consumes: nothing
- Produces: `Settings.result_cache: bool = True`, `Settings.result_cache_refresh_seconds: int = 3600`; table `result_cache(key TEXT PRIMARY KEY, body BLOB NOT NULL, computed_at INTEGER NOT NULL, last_used INTEGER NOT NULL, ms INTEGER NOT NULL DEFAULT 0)` created by `open_meta`.

**Security flag:** `none`

- [ ] **Step 1:** `git checkout -b feat/result-cache` in `dublin-bus-analytics`.
- [ ] **Step 2: Failing tests** in `tests/cache/test_cache.py`:
```python
def test_open_meta_creates_result_cache(tmp_path):
    c = open_meta(tmp_path)
    cols = [r[1] for r in c.execute("pragma table_info(result_cache)")]
    assert cols == ["key", "body", "computed_at", "last_used", "ms"]

def test_settings_defaults_and_env(monkeypatch):
    assert Settings().result_cache is True and Settings().result_cache_refresh_seconds == 3600
    monkeypatch.setenv("RESULT_CACHE", "false"); monkeypatch.setenv("RESULT_CACHE_REFRESH_SECONDS", "600")
    s = Settings.from_env()
    assert s.result_cache is False and s.result_cache_refresh_seconds == 600
```
- [ ] **Step 3:** Run `ulimit -v 2500000; .venv/bin/python -m pytest tests/cache -q` - expect FAIL.
- [ ] **Step 4:** Add the two fields (+ `from_env` via `_bool`/`_pos_int`) and `create table if not exists result_cache(...)` to `META_SCHEMA`. Re-run: PASS.
- [ ] **Step 5:** Commit `Add result_cache settings and table`.

### Task 1: Storage module `app/cache.py`

**Files:** Create `app/cache.py`; Test `tests/cache/test_cache.py`.

**Interfaces:**
- Consumes: `open_meta` table from Task 0; `meta_kv(k,v)` table (existing; confirm column names with `pragma table_info(meta_kv)` and use them for the generation counter under key `rc_gen`).
- Produces (exact):
  - `ALLOWED: re.Pattern` - matches `/api/` paths `all/routes|heatmap|forecast|lab/(distribution|variance|persistence)`, `map`, `map/shapes`, `routes/{r}/(summary|heatmap|forecast|accumulation|lab/(distribution|variance|persistence))`, `stops/{s}/stats`, `trips/{id}/series`; nothing else (not `routes/{r}/stops`, tracked, search, data, backup, export, health).
  - `make_key(path: str, query: list[tuple[str, str]]) -> str | None` - `None` when path not allowed; else `path + "?" + urlencode(sorted(pairs))` with `fresh` dropped and `since`/`until` values floored to a multiple of 3600 when they parse as ints (otherwise kept verbatim).
  - `generation(c) -> int`
  - `lookup(c, key: str, now: int | None = None) -> tuple[bytes, int] | None` - returns (decompressed JSON bytes, computed_at); bumps `last_used` only if it is > 600 s old.
  - `store(c, key: str, body: bytes, ms: int, gen: int | None, now: int | None = None) -> bool` - gzips body; returns False and writes nothing when `gen is not None and gen != generation(c)`.
  - `clear(c) -> None` - deletes every row whose key does not start with `srv:` and increments the generation, in one transaction.
  - `evict(c, now: int, max_age: int = 14*86400, max_entries: int = 500, max_bytes: int = 100*2**20) -> int` - non-`srv:` rows only; age first, then least-recently-used until both caps hold; returns rows deleted.
  - `stale_keys(c, now: int, used_within: int = 7*86400, min_age: int = 3000) -> list[str]` - non-`srv:` keys with `last_used >= now-used_within` and `computed_at <= now-min_age`, oldest `computed_at` first.

**Security flag:** `none`

**Does NOT cover:** rows with `srv:` prefix are never evicted or cleared (static server data, bounded by route count); multi-process coherence.

- [ ] **Step 1: Failing tests** (`tests/cache/test_cache.py`):
  - `test_key_rounds_since_until_and_ignores_fresh_and_order`: `make_key("/api/map", [("since","7300"),("a","1")]) == make_key("/api/map", [("a","1"),("since","10799"),("fresh","1")])`; and `since=10800` gives a different key.
  - `test_key_none_for_uncached_paths`: `/api/routes/15/stops`, `/api/routes/tracked`, `/api/export.json`, `/api/health` -> `None`; `/api/routes/15/lab/persistence`, `/api/all/forecast`, `/api/stops/s1/stats`, `/api/trips/t1/series` -> not `None`.
  - `test_store_lookup_roundtrip`: `store(c,k,b'{"a":1}',5,None,now=1000)` then `lookup(c,k,now=1000) == (b'{"a":1}', 1000)`; row `body` is gzip (smaller than input for a 10 kB input).
  - `test_lookup_bumps_last_used_only_when_old`: after lookup at `now=1100` `last_used` unchanged; at `now=1700` it equals 1700.
  - `test_clear_bumps_generation_and_refuses_stale_store`: `g=generation(c)`; `clear(c)`; `store(c,k,b"x",1,g)` is `False` and table empty; `srv:` row survives `clear`.
  - `test_evict_by_age_and_count_and_bytes`: age (15 days old row removed, 13 days kept); `max_entries=2` keeps the two most recently used; `max_bytes` keeps total body size under the limit; `srv:` rows never removed.
  - `test_stale_keys_order_and_filters`.
- [ ] **Step 2:** Run - FAIL (module missing).
- [ ] **Step 3: Implement** `app/cache.py` with the signatures above (stdlib only: `gzip`, `re`, `urllib.parse`, `time`).
- [ ] **Step 4:** Run `tests/cache` - PASS.
- [ ] **Step 5:** Commit `Add result cache storage module`.

### Task 2: HTTP middleware `app/cache_http.py`

**Files:** Create `app/cache_http.py`; Modify `app/main.py`; Test `tests/cache/test_http.py`.

**Interfaces:**
- Consumes: from `app.cache`: `make_key`, `lookup`, `store`, `generation`; `app.core.db.open_meta`; `Settings.result_cache`.
- Produces: `install(app: FastAPI) -> None` - adds an HTTP middleware (no-op when `app.state.settings.result_cache` is false). Also `app.state.result_cache_locks: dict[str, asyncio.Lock]` (internal). Response headers on allowed GETs: `X-Cache: hit|miss|bypass`, `X-Computed-At: <epoch>`.

**Security flag:** `none`

**Does NOT cover:** non-GET methods, non-200 responses, uncached paths (passed through untouched, no headers).

Behavior: allowed GET -> `key=make_key(request.url.path, request.query_params.multi_items())`. If `fresh` param present: skip lookup, `X-Cache: bypass`, compute, store. Otherwise lookup in a thread (`asyncio.to_thread` with a fresh `open_meta(data_dir)` per call, closed after); hit -> `Response(body, media_type="application/json", headers)`. Miss: take the per-key lock, re-check lookup, read `gen=generation(c)` BEFORE calling the handler, time the handler, store only if status 200, content-type JSON, and the parsed body has neither a non-empty `unavailable` list nor `order_source == "none"`. Wrap every cache DB call in `try/except sqlite3.Error` -> log and continue live. Run `install(app)` inside `create_app` after routers are included.

- [ ] **Step 1: Failing tests** (`tests/cache/test_http.py`, use `Env` from `tests/api/test_routes.py` with `env.add(...)` rows for route "15"; count handler calls by monkeypatching `routes._summary_db` with a counting wrapper):
  - `test_second_identical_request_is_hit_and_skips_handler`: two `GET /api/routes/15/summary`; second has `X-Cache: hit`, equal body, handler count 1, `X-Computed-At` is an int string.
  - `test_fresh_recomputes_and_overwrites`: third call `?fresh=1` -> `X-Cache: bypass`, handler count 2; a following plain GET is a hit with the newer `X-Computed-At`.
  - `test_since_within_same_hour_shares_entry`.
  - `test_uncached_paths_untouched`: `/api/routes/tracked` has no `X-Cache` header.
  - `test_degraded_responses_not_stored`: monkeypatch the map's `_order` to raise `_NoOrder`-style failure so `/api/map` returns non-empty `unavailable`; two requests -> handler runs twice, table empty. Same for a route summary with `order_source == "none"`.
  - `test_error_status_not_stored`: `/api/routes/15/lab/distribution?ci=abc` (422) twice -> table empty.
  - `test_clear_during_compute_refuses_store`: handler wrapper calls `cache.clear(open_meta(dir))` mid-compute; afterwards table has no row for that key.
  - `test_concurrent_misses_compute_once`: `asyncio.gather` of 5 identical requests via `httpx.ASGITransport` -> handler count 1.
  - `test_cache_db_failure_falls_back_live`: monkeypatch `cache.lookup` to raise `sqlite3.OperationalError` -> 200 with correct body.
  - `test_disabled_by_setting`: `Settings(result_cache=False)` -> no `X-Cache` header, table empty.
- [ ] **Step 2:** Run - FAIL. **Step 3:** Implement `install`; wire in `app/main.py`. **Step 4:** Run `tests/cache tests/api tests/rollup -q` - PASS (existing tests must be unaffected: where an existing test asserts counts across repeated identical requests, set `Settings(result_cache=False)` in that test's `Env`).
- [ ] **Step 5:** Commit `Serve heavy GETs from the result cache`.

### Task 3: Hourly warmer `app/cache_warm.py`

**Files:** Create `app/cache_warm.py`; Modify `app/main.py`; Test `tests/cache/test_warm.py`.

**Interfaces:**
- Consumes: `cache.stale_keys`, `cache.evict`, `open_meta`, `Settings.result_cache`, `Settings.result_cache_refresh_seconds`.
- Produces: `CacheWarmer(app, *, now_fn=time.time, sleep_fn=asyncio.sleep)` with `async run_once() -> int` (entries refreshed; never raises except cancel) and `async run() -> None` (sleep 60 s, then loop `run_once` then sleep `result_cache_refresh_seconds`).

**Security flag:** `none`

Behavior: `run_once` opens `evict(...)` first, then for each key of `stale_keys(c, now)` does `GET key + "&fresh=1"` (use `?` if the key has no query) through `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")` one at a time (loopback host so `ALLOWED_HOSTS` does not reject it); a non-200 or exception is logged and the old body is kept (the middleware only stores 200s). Start in the `create_app` lifespan beside the poller/scheduler only when `start_background and settings.result_cache`; cancel it in the same cleanup loop.

- [ ] **Step 1: Failing tests** (`tests/cache/test_warm.py`, `Env`, injected `now_fn`):
  - `test_run_once_refreshes_only_old_recently_used_entries`: seed three entries via `store` (old+used, fresh<3000 s, unused 8 days); `run_once()` returns 1 and only the first has a new `computed_at`.
  - `test_failed_recompute_keeps_old_body`: handler monkeypatched to raise -> body and `computed_at` unchanged, returns without exception, next key still processed.
  - `test_warm_request_passes_allowed_hosts`: `Settings(allowed_hosts=("example.test",))` -> entry still refreshed.
  - `test_run_evicts_expired`: a 15-day-old row is gone after `run_once()`.
  - `test_lifespan_starts_warmer_only_when_enabled`: with `start_background=True` and `result_cache=False` no warmer task is created (assert via `app.state.warmer is None`).
- [ ] **Step 2:** FAIL. **Step 3:** Implement and wire (`app.state.warmer`). **Step 4:** `tests/cache tests/test_main.py -q` PASS. **Step 5:** Commit `Add hourly result cache warmer`.

### Task 4: Invalidation hooks

**Files:** Modify `app/maintenance/day.py`, `app/api/routes.py` (`_change`); Test `tests/cache/test_http.py`.

**Interfaces:** Consumes `cache.clear(c)`. Produces: nothing.

**Security flag:** `none`

Behavior: in `day._remove`, call `cache.clear(c)` next to BOTH `_drop_rollups(c, d, route)` calls (before emptying and again after). In `routes._change`, after a successful `track`/`untrack`, `cache.clear` on a fresh `open_meta(data_dir)` connection (in the threadpool). Polling writes must NOT clear.

- [ ] **Step 1: Failing tests:** `test_remove_day_clears_cache` (store an entry, run the remove-day API/function, table empty, `srv:` row kept); `test_track_and_untrack_clear_cache`; `test_poller_ingest_does_not_clear` (call the tracker/`env.add` path and assert entry survives).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `tests/cache tests/maintenance tests/api -q` PASS. **Step 5:** Commit `Clear the result cache on data removal and track changes`.

### Task 5: Persisted stop lists, longer TTLs

**Files:** Modify `app/api/routes.py` (`STOPS_TTL`, `_directions`, `SHAPE_TTL`); Test `tests/api/test_stops_cache.py`.

**Interfaces:** Consumes `cache.lookup/store` with keys `srv:stops:{route}`. Produces: nothing new (same `_directions(request, r)` contract).

**Security flag:** `none`

**Does NOT cover:** serving an expired persisted entry when the server is down (failures stay uncached and unserved, per spec).

Behavior: `STOPS_TTL = 86400`, `SHAPE_TTL = 86400`. In `_directions`, order: memory cache -> persisted row (`lookup`, accept if `now - computed_at < STOPS_TTL`, `json.loads`, validate with the same shape check, put in memory) -> server call; after a successful, validated server call also `store(..., gen=None)` the raw JSON. Run the DB calls in the threadpool; sqlite errors fall through to the server call.

- [ ] **Step 1: Failing tests:** `test_stops_survive_restart` (call summary, create a second app on the same `data_dir`, make the fake server raise `ServerUnavailable`, summary still returns server order); `test_expired_persisted_stops_refetched` (computed_at older than 24 h -> server called); `test_failed_fetch_not_persisted`; `test_malformed_persisted_row_ignored`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `tests/api -q` PASS (adjust any existing test that relied on the 300 s TTL constant). **Step 5:** Commit `Persist route stop lists and lengthen stop/shape TTLs`.

### Task 6: UI bar and Refresh

**Files:** Modify `web/js/api.js`, `web/js/app.js`, `web/index.html`, `web/css/` (the stylesheet holding `.main-nav`); Test `web/test/api.test.mjs`.

**Interfaces:**
- Consumes: response headers `X-Computed-At`, query flag `fresh=1`; `dateTime(sec)` from `web/js/lib.js`.
- Produces (exports of `api.js`): `computedAt(): number | null` (oldest `X-Computed-At` seen since the last reset), `resetComputed(): void`, `refresh: { on: boolean }`; `request()` appends `fresh=1` to GET paths (using `&` if the path has `?`) while `refresh.on` is true, and records the header on success.

**Security flag:** `none`

Behavior: `index.html` gets `<p id="cache-bar" class="cache-bar" hidden></p>` immediately before `<main>`. In `app.js` `show()`: call `resetComputed()` at the start; after `await done`, if `computedAt()` is non-null set the bar to `Data computed <dateTime(t)> · ` plus a `Refresh` button (and un-hide it), else hide it. Button click: `refresh.on = true; try { await show(); } finally { refresh.on = false; }`. Use existing `h()` helper from `dom.js`; style with existing theme variables (readable in dark mode, 44 px touch target for the button).

- [ ] **Step 1: Failing tests** (`web/test/api.test.mjs`): `request records X-Computed-At (oldest wins)` using a `resp` that includes `headers: { get: (n) => n.toLowerCase()==='x-computed-at' ? '1000' : null }`; second call with `'900'` -> `computedAt() === 900`; `resetComputed()` -> `null`; `request adds fresh=1 only while refresh.on` (paths `/api/map` -> `/api/map?fresh=1`, `/api/map?a=1` -> `/api/map?a=1&fresh=1`); existing `resp()` helper without `headers` must still work (guard `res.headers?.get`).
- [ ] **Step 2:** Run `cd web && node --test` - FAIL. **Step 3:** Implement. **Step 4:** `node --test` PASS; `node --check js/app.js`.
- [ ] **Step 5:** Commit `Show computed-at time and Refresh button`.

### Task 7: Real-data verification, docs, version

**Files:** Modify `README.md`, `pyproject.toml` (version `0.5.0`; also any other place `0.4.2` appears - `grep -rn "0\.4\.2" --include=*.toml --include=*.md --include=*.yml .`).

**Interfaces:** Consumes everything. Produces: nothing.

**Security flag:** `none`

- [ ] **Step 1:** Make a new working copy: `cp -r <scratchpad>/bk <scratchpad>/bench` (never touch `bk`/`run`/the Downloads original).
- [ ] **Step 2 (parity):** Start two apps on copies of `bench` (ports 8092 `RESULT_CACHE=false`, 8093 cache on), both `BACKUP_ENABLED=false SERVER_URL=http://127.0.0.1:8080`, under `ulimit -v 2500000`. For every allowed endpoint used in my earlier timing run (`/all/routes /all/heatmap /all/forecast /map /map/shapes /routes/15/{summary,heatmap,forecast,accumulation} /all/lab/{distribution,variance,persistence} /routes/15/lab/{distribution,persistence}`) compare the cache-off JSON to the cache-on `?fresh=1` JSON with `python -I -c` + `json` equality. Expected: all equal (apart from time-dependent fields if any - if one differs, report it, do not paper over).
- [ ] **Step 3 (timing):** curl each endpoint twice against 8093 recording `X-Cache` and `time_total`. Expected: second call `hit`, < 0.1 s for every endpoint. Save the before/after table to `docs/perf/2026-11-16-result-cache.md` (before numbers: from the spec's problem section).
- [ ] **Step 4:** Document in `README.md`: the two env vars, the hourly refresh and Refresh button, the 24 h stop-list cache, that raw data is never modified. Bump version. Stop both apps.
- [ ] **Step 5:** Full suite: `ulimit -v 2500000; .venv/bin/python -m pytest -q` and `cd web && node --test` - expect all pass (report any pre-existing failures separately). Commit `Release 0.5.0: precomputed result cache`.
- [ ] **Step 6:** Final whole-branch review via `requesting-code-review`, then `finishing-a-development-branch` (merge decision stays with the user; tag/push only on their request).
