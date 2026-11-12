# Verification status

## Verified (Task 11)

- `docker build` of the analytics image succeeds (504 MB on disk); it serves the UI (`/`, `/js/app.js`, `/css/style.css` with CSP headers), 404s `/package.json`, lists seeded routes at `/api/routes/tracked`, runs as uid 1000, and the HEALTHCHECK turns `healthy`.
- With no data server reachable the poller logs "server unavailable" and the app stays up (`/api/health` ok).
- `docker compose --profile analytics config` in the server repo renders the analytics service (plain `docker compose config` omits it); `ANALYTICS_BACKUP_*` overrides and defaults (03:00, 7, true, /data/backups) both come through.
- pytest (`tests/test_docs.py`, `tests/api/test_static.py`, `tests/test_main.py`) and `node --test web/test/`.

## NOT verified (owed manually)

Nothing below was run: it needs a real API key, the Kotlin build, or a browser.

### 1. Full-stack smoke

```bash
scripts/smoke.sh      # builds and starts both services, tracks route 15, waits for arrivals
```

Passes when it prints `OK`. Stop with `docker compose --profile analytics down` in `../dublin-bus-server`.

### 2. Server `scheduledEpochSeconds` against the published timetable

```bash
STOP=<a stop id>; ROUTE=<route, e.g. 15>
curl -s "localhost:8080/v1/stops/$STOP/arrivals?route=$ROUTE" | python3 -m json.tool
# convert: date -d @<scheduledEpochSeconds> (TZ=Europe/Dublin)
TZ=Europe/Dublin date -d @<scheduledEpochSeconds> '+%F %T'
```

Compare that local time with the NTA / Transport for Ireland timetable for the same stop, route and trip. Expect: equal to the timetable time. `null` (absent) is acceptable only for the documented cases: nearest call more than 1800 s away, ambiguous loop trips, delay-only events on trips that call twice at the stop. Any other mismatch or null is a bug.

### 3. Browser check of the UI (http://127.0.0.1:8081, seed with `scripts/seed_demo.py` if needed)

- Routes page: tracked list, Backup card.
- Route page: heatmap legibility (colour and labels, light and dark).
- Map page, "Colour by": the default "Change per stop" colours each stop by the delay it adds over the previous stop of the same trip (a direction's first stop uses its own delay); "Cumulative delay" colours by delay against schedule. On a route with a known late-building run, compare both modes: change mode shows the stops where lateness builds in orange and later stops near neutral, while cumulative mode shows orange growing along the route. Subtitle, legend, tooltip and table header switch with the mode, and the choice survives a reload.
- An 80-stop route chart: stop label collisions.
- 360 px wide viewport: no horizontal scroll, controls usable.
- Theme toggle: persists across reload, both themes readable.
- Deep link `#/route/15%2FA` loads the route.
- DevTools console: no CSP violations or errors.

### 4. JSON export (manual)

- Routes page, Backup card: choose "All routes" and click Download JSON; the file `analytics-export-YYYYMMDD.json` downloads.
- Open it in a JSON viewer: keys in order `version`, `exported_at`, `filters`, `routes_tracked`, `gaps`, `arrivals`, `samples`, `complete`; `complete` is `true`.
- Tick compressed: a `.json.gz` downloads and gunzips to the same document shape.
- Pick one route: the filename ends `-<route>.json`, `filters.route` is set, and every arrival and sample belongs to that route.
- While one export is running (use a large data set), start a second from another tab: it gets HTTP 429 with `Retry-After`; after the first finishes a new export works.

### 5. Remove a day of data (manual)

- Take a backup, then `curl "http://127.0.0.1:8081/api/data/day?date=<today>"`: counts are non-zero for a day with data, `window` spans local midnight to next midnight, `months` lists the month files. With `&end=<next day>` the response has a per-day `days` list (2 entries) and totals over both days.
- Same call with `route=<id>`: counts are no larger than the all-routes counts.
- `POST /api/data/day/remove?date=<day>&confirm=<other day>` gives 422 and deletes nothing. For a range, `date=<d1>&end=<d2>&confirm=<d1>` (without `..<d2>`) also gives 422.
- With the correct `confirm` the POST returns `202 {"job_id": ...}` immediately. Poll `GET /api/data/day/remove/<job_id>`: `state` is `running` with `stage`, `day_index`/`day_count` and growing `done` counts, then `done` with `done` equal to the preview; the preview afterwards is all zero and other days are unchanged in the UI. An unknown job id gives 404.
- Range step: with a 2-day range (`date=<d1>&end=<d2>&confirm=<d1>..<d2>`), `day_count` is 2, `current_day` moves from the first to the second day, and `done` equals the range preview totals when finished. A range over 62 days gives 422.
- Start a second removal while one is running: `409` with the running `job_id`. During a backup the `409` has `job_id: null`.
- On a day overlapping a gzipped month, `skipped_months` names the `.db.gz` file and its checksum is unchanged.
- While the tracker is running, the collector keeps recording during a large removal (no `database is locked` in its log).
- Web UI, Routes page > Backup card > "Remove a day of data": From defaults to today (Dublin); To is optional. Press Preview on a test day (or a 2-day range): the counts match the curl preview and a "Remove these rows" button appears. Change a date or the route: the button and text disappear. Preview again, remove: the Remove button shows a spinner, a progress bar and running counts update, the final "Removed ..." counts match the preview, and a second Preview says "Nothing recorded".
- Removing a day that is still being recorded (today): preview afterwards may be non-zero while the collector runs (in-flight buses finalise with incomplete forecasts); this is expected, rerun once the day is complete.
- A `409`, `503` or job `error` names no raw error text; after an `error` with `done` counts, rerunning the same removal completes it. After restarting the app, polling an old job id gives 404.
- Cross-origin check: `curl -X POST -H "Origin: http://evil.example" ".../api/data/day/remove?date=<day>&confirm=<day>"` returns 403 (non-browser clients can still omit Origin; no authentication exists).
- Preview today's date in the web UI: the counts are followed by the "This day is still being recorded" warning (also after removal). A past date shows no such warning.
