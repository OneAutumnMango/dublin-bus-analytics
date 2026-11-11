// Settings page: every change applies immediately and is saved in this browser (localStorage).
import { h, notice, put } from '../dom.js';
import { DEFAULTS, getSettings, resetSettings, setSettings, storageWorks } from '../settings.js';

const STAT_LABELS = { mean: 'Mean', median: 'Median', min: 'Min', max: 'Max', band: 'Percentile band', ci: 'Confidence interval', std: 'Std deviation', ontime: 'On time %', n: 'Sample count (n)' };

const field = (label, control, hint) => h('div', { class: 'setting' }, h('label', { class: 'setting-label' }, label, control), hint ? h('span', { class: 'hint' }, hint) : null);
const select = (value, options, onChange) => h('select', { on: { change: (e) => onChange(e.target.value) } },
  options.map(([v, l]) => h('option', { value: String(v), selected: String(v) === String(value) }, l)));
const check = (label, checked, onChange) => h('label', { class: 'inline' }, h('input', { type: 'checkbox', checked, on: { change: (e) => onChange(e.target.checked) } }), label);

export function applyAppearance(s = getSettings()) {
  globalThis.document?.documentElement?.setAttribute?.('data-density', s.density);
}

export async function render(ctx) {
  ctx.title('Settings');
  const draw = () => {
    const s = getSettings();
    const theme = globalThis.dbaTheme?.get?.() || 'auto';
    ctx.main.replaceChildren();
    put(ctx.main,
      h('h1', {}, 'Settings'),
      h('p', { class: 'subtitle' }, 'Saved in this browser and applied straight away.'),
      storageWorks() ? null : notice('This browser is blocking storage, so settings last only until you close the tab.'),
      h('section', { class: 'card settings-group' }, h('h2', {}, 'Appearance'),
        field('Theme', select(theme, [['auto', 'Follow system'], ['light', 'Light'], ['dark', 'Dark']], (v) => { globalThis.dbaTheme?.set?.(v); })),
        field('Density', select(s.density, [['comfortable', 'Comfortable'], ['compact', 'Compact']], (v) => { setSettings({ density: v }); applyAppearance(); }))),
      h('section', { class: 'card settings-group' }, h('h2', {}, 'Tooltips'),
        check('Show detailed tooltips on charts', s.tip.on, (v) => { setSettings({ tip: { on: v } }); draw(); }),
        field('Delay units in tooltips', select(s.tip.units, [['min', 'Minutes'], ['sec', 'Seconds']], (v) => setSettings({ tip: { units: v } }))),
        h('fieldset', { disabled: !s.tip.on }, h('legend', {}, 'Statistics to show'),
          h('div', { class: 'checks' }, Object.entries(STAT_LABELS).map(([k, l]) => check(l, s.tip.stats[k], (v) => setSettings({ tip: { stats: { [k]: v } } })))))),
      h('section', { class: 'card settings-group' }, h('h2', {}, 'Charts'),
        field('Heatmap colour saturation', select(s.charts.heatCap, [[0, 'Automatic'], [1, '1 min'], [2, '2 min'], [5, '5 min'], [10, '10 min'], [15, '15 min'], [30, '30 min']], (v) => setSettings({ charts: { heatCap: Number(v) } })), 'Delays beyond this show the strongest colour.'),
        field('Stops per page (route view)', select(s.charts.pageSize, [[20, '20'], [40, '40'], [80, '80']], (v) => setSettings({ charts: { pageSize: Number(v) } }))),
        check('Show the “Show data table” button under charts', s.charts.tableToggle, (v) => setSettings({ charts: { tableToggle: v } }))),
      h('section', { class: 'card settings-group' }, h('h2', {}, 'Map'),
        field('Area colouring strength', select(s.map.areaOpacity, [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1].map((v) => [v, `${Math.round(v * 100)}%`]), (v) => setSettings({ map: { areaOpacity: Number(v) } })), 'How strongly the hex grid, circles or smooth gradient tint the map. Lower lets the streets show through.'),
        field('Area size (spread)', select(s.map.areaSize, [[150, 'Tight, 150 m'], [250, 'Small, 250 m'], [400, 'Medium, 400 m'], [600, 'Large, 600 m'], [800, 'Wide, 800 m'], [1200, 'Very wide, 1.2 km']], (v) => setSettings({ map: { areaSize: Number(v) } })), 'How far each stop’s colour spreads: hexagon size, circle radius, or reach of the smooth gradient.')),
      h('div', { class: 'row' }, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => { resetSettings(); applyAppearance(); draw(); } } }, 'Reset to defaults')));
  };
  draw();
}
export { DEFAULTS };
