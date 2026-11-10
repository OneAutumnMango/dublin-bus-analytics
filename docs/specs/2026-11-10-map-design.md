# Map page: tracked routes, delay colouring, regions, time window

Status: requirements given and basemap/slider choices confirmed by user 2026-11-10. Desktop only. Okabe-Ito palette.

## Page (`#/map`, nav link "Map")
MapLibre GL JS (self-hosted in `web/vendor/maplibre`, CSP build) on OpenFreeMap "positron" tiles, like the Android app (`MapLibreSupport.kt`). Dark theme: positron restyled in code (background/fill/line/text paints).
Layers (bottom to top): regions (hex cells) -> route lines -> stops.
- **Stops**: circle per stop coloured by mean delay with one diverging scale: Okabe-Ito blue `#0072B2` (early) -> neutral -> orange `#E69F00` (late); saturates at +/- cap minutes (default 5; settings heatCap if set). Stops without data: small grey hollow circle. Radius grows with sample count.
- **Legend**: horizontal gradient bar early..on time..late with minute labels at -cap, 0, +cap and a "no data" swatch.
- **Tooltips**: same stat-card as charts (shared `#chart-tip`): stop name (code), headline mean delay, mini bar, n, and a row per route (route: mean delay, n).
- **Regions toggle**: hex grid (~400 m) aggregating all visible routes' stops weighted by n; cell fill uses the same scale at ~35% opacity; only cells with >= 2 stops with data and n >= 20. Hover tooltip for a cell (stops, n, mean).
- **Route toggles**: a chip per tracked route (show/hide, all/none); lines are polylines through the ordered stops of each direction, drawn in muted Okabe-Ito categorical colours (`#009E73 #CC79A7 #F0E442 #999999` cycling), stops of hidden routes disappear, stops shared by visible routes combine.
- **Time window slider**: dual-handle 00:00-23:59, 5 min steps, two native range inputs layered (keyboard accessible, `aria-valuetext` HH:MM), label "06:00 - 10:30"; if start > end the window wraps midnight (label notes "overnight"). Applies Dublin-local scheduled time-of-day. Refetch debounced 300 ms; stale responses ignored.

## Backend
`GET /api/map` params: standard filters (`since`, `until`, `weekdays`, `min_confidence`, `window_*`, `ci`, `pct_*` ignored for map) plus `minutes_from`, `minutes_to` (0..1439, both or neither; from > to wraps).
Response: `{filters, minutes:{from,to}|null, routes:[{route, directions:{name:[stop_id...]}}], stops:[{stop_id, code, name, lat, lon, n, mean_delay, routes:[{route, n, mean_delay}]}], n}` for every tracked route; only stops with a position from the server's stop lists; unknown server -> `order_source:"none"` and empty routes list with a clear message. Uses `_heavy`/`_scan_arrivals` (memory-bounded, serialised reads). Minute-of-day filter implemented once in `_tz`/`stats.filter_rows` (`minutes=(lo,hi)`).

## CSP / privacy
CSP gains: `connect-src 'self' https://tiles.openfreemap.org; img-src 'self' data: blob: https://tiles.openfreemap.org; worker-src 'self' blob:`. Page tells the user that the basemap loads from tiles.openfreemap.org. No credentials or analytics data are sent.

## Non-goals
Live buses, offline tiles, per-direction toggles, date-range UI on the map (uses the default window, last N days), mobile layout.

## Failure modes
No tracked routes -> empty state with link. MapLibre/tiles blocked -> map area shows an error notice, controls disabled, data table of stops still shown. No data in window -> grey stops and a notice. Hex binning with 0/1 stops -> no cells. Slider thumbs crossing -> wrap, never invalid. Huge stop counts (all routes) -> circle layer only, no DOM per stop.
