# Remove-day: fix, range, live progress — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "Remove a day of data" actually work, let it remove a range of days as one background job, and show live progress (spinner on the button, bar, counts).
**Architecture:** `app/maintenance/day.py` keeps per-day removal and gains a progress callback and range parsing. A new `app/maintenance/jobs.py` runs a range removal in one background thread with in-memory status. `api.py` exposes start (202) / status endpoints and a range preview. The Routes page polls the job.
**Tech Stack:** Python/FastAPI/sqlite (pytest), vanilla ES modules (`node --test web/test/`).
**Spec:** docs/specs/2026-11-12-remove-day-progress-design.md
**Assumptions:** Single-process server — will NOT work with multiple workers (job state is in memory). Job status lost on restart (UI handles 404). Runs under the repo's `.venv` with `ulimit -v 2500000`, one pytest file per process (see session notes).

## Global Constraints
- Models for subagents: sonnet or cheaper only; max 2 agents; RAM-mindful.
- Max range 62 days (inclusive); `confirm` equals `date` (single day) or `date..end` (range).
- Origin check, backup lock (`app.backup.backup._lock`, non-blocking), `DayBusy` partial-count semantics stay.
- No external assets; spinner is CSS only and honours `prefers-reduced-motion`.
- Status endpoint never touches SQLite.

## Review Focus
- Remove button clicked on a fake DOM hides real-DOM bugs (`.all` exists only in `web/test/fakedom.mjs`): a source scan test pins that page code never calls it (Task 1).
- Job thread crashes with an unexpected exception: lock must still release and state must become `error` (Task 3).
- Range where `end < date`, or `end` given with a malformed value: 422 (Task 2).
- Remove clicked twice quickly / second tab: 409 with running `job_id`, UI attaches (Tasks 3, 5).
- Range crossing a gzipped month: skipped months are reported per job, not an error (Task 3).

---
## File Structure
- Modify `web/js/pages/routes.js` (fix, range UI, polling), `web/js/api.js` (new calls), `web/css/style.css` (spinner/progress)
- Modify `app/maintenance/day.py` (progress callback, `parse_range`, `preview_range`), `app/maintenance/api.py` (endpoints)
- Create `app/maintenance/jobs.py` (job runner)
- Tests: `tests/maintenance/test_day.py`, `tests/maintenance/test_api.py`, create `tests/maintenance/test_jobs.py`, `web/test/pages.test.mjs`, `web/test/api.test.mjs`

### Task 1: Fix the silent click bug (ships on its own)

**Files:**
- Modify: `web/js/pages/routes.js` (`remove()`)
- Test: `web/test/pages.test.mjs` (day section, ~line 419+)

**Interfaces:**
- Consumes: nothing
- Produces: nothing

**Security flag:** `none`

- [ ] **Step 1: Write failing tests** in the `// ---- remove a day of data ----` section:
```js
test('remove day: page code never calls non-DOM helpers (.all/.byTag/.byText exist only in fakedom)', async () => {
  const src = await (await import('node:fs/promises')).readFile(new URL('../js/pages/routes.js', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /\.(all|byTag|byText)\(/);
});
test('remove day: clicking Remove sends the POST and shows the result', async () => { /* use dayPage with PREV + a POST handler; preview, click p.rm(); settle; assert calls include 'POST /api/data/day/remove' and status text matches /Removed/ */ });
```
- [ ] **Step 2:** Run `node --test web/test/pages.test.mjs` → both FAIL (scan finds `rmSlot.all`).
- [ ] **Step 3:** In `remove(date, route)` capture the button via the click handler: change the preview's button handler to `on: { click: (ev) => remove(date, route, ev.currentTarget) }` and `remove(date, route, rmBtn)`; delete the `rmSlot.all` line. Wrap the body so any non-abort exception calls `say(\`Unexpected error: ${e.message}\`)`.
- [ ] **Step 4:** Run `node --test web/test/` → PASS.
- [ ] **Step 5: Commit** `git add web && git commit -m "Fix Remove day button: stop calling non-DOM .all"`

### Task 2: Range parsing, preview_range, progress callback

**Files:**
- Modify: `app/maintenance/day.py`
- Test: `tests/maintenance/test_day.py`

**Interfaces:**
- Consumes: existing `parse_day`, `preview`, `remove`
- Produces:
  - `MAX_DAYS = 62`
  - `parse_range(date: str, end: str | None, today: date | None = None) -> list[date]` (raises `DayError`; end None → `[d]`; `end < date` or span > MAX_DAYS → DayError)
  - `preview_range(data_dir, days: list[date], route: str | None) -> dict` (the `preview` shape summed over days, with `"date"` = first, `"end"` = last, plus `"days": [{"date","arrivals","forecasts","samples","gaps"}]`, `months`/`skipped_months` unioned)
  - `remove(data_dir, d, route, progress: Callable[[str, dict], None] | None = None) -> dict` where `progress(stage, out)` is called after every committed chunk with stage in `{"samples","arrivals","gaps"}`

**Security flag:** `security` (input validation)

**Does NOT cover:** non-contiguous days; ranges over 62 days.

- [ ] **Step 1: Write failing tests:** `parse_range("2026-11-05", None) == [date(2026,11,5)]`; 3-day range length 3; `end` before date → DayError; 63 days → DayError; malformed `end` → DayError; `preview_range` over two seeded days sums counts and lists both in `days`; `remove(..., progress=cb)` calls `cb` with stages `samples` then `arrivals` and the dict passed has monotonically growing counts.
- [ ] **Step 2:** `.venv/bin/python -m pytest tests/maintenance/test_day.py` → FAIL (missing names).
- [ ] **Step 3:** Implement the signatures above; call `progress` next to each `_between_chunks()` (samples loop lives in `_samples_one`: thread `progress` through `_scan_months` via `state`, e.g. `state["_progress"]`, stripped before return).
- [ ] **Step 4:** Run test file → PASS.
- [ ] **Step 5: Commit** `git commit -m "day: range parsing, range preview, progress callback"`

### Task 3: Job runner

**Files:**
- Create: `app/maintenance/jobs.py`
- Test: `tests/maintenance/test_jobs.py`

**Interfaces:**
- Consumes: `day.remove`, `day.preview_range`, `day.DayBusy`
- Produces:
  - `class JobBusy(Exception)` with `.job_id: str | None`
  - `start(data_dir, days: list[date], route: str | None, lock) -> str` — caller has already acquired `lock`; `start` raises `JobBusy` if a job is running (caller then releases), otherwise starts a daemon thread that releases `lock` in `finally` and returns the job id
  - `get(job_id: str) -> dict | None` returning `{"job_id","state":"running|done|error","stage","day_index","day_count","current_day","done":{arrivals,forecasts,samples,gaps},"total":{same},"skipped_months":[...],"error":str|None}` (on error, `done` holds the partial counts); finished records kept 600 s
  - `running_id() -> str | None`

**Security flag:** `none`

**Does NOT cover:** cancel, multi-process, persistence across restart.

- [ ] **Step 1: Write failing tests:** start over 2 seeded days, poll `get` until done → counts equal `preview_range` totals, `day_index == 2`; second `start` while running raises `JobBusy` carrying the id; a patched `day.remove` raising `DayBusy` on day 2 → `state=="error"`, day 1 counts present, error names day 2; a patched `day.remove` raising `RuntimeError` → `state=="error"` and `lock` released; `get("nope")` is None; skipped gz month appears in `skipped_months`.
- [ ] **Step 2:** `.venv/bin/python -m pytest tests/maintenance/test_jobs.py` → FAIL.
- [ ] **Step 3:** Implement with a module lock protecting a dict of job records; thread loop calls `day.remove(..., progress=cb)` per day and folds each day's counts into `done`; `total` from `preview_range` taken at start inside the thread's first step (before state flips from "starting"; set `state="running"` immediately so `get` is never empty).
- [ ] **Step 4:** Run file → PASS.
- [ ] **Step 5: Commit** `git commit -m "maintenance: background removal job with progress"`

### Task 4: API endpoints

**Files:**
- Modify: `app/maintenance/api.py`
- Test: `tests/maintenance/test_api.py`

**Interfaces:**
- Consumes: `parse_range`, `preview_range`, `jobs.start/get/running_id/JobBusy`
- Produces: `GET /api/data/day?date&end&route` (single day keeps today's response shape plus `"end"`/`"days"`); `POST /api/data/day/remove?date&end&route&confirm` → `202 {"job_id"}`; 409 `{"detail", "job_id"}` when running/lock busy; `GET /api/data/day/remove/{job_id}` → `jobs.get` dict or 404

**Security flag:** `security`

- [ ] **Step 1: Update/write failing tests:** adapt existing remove tests to 202 + poll helper; range confirm `"2026-11-05..2026-11-06"` accepted, plain `date` confirm for a range → 422; end before date → 422; origin check still 403; second POST while running → 409 with `job_id`; unknown job → 404; lock released after done.
- [ ] **Step 2:** `.venv/bin/python -m pytest tests/maintenance/test_api.py` → FAIL.
- [ ] **Step 3:** Rewrite `remove_day`: validate, acquire `_backup._lock` non-blocking (409 if busy, include `jobs.running_id()`), `jobs.start(...)` (on `JobBusy` release lock, 409 with id), return 202. Status route has no DB access.
- [ ] **Step 4:** Run file → PASS; also `.venv/bin/python -m pytest tests/test_main.py`.
- [ ] **Step 5: Commit** `git commit -m "API: range preview, async remove job, status endpoint"`

### Task 5: UI — range, spinner, polling

**Files:**
- Modify: `web/js/api.js`, `web/js/pages/routes.js`, `web/css/style.css`
- Test: `web/test/api.test.mjs`, `web/test/pages.test.mjs`

**Interfaces:**
- Consumes: Task 4 endpoints
- Produces: `api.dayPreview({date,end,route},o)`, `api.removeDay({date,end,route},o)` → `{job_id}` (confirm = `end ? date+'..'+end : date`), `api.removeJob(jobId,o)`; DOM ids `day-date`, `day-end` (new, optional), `day-route`, `day-status`; Remove button class `btn btn-danger solid` gains `busy` class and `aria-busy="true"` while running; `<progress id="day-progress">` in `#day-status`.

**Security flag:** `none`

- [ ] **Step 1: Write failing tests:** api: removeDay builds the confirm for single and range; removeJob GETs the path. Pages: (a) end field exists with a label; (b) clicking Remove → button has `aria-busy`, class `busy`, label matches /Removing/; a progress element appears with value from a polled `running` status (`done`/`total` sums) and text "day 1 of 2"; (c) polled `done` → summary "Removed …", button state cleared; (d) polled `error` → message plus "Part of the day was removed"; (e) POST 409 with `job_id` → attaches and polls; (f) poll 404 → text contains "Status lost". Use a short poll interval injectable via `ctx.pollMs` (default 1000).
- [ ] **Step 2:** `node --test web/test/` → FAIL.
- [ ] **Step 3:** Implement. Poll loop checks `signal.aborted` and the `token`; stops on done/error/404. Spinner CSS: `.btn.busy::before` a 0.9em border-spinner with `@keyframes spin`; under `@media (prefers-reduced-motion: reduce)` no animation (static ring). `<progress>` styled with existing tokens. Per-day list from preview rendered as a short `<ul>` (hidden when single day).
- [ ] **Step 4:** `node --test web/test/` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Routes UI: day range, button spinner, live removal progress"`

### Task 6: Docs and manual check

**Files:**
- Modify: `README.md`, `docs/verification.md` (remove-day wording → job + range)

**Interfaces:** Consumes/Produces: nothing
**Security flag:** `none`

- [ ] **Step 1:** Update the endpoint descriptions to: remove is async (202 + status endpoint), supports `end`, max 62 days.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_docs.py` → PASS.
- [ ] **Step 3: Manual check** (not by a subagent): `.venv/bin/python scripts/seed_demo.py` data, run the app, open Routes in the browser, preview a 2-day range, click Remove, confirm spinner, bar and final counts, then re-Preview shows "Nothing recorded".
- [ ] **Step 4: Commit** `git commit -m "Docs: async day removal and ranges"`
