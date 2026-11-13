# Stats lab (sub-project 1 of 5)

## Roadmap

Five sub-projects, each with its own spec and plan, in this order. Only #1 is specified here.

1. **Stats lab shell + distribution/buffer + persistence/variance** (this spec)
2. Forecast skill vs timetable: `1 - MAE(error_s) / MAE(delay)` per horizon on the same arrivals, plus interval coverage and bias. Uses the existing `forecast` table.
3. Headway stats (excess wait time, CV, bunching/gapping): from `actual` gaps of consecutive observed trips at a stop. A pair counts only when both trips were observed; each stop shows coverage.
4. Trend and control chart: rolling median with day-block bootstrap CI; EWMA/CUSUM.
5. Rain vs delay: OpenWeatherMap One Call 3.0 (key `OPENWEATHER_API_KEY` in git-ignored `analytics.env`; daily limit set to 1000 in the OpenWeather dashboard; verified working 2026-11-13). New table, throttled backfill, new outbound call. Compare within the same weekday-hour; report an association, not a cause.

No sub-project needs a server change. Counting missed/cancelled trips would, and is out of scope.

## Goal

A per-route "Stats lab" page showing distribution shape, delay persistence along the trip, and variance decomposition, each with plain-English explanation and honest uncertainty.

## Scope

In: new pure module, three endpoints, one page, tests, README section.
Out: per-stop picker, forecast skill, headways, trend charts, weather, any server change.

## Design

### `app/analysis/lab.py` (pure; no I/O, no db imports; style of `stats.py`)

- **Distribution**: ECDF points (downsampled to about 200), normal QQ points (about 100 quantiles vs theoretical normal), skew, excess kurtosis, buffer time = p95 - median.
- **Persistence**: lag-1..5 autocorrelation of delay along the trip in stop order (trip rows built as in `stop_changes`; a stop seen twice on one trip is ambiguous and skipped); recovery slope = regression of downstream delay on first-stop delay.
- **Variance**: adjusted eta-squared (omega-squared) of delay by hour, weekday, stop and date. Values overlap and do not sum to 100%.
- **Uncertainty**: day-block bootstrap (resample whole Dublin service days), 200 resamples, fixed seed so results are stable between reloads. Resample per-day sorted arrays. Every figure carries `n` and `days`. `low_n` when n < 10 (existing rule). When days < 5 the CI is null and the UI says "not enough days yet".
- Degenerate data returns null fields, never NaN/inf (as `stats.py`). Invalid parameters raise ValueError.

### API

`GET /api/routes/{r}/lab/distribution`, `/lab/persistence`, `/lab/variance`. Same filter parameters as the other route endpoints (`_cond`). Run under `_heavy` / `_scan` with compact arrays so the existing heavy-request limit and low-RAM host are respected. Nothing is stored; results are computed per request.

### UI

New hash route `#/lab/{route}` using the existing filter bar, linked from the Route and Analytics pages. Plain ES modules, no build step. Three sections, one per endpoint:

- Distribution: ECDF (on-time window shaded, p50/p95 marked), QQ plot, tiles for skew, kurtosis, buffer time.
- Persistence: autocorrelation by lag, recovery slope.
- Variance: adjusted eta-squared bars for hour, weekday, stop, date.

Each section has a "What am I looking at?" box, open by default, in plain English, plus a one-line reading of the current result (for example: "Half of buses are within X min of schedule; 1 in 20 are more than Y min late."). Every chart has a data table, as on the Analytics page.

### Errors

Empty selection shows the existing empty state. Endpoint errors use the existing region/notice handling.

## Failure modes

- Bootstrap cost on a large route: resample per-day arrays, cap resamples at 200, use the heavy-request limit; measure on the largest route before finishing.
- Few days of data: CI suppressed below 5 days.
- Non-goal: eta-squared overlap is not modelled jointly; the explanation says it is not additive.
- Non-goal: a normal QQ plot on heavy-tailed delays will curve; that is the information shown.

## Testing

- Unit tests for each pure function against known distributions, including degenerate input and bootstrap determinism.
- API test for the three endpoints (including filters and an empty selection).
- JS tests for chart geometry; `tests/test_docs.py` for the README section.
- Run heavy test files one at a time with `ulimit -v 2500000`.
