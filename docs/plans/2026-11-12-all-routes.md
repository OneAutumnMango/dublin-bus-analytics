# All Routes Aggregated View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "All routes" page (`#/all`) with a per-route mean/median comparison, a pooled weekday x hour heatmap and pooled forecast accuracy across all tracked routes.
**Architecture:** Three additive endpoints `/api/all/{routes,heatmap,forecast}` loop over `poller.tracked()` one route at a time through the existing `_heavy` gate and per-route scan code, merging results in memory-flat accumulators. A new ES-module page `web/js/pages/all.js` loads each section through `region()`; the sidebar gets a link to it.
**Tech Stack:** Python 3.12 / FastAPI / sqlite / numpy / pytest; plain ES modules / node:test (no build step).
**Spec:** `docs/specs/2026-11-12-all-routes-design.md`
**Assumptions:** Assumes "all" = routes currently in `routes_tracked` (will NOT include untracked routes with stored history). Assumes the heatmap worker `_heatmap_db` returns per-cell `mean_delay` and `n` (pooling by n*mean is then exact up to float rounding). Assumes forecast errors are integer seconds (the 1 s histogram is then exact for |error| <= 3600).

## Global Constraints

- Delay = actual minus scheduled seconds; negative = early. UI shows minutes via existing `fmtDelay`/`fmtMinutes`.
- Existing query params and validation (`P` dependency) apply unchanged; responses echo `filters`.
- A per-route request cap of `MAX_ROWS` (500,000) applies per route, not to the pooled total.
- No tracked routes -> HTTP 200 with empty data, never an error.
- Values with n < 10 are `low_n` and rendered greyed (existing `LOW_N`).
- Pooled by arrival, not by route; the page states this.
- Test runs: one pytest file per process, `ulimit -v 2500000`, `OPENBLAS_NUM_THREADS=1`, Python via `/home/david/dbus/.venv`; max 2 agents, sonnet or cheaper.
- No `git commit` unless the user asks (repo convention for this session); steps below say "Checkpoint" instead.

## Review Focus

- A tracked route with zero arrivals: appears in `/all/routes` with `n=0` and null stats, no crash, listed "no data" in the UI and absent from the chart.
- A tracked route name that needs URL-encoding (e.g. `sA/B`): chart/table link must use `buildHash`, not string concatenation.
- Forecast errors beyond +/-3600 s: n/mean/MAE still include them; p5/p95 clamp, no index error.
- Median/mean `null` for every route (nothing recorded): comparison chart and table show an empty state, not NaN axes.
- Sorting with null medians: nulls always sort last in both directions.

---

## File Structure

- `app/api/routes.py` (modify): add the `/all/*` endpoints and their private helpers, next to the per-route ones.
- `tests/api/test_routes.py` (modify): add `# ---------- all routes ----------` tests using the existing `env` fixture.
- `web/js/lib.js` (modify): register route name `all`.
- `web/js/api.js` (modify): `allRoutes`, `allHeatmap`, `allForecast`.
- `web/js/charts.js` (modify): optional `median` marker in the range chart.
- `web/js/pages/analytics.js` (modify): export `legend` and `drawForecast` for reuse.
- `web/js/pages/all.js` (create): the page.
- `web/js/app.js`, `web/js/sidebar.js` (modify): registration and button.
- `web/test/*.mjs` (modify/create): see tasks.
- `README.md`, `docs/verification.md` (modify): one line each.

---

### Task 1: `GET /api/all/routes`

**Files:**
- Modify: `app/api/routes.py`
- Test: `tests/api/test_routes.py`

**Interfaces:**
- Consumes: existing `_heavy(request, p, where, args, work, delay=True)`, `_scan_arrivals(request, p, where, args, cols, fn)`, `_summ(delays, p)`, `_filters(p)`, `_clean`, `P`, `request.app.state.poller.tracked()`.
- Produces: `async def _each_tracked(request, p, worker, delay=True) -> tuple[list[tuple[str, object]], list[str]]` returning `([(route, worker_result)...], truncated_routes)`; `worker(request, route, p) -> object` is a sync function run in a thread under `_heavy`. Endpoint `GET /api/all/routes` -> `{"filters": {...}, "routes": [{"route","n","low_n","mean","median","on_time_pct","reliability","p_low","p_high"}...]}`.

**Security flag:** `none`

**Does NOT cover:** untracked routes with stored data; stop-level data.

- [x] **Step 1: Write failing tests** in `tests/api/test_routes.py`:

```python
def test_all_routes_stats_per_route(env):
    env.app.state.poller.track("15"); env.app.state.poller.track("46A"); env.app.state.poller.track("empty")
    for d in (60, 120, 600): env.add(route="15", delay=d)
    env.add(route="46A", delay=-60)
    env.add(route="untracked", delay=9999)
    j = env.get("/api/all/routes").json()
    by = {r["route"]: r for r in j["routes"]}
    assert set(by) == {"15", "46A", "empty"}
    assert by["15"]["n"] == 3 and by["15"]["median"] == 120 and by["15"]["mean"] == 260
    assert by["46A"]["n"] == 1 and by["46A"]["low_n"] is True
    assert by["empty"]["n"] == 0 and by["empty"]["mean"] is None and by["empty"]["median"] is None
    assert j["filters"]["realtime_only"] is True

def test_all_routes_no_tracked(env):
    j = env.get("/api/all/routes").json()
    assert j["routes"] == []

def test_all_routes_reports_truncated_routes(env, monkeypatch):
    monkeypatch.setattr("app.api.routes.MAX_ROWS", 2)
    env.app.state.poller.track("15"); env.app.state.poller.track("46A")
    for i in range(4): env.add(route="15", delay=i, scheduled=T0 + i)
    env.add(route="46A", delay=1)
    j = env.get("/api/all/routes").json()
    assert j["filters"]["truncated_routes"] == ["15"]
```

- [x] **Step 2: Run** `cd /home/david/dbus/dublin-bus-analytics && ulimit -v 2500000; OPENBLAS_NUM_THREADS=1 ../.venv/bin/pytest -q tests/api/test_routes.py -k all_routes` — Expected: FAIL (404 on `/api/all/routes`).

- [x] **Step 3: Implement** `_each_tracked` and the endpoint in `app/api/routes.py`.
  - `_each_tracked`: `routes = sorted(await run_in_threadpool(request.app.state.poller.tracked))`; for each route `res, trunc = await _heavy(request, p, "route = ?", [r], lambda p2: (worker(request, r, p2), p2["truncated"]), delay)` (bind `r` as a default arg); collect routes where `trunc` is true.
  - `_route_stats_db(request, r, p)`: `_scan_arrivals(..., "delay", fn)` appending into one `array("q")`, then `{"route": r, **_summ(delays, p)}` trimmed to the response fields (`n, low_n, mean, median, on_time_pct, reliability, p_low, p_high`).
  - Endpoint `@router.get("/all/routes")`: `{"filters": {**_filters(p), "truncated_routes": trunc}, "routes": [...]}` passed through `_clean`. Place the three `/all/*` handlers together in a new `# ---- all routes ----` section.

- [x] **Step 4: Run** the same command. Expected: PASS.
- [x] **Step 5: Checkpoint** (no commit): report changed files.

---

### Task 2: `GET /api/all/heatmap`

**Files:**
- Modify: `app/api/routes.py`
- Test: `tests/api/test_routes.py`

**Interfaces:**
- Consumes: `_each_tracked`, `_heatmap_db(request, r, p)` (returns `{"cells":[{weekday,hour,n,low_n,mean_delay}], "n":...}`), `LOW_N`, `_filters`, `_clean`.
- Produces: `GET /api/all/heatmap` -> `{"filters": {..., "truncated_routes": [...]}, "n": int, "cells": [{"weekday","hour","n","low_n","mean_delay"}...]}` sorted by (weekday, hour).

**Security flag:** `none`

**Does NOT cover:** weekday/hour filters are honoured by the endpoint (as in per-route) but the UI strips them.

- [x] **Step 1: Write failing tests:**

```python
def test_all_heatmap_one_route_equals_per_route(env):
    env.app.state.poller.track("15")
    for i, d in enumerate((60, 120, -30)): env.add(route="15", delay=d, scheduled=T0 + i * 3600)
    a = env.get("/api/all/heatmap").json()
    r = env.get("/api/routes/15/heatmap").json()
    assert a["cells"] == r["cells"] and a["n"] == r["n"]

def test_all_heatmap_pools_two_routes_by_arrival(env):
    env.app.state.poller.track("15"); env.app.state.poller.track("46A")
    env.add(route="15", delay=60, scheduled=T0); env.add(route="15", delay=120, scheduled=T0 + 1)
    env.add(route="46A", delay=300, scheduled=T0 + 2)
    env.add(route="untracked", delay=9999, scheduled=T0)
    j = env.get("/api/all/heatmap").json()
    assert j["n"] == 3 and len(j["cells"]) == 1
    c = j["cells"][0]
    assert c["n"] == 3 and c["mean_delay"] == pytest.approx(160) and c["low_n"] is True

def test_all_heatmap_empty(env):
    j = env.get("/api/all/heatmap").json()
    assert j["cells"] == [] and j["n"] == 0
```

- [x] **Step 2: Run** `... -k all_heatmap` — Expected: FAIL (404).
- [x] **Step 3: Implement** `@router.get("/all/heatmap")`: `_each_tracked(request, p, _heatmap_db)`; accumulate `cnt[(wd,hr)] += cell["n"]`, `tot[...] += cell["mean_delay"] * cell["n"]`; build cells like `_heatmap_db` does (`low_n = n < LOW_N`).
- [x] **Step 4: Run** same. Expected: PASS.
- [x] **Step 5: Checkpoint.**

---

### Task 3: `GET /api/all/forecast`

**Files:**
- Modify: `app/api/routes.py`
- Test: `tests/api/test_routes.py`

**Interfaces:**
- Consumes: `_each_tracked(..., delay=False)`, `_scan`, `_cond`, `_tz`, `HORIZONS`, `LOW_N`, `_filters`, `_clean`, numpy as `np`.
- Produces: `FC_CLAMP = 3600`; `def _forecast_parts_db(request, r, p) -> dict[int, tuple[int, float, float, np.ndarray]]` (per horizon: n, sum, abs_sum, counts array of length `2*FC_CLAMP+1` indexed by `clamp(error)+FC_CLAMP`); `def _hist_percentile(counts: np.ndarray, n: int, q: float) -> float` (linear interpolation between order statistics, same definition as `np.percentile`); `GET /api/all/forecast` -> `{"filters": {...,"truncated_routes":[...]}, "horizons": [{"horizon_min","n","low_n","mean_error","mae","p_low","p_high"}...]}` for horizons 30,20,10,5,2 always present (null stats when n=0).

**Security flag:** `none`

**Does NOT cover:** exact percentiles beyond +/-3600 s (clamped).

- [ ] **Step 1: Write failing tests:**

```python
def test_hist_percentile_matches_numpy():
    from app.api.routes import _hist_percentile, FC_CLAMP
    import numpy as np
    errs = [-300, -5, 0, 0, 7, 60, 61, 900, 3000]
    counts = np.zeros(2 * FC_CLAMP + 1, dtype=np.int64)
    for e in errs: counts[e + FC_CLAMP] += 1
    for q in (5, 25, 50, 95, 100, 0):
        assert _hist_percentile(counts, len(errs), q / 100) == pytest.approx(np.percentile(errs, q))

def test_all_forecast_one_route_equals_per_route(env):
    env.app.state.poller.track("15")
    ids = [env.add(route="15", delay=0, scheduled=T0 + i) for i in range(5)]
    for aid, e in zip(ids, (60, -120, 5, 300, -7)): env.fc(aid, 10, e)
    a = {h["horizon_min"]: h for h in env.get("/api/all/forecast").json()["horizons"]}
    r = {h["horizon_min"]: h for h in env.get("/api/routes/15/forecast").json()["horizons"]}
    for k in r:
        for f in ("n", "mean_error", "mae", "p_low", "p_high", "low_n"):
            assert a[k][f] == pytest.approx(r[k][f]) if r[k][f] is not None else a[k][f] is None

def test_all_forecast_pools_two_routes_and_clamps(env):
    env.app.state.poller.track("15"); env.app.state.poller.track("46A")
    env.fc(env.add(route="15", delay=0), 10, 100)
    env.fc(env.add(route="46A", delay=0), 10, -300)
    env.fc(env.add(route="46A", delay=0), 10, 100000)   # beyond clamp
    env.fc(env.add(route="untracked", delay=0), 10, 5)
    h = {x["horizon_min"]: x for x in env.get("/api/all/forecast").json()["horizons"]}[10]
    assert h["n"] == 3 and h["mean_error"] == pytest.approx((100 - 300 + 100000) / 3)
    assert h["p_high"] <= 3600

def test_all_forecast_no_tracked(env):
    hs = env.get("/api/all/forecast").json()["horizons"]
    assert [h["horizon_min"] for h in hs] == [30, 20, 10, 5, 2] and all(h["n"] == 0 and h["p_low"] is None for h in hs)
```

- [ ] **Step 2: Run** `... -k "hist_percentile or all_forecast"` — Expected: FAIL (ImportError / 404).
- [ ] **Step 3: Implement.** `_forecast_parts_db` uses the same SQL as `_forecast_db` (copy its select/join/filters verbatim, including `_tz` on `scheduled` at index 2) and fills per-horizon `np.bincount`-style counts per chunk (`np.add.at` or `np.bincount(minlength=...)`), plus running sum and abs-sum computed from unclamped errors. `_hist_percentile`: `rank = q*(n-1)`; `lo, hi = floor, ceil`; find values at those order-statistic ranks from `np.cumsum(counts)` via `np.searchsorted(cum, rank+1)`; interpolate `v_lo + (v_hi - v_lo) * (rank - lo)`; offset back by `FC_CLAMP`. Endpoint merges counts (elementwise add), sums, abs-sums across routes; `n==0` gives null stats; `low_n = n < LOW_N`.
- [ ] **Step 4: Run** same command. Expected: PASS. Then run the whole file once: `... pytest -q tests/api/test_routes.py` — Expected: all pass (no regressions).
- [ ] **Step 5: Checkpoint.**

---

### Task 4: Frontend plumbing: route name and API methods

**Files:**
- Modify: `web/js/lib.js`, `web/js/api.js`, `web/test/contract.test.mjs`, `web/test/lib.test.mjs`, `web/test/api.test.mjs`

**Interfaces:**
- Consumes: `buildQuery`, `qs`, `get` already in `api.js`.
- Produces: `ROUTES.all = []` so `buildHash('all', {}, q)` -> `#/all[?q]` and `parseHash('#/all')` -> `{name:'all', params:{}, query}`; `api.allRoutes(q, o)`, `api.allHeatmap(q, o)`, `api.allForecast(q, o)` GETting `/api/all/routes`, `/api/all/heatmap`, `/api/all/forecast` with the query string.

**Security flag:** `none`

**Does NOT cover:** nothing.

- [x] **Step 1: Write failing tests.** In `lib.test.mjs` add `['all', {}, {}]` to the existing hash round-trip table (line ~178) and assert `parseHash('#/all')` equals `{ name: 'all', params: {}, query: {} }` and `parseHash('#/all/x').name === 'notfound'`. In `api.test.mjs` add a test that each of the three methods requests the right path with `?since=5`. In `contract.test.mjs` add, next to the heatmap/forecast contract test, a test calling the three live endpoints with `${q}` and `has(...)` for keys: routes `['routes','filters']`, heatmap `['cells','n','filters']`, forecast `['horizons','filters']`.
- [x] **Step 2: Run** `cd web && node --test test/lib.test.mjs test/api.test.mjs` (see `web/package.json` for the project's test script; use it if different) — Expected: FAIL.
- [x] **Step 3: Implement** the one-line `ROUTES` entry and the three api methods mirroring `heatmap`/`forecast`.
- [x] **Step 4: Run** same. Expected: PASS (contract test runs only when its env/server is available, per its existing opts).
- [x] **Step 5: Checkpoint.**

---

### Task 5: Median marker in the range chart

**Files:**
- Modify: `web/js/charts.js`, `web/test/charts.test.mjs`

**Interfaces:**
- Consumes: existing `rangeDomain(items)`, `rangeGeometry(items, domain, o)`, `renderRangeChart`, `rangeTip`.
- Produces: range items accept optional `median` (seconds, may be null); `rangeDomain` includes it; geometry rows gain `xMedian` (same clamp as `xMean`); `renderRangeChart` draws `<circle class="median">` (r=4, fill none, ring) at `(xMedian, cy)` when non-null; `rangeTip` appends `median <fmtDelay>` right after mean when present. Items without `median` behave exactly as before.

**Security flag:** `none`

**Does NOT cover:** the median marker has no whisker/band of its own.

- [x] **Step 1: Write failing tests** in `charts.test.mjs`: (a) `rangeDomain([{mean:0, median:600, ...}])` upper bound >= 600 s; (b) `rangeGeometry` row has `xMedian` between `x0` and `x1` and `null` when median is null/absent; (c) `rangeTip({label:'R',mean:60,median:30,n:12})` contains `mean +1.0 min, median +0.5 min` using the same formatting `fmtDelay` yields (assert with `fmtDelay` imported rather than a literal); (d) render with the fake DOM produces one `circle` with class `median` per non-empty row that has a median, zero otherwise; (e) existing snapshots/tests unchanged.
- [x] **Step 2: Run** `cd web && node --test test/charts.test.mjs` — Expected: FAIL.
- [x] **Step 3: Implement** in `charts.js` (items doc comment updated to list `median`). Add a CSS rule next to the existing `.mean` styles in `web/css/style.css`: `.range-chart .median { fill: none; stroke: currentColor; stroke-width: 2 }` plus a legend sentence is added by the page (Task 6), not here.
- [x] **Step 4: Run** same plus `node --test test/svgns.test.mjs test/contrast.test.mjs`. Expected: PASS.
- [x] **Step 5: Checkpoint.**

---

### Task 6: The `#/all` page

**Files:**
- Create: `web/js/pages/all.js`
- Modify: `web/js/pages/analytics.js` (export `legend` and `drawForecast`), `web/js/app.js` (import `* as all`, add to `PAGES`, `NAV.all = 'routes'`), `web/css/style.css` (sortable header button styling, only if needed)
- Test: `web/test/allpage.test.mjs` (create)

**Interfaces:**
- Consumes: `api.allRoutes/allHeatmap/allForecast` (Task 4); range chart `median` support (Task 5); from analytics.js `legend(maxAbs)` and `drawForecast(route, d, el)` (pass `''` as route; it does not use it); `setup`, `filterBar`, `filterNotices`, `windowText`, `crumbs` from `pages/common.js`; `heatQuery`, `forecastQuery` from analytics.js; `region`, `dataTable`, `figure`, `put`, `h`, `link`, `emptyState`, `notice`, `buildHash`, `heatmapGrid`, `renderHeatmap`, `rangeGeometry`, `rangeDomain`, `renderRangeChart`, `fmtDelay`, `fmtPct`, `fmtCount`, `getSettings`.
- Produces: `export function sortRoutes(routes, key, dir)` where `key` in `route|n|mean|median|on_time_pct|reliability`, `dir` in `asc|desc`, returns a new array; null/undefined values last in both directions; `route` sorts with `localeCompare(…, undefined, {numeric:true})`. `export async function render(ctx)`.

**Security flag:** `none`

**Does NOT cover:** heatmap cell selection (cells are not clickable on this page); stop-level data.

- [x] **Step 1: Write failing tests** in `web/test/allpage.test.mjs` (follow the fakedom pattern of `sidebar.test.mjs` / `pages.test.mjs`):
  - `sortRoutes`: median desc puts `null` last; median asc puts `null` last; `route` numeric order gives `['9','15','46A']`; input array is not mutated.
  - render with stubbed fetch for the three endpoints and routes `15`, `46A`, `empty` (n=0): page contains `h1` text `All tracked routes`; the hint text `Pooled by arrival, not by route.`; table rows default-sorted by median descending with `empty` last and its cell text `no data`; the chart's route link hrefs equal `buildHash('analytics', { route }, linkQuery)` (check with a route named `sA/B` -> `#/analytics/sA%2FB`); the median-header button click toggles `aria-sort` to `ascending` and reorders rows.
  - render with `{routes: []}` -> an element with text matching `/No routes tracked/`.
  - heatmap request omits `weekdays`/`hours` params, forecast request omits `pct_lo`/`pct_hi`.
  - `filters.truncated_routes: ['15']` produces a notice naming `15`.
  - one failing endpoint (500) shows an error box with Retry inside that section only; the other two sections still render.
- [x] **Step 2: Run** `cd web && node --test test/allpage.test.mjs` — Expected: FAIL (module missing).
- [x] **Step 3: Implement** `all.js` following `analytics.js`'s structure:
  - `render(ctx)`: `s = setup(ctx)`; `ctx.title('All routes')`; `put(ctx.main, crumbs(link('#/', '← All routes list')), h('h1', {}, 'All tracked routes'), h('p', {class:'subtitle'}, '… Pooled by arrival, not by route.'), ...filterBar(ctx, 'all', {}, s), ...three sections)`; three `region()`s loaded with `Promise.all`; heatmap/forecast use `heatQuery`/`forecastQuery` of `s.apiQuery`; announce strings like the per-route page.
  - Comparison section: sort state `{key:'median', dir:'desc'}` in closure; `draw()` rebuilds a `figure` (chart + table). Chart items for routes with non-null mean: `{label: 'Route '+r, short: r, href: buildHash('analytics',{route:r}, s.linkQuery), n, low_n, mean, median, min:null, max:null, lo:p_low, hi:p_high}`, in the current sort order, `renderRangeChart` with `axisLabel: 'minutes vs schedule'`. Table columns: Route (link), n, Mean, Median, On time (`fmtPct`), Reliability; header cells are `<button>` with `aria-sort` on the active one; n < 10 rows get the existing low-n row class used by `dataTable` for `low_n`. Legend line: "Diamond: mean · ring: median · bar: 5th to 95th percentile…" with percentile labels from existing controls formatting. Empty state when no routes tracked or no route has data.
  - Heatmap section: `heatmapGrid(cells, {cap: getSettings().charts.heatCap * 60})`, `renderHeatmap` without `onSelect`, `dataTable` fallback, `legend(grid.maxAbs)` from analytics.js; heading `When are buses late? Mean delay by weekday and hour, all routes`.
  - Forecast section: `drawForecast('', d, el)`.
  - Truncation notice: if `data.filters.truncated_routes?.length`, `notice(...)` naming those routes and the 500,000 per-route cap, shown above each section that returned it.
- [x] **Step 4: Run** `node --test test/allpage.test.mjs test/pages.test.mjs` — Expected: PASS (pages tests confirm analytics.js export change broke nothing).
- [x] **Step 5: Checkpoint.**

---

### Task 7: Sidebar "All routes" button

**Files:**
- Modify: `web/js/sidebar.js`, `web/css/style.css` (only if the link needs spacing), `web/test/sidebar.test.mjs`

**Interfaces:**
- Consumes: `buildHash('all')`, existing `markActive`, `link`.
- Produces: an `<a data-all href="#/all">All routes</a>` rendered directly under the "Tracked routes" heading and above `trackedBox`, present even with zero tracked routes; `aria-current="page"` exactly when `parseHash(location.hash).name === 'all'`.

**Security flag:** `none`

**Does NOT cover:** the link is not an Untrack target and is not counted among tracked routes.

- [x] **Step 1: Write failing tests** in `sidebar.test.mjs`: (a) the sidebar contains one link with text `All routes` and href `#/all`; (b) with hash `#/all` it has `aria-current='page'` and no route link does; with `#/analytics/sA` it has none; (c) it exists when no routes are tracked. Update the existing test `sidebar: tracked links are url-encoded` which asserts `root.byTag('a').map(href)`: filter out the All-routes link (or expect `'#/all'` first) so it keeps asserting the tracked links only.
- [x] **Step 2: Run** `cd web && node --test test/sidebar.test.mjs` — Expected: FAIL.
- [x] **Step 3: Implement** in `sidebar.js`: create the link once in `renderSidebar` (outside `draw()` so confirm/untrack redraws do not rebuild it), include it in `root.replaceChildren(... h('h2', {}, 'Tracked routes'), allLink, trackedBox)`, and extend `markActive` to set/remove `aria-current` on it using `parseHash(...).name === 'all'`.
- [x] **Step 4: Run** `node --test test/sidebar.test.mjs` — Expected: PASS. Then the whole suite: `cd web && npm test` (or `node --test test/`) — Expected: all pass.
- [x] **Step 5: Checkpoint.**

---

### Task 8: Docs, seed data check, final verification

**Files:**
- Modify: `README.md` (Analytics bullet near line 37: add "an All routes page pooling every tracked route plus a per-route mean/median comparison"), `docs/verification.md` (add a manual check: "All routes page: chart legibility with 20+ routes, light and dark; sorting by keyboard").

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

**Security flag:** `none`

**Does NOT cover:** performance on the Pi (measured separately; spec non-goal).

- [ ] **Step 1: Edit** the two docs as described (exact text above).
- [ ] **Step 2: Run backend suite** one file per process: `for f in tests/api/test_routes.py tests/test_docs.py tests/test_seed_demo.py; do ulimit -v 2500000; OPENBLAS_NUM_THREADS=1 ../.venv/bin/pytest -q $f || break; done` — Expected: all pass (`test_docs.py` may assert README/docs structure; fix wording if it fails).
- [ ] **Step 3: Run frontend suite** `cd web && npm test` — Expected: all pass.
- [ ] **Step 4: Visual check (by the main session, not a subagent):** start the app with demo data (`scripts/seed_demo` per README), open `#/all` in the built-in browser; confirm chart with mean diamond + median ring, sort headers, heatmap, forecast, light and dark theme, narrow width.
- [ ] **Step 5: Checkpoint:** report results; ask the user whether to commit/merge.
