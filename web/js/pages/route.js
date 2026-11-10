// Route view: every stop in route order with mean delay, min/max whiskers, percentile band against the zero line.
import { api, isAbort } from '../api.js';
import { getSettings } from '../settings.js';
import { rangeTipData } from '../tipdata.js';
import { renderRangeChart, rangeDomain, rangeGeometry, rangeSummary } from '../charts.js';
import { dataTable, emptyState, figure, h, link, tile, put } from '../dom.js';
import { buildHash, controlsToQuery, fmtCount, fmtDelay, fmtNum, fmtPct, fmtPctile, paginate, plural, stopNameMap, stopCodeMap, stopLabel, describeDelay, directionLabels } from '../lib.js';
import { region } from '../view.js';
import { crumbs, filterBar, filterNotices, setup, windowText } from './common.js';


export async function render(ctx) {
  const { route } = ctx.params;
  const s = setup(ctx);
  const dir = ctx.query.dir || '';
  const page = Number(ctx.query.page) || 1;
  ctx.title(`Route ${route}`);
  const box = h('div', {});
  put(ctx.main, 
    crumbs(link('#/', '← All routes'), link(buildHash('analytics', { route }, s.linkQuery), 'Analytics for this route')),
    h('h1', {}, `Route ${route}`),
    h('p', { class: 'subtitle' }, 'Delay at each stop: negative = early, positive = late. Pick a stop to see its arrival distribution.'),
    ...filterBar(ctx, 'route', { route }, s, dir ? { dir } : {}), box);
  const fetcher = async (signal) => {
    const [summary, stops] = await Promise.all([
      api.summary(route, s.apiQuery, { signal }),
      api.stopsOf(route, { signal }).catch((e) => { if (isAbort(e)) throw e; return null; }),
    ]);
    return { summary, names: stopNameMap(stops?.directions), codes: stopCodeMap(stops?.directions), dirs: directionLabels(stops?.directions, route) };
  };
  await region(box, ctx.signal).load(fetcher, ({ summary, names, codes, dirs }, el) => draw(ctx, route, s, summary, names, codes, dirs, dir, page, el),
    { announce: ({ summary }) => `Route ${route}: loaded ${plural((summary.stops || []).length, 'stop')}` });
}

function draw(ctx, route, s, data, names, codes, dirs, dir, page, el) {
  put(el, ...filterNotices(data));
  const all = data.stops || [];
  if (!all.length) {
    put(el, emptyState(`No confirmed arrivals for route ${route} in this selection. Is the route tracked, and has the poller run long enough? Try a wider date range or include unconfirmed arrivals.`));
    return;
  }
  const dirKeys = [...new Set(all.flatMap((x) => x.directions || []))];
  const stops = dir ? all.filter((x) => (x.directions || []).includes(dir)) : all;
  const hashFor = (extra) => buildHash('route', { route }, { ...controlsToQuery(s.controls), ...(dir ? { dir } : {}), ...extra });
  if (dirKeys.length > 1) {
    const sel = h('select', { id: 'dir', on: { change: (ev) => ctx.go(buildHash('route', { route }, { ...controlsToQuery(s.controls), dir: ev.target.value })) } },
      h('option', { value: '', selected: !dir }, 'All directions'), dirKeys.map((d) => h('option', { value: d, selected: d === dir }, dirs.get(d) || d)));
    put(el, h('div', { class: 'row card' }, h('label', { for: 'dir' }, 'Direction', sel)));
  }
  const withData = stops.filter((x) => x.n > 0).length;
  put(el, h('div', { class: 'tiles' }, tile('Arrivals analysed', fmtCount(data.n), { lowN: data.low_n }),
    tile('Stops with data', fmtCount(withData), { note: dir ? (dirs.get(dir) || `direction ${dir}`) : null })), h('p', { class: 'hint' }, windowText(data)));
  if (!stops.length) { put(el, emptyState('No stops with data in this direction.')); return; }

  const items = stops.map((x) => ({ id: x.stop_id, label: stopLabel(x.stop_id, names, codes), short: names.get(x.stop_id) || codes.get(x.stop_id) || x.stop_id, href: buildHash('stop', { route, stop: x.stop_id }, controlsToQuery(s.controls)),
    n: x.n, low_n: x.low_n, mean: x.mean, min: x.min, max: x.max, lo: x.p_low, hi: x.p_high, raw: x }));
  for (const it of items) it.tip = rangeTipData(it, it.raw);
  const domain = rangeDomain(items);
  const p = paginate(items.length, page, getSettings().charts.pageSize);
  const slice = items.slice(p.start, p.end);
  const geo = rangeGeometry(slice, domain);
  const f = data.filters;
  const band = `${fmtPctile(f.pct_lo)} to ${fmtPctile(f.pct_hi)} percentile`;
  const svg = renderRangeChart(geo, { title: `Delay by stop, route ${route}`, desc: `${rangeSummary(slice)} Whiskers show min and max, the bar the ${band}.` });
  const table = dataTable({ caption: `Delay per stop, route ${route} (seconds shown as minutes; negative = early)`, rows: slice,
    columns: [
      { label: '#', num: true, get: (r) => (r.raw.position == null ? '–' : String(r.raw.position + 1)) },
      { label: 'Stop', get: (r) => link(r.href, r.label) },
      { label: 'n', num: true, get: (r) => fmtCount(r.n) },
      { label: 'Mean', num: true, get: (r) => `${fmtDelay(r.mean)} (${describeDelay(r.mean)})` },
      { label: 'Min', num: true, get: (r) => fmtDelay(r.min) }, { label: 'Max', num: true, get: (r) => fmtDelay(r.max) },
      { label: band, num: true, get: (r) => `${fmtDelay(r.lo)} to ${fmtDelay(r.hi)}` },
      { label: 'On time', num: true, get: (r) => fmtPct(r.raw.on_time_pct) },
      { label: 'Reliability', num: true, get: (r) => fmtNum(r.raw.reliability, 1) },
    ] });
  const legend = h('p', { class: 'legend' }, `Bar: ${band} · whiskers: min to max · diamond: mean (hollow diamond, dashed bar and italic label: fewer than 10 samples) · dashed line: exactly on schedule · left of it early, right of it late`);
  if (p.pages > 1) {
    put(el, h('nav', { class: 'pager', 'aria-label': 'Stop pages' },
      p.page > 1 ? link(hashFor({ page: p.page - 1 }), '← Previous') : null,
      h('span', {}, `Stops ${p.start + 1}–${p.end} of ${items.length}`),
      p.page < p.pages ? link(hashFor({ page: p.page + 1 }), 'Next →') : null));
  }
  put(el, figure({ title: 'Delay by stop', svg, table, legend }));
}
