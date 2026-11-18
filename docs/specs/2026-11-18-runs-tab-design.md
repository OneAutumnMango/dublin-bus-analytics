# Runs tab: investigate past bus runs (design)

Size: small-to-medium (one endpoint, one page, nav). Pre-approved by the user (AFK); decisions below are mine, for review afterwards.

## Goal
A top-bar tab "Runs" (next to Map) to look at one service day of one or more routes (e.g. 12 Nov, route 4): every run (trip) with scheduled vs actual times.

## UI
`#/runs?routes=4,15&date=2026-11-12` (hash holds state, linkable). Form: routes (comma list, max 5), date. Per route a section with:
- Tiles: runs, arrivals recorded, mean delay, worst delay.
- Time-space diagram ("stringline"): x = Dublin time of day, y = stop position along a direction (one diagram per direction). Per run a dashed grey line through scheduled times and a solid line through actual times, coloured by the run's mean delay (early/on time/late classes already in the CSS). Hover/focus gives the run tip.
- Runs table (also the accessible alternative): trip, direction, first scheduled, last scheduled, stops recorded, missing, mean/max delay. Trip links to the existing trip page for the first stop. Clicking a row (link "Details") expands a per-stop table: stop, scheduled, actual, delay.
Per-stop table shows `no arrival recorded` when actual is null.

## API
`GET /api/routes/{r}/runs?date=YYYY-MM-DD` -> `{route, date, order_source, directions:{dir:[stop ids]}, n, truncated, runs:[{trip_id, direction, visits:[{stop_id,pos,scheduled,actual,delay,confidence,realtime}]}]}`.
- Rows: `arrivals where service_date=? and route=?` (index ix_arr_date), ordered by trip, scheduled. All rows are returned, including unconfirmed and scheduled-only ones, each with its flags (the page shows them differently, not hidden).
- Date validated with `maintenance.parse_day` (422 on bad). Cap 20,000 rows (`truncated`).
- Direction of a run: the server direction listing containing most of its stops; `pos` = index in it. Order unavailable -> `order_source:"none"`, direction/pos null, page shows table only.
- Not result-cached (cheap indexed read, past days still change while the day's data arrives).

## Failure-mode check
1. Many runs make the diagram unreadable (route 4: ~100 runs/day). Minor: thin lines, per-direction diagrams, table is the precise view. Non-goal: zoom/brush.
2. Late-night trips span midnight / service_date is by first sight in Dublin time. Minor: x axis is the day plus overflow, scaled to the data extent, not 0-24 h.
3. Stop order missing -> no diagram. Minor: table still works.
4. Runs touching a stop in both directions (loops): direction by majority; stops in the minority direction get pos null and are dropped from the diagram only.

## Tests
pytest for the endpoint (grouping, direction/pos, validation, empty, order none); node tests for geometry in charts.js, hash round trip, page render; e2e param row for `#/runs`.
