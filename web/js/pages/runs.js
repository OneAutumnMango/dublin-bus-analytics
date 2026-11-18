// Runs view: every run (trip) of one or more routes on one service day, one row per run, scheduled against actual times.
import { api } from '../api.js';
import { renderRunsChart, runsGeometry } from '../charts.js';
import { visitTipData } from '../tipdata.js';
import { dataTable, emptyState, figure, h, link, notice, put, tile } from '../dom.js';
import { buildHash, clockTime, dateTime, describeDelay, fmtCount, fmtDelay, isNum, plural, stopCodeMap, stopLabel, stopNameMap } from '../lib.js';
import { region } from '../view.js';
import { MAX, timeRangeSlider } from '../slider.js';
import { windowLabel } from '../mapdata.js';

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

const WIN_PRESETS = [['Whole day', 0, MAX], ['Early', 240, 420], ['Morning peak', 420, 600], ['Daytime', 600, 960], ['Evening peak', 960, 1140], ['Evening', 1140, MAX]];
const DEFAULT_WIN = [420, 540];

// Epoch of 00:00 Dublin time on `date` (YYYY-MM-DD).
export function dayStart(date) {
  const [y, m, d] = date.split('-').map(Number);
  const utc = Date.UTC(y, m - 1, d) / 1000;
  return utc - Number(clockTime(utc).slice(0, 2)) * 3600; // Dublin is 0 or +1 h at UTC midnight
}

// Minutes since local midnight (can pass 1440 for after-midnight stops of the service day); window end MAX means "to the end of the day".
export const inWindow = (t, ds, [a, b]) => isNum(t) && t >= ds + a * 60 && (b >= MAX || t <= ds + b * 60);
export const runInWindow = (run, ds, win) => run.visits.some((v) => inWindow(v.scheduled, ds, win) || inWindow(v.actual, ds, win));

const winFromQuery = (q) => {
  const a = Number(q.from), b = Number(q.to);
  return q.from !== undefined && q.to !== undefined && Number.isInteger(a) && Number.isInteger(b) && a >= 0 && b <= MAX && a < b ? [a, b] : DEFAULT_WIN;
};

export async function render(ctx) {
  const routes = [...new Set((ctx.query.routes || '').split(/[\s,]+/).filter(Boolean))];
  const date = ctx.query.date || '';
  let win = winFromQuery(ctx.query);
  ctx.title('Runs');
  const routesIn = h('input', { id: 'runs-routes', type: 'text', value: routes.join(', '), placeholder: 'e.g. 4, 15', 'aria-describedby': 'runs-hint', autocomplete: 'off' });
  const dateIn = h('input', { id: 'runs-date', type: 'date', value: date || dateTime(ctx.nowSec() - 86400).slice(0, 10) });
  const query = () => ({ routes: routesIn.value.split(/[\s,]+/).filter(Boolean).join(','), date: dateIn.value, from: win[0], to: win[1] });
  const apply = (ev) => { ev?.preventDefault?.(); ctx.go(buildHash('runs', {}, query())); };
  const redraws = []; // one per route section: redraw from the data already loaded
  const slider = timeRangeSlider({ step: 15, presets: WIN_PRESETS, from: win[0], to: win[1], onChange: (a, b) => {
    win = a === b ? [a, Math.min(a + 60, MAX)] : [Math.min(a, b), Math.max(a, b)];
    if (a >= b) slider.set(win[0], win[1]);
    try { globalThis.history.replaceState(null, '', buildHash('runs', {}, { ...ctx.query, ...query(), routes: routes.join(','), date })); } catch { /* the address bar is a convenience */ }
    redraws.forEach((f) => f());
  } });
  const results = h('div', {});
  put(ctx.main, h('h1', {}, 'Runs'),
    h('p', { class: 'subtitle' }, 'Look at one past service day: every run of a route as a row, with a hollow dot where each stop was scheduled and a filled dot where the bus actually arrived. Times are Dublin time.'),
    h('form', { class: 'row card', on: { submit: apply } },
      h('label', { for: 'runs-routes' }, 'Routes', routesIn), h('label', { for: 'runs-date' }, 'Service day', dateIn),
      h('button', { class: 'btn', type: 'submit' }, 'Show runs'),
      h('p', { class: 'hint', id: 'runs-hint' }, `Up to ${MAX_ROUTES} routes, separated by commas. Only tracked routes have recorded runs.`)),
    h('div', { class: 'card' }, h('span', { class: 'row-label' }, 'Time of day'), slider.el,
      h('p', { class: 'hint' }, 'A run is shown when any of its stops is scheduled or arrives in this window; the diagram covers exactly the window.')), results);
  if (!routes.length && !date) { put(results, emptyState('Pick a route and a day to see its runs.')); return; }
  if (!routes.length) { put(results, notice('Enter at least one route.')); return; }
  if (!DATE_RE.test(date)) { put(results, notice('The day must be a date like 2026-11-12.')); return; }
  if (routes.length > MAX_ROUTES) put(results, notice(`Only the first ${MAX_ROUTES} routes are shown.`));
  const ds = dayStart(date);
  await Promise.all(routes.slice(0, MAX_ROUTES).map((route) => {
    const box = h('div', {});
    const body = h('div', {});
    put(results, h('section', {}, h('h2', {}, `Route ${route}`, h('span', { class: 'hint' }, ` · ${date}`)), box));
    const fetcher = async (signal) => {
      const [data, stops] = await Promise.all([api.runs(route, date, { signal }), api.stopsOf(route, { signal }).catch(() => null)]);
      return { data, stops };
    };
    return region(box, ctx.signal).load(fetcher, ({ data, stops }, el) => {
      el.append(body);
      const paint = () => { body.replaceChildren(); draw(route, date, data, stops, body, ds, win); };
      redraws.push(paint);
      paint();
    }, { announce: ({ data }) => `Route ${route}: ${plural(data.runs.length, 'run')} on the day` });
  }));
}

function draw(route, date, data, stops, el, ds, win) {
  const names = stopNameMap(stops?.directions), codes = stopCodeMap(stops?.directions);
  const label = (id) => stopLabel(id, names, codes);
  if (!data.runs.length) { put(el, emptyState(`No arrivals recorded for route ${route} on ${date}. Was the route tracked that day?`)); return; }
  const when = windowLabel(win[0], win[1]);
  const runs = data.runs.filter((r) => runInWindow(r, ds, win)).map(summarise);
  if (data.truncated) put(el, notice('This day has more arrivals than the API returns at once, so only the earliest are shown.'));
  if (!runs.length) { put(el, emptyState(`The route has ${plural(data.runs.length, 'run')} on ${date}, but none has a stop in ${when}. Move the time slider.`)); return; }
  const visits = runs.flatMap((r) => r.visits);
  const arrived = visits.filter((v) => v.actual != null).length;
  const delays = visits.map((v) => v.delay).filter(isNum);
  put(el, h('div', { class: 'tiles' }, tile('Runs in window', fmtCount(runs.length), { note: `of ${fmtCount(data.runs.length)} that day` }), tile('Arrivals recorded', fmtCount(arrived), { note: `of ${fmtCount(visits.length)} stops visited` }),
    tile('Mean delay', fmtDelay(mean(delays)), { note: describeDelay(mean(delays)) }), tile('Worst delay', fmtDelay(delays.length ? Math.max(...delays) : null))));
  const table = (rs, caption) => dataTable({ caption, rows: rs, columns: [
    { label: 'Run', get: (r) => r.trip_id }, { label: 'First stop scheduled', get: (r) => clockTime(r.first) }, { label: 'Last stop scheduled', get: (r) => clockTime(r.last) },
    { label: 'Stops', num: true, get: (r) => fmtCount(r.stops) }, { label: 'No arrival', num: true, get: (r) => fmtCount(r.missing) },
    { label: 'Mean delay', num: true, get: (r) => fmtDelay(r.delay) }, { label: 'Worst delay', num: true, get: (r) => fmtDelay(r.worst) }] });
  const t1 = win[1] >= MAX ? Math.max(...visits.flatMap((v) => [v.scheduled, v.actual]).filter(isNum), ds + MAX * 60) : ds + win[1] * 60;
  const geo = runsGeometry(runs.map((r) => ({ ...r, label: `${clockTime(r.first)}${r.direction ? ` · ${r.direction}` : ''}`,
    visits: r.visits.map((v) => ({ ...v, tip: visitTipData(v, label(v.stop_id), r.trip_id, status(v), clockTime) })) })), { domain: [ds + win[0] * 60, t1] });
  const insp = inspector(route, runs, label);
  const svg = renderRunsChart(geo, { onSelect: insp.select, title: `Runs of route ${route}`, desc: `${plural(runs.length, 'run')} on ${date} in ${when}; each row is one run with a dot per stop at its scheduled and actual time.` });
  put(el, figure({ title: `${plural(runs.length, 'run')} in ${when}, by first scheduled time`, svg, table: table(runs, `Runs of route ${route}, ${date}, ${when}`), level: 'h3',
    legend: h('p', { class: 'legend' }, 'One row per run. Each stop is a hollow dot at its scheduled time and a filled dot at the time the bus actually arrived, joined by a bar that shows the delay. Blue: early, orange: late, green: on time. A hollow dot alone: no arrival recorded. Hover a dot for the stop, click to inspect the run. Scroll sideways for the rest of the window.') }));
  put(el, insp.el);
}

// Pick one run to see every stop: scheduled, actual, delay and how sure we are.
function inspector(route, runs, label) {
  const holder = h('div', {});
  const pick = h('select', { id: `runs-pick-${route}`, on: { change: (ev) => show(Number(ev.target.value)) } },
    h('option', { value: '-1' }, 'Choose a run…'), runs.map((r, i) => h('option', { value: String(i) }, `${clockTime(r.first)} · ${r.trip_id} · ${fmtDelay(r.delay)}`)));
  const card = h('div', { class: 'card' }, h('label', { for: pick.id }, 'Inspect a run (or click a run in the diagram)', pick), holder);
  function show(i) {
    pick.value = String(i);
    holder.replaceChildren();
    if (i < 0) return;
    const r = runs[i];
    put(holder, dataTable({ caption: `Stops of run ${r.trip_id}`, rows: r.visits, columns: [
      { label: 'Stop', get: (v) => link(buildHash('trip', { trip: r.trip_id, stop: v.stop_id }, { route, seq: v.scheduled }), label(v.stop_id)) },
      { label: 'Scheduled', get: (v) => clockTime(v.scheduled) }, { label: 'Actual', get: (v) => (v.actual == null ? '–' : clockTime(v.actual)) },
      { label: 'Delay', num: true, get: (v) => fmtDelay(v.delay) }, { label: 'Status', get: status }] }));
  }
  return { el: card, select: (trip) => { show(runs.findIndex((r) => r.trip_id === trip)); card.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' }); } };
}
