# Analytics page: hour slider, day buttons, clickable heatmap, bar fix

Scope: dublin-bus-analytics/web only (no backend change).

## 1. Hour slider
Replace the hour dropdowns in controls.js with `timeRangeSlider` (slider.js), step 60 min, same presets, wraps midnight.
Pure helpers convert slider minutes <-> (hFrom, hTo inclusive):
- full day (0..1439) <-> blank, blank
- hFrom = floor(a/60); hTo = (b === 1439 ? 23 : b/60 - 1 mod 24); a == b (not full) = single hour a
- legacy: only hf set -> single hour hf; only ht set -> single hour ht
URL keys hf/ht unchanged.

## 2. Day buttons
Weekday checkboxes become 7 toggle `<button aria-pressed>` pills (Mon..Sun); none selected = all days; `wd` key unchanged.

## 3. Heatmap
- Fetched without weekdays/hours; date range, confirmed and other filters still apply.
- Cell click navigates to analytics with wd=<day>, hf=ht=<hour>; clicking the selected cell clears day/hour. Selected cell outlined (single day + single hour filter).
- Cells: role=button, tabindex, Enter/Space. Data table kept.

## 4. Bar chart bug
Header rule `.bar { height:100%; display:flex }` hits chart rects. Rename chart class to `acc-bar` (charts.js, style.css); test guards against the clash.

## Non-goals
Multi-cell selection (API filter is a day x hour cross product); clicking replaces any multi-day selection. Axis wording unchanged.

## Testing
Unit: hour mapping round trip (all windows, wrap, legacy links); heatmap query omits wd/hours; cell click hash; acc-bar class and no `.chart .bar`. Existing web/test suite must pass.
