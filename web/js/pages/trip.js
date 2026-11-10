// Trip view: how the displayed due-minutes evolved for one visit of a trip at a stop.
import { api } from '../api.js';
import { renderStepChart, stepGeometry } from '../charts.js';
import { dataTable, emptyState, figure, h, link, notice, tile, put } from '../dom.js';
import { buildHash, clockTime, controlsToQuery, controlsFromQuery, dateTime, describeDelay, dueSteps, fmtCount, fmtDelay } from '../lib.js';
import { region } from '../view.js';
import { crumbs } from './common.js';

export async function render(ctx) {
  const { trip, stop } = ctx.params;
  const route = ctx.query.route || '';
  ctx.title(`Trip ${trip}`);
  const backQuery = controlsToQuery(controlsFromQuery(ctx.query).controls);
  const box = h('div', {});
  put(ctx.main, 
    crumbs(link('#/', '← All routes'), route ? link(buildHash('stop', { route, stop }, backQuery), `Stop ${stop}, route ${route}`) : null),
    h('h1', {}, `Trip ${trip}`),
    h('p', { class: 'subtitle' }, `Minutes shown to passengers (due) at stop ${stop}, over time. The line holds the feed’s latest prediction until it changes and ends at the last reading: it is never extended past it.`), box);
  await region(box, ctx.signal).load((signal) => api.series(trip, stop, {}, { signal }), (d, el) => draw(ctx, trip, stop, route, d, el));
}

function draw(ctx, trip, stop, route, d, el) {
  const visits = d.visits || [];
  if (!visits.length) { put(el, emptyState('No prediction samples stored for this trip at this stop.')); return; }
  if (d.truncated) put(el, notice('Some visits or samples were left out (the API caps the size of a series). Older visits or samples were dropped.'));
  const want = Number(ctx.query.seq);
  let cur = Math.max(0, visits.findIndex((v) => v.seq === want));
  const holder = h('div', {});
  const picker = visits.length > 1 ? h('div', { class: 'row card' }, h('label', { for: 'visit' }, 'Visit (this trip calls here more than once, or ran on several days)',
    h('select', { id: 'visit', on: { change: (ev) => { cur = Number(ev.target.value); show(); try { globalThis.history.replaceState(null, '', buildHash('trip', { trip, stop }, { ...ctx.query, seq: visits[cur].seq })); } catch { /* ignore */ } } } },
      visits.map((v, i) => h('option', { value: String(i), selected: i === cur }, `${dateTime(v.scheduled)} scheduled${v.arrival ? '' : ' (never arrived)'}`))))) : null;
  put(el, picker, holder);
  function show() { holder.replaceChildren(...visitView(visits[cur], trip, stop)); }
  show();
}

function visitView(v, trip, stop) {
  const a = v.arrival;
  const out = [];
  const tiles = [tile('Scheduled', dateTime(v.scheduled)), tile('Last reading', v.last_seen_ts ? clockTime(v.last_seen_ts) : '–', { note: `${fmtCount(v.points.length)} samples` })];
  if (a) {
    tiles.push(tile('Actual arrival', a.actual ? dateTime(a.actual) : '–'), tile('Delay', fmtDelay(a.delay), { note: describeDelay(a.delay) }),
      tile('Confirmed', a.confidence ? 'yes' : 'no', { note: a.confidence ? 'a due-now reading was seen' : 'inferred from the last close prediction' }));
  }
  out.push(h('div', { class: 'tiles' }, tiles));
  if (!a) out.push(notice(`This bus disappeared from the feed before arriving${v.last_seen_ts ? ` (last seen ${clockTime(v.last_seen_ts)})` : ''}, so there is no arrival time.`, 'info'));
  if (!v.points.length) { out.push(emptyState('This visit has an arrival record but no stored prediction samples.')); return out; }
  const steps = dueSteps(v.points, v.last_seen_ts);
  const markers = [{ t: v.scheduled, label: `scheduled ${clockTime(v.scheduled)}`, kind: 'scheduled' }];
  if (a && a.actual) markers.push({ t: a.actual, label: `arrived ${clockTime(a.actual)}`, kind: 'arrival' });
  const geo = stepGeometry(steps, markers);
  const svg = renderStepChart(geo, { title: `Due minutes for trip ${trip} at stop ${stop}`,
    desc: `Step chart from ${clockTime(steps[0].t)} to ${clockTime(steps[steps.length - 1].t)}: due minutes started at ${steps[0].due} and ended at ${steps[steps.length - 1].due}.${a && a.actual ? ` The bus arrived at ${clockTime(a.actual)}, ${describeDelay(a.delay)}.` : ''}` });
  const table = dataTable({ caption: 'Stored samples (a row is written when the prediction changes)', rows: v.points, columns: [
    { label: 'Time', get: (p) => clockTime(p.ts) }, { label: 'Due (min)', num: true, get: (p) => String(p.due_min) },
    { label: 'Expected arrival', get: (p) => clockTime(p.expected) }, { label: 'Live prediction', get: (p) => (p.realtime ? 'yes' : 'scheduled only') }] });
  out.push(figure({ title: 'Due minutes over time', svg, table,
    legend: h('p', { class: 'legend' }, 'Solid line: minutes shown to passengers · dotted vertical line: scheduled time · dashed vertical line: actual arrival.') }));
  return out;
}
