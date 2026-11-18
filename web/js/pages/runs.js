// Runs view: every run (trip) of one or more routes on one service day, scheduled against actual times.
import { api } from '../api.js';
import { renderRunsChart, runsGeometry } from '../charts.js';
import { dataTable, emptyState, figure, h, link, notice, put, tile } from '../dom.js';
import { buildHash, clockTime, dateTime, describeDelay, fmtCount, fmtDelay, isNum, plural, stopCodeMap, stopLabel, stopNameMap } from '../lib.js';
import { region } from '../view.js';

const MAX_ROUTES = 5;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

// Per-run numbers shown in the table and used to colour the diagram line.
export function summarise(run) {
  const vs = run.visits;
  const delays = vs.map((v) => v.delay).filter(isNum);
  return { ...run, first: vs[0].scheduled, last: vs[vs.length - 1].scheduled, stops: vs.length, missing: vs.filter((v) => v.actual == null).length,
    delay: mean(delays), worst: delays.length ? Math.max(...delays) : null };
}

export const status = (v) => (v.actual == null ? 'no arrival recorded' : !v.realtime ? 'scheduled only' : v.confidence ? 'confirmed' : 'estimated');

export async function render(ctx) {
  const routes = [...new Set((ctx.query.routes || '').split(/[\s,]+/).filter(Boolean))];
  const date = ctx.query.date || '';
  ctx.title('Runs');
  const routesIn = h('input', { id: 'runs-routes', type: 'text', value: routes.join(', '), placeholder: 'e.g. 4, 15', 'aria-describedby': 'runs-hint', autocomplete: 'off' });
  const dateIn = h('input', { id: 'runs-date', type: 'date', value: date || dateTime(ctx.nowSec() - 86400).slice(0, 10) });
  const apply = (ev) => {
    ev?.preventDefault?.();
    ctx.go(buildHash('runs', {}, { routes: routesIn.value.split(/[\s,]+/).filter(Boolean).join(','), date: dateIn.value }));
  };
  const results = h('div', {});
  put(ctx.main, h('h1', {}, 'Runs'),
    h('p', { class: 'subtitle' }, 'Look at one past service day: every run of a route as a line through the stops, dashed for the scheduled times and solid for the times the bus actually arrived. Times are Dublin time.'),
    h('form', { class: 'row card', on: { submit: apply } },
      h('label', { for: 'runs-routes' }, 'Routes', routesIn), h('label', { for: 'runs-date' }, 'Service day', dateIn),
      h('button', { class: 'btn', type: 'submit' }, 'Show runs'),
      h('p', { class: 'hint', id: 'runs-hint' }, `Up to ${MAX_ROUTES} routes, separated by commas. Only tracked routes have recorded runs.`)), results);
  if (!routes.length && !date) { put(results, emptyState('Pick a route and a day to see its runs.')); return; }
  if (!routes.length) { put(results, notice('Enter at least one route.')); return; }
  if (!DATE_RE.test(date)) { put(results, notice('The day must be a date like 2026-11-12.')); return; }
  if (routes.length > MAX_ROUTES) put(results, notice(`Only the first ${MAX_ROUTES} routes are shown.`));
  await Promise.all(routes.slice(0, MAX_ROUTES).map((route) => {
    const box = h('div', {});
    put(results, h('section', {}, h('h2', {}, `Route ${route}`, h('span', { class: 'hint' }, ` · ${date}`)), box));
    const fetcher = async (signal) => {
      const [data, stops] = await Promise.all([api.runs(route, date, { signal }), api.stopsOf(route, { signal }).catch(() => null)]);
      return { data, stops };
    };
    return region(box, ctx.signal).load(fetcher, ({ data, stops }, el) => draw(route, date, data, stops, el), { announce: ({ data }) => `Route ${route}: ${plural(data.runs.length, 'run')}` });
  }));
}

function draw(route, date, data, stops, el) {
  const runs = data.runs.map(summarise);
  if (!runs.length) { put(el, emptyState(`No arrivals recorded for route ${route} on ${date}. Was the route tracked that day?`)); return; }
  const names = stopNameMap(stops?.directions), codes = stopCodeMap(stops?.directions);
  const label = (id) => stopLabel(id, names, codes);
  const visits = data.runs.flatMap((r) => r.visits);
  const arrived = visits.filter((v) => v.actual != null).length;
  const delays = visits.map((v) => v.delay).filter(isNum);
  if (data.truncated) put(el, notice('This day has more arrivals than the API returns at once, so only the earliest are shown.'));
  if (data.order_source === 'none') put(el, notice('Stop order is unavailable (the bus data server could not be reached), so there is no diagram; the tables still work.', 'info'));
  put(el, h('div', { class: 'tiles' }, tile('Runs', fmtCount(runs.length)), tile('Arrivals recorded', fmtCount(arrived), { note: `of ${fmtCount(visits.length)} stops visited` }),
    tile('Mean delay', fmtDelay(mean(delays)), { note: describeDelay(mean(delays)) }), tile('Worst delay', fmtDelay(delays.length ? Math.max(...delays) : null))));
  const table = (rs, caption) => dataTable({ caption, rows: rs, columns: [
    { label: 'Run', get: (r) => r.trip_id }, { label: 'First stop scheduled', get: (r) => clockTime(r.first) }, { label: 'Last stop scheduled', get: (r) => clockTime(r.last) },
    { label: 'Stops', num: true, get: (r) => fmtCount(r.stops) }, { label: 'No arrival', num: true, get: (r) => fmtCount(r.missing) },
    { label: 'Mean delay', num: true, get: (r) => fmtDelay(r.delay) }, { label: 'Worst delay', num: true, get: (r) => fmtDelay(r.worst) }] });
  const dirs = Object.entries(data.directions || {}).filter(([k]) => runs.some((r) => r.direction === k));
  for (const [dir, ids] of dirs) {
    const rs = runs.filter((r) => r.direction === dir);
    const first = names.get(ids[0]), lastN = names.get(ids[ids.length - 1]);
    const title = first && lastN ? `${first} – ${lastN}` : `Direction ${dir}`;
    const geo = runsGeometry(rs.map((r) => ({ ...r, tip: `Run ${r.trip_id}: ${clockTime(r.first)} to ${clockTime(r.last)}, ${plural(r.stops, 'stop')}, mean delay ${fmtDelay(r.delay)}` })), ids.length);
    const svg = renderRunsChart(geo, { title: `Runs of route ${route}, ${title}`, desc: `${plural(rs.length, 'run')} on ${date}, scheduled and actual times at each stop.`, stopLabel: (pos) => label(ids[pos]) });
    put(el, figure({ title: `${title} · ${plural(rs.length, 'run')}`, svg, table: table(rs, `Runs of route ${route}, ${title}, ${date}`), level: 'h3',
      legend: h('p', { class: 'legend' }, 'Each line is one run. Dashed grey: scheduled times. Solid: actual times, blue if the run was early on average, orange if late, green if exactly on time. Gaps: no arrival recorded.') }));
  }
  const rest = runs.filter((r) => !dirs.some(([k]) => k === r.direction));
  if (rest.length) put(el, h('h3', {}, 'Runs without a known direction'), table(rest, `Runs of route ${route} without a known direction, ${date}`));
  put(el, inspector(route, runs, label));
}

// Pick one run to see every stop: scheduled, actual, delay and how sure we are.
function inspector(route, runs, label) {
  const holder = h('div', {});
  const pick = h('select', { id: `runs-pick-${route}`, on: { change: (ev) => show(Number(ev.target.value)) } },
    h('option', { value: '-1' }, 'Choose a run…'), runs.map((r, i) => h('option', { value: String(i) }, `${clockTime(r.first)} · ${r.trip_id} · ${fmtDelay(r.delay)}`)));
  function show(i) {
    holder.replaceChildren();
    if (i < 0) return;
    const r = runs[i];
    put(holder, dataTable({ caption: `Stops of run ${r.trip_id}`, rows: r.visits, columns: [
      { label: 'Stop', get: (v) => link(buildHash('trip', { trip: r.trip_id, stop: v.stop_id }, { route, seq: v.scheduled }), label(v.stop_id)) },
      { label: 'Scheduled', get: (v) => clockTime(v.scheduled) }, { label: 'Actual', get: (v) => (v.actual == null ? '–' : clockTime(v.actual)) },
      { label: 'Delay', num: true, get: (v) => fmtDelay(v.delay) }, { label: 'Status', get: status }] }));
  }
  return h('div', { class: 'card' }, h('label', { for: pick.id }, 'Inspect a run', pick), holder);
}
