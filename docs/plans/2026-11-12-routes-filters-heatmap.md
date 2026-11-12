# Analytics filters and heatmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:subagent-driven-development (recommended) or superpowers-optimized:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hour-of-day slider, day toggle buttons, clickable filter-independent heatmap, and a fix for the full-height accumulation bars on the analytics page.
**Architecture:** Pure helpers in `lib.js` map slider minutes to the existing `hFrom`/`hTo` controls; `controls.js` swaps its inputs for the slider and toggle buttons (URL keys unchanged); `analytics.js` fetches the heatmap without weekday/hours and makes cells navigate to a single day+hour filter.
**Tech Stack:** Vanilla ES modules, node:test (`cd web && node --test test/`), custom fakedom in `web/test/fakedom.mjs`.
**Spec:** docs/specs/2026-11-12-routes-filters-heatmap-design.md
**Assumptions:** Assumes the API `hours` filter is a list of whole Dublin hours and `weekdays` a list 0..6 — will NOT work if the backend changes these. Assumes `hTo` is inclusive (as `hoursList` does).

## Global Constraints
- URL keys `wd`, `hf`, `ht` and `hoursList`/`toApiQuery` semantics unchanged; old links keep working.
- Hour windows use whole hours: slider minutes 0..1439 at step 60; end 1439 means hour 23 inclusive.
- Accessibility: toggles are `<button aria-pressed>`; heatmap cells keyboard operable.

## Review Focus
- Link with only `hf=7` (no `ht`): slider shows single hour 07:00; applying unchanged must keep hf=ht=7 semantics. Test in Task 2.
- Wrapping window 22 to 05 round-trips. Test in Task 2.
- Clicking the already-selected heatmap cell clears wd/hf/ht but preserves other filters (since/until/conf). Test in Task 4.
- Heatmap request carries no weekdays/hours even when the page filters have them. Test in Task 4.

## File Structure
- `web/js/lib.js`: add hour/slider mapping helpers.
- `web/js/slider.js`: accept `presets` option.
- `web/js/controls.js`: slider + day buttons.
- `web/js/pages/analytics.js`, `web/js/charts.js`: heatmap query, click, selection.
- `web/css/style.css`: day buttons, selected cell, `acc-bar`.

---
### Task 1: Fix full-height accumulation bars
**Files:** Modify `web/js/charts.js` (renderAccumulation, line ~298), `web/css/style.css` (line ~164-165); Test `web/test/charts.test.mjs`
**Interfaces:** Consumes: nothing. Produces: nothing.
**Security flag:** `none`
**Cause:** header rule `.bar { height:100%; display:flex; ... }` (style.css:59, also :325) applies to the chart rects.

- [x] **Step 1: Failing test** in charts.test.mjs: render `renderAccumulation` with 3 stops having `segment_delta` values; assert every non-hit rect with class containing `acc-bar` exists, no rect has class token `bar`, and `early`/`late` classes are still set (`acc-bar late` for delta>0, `acc-bar early` otherwise). Also read `css/style.css` text and assert it contains no `.chart .bar` selector.
- [x] **Step 2:** Run `cd web && node --test test/charts.test.mjs`; Expected FAIL.
- [x] **Step 3:** In charts.js change rect class `bar ${late|early}` to `acc-bar ${late|early}`. In style.css rename `.chart .bar` rules (fill, `.early`, `.late`, `:hover`) to `.chart .acc-bar`.
- [x] **Step 4:** Run `node --test test/` ; Expected all PASS.
- [x] **Step 5:** Commit `Fix accumulation bars inheriting header .bar layout`.

### Task 2: Hour-window mapping and slider presets
**Files:** Modify `web/js/lib.js`, `web/js/slider.js`; Test `web/test/lib.test.mjs`, `web/test/slider.test.mjs`
**Interfaces:** Consumes: `timeRangeSlider({from,to,step,onChange})` from slider.js. Produces:
- lib.js `hoursToWindow(hFrom, hTo) -> [a, b]` (minutes) and `windowToHours(a, b) -> { hFrom, hTo }` (`''` when blank).
- slider.js `timeRangeSlider({ ..., presets })` where `presets` defaults to `PRESETS`; exported `HOUR_PRESETS`.
**Security flag:** `none`

Rules: blank,blank -> [0,1439]. hf only -> single hour [h*60, h*60+60]; ht only -> same with ht. Both set, non-wrapping h1<=h2 -> [h1*60, (h2+1)*60] with end 1440 clamped to 1439; wrapping h1>h2 -> a=h1*60, b=(h2+1)*60. Inverse: a=0&&b=1439 -> blanks; hFrom=floor(a/60); hTo = b===1439 ? 23 : ((b/60 - 1) + 24) % 24; a===b (not full) -> single hour hFrom with hTo=hFrom.
`HOUR_PRESETS` = All day [0,1439], Morning peak [420,600], Daytime [600,960], Evening peak [960,1140], Night [1320,360].

- [x] **Step 1: Failing tests** in lib.test.mjs: for every hFrom 0..23 and hTo 0..23, `windowToHours(...hoursToWindow(f,t))` returns `{hFrom:f,hTo:t}` except f==0&&t==23 which returns blanks; `hoursToWindow('', '')` is [0,1439]; `hoursToWindow(7,'')` is [420,480] and `windowToHours(420,480)` is `{hFrom:7,hTo:7}`; `hoursToWindow('',9)` is [540,600]; (22,5) -> [1320,360] and back. slider.test.mjs: with `presets: [['X',60,120]]` only one preset button exists and clicking it calls onChange(60,120).
- [x] **Step 2:** Run `node --test test/lib.test.mjs test/slider.test.mjs`; Expected FAIL.
- [x] **Step 3:** Implement helpers in lib.js and the `presets` option (default PRESETS) plus `HOUR_PRESETS` export in slider.js.
- [x] **Step 4:** Run same command; Expected PASS.
- [x] **Step 5:** Commit `Add hour-window mapping and slider presets option`.

### Task 3: Slider and day buttons in the filter form
**Files:** Modify `web/js/controls.js`, `web/css/style.css`; Test `web/test/pages.test.mjs` (or a new `web/test/controls.test.mjs` using fakedom)
**Interfaces:** Consumes: `hoursToWindow`, `windowToHours` (Task 2), `timeRangeSlider`, `HOUR_PRESETS`. Produces: `controlsForm(initial, onApply)` unchanged signature; applied controls still `{hFrom, hTo, weekdays, ...}` with `hFrom` number or `''`, `hTo` number or `''`.
**Security flag:** `none`

- [x] **Step 1: Failing tests:** build the form with `initial.weekdays=[0,2]`, `hFrom=7`, `hTo=9`; assert 7 day buttons with `aria-pressed` "true" exactly for Mon and Wed; clicking Tue sets it "true"; submitting calls onApply with `weekdays:[0,1,2]`, `hFrom:7,hTo:9`; with default controls submit gives `hFrom:'',hTo:''`; no `<select>` remains in the form; Reset clears pressed states and applies blanks.
- [x] **Step 2:** Run `node --test test/`; Expected FAIL.
- [x] **Step 3:** In controls.js replace the hour selects with `timeRangeSlider({ step: 60, presets: HOUR_PRESETS, from/to: hoursToWindow(initial.hFrom, initial.hTo) })`; `read()` uses `windowToHours(...slider.get())`. Replace checkboxes with `button[type=button].day-btn` toggling `aria-pressed`, in a `.day-btns` row; fieldset legend "Days of week", hint "None selected = every day". Reset must call `slider.set(0,1439)` and unpress days. Collapse rule: `windowToHours` maps an on-hour start==end window to all 24 hours, so in the slider's onChange (and before read) if `a === b` and the window is not full, call `slider.set(a, Math.min(a + 60, 1439))` so dragging both handles together selects that single hour, not the whole day. Test: onChange(120,120) leaves slider.get() at [120,180] and applying gives `hFrom:2,hTo:2`. CSS: pill buttons with distinct pressed state (filled `--early`-style accent, not colour-only: also bold text).
- [x] **Step 4:** Run `node --test test/`; Expected PASS.
- [x] **Step 5:** Commit `Use slider and toggle buttons for hour and day filters`.

### Task 4: Clickable heatmap independent of day/hour filters
**Files:** Modify `web/js/pages/analytics.js`, `web/js/charts.js` (renderHeatmap), `web/css/style.css`; Test `web/test/pages.test.mjs`, `web/test/charts.test.mjs`
**Interfaces:** Consumes: `toApiQuery` query object, `controlsToQuery`, `buildHash`, `ctx.go`. Produces: exported `heatQuery({ weekdays, hours, ...rest }) -> rest` in analytics.js; `renderHeatmap(grid, { title, desc, selected, onSelect, doc })` where `selected` is `{wd, hour}|null` and `onSelect(wd, hour)` is called on click/Enter/Space.
**Security flag:** `none`

- [ ] **Step 1: Failing tests:** charts.test: cells render `role=button`, `tabindex=0`, `aria-label` like "Mon 08:00, mean +5.0 min", `aria-pressed` true only for `selected`, click and Enter/Space call `onSelect(wd,hour)`. pages.test: `heatQuery({weekdays:[1],hours:[8],ci:'0.95'})` equals `{ci:'0.95'}`; with route page filters wd=1,hf=ht=8 the heatmap API call has no weekdays/hours while accumulation/forecast calls keep them; selection derived only when `weekdays.length===1` and `hoursList` has one hour; clicking an unselected cell calls `ctx.go` with a hash whose query has `wd=<d>&hf=<h>&ht=<h>` and preserved since/until/conf; clicking the selected cell yields a hash with no wd/hf/ht but the other keys preserved.
- [ ] **Step 2:** Run `node --test test/`; Expected FAIL.
- [ ] **Step 3:** analytics.js: call `api.heatmap(route, heatQuery(q), ...)`; compute `selected` from `s.controls`; `onSelect` builds `controlsToQuery({ ...s.controls, weekdays:[wd], hFrom:hour, hTo:hour })` (or with weekdays `[]`, blanks when clearing) and calls `ctx.go(buildHash('analytics', {route}, query))`. charts.js: add the button attributes and handlers on each cell `g`, add class `cell-selected` on selected. CSS: `.cell-selected rect` visible outline (2px, `--text`), `.cell:focus-visible` outline, `cursor:pointer`. Update the heatmap hint/legend text to say "click a square to filter the charts below to that day and hour; this heatmap ignores the day and hour filters."
- [ ] **Step 4:** Run `node --test test/`; Expected PASS.
- [ ] **Step 5:** Commit `Make heatmap clickable and independent of day/hour filters`.

### Task 5: Visual check
- [ ] Start the app as in README (non-container dev setup), open an analytics page in the built-in browser: bars now small and coloured; slider and day buttons render in light and dark themes; clicking a square updates the URL, buttons, slider and lower charts; clicking it again clears. Report anything off.

## Self-review
Spec coverage: slider (T2,T3), day buttons (T3), heatmap ignore + click + clear (T4), bar fix (T1), non-goals unchanged. Slider label shows the slider's exclusive end (e.g. 07:00–10:00 for hours 7–9), a small deviation from the spec's "09:59" wording; accepted.
