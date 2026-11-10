# Graphics revamp: data-dense terminal dashboard

Status: approved by user 2026-11-10. Desktop only. Replaces the soft-card pass (commit "Graphics overhaul").

## Scope
Visual and structural revamp of `web/`: layout, typography, colour, chart styling, hover tooltips.
Non-goals: mobile layout, charting library, API or data changes, changes to DOM text contracts used by tests.

## Direction
Dark, data-dense monitoring look. Theme still follows the system (existing toggle kept); light variant stays in step.

## Layout
- 48px top bar; fixed compact 260px sidebar rail; content up to 1600px.
- No card around every block: sections split by hairline rules; only charts sit on a surface panel.
- Route/analytics header: title plus key figures in one dense strip.
- Analytics: heatmap full width, then accumulation per direction, then forecast.

## Typography
Inter (UI) + JetBrains Mono (numbers, axis labels, tiles) via Google Fonts with system fallbacks. Scale 11/12/13/15/22 px. Small-caps letter-spaced section labels.

## Colour
Near-black blue bg (#0b0f14), elevated panels, hairline borders, blue=early / orange=late, single accent. Token names kept; hex tokens must pass `contrast.test.mjs`; manual dark block equals media dark block.

## Charts
Restyle only (geometry and tests unchanged), plus: gradient area under accumulation line, glow on mean markers, thin rounded bars, square heatmap cells with tight gaps, row/cell hover highlight.
Custom floating tooltip (new `js/tooltip.js`): reads existing SVG `<title>` text on hover/focus, clamps to viewport, position: fixed. `<title>` elements stay for screen readers.

## Directions
Labels "First stop – Last stop (bus N)" stay (already shipped).

## Failure modes
- Tooltip at viewport edges / scroll containers: fixed positioning, clamped. Minor.
- Font load failure: system fallback. Minor.
- Layout changes breaking page tests: keep DOM contract, change via CSS and wrapper classes, run suite per step. Critical if ignored.
- Contrast regressions: contrast test every commit.

## Testing
`node --test web/test/` under `ulimit -v 2500000`; new unit test for tooltip text/clamp logic; contrast test covers palette.

## Process
Sonnet only, inline, no parallel agents, small commits (tokens/type, layout, charts, tooltip), one review at the end.
