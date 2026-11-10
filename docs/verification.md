# Verification status

## Verified (Task 11)

- `docker build` of the analytics image succeeds (504 MB on disk); it serves the UI (`/`, `/js/app.js`, `/css/style.css` with CSP headers), 404s `/package.json`, lists seeded routes at `/api/routes/tracked`, runs as uid 10001, and the HEALTHCHECK turns `healthy`.
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
- An 80-stop route chart: stop label collisions.
- 360 px wide viewport: no horizontal scroll, controls usable.
- Theme toggle: persists across reload, both themes readable.
- Deep link `#/route/15%2FA` loads the route.
- DevTools console: no CSP violations or errors.
