# Rich tooltips, bigger hit targets, settings page

Status: approved by user 2026-11-10. Desktop only. Storage: browser localStorage.

## Tooltip (stat card)
Chart marks carry a JSON payload in `data-tip-json` (pure builders in `js/tipdata.js`): `{title, head:{sec}, sub, rows:[{k,l,fmt,v}], range?}`.
Rows are keyed (`mean median min max band ci std ontime n`) so the settings can hide them; missing stats are omitted.
Card: bold title, coloured headline delay (blue early / orange late), two-column label/value grid, optional mini range bar (min-max, band, mean tick), footer warning for n<10. Built with textContent/DOM only. `<title>` plain text stays for AT/fallback.
Per chart: stop chart (mean, median, min, max, band, CI, std, on-time, n), accumulation (mean, change from previous stop, n), heatmap (weekday+hour, mean, n), forecast (horizon, mean error, MAE, band, n), histogram (bin range, count, share).

## Hit targets
Transparent padded elements carry the tip: dots r=9; accumulation bars +4px each side; stop-chart rows full row height; histogram bars full plot height; heatmap cells already full.

## Settings (`#/settings`, nav link)
Appearance: theme auto/dark/light (shares `dba-theme`), density comfortable/compact (`data-density` on html).
Tooltips: on/off, per-stat checkboxes, units min/sec (tooltip only).
Charts: heatmap saturation auto or fixed minutes, stops per page 20/40/80, show data-table toggle.
Stored as one JSON object under `dba-settings`; field-by-field validation to defaults; applies instantly; Reset button; note when storage is blocked.

## Non-goals
Server-side settings, filter defaults, mobile layout, units outside tooltips.

## Failure modes
Corrupt/stale storage -> defaults per field. Blocked storage -> in-memory settings + notice. Overlapping hit areas -> clamped to own slot. Payload parse errors -> fall back to title text. Tooltip never uses innerHTML.
