// Stop view: delay distribution, confidence interval of the mean, percentile band, on-time %, recent arrivals.
import { api, isAbort } from '../api.js';
import { histogramGeometry, renderHistogram } from '../charts.js';
import { dataTable, emptyState, figure, h, link, tile, put } from '../dom.js';
import { buildHash, clockTime, controlsToQuery, dateTime, describeDelay, describeRange, fmtCount, fmtDelay, fmtLevel, fmtNum, fmtPct, fmtPctile, plural, stopNameMap, stopCodeMap, stopLabel } from '../lib.js';
import { region } from '../view.js';
import { crumbs, filterBar, filterNotices, setup, windowText } from './common.js';

export async function render(ctx) {
  const { route, stop } = ctx.params;
  const s = setup(ctx);
  ctx.title(`Stop ${stop} · route ${route}`);
  const nameSlot = h('span', { class: 'muted' }, stop);
  const box = h('div', {});
  put(ctx.main, 
    crumbs(link('#/', '← All routes'), link(buildHash('route', { route }, s.linkQuery), `Route ${route}`), link(buildHash('analytics', { route }, s.linkQuery), 'Route analytics')),
    h('h1', {}, 'Stop ', nameSlot, ` on route ${route}`),
    h('p', { class: 'subtitle' }, 'How this stop’s buses compare with the timetable. Negative = early, positive = late.'),
    ...filterBar(ctx, 'stop', { route, stop }, s), box);
  const fetcher = async (signal) => {
    const [stats, stops] = await Promise.all([
      api.stopStats(stop, route, s.apiQuery, { signal }),
      api.stopsOf(route, { signal }).catch((e) => { if (isAbort(e)) throw e; return null; }),
    ]);
    return { stats, names: stopNameMap(stops?.directions), codes: stopCodeMap(stops?.directions) };
  };
  await region(box, ctx.signal).load(fetcher, ({ stats, names, codes }, el) => {
    const nm = names.get(stop), code = codes.get(stop);
    if (nm || code) { nameSlot.textContent = stopLabel(stop, names, codes); ctx.title(`${nm || `Stop ${code}`} · route ${route}`); }
    draw(route, stop, s, stats, el);
  }, { announce: ({ stats }) => `Stop ${stop}: loaded ${plural(stats.n, 'arrival')}` });
}

function draw(route, stop, s, d, el) {
  put(el, ...filterNotices(d));
  if (!d.n) {
    put(el, emptyState('No confirmed arrivals at this stop for this selection. Try a wider date range, other filters, or include unconfirmed arrivals.'));
    return;
  }
  const f = d.filters, x = d.delay;
  const win = `${fmtDelay(f.window_lo)} to ${fmtDelay(f.window_hi)}`;
  put(el, h('p', { class: 'card' }, h('strong', {}, 'Usually arrives '), `${describeRange(x.ci_low, x.ci_high)} vs scheduled`, ` (${fmtLevel(f.ci)} confidence interval of the mean delay, n=${fmtCount(d.n)}). `,
    h('strong', {}, `${fmtLevel(f.pct_hi - f.pct_lo)} of buses `), `arrive ${describeRange(x.p_low, x.p_high)} vs scheduled (${fmtPctile(f.pct_lo)} to ${fmtPctile(f.pct_hi)} percentile).`));
  put(el, h('div', { class: 'tiles' },
    tile('Mean delay', fmtDelay(x.mean), { note: describeDelay(x.mean), lowN: d.low_n, n: d.n }),
    tile(`${fmtLevel(f.ci)} CI of mean`, `${fmtDelay(x.ci_low)} to ${fmtDelay(x.ci_high)}`, { lowN: d.low_n, n: d.n }),
    tile('Median', fmtDelay(x.median), { note: `min ${fmtDelay(x.min)}, max ${fmtDelay(x.max)}`, lowN: d.low_n, n: d.n }),
    tile('On time', fmtPct(d.on_time_pct), { note: `window ${win}`, lowN: d.low_n, n: d.n }),
    tile('Reliability', fmtNum(d.reliability, 1), { note: 'on-time % reduced by spread (0 to 100)', lowN: d.low_n, n: d.n })),
  h('p', { class: 'hint' }, windowText(d)));

  const geo = histogramGeometry(d.histogram);
  const lines = [{ x: geo.xOf(x.mean), cls: 'mean-line' }, { x: geo.xOf(x.p_low), cls: 'pct' }, { x: geo.xOf(x.p_high), cls: 'pct' }];
  const band = x.ci_low != null ? { x0: geo.xOf(x.ci_low), x1: geo.xOf(x.ci_high) } : null;
  const svg = renderHistogram(geo, { title: `Delay distribution at stop ${stop}`, markers: lines, band,
    desc: `Histogram of ${fmtCount(d.n)} arrivals in one-minute bins. Mean ${fmtDelay(x.mean)}; ${fmtLevel(f.ci)} interval of the mean ${describeRange(x.ci_low, x.ci_high)}. Negative means early.` });
  const table = dataTable({ caption: 'Arrivals per one-minute delay bin', rows: d.histogram.filter((b) => b.count > 0),
    columns: [{ label: 'Delay bin', get: (b) => (b.lo === null ? `below ${fmtDelay(b.hi)}` : b.hi === null ? `${fmtDelay(b.lo)} and later` : `${fmtDelay(b.lo)} to ${fmtDelay(b.hi)}`) },
      { label: 'Arrivals', num: true, get: (b) => fmtCount(b.count) }] });
  const legend = h('p', { class: 'legend' }, 'Bars: arrivals per minute of delay (blue = early, orange = late). Shaded: confidence interval of the mean · solid line: mean · dotted lines: percentile band · dashed line: on schedule.');
  put(el, figure({ title: 'Delay distribution', svg, table, legend }));

  const q = controlsToQuery(s.controls);
  put(el, h('section', { class: 'card', 'aria-labelledby': 'h-recent' }, h('h2', { id: 'h-recent' }, `Most recent arrivals (${d.recent.length})`),
    d.recent.length ? dataTable({ caption: 'Newest first. Open a trip to see how its predictions evolved.', rows: d.recent, columns: [
      { label: 'Scheduled (Dublin time)', get: (r) => dateTime(r.scheduled) }, { label: 'Actual (Dublin time)', get: (r) => clockTime(r.actual) },
      { label: 'Delay', num: true, get: (r) => `${fmtDelay(r.delay)} (${describeDelay(r.delay)})` },
      { label: 'Confirmed', get: (r) => (r.confidence ? 'yes' : 'no') },
      { label: 'Trip', get: (r) => link(buildHash('trip', { trip: r.trip_id, stop }, { seq: r.seq, route, ...q }), `Trip ${r.trip_id}`, { 'aria-label': `Prediction series of trip ${r.trip_id}, scheduled ${dateTime(r.scheduled)}` }) }] })
      : emptyState('No recent arrivals.')));
}
