# Stats Lab All-Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `#/lab` page and `/api/all/lab/{distribution,variance,persistence}` endpoints that pool the Stats lab across all tracked routes.

**Architecture:** Generalise the per-route lab scans to take a SQL `where` + args (pooled scan over tracked routes), add a `route` factor to the all-routes variance, and pool persistence per route by adding per-day additive sums. The page reuses `web/js/pages/lab.js` with an "all routes" mode.

**Tech Stack:** Python 3.12, FastAPI, numpy; plain ES modules; pytest, `node --test`.

**Spec:** `docs/specs/2026-11-13-stats-lab-all-routes-design.md` (and the base `docs/specs/2026-11-13-stats-lab-design.md`)

**Assumptions:**
- Assumes `arrivals.route` is populated for every row (it is: the poller writes it). Will NOT work for rows with a null route; they are ignored by the `route in (...)` filter.
- Assumes `_each_tracked` and `_heavy(request, p, where, args, work)` behave as described in the code (read them first).

## Global Constraints

- Models for subagents: sonnet or cheaper only, never opus. At most 2 agents at once. Run Python under `ulimit -v 2500000`; never run `tests/api/test_routes.py` in full inside a task (it is slow and heavy); run `tests/api/test_lab_routes.py`, `tests/analysis/test_lab.py` and `-k accumulation` only. WSL crashes if RAM runs out.
- Commit at the end of every task, ending the message with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; specific paths only.
- All Global Constraints of `docs/plans/2026-11-13-stats-lab.md` still apply (pure `lab.py`, None-not-NaN, `low_n` n<10, day-block bootstrap 200 resamples fixed seed, CI hidden under 5 days, `P` params, `_heavy`, `_clean`, no innerHTML, plain-English explanations).
- Per-route endpoints and their tests must keep passing unchanged. No server change. Nothing stored.

## Review Focus

- No tracked routes: 200 with `n: 0`, empty arrays, `routes: 0`; the page shows the empty state.
- Persistence where every route's stop order is unavailable: 200, `order_source: "none"`, `routes_skipped` lists them all; where only some are unavailable: pooled over the rest, `routes_skipped` lists the others.
- One stop id shared by two routes is a single `stop` level; `route` factor `levels` equals the number of routes with data.
- A busy route and a quiet one: the pooled result equals the per-route function applied to the union of rows (pinning test).
- Time and memory for 5 routes x ~80k rows (about 400k pooled) stays within the existing bar (under 15 s per endpoint, under 2.5 GB cap).

## File Structure

- Modify `app/api/routes.py`: parameterise the lab scans by `(where, args)`; add three `/all/lab/*` handlers; add the `route` factor to the pooled variance.
- Modify `tests/api/test_lab_routes.py`: pooled endpoint tests (seed two or three routes with `Env`).
- Modify `web/js/api.js`, `web/js/app.js`, `web/js/lib.js`, `web/js/sidebar.js`, `web/js/pages/lab.js`, `web/js/pages/all.js`, `web/js/labtext.js`, `web/css/style.css` and tests under `web/test/` for the page.

---

### Task 1: Pooled backend

**Files:**
- Modify: `app/api/routes.py`
- Modify: `tests/api/test_lab_routes.py`

**Interfaces:**
- Consumes: `_heavy(request, p, where, args, work)`, `_scan_arrivals(request, p, where, args, cols, fn)`, `_each_tracked(request, p, fn)`, `_directions`, `_NoOrder`, `_matched_trips`, `_filters`, `_clean`, `lab.day_bootstrap`, `lab.omega_squared(codes, y, levels=...)`, `lab.autocorr_sums`, `lab.recovery_sums`, `lab.autocorr_from_sums`, `lab.recovery_from_sums`, and the existing `_distribution_db`, `_variance_db`, `_persistence_db` helpers (read how they are written first).
- Produces:
  - `GET /api/all/lab/distribution` -> per-route distribution shape without `route`, plus `routes: int`.
  - `GET /api/all/lab/variance` -> `{filters, n, days, low_n, enough_days, routes, factors:[hour, weekday, stop, date, route]}` (five entries, in that order).
  - `GET /api/all/lab/persistence` -> per-route persistence shape without `route`, plus `routes_used: list[str]`, `routes_skipped: list[str]`, `order_source`, `routes: int`.
  - The per-route `/routes/{r}/lab/*` responses unchanged.

**Security flag:** `none`
**Does NOT cover:** weighting routes equally; per-route breakdowns; cross-route stop alignment.

- [ ] **Step 1: Write failing tests** in `tests/api/test_lab_routes.py` (seed 3 tracked routes via the `Env` helper; track them the way existing `/all/*` tests do):
  - `test_all_distribution_matches_pooled_rows`: tiles equal `lab.shape_stats` over the union of all routes' delays (approx 1e-9); `routes == 3`; `n` is the sum.
  - `test_all_distribution_no_tracked_routes`: 200, `n == 0`, `ecdf == []`, `routes == 0`, finite JSON.
  - `test_all_variance_has_route_factor`: five factors in order `hour, weekday, stop, date, route`; delays that depend only on route give `route.omega2 > 0.9` and `route.levels == 3`; each of the other four equals `lab.omega_squared` on the same pooled data (approx 1e-9).
  - `test_all_variance_shared_stop_is_one_level`: the same `stop_id` used by two routes counts once in `stop.levels`.
  - `test_all_persistence_pools_routes`: two routes with their own stop lists (stub like the existing persistence tests); `routes_used` has both, lag `r`/`n` equal `lab.autocorr_by_lag` on all trips of both routes combined.
  - `test_all_persistence_skips_unavailable_route`: server stop order fails for one route -> that route in `routes_skipped`, the other pooled, `order_source == "server"`; all fail -> `order_source == "none"`, `routes_used == []`, 200.
  - `test_all_endpoints_filters_echoed`: `weekdays=` filter reduces `n` and is echoed in `filters`.
  - `test_per_route_endpoints_unchanged`: the existing per-route tests still pass (run them).
- [ ] **Step 2:** Run `ulimit -v 2500000; pytest -q tests/api/test_lab_routes.py -k "all_"`. Expected: FAIL (404).
- [ ] **Step 3: Implement** the handlers. Distribution and variance: call the existing per-route logic with `where = f"route in ({marks})"` and the tracked route list as args through `_heavy` (generalise `_distribution_db`/`_variance_db` to take `(where, args)` and, for variance, also select `route` to build route codes with `levels=` the number of routes with data; keep the per-route handlers calling the same code with `where="route = ?"`). Persistence: use `_each_tracked` with a function that returns, per route, `{service_date: vector}` built exactly as `_persistence_db` does (catching `_NoOrder` per route -> skipped); add vectors per date across routes; reuse the existing bootstrap/response code.
- [ ] **Step 4:** Run `ulimit -v 2500000; pytest -q tests/api/test_lab_routes.py tests/analysis/test_lab.py` and `pytest -q tests/api/test_routes.py -k "accumulation or all_"`. Expected: PASS.
- [ ] **Step 5: Commit**
```bash
git add app/api/routes.py tests/api/test_lab_routes.py
git commit -m "Add pooled all-routes Stats lab endpoints"
```

---

### Task 2: All-routes page and navigation

**Files:**
- Modify: `web/js/api.js`, `web/js/app.js`, `web/js/lib.js`, `web/js/sidebar.js`, `web/js/pages/lab.js`, `web/js/pages/all.js`, `web/js/labtext.js`, `web/css/style.css`
- Test: `web/test/api.test.mjs`, `web/test/labpage.test.mjs`, `web/test/sidebar.test.mjs`, `web/test/allpage.test.mjs`, `web/test/labtext.test.mjs`, `web/test/lib.test.mjs`

**Interfaces:**
- Consumes: the three `/api/all/lab/*` endpoints (Task 1); existing lab page, charts, popovers, `EXPLAIN`.
- Produces: `api.allLabDistribution(q, o)`, `api.allLabVariance(q, o)`, `api.allLabPersistence(q, o)`; hash route `#/lab` with no params (page name `lab`, `ROUTES.lab` must accept no route: check how `#/all` is registered and parse `#/lab` -> `{name: 'lab', params: {}}` while `#/lab/15` still gives `{route: '15'}`); `render(ctx)` in `pages/lab.js` switches to all-routes mode when `ctx.params.route` is absent.

**Security flag:** `none`
**Does NOT cover:** per-route breakdown tables in the pooled page.

- [ ] **Step 1: Write failing tests:**
  - api: `allLabDistribution({since: 5})` requests `/api/all/lab/distribution?since=5` (and the other two).
  - lib: `parseHash('#/lab')` gives name `lab` with no route; `buildHash('lab')` gives `#/lab`; per-route behaviour unchanged.
  - labpage: at `#/lab` the h1 is `Stats lab for all routes`, a note shows `{routes} routes pooled`; the variance chart has five bars including `route`; `routes_skipped` non-empty shows `N routes left out: stop order unavailable`; `routes: 0` shows the empty state; sections load independently (a failing endpoint leaves the others); per-route page unchanged.
  - sidebar: a `Stats lab (all routes)` link with href `#/lab` under the `All routes` link; aria-current on `#/lab`.
  - allpage: the All routes page has a link to `#/lab`.
  - labtext: the variance explanation mentions the route factor only in all-routes mode (`EXPLAIN.variance` unchanged for per-route; add `EXPLAIN.varianceAll` or an exported helper used for the all-routes popover) and `readVariance` names `route` when it is the top factor.
- [ ] **Step 2:** Run `node --test web/test/`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** the api methods, route registration, sidebar and All-routes links, the all-routes mode in `lab.js` (same sections, charts, popovers, pairing, hover, equal heights), the note and skipped-routes line, and the explanation text. The `variance` bar chart takes the five factors from the response unchanged (the chart already renders whatever factors arrive; check label space for five bars).
- [ ] **Step 4:** Run `node --test web/test/`. Expected: all PASS.
- [ ] **Step 5: Commit**
```bash
git add web/js web/css web/test
git commit -m "Add the all-routes Stats lab page and links"
```

---

### Task 3: Verify

**Files:** none
**Interfaces:** Consumes: everything. Produces: nothing.
**Security flag:** `none`

- [ ] **Step 1:** Under `ulimit -v 2500000`, one at a time: `pytest -q tests/analysis tests/api/test_lab_routes.py tests/test_docs.py`, then `pytest -q tests/api/test_routes.py`; then `node --test web/test/`. Expected: all PASS. (`tests/export` hangs as a directory on `main` as well; run its four files individually if needed.)
- [ ] **Step 2:** Time the three `/api/all/lab/*` endpoints on ~400k pooled arrivals across 5 routes under the memory cap (reuse the measurement approach of Task 12 of the base plan); each under 15 s. Add the README sentence for `#/lab` and the endpoints; run `tests/test_docs.py`.
- [ ] **Step 3:** One code review on the branch diff with the Review Focus above; then `verification-before-completion`.
