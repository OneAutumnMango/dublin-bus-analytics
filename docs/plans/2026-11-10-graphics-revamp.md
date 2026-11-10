# Graphics Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-optimized:executing-plans (inline; RAM is tight) to implement this plan task-by-task.

**Goal:** Restyle the web UI as a dense dark terminal-style dashboard with richer charts and a floating hover tooltip.
**Architecture:** Mostly CSS (tokens, type, layout) plus small additive JS: an area-fill path in the accumulation chart and a delegated tooltip module that reads SVG `<title>` text. The DOM text contract used by tests is unchanged.
**Tech Stack:** Vanilla ES modules, CSS custom properties, node:test with `web/test/fakedom.mjs`.
**Spec:** docs/specs/2026-11-10-graphics-revamp-design.md
**Assumptions:** Desktop browsers only; assumes Google Fonts reachable but works without (system fallback). Will NOT cover mobile.

## Global Constraints
- Run tests as `cd web && (ulimit -v 2500000; node --test test/)`; baseline 102 pass, 0 fail.
- Sonnet only, no parallel agents, one review at the end, commit after each task.
- `web/test/contrast.test.mjs` must pass: hex tokens only in the three theme blocks; manual dark block == media dark block; token names `bg surface surface-2 text muted border control-border accent accent-contrast focus early late ok err-bg err warn-bg warn grid band band-line hm-*` kept.
- Theme follows system (existing `theme.js` toggle kept).
- Keep `<title>` elements in SVG; never put dynamic text in innerHTML.
- Spec palette: dark bg `#0b0f14`; blue = early, orange = late.

## Review Focus
- Tooltip near right/bottom viewport edge: must stay fully on screen (Task 4 test).
- Chart with a single stop or all-null delays: area path must be empty, not `NaN` (Task 3 test).
- Element with no `<title>` child under the pointer: tooltip hides (Task 4 test).

---

### Task 1: Tokens and typography
**Files:** Modify `web/css/style.css`, `web/index.html`; Test `web/test/contrast.test.mjs` (existing).
**Interfaces:** Consumes nothing. Produces CSS tokens `--font`, `--mono`, `--fs-xs: 0.6875rem`, `--fs-sm: 0.75rem`, `--fs: 0.8125rem`, `--fs-md: 0.9375rem`, `--fs-xl: 1.375rem`, `--radius: 8px`, `--radius-sm: 6px`.
**Security flag:** none. **Does NOT cover:** mobile sizing.
- [ ] **Step 1:** In `index.html` add Google Fonts `<link rel="preconnect">` and stylesheet for `Inter:wght@400;500;600;700` and `JetBrains+Mono:wght@400;500;600` (display=swap) before the style link.
- [ ] **Step 2:** In `style.css` set dark tokens to the spec palette (`--bg:#0b0f14; --surface:#121820; --surface-2:#19212b; --border:#222c38; --grid:#1c2531; --text:#e6ebf2; --muted:#9aa7b8`), keep accents/semantic/heatmap values that already pass; apply identical values in both dark blocks; light theme gets a cool-grey counterpart (`--bg:#f1f3f7`). Set `--font:"Inter",system-ui,...`, `--mono:"JetBrains Mono",ui-monospace,Consolas,monospace`, and the scale above.
- [ ] **Step 3:** Use `--mono` for `.tile .value`, `.chart text`, `td.num`, `th.num`, inputs of type number; uppercase letter-spaced `--fs-xs` for `h2` section labels, `legend`, `.tile .label`; `h1` at `--fs-xl`, weight 650, tight tracking.
- [ ] **Step 4:** Run the test command; expected 102 pass, 0 fail.
- [ ] **Step 5:** `git add web && git commit -m "Revamp tokens and typography"`

### Task 2: Layout and structure
**Files:** Modify `web/css/style.css`, `web/index.html`, `web/js/pages/route.js`, `web/js/pages/analytics.js` (class names only).
**Interfaces:** Consumes Task 1 tokens. Produces CSS classes `.panel` (surface box for charts), `.stat-strip` (replaces `.tiles` styling; `.tiles` markup is kept and restyled).
**Security flag:** none. **Does NOT cover:** reflow below 1100px.
- [ ] **Step 1:** Header: 48px high, brand with a small gradient dot, nav links and theme button right-aligned, 1px bottom rule, no blur.
- [ ] **Step 2:** `.layout` grid `260px 1fr` max-width 1800px; sidebar sticky under the 48px bar, full height, hairline right border, compact rows (28px), active route with accent left bar. `main` max-width 1600px, padding 24px 32px.
- [ ] **Step 3:** Remove card chrome: `.card` becomes borderless, no shadow, no padding; `figure.card` and `.controls.card` keep a `--surface` background with 1px border and `--radius`. Sections separated by a 1px top rule and 24px spacing.
- [ ] **Step 4:** `.tiles` renders as one horizontal strip: single bordered row, tiles separated by vertical hairlines, mono 22px values, no individual shadows.
- [ ] **Step 5:** Filter form: fieldsets in a 4-column grid on wide screens, compact 30px inputs; table rows 28px with hover.
- [ ] **Step 6:** Run tests (expect 102 pass), then commit `Revamp layout and structure`.

### Task 3: Chart styling and area fill
**Files:** Modify `web/js/charts.js`, `web/css/style.css`; Test `web/test/charts.test.mjs`.
**Interfaces:** Consumes `linePath(points)`, `accumulationGeometry(stops)` (existing). Produces `export function areaPath(points, baseY)` returning `''` when fewer than 2 finite points, otherwise one closed `M…L…V{baseY}H…Z` sub-path per contiguous run of points with finite `y`.
**Security flag:** none. **Does NOT cover:** area fill for the range chart or step chart.
- [ ] **Step 1:** Test (charts.test.mjs): `areaPath([{x:0,y:10},{x:10,y:20}], 100)` equals `'M0 10L10 20V100H0Z'`; `areaPath([{x:0,y:null},{x:5,y:3}], 100)` equals `''`; `areaPath([{x:0,y:1},{x:1,y:null},{x:2,y:2},{x:3,y:3}], 9)` has exactly one `M` (the 1-point run is dropped) ; `renderAccumulation` output contains a `path.acc-area` and no attribute value contains `NaN`.
- [ ] **Step 2:** Run charts tests; expect FAIL (areaPath not exported).
- [ ] **Step 3:** Implement `areaPath` in charts.js; in `renderAccumulation` add a `<defs><linearGradient id>` (unique per chart, two `<stop>` elements with classes `acc-stop-a`/`acc-stop-b`) and a `path.acc-area` with `fill:url(#id)` drawn before the line, base y = `geo.panelA.top + geo.panelA.h`.
- [ ] **Step 3b:** CSS: `.acc-stop-a { stop-color: var(--accent); stop-opacity: .35 } .acc-stop-b { stop-color: var(--accent); stop-opacity: 0 }`; mean diamonds get `filter: drop-shadow(0 0 4px currentColor)` via early/late fill; bars `rx:2`, 70% opacity, full on hover; heatmap cells `rx:2` with the 1px gap kept; gridlines `#1c2531`-level via `--grid`; zero line accent-tinted; row hover highlight rect via `.row:hover { opacity:1 }` and siblings dimmed.
- [ ] **Step 4:** Run full tests; expect 103+ pass, 0 fail. Commit `Charts: area fill and refined marks`.

### Task 4: Floating tooltip
**Files:** Create `web/js/tooltip.js`; Modify `web/js/app.js`, `web/css/style.css`; Test `web/test/tooltip.test.mjs`.
**Interfaces:** Consumes the SVG `<title>` convention in `charts.js`. Produces:
- `export function tipText(el)` -> string|null: text of the first direct child `<title>` of `el` or its nearest ancestor `g`/`rect`/`circle`/`path` carrying one (walk at most 3 levels, stop at `svg`).
- `export function placeTip(x, y, w, h, vw, vh)` -> `{left, top}`: places the tip 14px right and below the pointer, flipping left/up when it would pass `vw - 8` / `vh - 8`, never below 8.
- `export function installTooltip(doc = document)`: adds one `pointermove`/`pointerleave`/`focusin`/`focusout` delegated listener set that shows a `div#chart-tip[role=presentation]` (position fixed) for elements inside `svg.chart`, and temporarily removes the native `<title>` hover by setting `pointer-events` untouched (screen readers keep the titles).
**Security flag:** none (text via `textContent`). **Does NOT cover:** touch devices.
- [ ] **Step 1:** Tests: `placeTip(100,100,120,40,1000,800)` -> `{left:114,top:114}`; `placeTip(950,780,120,40,1000,800)` -> left = 950-14-120 = 816, top = 780-14-40 = 726; `placeTip(2,2,120,40,100,30)` -> left and top both >= 8; `tipText` on a fakedom `g` with a `title` child returns its text, on an element with none returns `null`.
- [ ] **Step 2:** Run `node --test test/tooltip.test.mjs`; expect FAIL (module missing).
- [ ] **Step 3:** Implement as specified; call `installTooltip()` once from `app.js` after boot. Tip styled: `--surface-2` bg, 1px `--border`, `--radius-sm`, mono `--fs-sm`, soft shadow, max-width 320px, hidden by default, `pointer-events:none`, z-index 1000.
- [ ] **Step 4:** Run full tests; expect all pass. Commit `Add floating chart tooltip`.

### Task 5: Verify and review
- [ ] **Step 1:** Full test run (command in Global Constraints); expect 0 fail.
- [ ] **Step 2:** Serve with `docker compose --profile analytics up` or `python -m app.main` per README only if RAM allows (check `free -m` first, need > 1.5 GB free); otherwise ask the user to look in their browser.
- [ ] **Step 3:** One inline review of `git diff` for the revamp commits via `requesting-code-review` (Sonnet), apply findings, commit once.

## Self-review
Spec coverage: layout, typography, colour, chart restyle, area fill, tooltip, directions kept. Types: `areaPath`, `tipText`, `placeTip`, `installTooltip` named identically in Produces and steps. No scope reductions.
