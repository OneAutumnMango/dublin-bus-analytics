# Remove-day: fix silent failure + live progress

## Problem
Pressing "Remove" on the Routes page does nothing: no request, no log, no message.
Cause: `web/js/pages/routes.js` `remove()` calls `rmSlot.all(...)`; DOM elements have no `.all`,
so a TypeError is thrown in the async handler before any request, and the rejection is silent.
Backend tests call the API directly and never hit it. The UI also only shows "Removing…" with
no progress even when it works.

## Scope
1. Fix the click handler bug; surface unexpected exceptions in the status area.
2. Run removal as a background job with live progress (bar, stage, running counts).
3. Visible "it is happening" feedback: spinner on the Remove button, `aria-busy`, label "Removing… day 2 of 5".
4. Remove a range of days (From, To; To optional = single day) as one job, days processed in order.

## Non-goals
Arbitrary non-contiguous day queue (use a range or run again), vacuum/disk shrink, job cancel, editing gzipped months, job history, fixing the tracker re-creating rows for a live day (existing note stays).

## Part 1: frontend fix
- `remove()` gets the button from the click event (or a held reference), not `rmSlot.all`.
- Handler wrapped in try/catch: any unexpected error is shown in `#day-status`.
- Regression test: click the real Remove button; assert the POST is sent.

## Part 2: backend job
- `POST /api/data/day/remove` (origin check, `confirm == date`, backup lock unchanged) returns `202 {job_id}`; the job thread holds `_backup._lock` for its whole run and releases it at the end.
- `GET /api/data/day/remove/{job_id}` returns `{state: running|done|error, stage, done:{samples,forecasts,arrivals,gaps}, total:{...}, skipped_months, error?}`. Unknown id: 404.
- `total` comes from `preview()` at job start. Progress counters are updated per committed chunk (existing `_between_chunks` hook point / `out` counters). Status is read from memory only, never SQLite.
- One job at a time: a start while one runs (or a backup holds the lock) returns 409 with the running `job_id` when there is one.
- Job records live in memory ~10 min after finishing. On `DayBusy`, state=error with the partial `done` counts and the existing message.
- `day.remove()` keeps its semantics; it gains an optional progress callback.

## Range of days
- Params: `date` (From), optional `end` (To, inclusive, >= date). Each day is validated with `parse_day`; max range 62 days (422 beyond). `confirm` must equal `date` for a single day, or `date..end` for a range.
- Preview (`GET /api/data/day`, optional `end`) returns the summed counts plus a per-day list `days:[{date, arrivals, forecasts, samples, gaps}]`.
- The job iterates days in order calling `day.remove()` per day. `total` is the summed preview; `progress` adds `day_index`, `day_count`, `current_day`.
- On `DayBusy` on day k: state=error, days before k are complete, `done` counts include the partial day, message names the day; rerunning the same range finishes it (idempotent).
- UI: the To field is optional; the preview shows total and a short per-day list; the Remove button label includes the day count.

## Part 3: frontend progress
- On click the button immediately shows a CSS spinner (no external assets), its label changes to "Removing…", and it is disabled with `aria-busy="true"`; the status area also shows the bar. Respects `prefers-reduced-motion` (spinner becomes static text/dots).
- After 202, poll the job ~1/s: `<progress>` bar (sum done / sum total), stage text, running counts, buttons disabled.
- Finish: final summary (existing `counts`, skip and live notes). Error: message plus "Part of the day was removed: …; try again."
- 409 with `job_id` (second tab / reload): attach and poll that job.
- Poll 404 (server restarted): "Status lost; run Preview to check, rerun finishes the removal."
- Polling stops on navigation (existing `signal`) and on done/error.

## Failure modes
- Poll during DB lock: status served from memory. Minor.
- Server restart mid-job: 404 path; delete is idempotent so a rerun finishes. Minor.
- Two tabs: single-job rule + attach. Handled.
- Tracker re-creating live-day rows: documented non-goal.

## Testing
- Backend: range of 3 days removes all, 63-day range 422, wrong confirm for range 422, failure on day 2 leaves day 1 complete and reports day 2; 202 + poll to done; counts match preview; 409 single-job with `job_id`; busy error state with partial counts; unknown id 404; lock released after done/error; origin and confirm checks still enforced.
- Frontend: regression click test; spinner/aria-busy appear on click and clear on done/error; progress renders from polled states; error, attach and 404 paths.
