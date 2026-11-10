# Tooltips and Settings Implementation Plan

> **For agentic workers:** use executing-plans inline (RAM tight). Sonnet only, one review at the end, commit per task.

**Goal:** Stat-card tooltips, larger hit targets, and a localStorage settings page.
**Architecture:** `settings.js` (pure load/validate/save + subscribe), `tipdata.js` (pure payload builders), `tooltip.js` renders payloads honouring settings, `pages/settings.js` form. Charts emit `data-tip-json` and transparent hit elements.
**Tech Stack:** Vanilla ES modules, node:test + fakedom.
**Spec:** docs/specs/2026-11-10-tooltips-settings-design.md
**Assumptions:** localStorage may be blocked; assumes desktop browser.

## Global Constraints
- `cd web && (ulimit -v 2500000; node --test test/)`; baseline 110 pass.
- No innerHTML; contrast test must pass; DOM text contracts in existing tests stay.

## Review Focus
- Corrupt JSON / wrong types in `dba-settings` fall back per field (Task 1 test).
- Tooltip payload missing stats or non-finite numbers shows no "NaN"/"undefined" (Task 3/4 tests).
- localStorage throwing must not break the app (Task 1 test).

### Task 1: settings.js
Files: Create `web/js/settings.js`, `web/test/settings.test.mjs`.
Produces: `DEFAULTS = {theme:'auto', density:'comfortable', tip:{on:true, units:'min', stats:{mean,median,min,max,band,ci,std,ontime,n:true}}, charts:{heatCap:0, pageSize:40, tableToggle:true}}`; `validate(raw)->settings` (per-field fallback, heatCap 0 or 1..60, pageSize in 20|40|80); `getSettings()`, `setSettings(patch)` (deep merge, validate, persist, notify), `resetSettings()`, `onSettings(fn)->unsubscribe`, `storageWorks()`.
Tests: defaults on empty; garbage JSON -> defaults; wrong types per field fall back individually; unknown keys dropped; storage that throws -> works in memory and `storageWorks()` false; subscribe fires on set/reset.
Commit.

### Task 2: settings page, nav, apply
Files: Create `web/js/pages/settings.js`; modify `app.js` (PAGES), `lib.js` ROUTES (`settings: []`), `index.html` (nav link), `css/style.css` (density, form), `theme.js`.
Behaviour: `applyAppearance(s)` sets `data-density`; theme changes write `dba-theme` as theme.js does and set `data-theme`; header toggle label updates. Page renders groups with labels, applies on change, Reset button, storage-blocked notice.
Tests: parseHash('#/settings') -> name settings; page render has h1, controls for each setting, changing a control calls setSettings; reset restores.
Commit.

### Task 3: tipdata.js + hit targets
Files: Create `web/js/tipdata.js`, `web/test/tipdata.test.mjs`; modify `charts.js`, pages (route/analytics/stop) to pass extras.
Produces: `rangeTipData(item, extra)`, `accTipData(stop, label, pos)`, `heatTipData(cell)`, `forecastTipData(h)`, `histTipData(bin, total)`; each returns `{title, head:{sec,cls}|null, sub, rows:[{k,l,fmt,v}], range?:{min,max,lo,hi,mean}, foot}`; non-finite values omitted. Charts: `data-tip-json` on marks plus transparent `.hit` shapes (dots r=9, acc bars +4px, row-height rects, full-height histogram bars).
Tests: builders omit null stats, no NaN in JSON; render output has `.hit` elements and parseable JSON.
Commit.

### Task 4: tooltip card rendering
Files: modify `tooltip.js`, `css/style.css`, add `web/js/tipcard.js` (pure `renderCard(doc, payload, settings)`), tests.
Behaviour: card DOM (title, headline, grid, mini range bar, foot) via createElement/textContent; honours `tip.on`, per-stat toggles, units (`fmtTipDelay(sec, units)` exported from lib); falls back to `<title>` text.
Tests: renderCard row filtering, units sec vs min, no "NaN", tip.on=false returns null.
Commit.

### Task 5: chart options
Heatmap cap (`heatmapGrid` option `capAbs` seconds when heatCap>0), `PAGE` from settings in route.js, `figure()` omits the toggle button when `tableToggle` false (table stays reachable via keyboard? when off the table is hidden but the figure keeps an aria-label summary).
Tests: heatmapGrid with cap saturates at cap; figure with toggle off has no button.
Commit.

### Task 6: verify and review
Full tests; single inline review of the diff; fix findings; update state.md.
