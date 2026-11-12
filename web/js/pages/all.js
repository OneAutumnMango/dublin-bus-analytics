// All tracked routes: sortable comparison of every route, plus pooled heatmap and forecast accuracy.
import { getSettings } from '../settings.js';
import { api } from '../api.js';
import { heatmapGrid, rangeDomain, rangeGeometry, renderHeatmap, renderRangeChart, WEEKDAYS } from '../charts.js';
import { dataTable, emptyState, figure, h, link, notice, put } from '../dom.js';
import { buildHash, fmtCount, fmtDelay, fmtNum, fmtPct, fmtPctile, plural } from '../lib.js';
import { region } from '../view.js';
import { crumbs, filterBar, filterNotices, setup, windowText } from './common.js';
import { drawForecast, forecastQuery, heatQuery, legend } from './analytics.js';

const KEYS = ['route', 'n', 'mean', 'median', 'on_time_pct', 'reliability'];
const nil = (v) => v === null || v === undefined || Number.isNaN(v);

// Returns a new array; missing values sort last in both directions.
export function sortRoutes(routes, key, dir) {
  const k = KEYS.includes(key) ? key : 'median', sign = dir === 'asc' ? 1 : -1;
  return [...routes].sort((a, b) => {
    const x = a[k], y = b[k];
    if (nil(x) || nil(y)) return nil(x) && nil(y) ? 0 : nil(x) ? 1 : -1;
    const c = k === 'route' ? String(x).localeCompare(String(y), undefined, { numeric: true }) : x - y;
    return c * sign;
  });
}

const truncNotice = (d) => {
  const t = d?.filters?.truncated_routes;
  return t?.length ? notice(`Too many arrivals matched for ${plural(t.length, 'route')} (${t.join(', ')}): only the most recent rows of each are included (per-route cap of 500,000 arrivals). Narrow the date range for a complete view.`) : null;
};

export async function render(ctx) {
  const s = setup(ctx);
  ctx.title('All routes');
  const sections = ['cmp', 'heat', 'fc'].map(() => h('section', { class: 'analytics-section' }));
  put(ctx.main,
    crumbs(link('#/', '← All routes list')),
    h('h1', {}, 'All tracked routes'),
    h('p', { class: 'subtitle' }, 'Delay = actual minus scheduled: negative = early, positive = late. Every tracked route is compared, then pooled. Pooled by arrival, not by route.'),
    ...filterBar(ctx, 'all', {}, s), ...sections);
  const [cmp, heat, fc] = sections.map((el) => region(el, ctx.signal));
  const q = s.apiQuery, o = (signal) => ({ signal });
  await Promise.all([
    cmp.load((sig) => api.allRoutes(q, o(sig)), (d, el) => drawRoutes(d, el, s), { announce: (d) => `All routes: loaded ${plural((d.routes || []).length, 'route')}` }),
    heat.load((sig) => api.allHeatmap(heatQuery(q), o(sig)), drawHeat, { announce: (d) => `All routes heatmap: loaded ${plural((d.cells || []).length, 'cell')}` }),
    fc.load((sig) => api.allForecast(forecastQuery(q), o(sig)), (d, el) => { put(el, truncNotice(d)); drawForecast('', d, el); }, { announce: () => 'All routes forecast accuracy loaded' }),
  ]);
}

function drawRoutes(d, el, s) {
  const f = d.filters || {};
  const head = [h('h2', {}, 'How do the routes compare?'), ...filterNotices(d), truncNotice(d)];
  put(el, head);
  const routes = d.routes || [];
  if (!routes.length) { put(el, emptyState('No routes tracked yet. Search for a route in the sidebar to start tracking it.')); return; }
  if (!routes.some((r) => !nil(r.mean))) { put(el, emptyState('No data for this selection.')); return; }
  const sort = { key: 'median', dir: 'desc' };
  const holder = h('div', {});
  const band = `${fmtPctile(f.pct_lo)} to ${fmtPctile(f.pct_hi)} percentile`;
  const cols = [['route', 'Route', false], ['n', 'n', true], ['mean', 'Mean', true], ['median', 'Median', true], ['on_time_pct', 'On time', true], ['reliability', 'Reliability', true]];
  const draw = () => {
    const sorted = sortRoutes(routes, sort.key, sort.dir);
    const items = sorted.filter((r) => !nil(r.mean)).map((r) => ({ label: `Route ${r.route}`, short: r.route, href: buildHash('analytics', { route: r.route }, s.linkQuery),
      n: r.n, low_n: r.low_n, mean: r.mean, median: r.median, min: null, max: null, lo: r.p_low, hi: r.p_high }));
    const svg = renderRangeChart(rangeGeometry(items, rangeDomain(items)), { title: 'Delay by route', axisLabel: 'minutes vs schedule',
      desc: `Mean and median delay of ${plural(items.length, 'route')}, in the order of the table. Bars show the ${band}.` });
    const stat = (r, v, fmt) => (r.n === 0 || nil(v) ? '—' : fmt(v));
    const table = dataTable({ caption: 'Delay per route (negative = early). Activate a column header to sort.', rows: sorted, columns: cols.map(([key, label, num]) => ({
      num,
      label: h('button', { class: 'sort-btn', type: 'button', on: { click: () => {
        if (sort.key === key) sort.dir = sort.dir === 'asc' ? 'desc' : 'asc'; else { sort.key = key; sort.dir = key === 'route' ? 'asc' : 'desc'; }
        draw();
      } } }, label),
      get: (r) => {
        if (key === 'route') return link(buildHash('analytics', { route: r.route }, s.linkQuery), r.route);
        if (key === 'n') return fmtCount(r.n);
        if (key === 'mean' && r.n === 0) return 'no data';
        if (key === 'mean' || key === 'median') return stat(r, r[key], fmtDelay);
        return stat(r, r[key], key === 'on_time_pct' ? fmtPct : (v) => fmtNum(v, 0));
      } })) });
    cols.forEach(([key], i) => {
      const th = table.byTag ? table.byTag('th')[i] : [...table.querySelectorAll('th')][i];
      th.setAttribute('aria-sort', sort.key === key ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
    holder.replaceChildren(figure({ title: 'Routes compared', svg, table, level: 'h3',
      legend: h('p', { class: 'legend' }, `Diamond: mean · ring: median · bar: ${band} of delay · right of the dashed line is late, left is early. Sorted by ${cols.find((c) => c[0] === sort.key)[1].toLowerCase()} (${sort.dir === 'asc' ? 'low to high' : 'high to low'}).`) }));
  };
  draw();
  put(el, holder, h('p', { class: 'hint' }, 'Pooled by arrival, not by route. ', windowText({ ...d, n: routes.reduce((a, r) => a + (r.n || 0), 0) })));
}

function drawHeat(d, el) {
  put(el, h('h2', {}, 'When are buses late? Mean delay by weekday and hour, all routes'), ...filterNotices(d), truncNotice(d));
  const cells = d.cells || [];
  if (!cells.length) { put(el, emptyState('No data for this selection.')); return; }
  const grid = heatmapGrid(cells, { cap: getSettings().charts.heatCap * 60 });
  const svg = renderHeatmap(grid, { title: 'Mean delay by weekday and hour, all routes',
    desc: `${cells.length} weekday and hour combinations have data (n=${fmtCount(d.n)} arrivals, all tracked routes pooled). Colour runs from blue (early) through grey (on time) to orange (late); the cell text is the mean delay in minutes. Colour saturates at ${fmtNum(grid.maxAbs / 60, 1)} min.` });
  const table = dataTable({ caption: 'Mean delay per weekday and hour (Dublin time; negative = early)', rows: cells.filter((c) => c.n > 0), columns: [
    { label: 'Weekday', get: (c) => WEEKDAYS[c.weekday] }, { label: 'Hour', get: (c) => `${String(c.hour).padStart(2, '0')}:00` },
    { label: 'n', num: true, get: (c) => fmtCount(c.n) }, { label: 'Mean delay', num: true, get: (c) => fmtDelay(c.mean_delay) }] });
  put(el, figure({ title: 'Heatmap', svg, table, legend: legend(grid.maxAbs), level: 'h3' }),
    h('p', { class: 'hint' }, 'This heatmap ignores the day and hour filters. ', windowText(d)));
}
