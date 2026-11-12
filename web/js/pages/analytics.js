// Analytics view: weekday x hour heatmap, delay accumulation along the route, forecast accuracy by horizon.
import { getSettings } from '../settings.js';
import { forecastTipData } from '../tipdata.js';
import { api, isAbort } from '../api.js';
import { accumulationGeometry, heatmapGrid, renderAccumulation, renderHeatmap, renderRangeChart, rangeDomain, rangeGeometry, WEEKDAYS } from '../charts.js';
import { dataTable, emptyState, figure, h, link, notice, put } from '../dom.js';
import { buildHash, controlsToQuery, hoursList, directionLabels, fmtCount, fmtDelay, fmtNum, plural, stopNameMap, stopCodeMap, stopLabel } from '../lib.js';
import { region } from '../view.js';
import { crumbs, filterBar, filterNotices, setup, windowText } from './common.js';

export const heatQuery = ({ weekdays, hours, ...rest }) => rest;
export const forecastQuery = ({ pct_lo, pct_hi, ...rest }) => rest;
const BUCKETS = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5];

export async function render(ctx) {
  const { route } = ctx.params;
  const s = setup(ctx);
  ctx.title(`Analytics · route ${route}`);
  const sections = ['heat', 'acc', 'fc'].map(() => h('section', { class: 'analytics-section' }));
  put(ctx.main, 
    crumbs(link('#/', '← All routes'), link(buildHash('route', { route }, s.linkQuery), `Route ${route} stops`)),
    h('h1', {}, `Analytics for route ${route}`),
    h('p', { class: 'subtitle' }, 'Delay = actual minus scheduled: negative = early, positive = late. Statistics use confirmed arrivals unless you change the filter.'),
    ...filterBar(ctx, 'analytics', { route }, s), ...sections);
  const [heat, acc, fc] = sections.map((el) => region(el, ctx.signal));
  const q = s.apiQuery, o = (signal) => ({ signal });
  await Promise.all([
    heat.load((sig) => api.heatmap(route, heatQuery(q), o(sig)), (d, el) => drawHeat(route, d, el, ctx, s), { announce: (d) => `Route ${route} heatmap: loaded ${plural((d.cells || []).length, 'cell')}` }),
    acc.load(async (sig) => {
      const [d, stops] = await Promise.all([api.accumulation(route, q, o(sig)), api.stopsOf(route, o(sig)).catch((e) => { if (isAbort(e)) throw e; return null; })]);
      return { d, names: stopNameMap(stops?.directions), codes: stopCodeMap(stops?.directions), dirs: directionLabels(stops?.directions, route) };
    }, ({ d, names, codes, dirs }, el) => drawAcc(route, d, names, codes, dirs, el), { announce: ({ d }) => `Route ${route} delay accumulation: loaded ${plural(d.groups.length, 'group')}` }),
    // the forecast endpoint always reports the 5th and 95th percentiles: the percentile setting does not apply
    fc.load((sig) => api.forecast(route, forecastQuery(q), o(sig)), (d, el) => drawForecast(route, d, el), { announce: () => `Route ${route} forecast accuracy loaded` }),
  ]);
}

function legend(maxAbs) {
  return h('div', { class: 'legend', 'aria-label': 'Colour legend' }, h('span', {}, 'early (blue)'),
    h('span', { class: 'swatches' }, BUCKETS.map((b) => h('span', { class: `swatch hm-b${b < 0 ? 'n' : ''}${Math.abs(b)}` }, b === 0 ? '0' : b < 0 ? '−' : '+'))),
    h('span', {}, 'late (orange)'), h('span', { class: 'key' }, `Colour saturates at ±${fmtNum(maxAbs / 60, 1)} min · grey cell: no data · small dot in the corner and italic table row: fewer than 10 samples · each cell shows the mean delay in minutes`));
}

function drawHeat(route, d, el, ctx, s) {
  const c = s.controls, hrs = hoursList(c.hFrom, c.hTo);
  const selected = c.weekdays.length === 1 && hrs && hrs.length === 1 ? { wd: c.weekdays[0], hour: hrs[0] } : null;
  const onSelect = (wd, hour) => {
    const same = selected && selected.wd === wd && selected.hour === hour;
    const next = same ? { ...c, weekdays: [], hFrom: '', hTo: '' } : { ...c, weekdays: [wd], hFrom: hour, hTo: hour };
    ctx.go(buildHash('analytics', { route }, controlsToQuery(next)));
  };
  put(el, h('h2', {}, 'When is the route late? Mean delay by weekday and hour'), ...filterNotices(d));
  const cells = (d.cells || []);
  if (!cells.length) { put(el, emptyState('No data for this selection.')); return; }
  const grid = heatmapGrid(cells, { cap: getSettings().charts.heatCap * 60 });
  const svg = renderHeatmap(grid, { title: `Mean delay by weekday and hour, route ${route}`,
    desc: `${cells.length} weekday and hour combinations have data (n=${fmtCount(d.n)} arrivals). Colour runs from blue (early) through grey (on time) to orange (late); the cell text is the mean delay in minutes. Colour saturates at ${fmtNum(grid.maxAbs / 60, 1)} min. Each square is a button: activate it to filter the charts below to that day and hour.`, selected, onSelect });
  const table = dataTable({ caption: 'Mean delay per weekday and hour (Dublin time; negative = early)', rows: cells.filter((c) => c.n > 0), columns: [
    { label: 'Weekday', get: (c) => WEEKDAYS[c.weekday] }, { label: 'Hour', get: (c) => `${String(c.hour).padStart(2, '0')}:00` },
    { label: 'n', num: true, get: (c) => fmtCount(c.n) }, { label: 'Mean delay', num: true, get: (c) => fmtDelay(c.mean_delay) }] });
  put(el, figure({ title: 'Heatmap', svg, table, legend: legend(grid.maxAbs), level: 'h3' }), h('p', { class: 'hint' }, 'Click a square to filter the charts below to that day and hour; this heatmap ignores the day and hour filters. ', windowText(d)));
}

function drawAcc(route, d, names, codes, dirs, el) {
  put(el, h('h2', {}, 'Where is time lost along the route?'), ...filterNotices(d));
  if (d.order_source === 'none') { put(el, emptyState('This view needs the stop order from the bus data server, which is unavailable right now.')); return; }
  if (!d.groups.length) { put(el, emptyState('No data for this selection.')); return; }
  for (const g of d.groups) {
    const name = g.fallback ? 'Unmatched trips' : (dirs.get(g.direction) || `Direction ${g.direction}`);
    const head = h('h3', {}, `${name} · ${fmtCount(g.n)} trips`);
    if (g.fallback || !g.stops.length) {
      put(el, head, notice(`${plural(g.n, 'trip')} could not be matched to a single direction of the server’s stop list, so no per-stop numbers are shown for them.`, 'info'));
      continue;
    }
    const geo = accumulationGeometry(g.stops);
    const label = (x) => stopLabel(x.stop_id, names, codes);
    const svg = renderAccumulation(geo, g.stops, { title: `Delay accumulation, ${name}`, stopLabel: label,
      desc: `Mean delay at each of ${g.stops.length} stops, from ${fmtDelay(g.stops[0].mean_delay)} at the first to ${fmtDelay(g.stops[g.stops.length - 1].mean_delay)} at the last. Bars below show the change from the previous stop, measured on trips seen at both: positive loses time, negative gains it.` });
    const table = dataTable({ caption: `${name}: mean delay at each stop and change from the previous stop (negative = early)`, rows: g.stops, columns: [
      { label: '#', num: true, get: (x) => String(x.position + 1) }, { label: 'Stop', get: label },
      { label: 'n', num: true, get: (x) => fmtCount(x.n) }, { label: 'Mean delay', num: true, get: (x) => fmtDelay(x.mean_delay) },
      { label: 'Change from previous stop', num: true, get: (x) => fmtDelay(x.segment_delta) }, { label: 'n (segment)', num: true, get: (x) => fmtCount(x.n_delta) }] });
    put(el, head, figure({ title: name, svg, table, level: 'h4',
      legend: h('p', { class: 'legend' }, `Line: mean delay at each stop (up = later) · bars: change from the previous stop (up = loses time, down = gains time). The bars come from trips observed at both stops (up to ${plural(g.n_delta, 'trip')}), so they need not add up to the line, which averages different trips at each stop.`) }));
  }
}

function drawForecast(route, d, el) {
  put(el, h('h2', {}, 'How accurate are the live predictions?'), ...filterNotices(d));
  const hs = d.horizons || [];
  if (!hs.some((x) => x.n > 0)) { put(el, emptyState('No forecast data for this selection yet. Forecast accuracy needs arrivals with stored predictions.')); return; }
  const items = hs.map((x) => ({ label: `${x.horizon_min} min before`, n: x.n, low_n: x.low_n, mean: x.mean_error, min: null, max: null, lo: x.p_low, hi: x.p_high, tip: forecastTipData(x) }));
  const geo = rangeGeometry(items, rangeDomain(items));
  const svg = renderRangeChart(geo, { title: 'Prediction error by horizon', axisLabel: 'minutes: actual minus predicted', zeroLabel: 'exactly as predicted',
    desc: `Mean prediction error per horizon: ${hs.map((x) => `${x.horizon_min} minutes before: ${fmtDelay(x.mean_error)} (${plural(x.n, 'sample')})`).join('; ')}. Positive means the bus came later than predicted.` });
  const table = dataTable({ caption: 'Error = actual arrival minus the prediction shown that long before (positive = bus later than predicted)', rows: hs, columns: [
    { label: 'Horizon', get: (x) => `${x.horizon_min} min before` }, { label: 'n', num: true, get: (x) => fmtCount(x.n) },
    { label: 'Mean error', num: true, get: (x) => fmtDelay(x.mean_error) }, { label: 'Mean absolute error', num: true, get: (x) => fmtDelay(x.mae) },
    { label: '5th to 95th percentile (fixed)', num: true, get: (x) => `${fmtDelay(x.p_low)} to ${fmtDelay(x.p_high)}` }] });
  put(el, figure({ title: 'Forecast accuracy', svg, table, level: 'h3',
    legend: h('p', { class: 'legend' }, 'Diamond: mean error \u00b7 bar: 5th to 95th percentile of errors \u00b7 right of the dashed line the bus came later than predicted (positive = bus later than predicted), left of it earlier. The table adds the mean absolute error (typical size of a miss).') }),
  h('p', { class: 'hint' }, 'The percentile setting in the filters does not apply to forecasts: this view always reports the 5th and 95th percentiles.'));
}
