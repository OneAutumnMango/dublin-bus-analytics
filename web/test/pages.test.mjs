import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, installFetch, fail, settle, FNode } from './fakedom.mjs';

const { main: MAIN } = installDom();
const ctxOf = (params, query = {}) => {
  const main = new FNode('main');
  const gos = [];
  return { main, params, query, gos, signal: new AbortController().signal, title() {}, go: (x) => gos.push(x), reload() {}, nowSec: () => 1_700_000_000 };
};
const text = (n) => n.textContent;
const NOTE = /Fewer than 10/;

const stat = (id, over = {}) => ({ stop_id: id, position: 0, direction: 'out', directions: ['out'], n: 30, low_n: false, mean: 120, min: -60, max: 600, std: 50, median: 100,
  ci_low: 100, ci_high: 140, p_low: -30, p_high: 400, on_time_pct: 80, reliability: 70, ...over });
const filters = (over = {}) => ({ ci: 0.95, pct_lo: 0.05, pct_hi: 0.95, window_lo: -60, window_hi: 300, since: 1_690_000_000, until: null, weekdays: null, hours: null, min_confidence: 1, truncated: false, realtime_only: true, ...over });
const stopsResp = { route: '15', directions: { out: [{ id: 's1', name: '<img src=x onerror=alert(1)>' }, { id: 's2', name: 'Two' }], back: [{ id: 's2' }] } };

test('routes page: lists tracked routes, untrack, search + track, status text', async () => {
  const tracked = ['15', '15/A'];
  const calls = installFetch({
    'GET /api/health': { ok: true }, 'GET /api/routes/tracked': () => ({ routes: [...tracked] }),
    'GET /api/routes/15/summary': { n: 1200, stops: [{}, {}], filters: filters() }, 'GET /api/routes/15%2FA/summary': { n: 0, stops: [], filters: filters() },
    'DELETE /api/routes/tracked/15%2FA': () => { tracked.splice(1, 1); return { routes: [...tracked] }; },
    'PUT /api/routes/tracked/46A': () => { tracked.push('46A'); return { routes: [...tracked] }; },
    'GET /api/routes': [{ id: 'r1', shortName: '46A', longName: 'Phoenix Park <b>x</b>' }, { id: 'r2', shortName: '15', longName: 'Ballycullen' }],
  });
  const ctx = ctxOf({});
  await (await import('../js/pages/routes.js')).render(ctx);
  await settle();
  const links = ctx.main.byTag('a').map((a) => a.getAttribute('href'));
  assert.ok(links.includes('#/route/15%2FA'));
  assert.ok(links.includes('#/analytics/15'));
  assert.match(text(ctx.main), /1,200 arrivals logged/);
  assert.match(text(ctx.main), /No arrivals logged yet/);
  assert.equal(ctx.main.byTag('h1').length, 1);
  ctx.main.byText('button', /^Untrack$/)[1].dispatch('click');
  await settle();
  assert.ok(!text(ctx.main).includes('Route 15/A'));
  const form = ctx.main.byTag('form')[0];
  ctx.main.byTag('input')[0].value = '46';
  form.dispatch('submit');
  await settle();
  assert.ok(calls.some((c) => c.url === '/api/routes?q=46'));
  assert.match(text(ctx.main), /Phoenix Park <b>x<\/b>/); // shown literally as text
  assert.equal(ctx.main.byText('span', /^Tracked$/).length, 1); // 15 already tracked
  ctx.main.byText('button', /^Track$/)[0].dispatch('click');
  await settle();
  assert.match(text(ctx.main), /Route 46A/);
});

test('routes page: unreachable API shows banner with retry and no crash', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  const ctx = ctxOf({});
  await (await import('../js/pages/routes.js')).render(ctx);
  assert.match(text(ctx.main), /unreachable/);
  assert.ok(ctx.main.byText('button', /Retry/).length === 1);
  assert.match(text(ctx.main), /unavailable while the API/);
});

test('routes page: backup button shows the file and updates the download link', async () => {
  installFetch({ 'GET /api/health': { ok: true }, 'GET /api/routes/tracked': { routes: [] }, 'POST /api/backup': { file: 'b 1.tar', size: 2048 } });
  const ctx = ctxOf({});
  await (await import('../js/pages/routes.js')).render(ctx);
  assert.match(text(ctx.main), /No routes tracked yet/);
  const btn = ctx.main.byText('button', /Run backup now/)[0];
  btn.dispatch('click', { currentTarget: btn });
  await settle();
  assert.match(text(ctx.main), /Backup b 1.tar created \(2 KiB\)/);
  assert.equal(ctx.main.byText('a', /Download latest/)[0].getAttribute('href'), '/api/backup/download?file=b%201.tar');
});

const routePage = () => import('../js/pages/route.js');

test('route page: chart rows, links, low_n greyed, names as text, truncated + order notices', async () => {
  const calls = installFetch({ 'GET /api/routes/15/summary': { route: '15', filters: filters({ truncated: true, since: 1_699_000_000 }), order_source: 'none', n: 40, low_n: false,
    stops: [stat('s1', { position: null, directions: [] }), stat('s2', { n: 3, low_n: true, std: null, ci_low: null, ci_high: null, directions: ['out', 'back'], direction: null }),
      { stop_id: 's3', position: null, direction: null, directions: [], n: 0, low_n: true, mean: null, min: null, max: null, std: null, median: null, ci_low: null, ci_high: null, p_low: null, p_high: null, on_time_pct: null, reliability: null }] },
  'GET /api/routes/15/stops': stopsResp });
  const ctx = ctxOf({ route: '15' }, { ci: '90' });
  await (await routePage()).render(ctx);
  assert.ok(calls.some((c) => c.url.includes('/summary?') && /ci=0\.9&/.test(c.url) && /since=\d+/.test(c.url)), 'explicit since is always sent');
  assert.match(text(ctx.main), /most recent rows only/);
  assert.match(text(ctx.main), /Stop order is unavailable/);
  assert.equal(ctx.main.byTag('h1').length, 1);
  const svg = ctx.main.byTag('svg')[0];
  assert.equal(svg.getAttribute('role'), 'img');
  assert.ok(svg.byTag('title').length >= 2 && svg.byTag('desc').length === 1);
  assert.ok(svg.all((n) => n.tag === 'a').some((a) => a.getAttribute('href').startsWith('#/stop/15/s2')));
  assert.equal(svg.all((n) => n.tag === 'g' && /low-n/.test(n.getAttribute('class') || '')).length, 2);
  assert.ok(svg.byTag('title').some((t) => NOTE.test(t.textContent)));
  assert.ok(text(ctx.main).includes('<img src=x onerror=alert(1)>'));
  assert.match(text(ctx.main), /–/); // null stats formatted, not "null"/"NaN"
  assert.ok(!/NaN|undefined|null/.test(text(ctx.main)));
  assert.equal(ctx.main.byText('button', /Show data table/).length, 1);
  assert.equal(ctx.main.byTag('select').filter((s) => s.getAttribute('id') === 'dir').length, 1, 'direction selector when several directions');
});

test('route page: single direction hides selector; empty; 422 error with retry; 502', async () => {
  installFetch({ 'GET /api/routes/15/summary': { route: '15', filters: filters(), order_source: 'server', n: 30, low_n: false, stops: [stat('s1')] }, 'GET /api/routes/15/stops': fail(502, 'down') });
  let ctx = ctxOf({ route: '15' });
  await (await routePage()).render(ctx);
  assert.equal(ctx.main.byTag('select').filter((s) => s.getAttribute('id') === 'dir').length, 0);
  assert.equal(ctx.main.byTag('svg').length, 1);
  installFetch({ 'GET /api/routes/15/summary': { route: '15', filters: filters(), order_source: 'server', n: 0, low_n: true, stops: [] } });
  ctx = ctxOf({ route: '15' });
  await (await routePage()).render(ctx);
  assert.match(text(ctx.main), /No confirmed arrivals/);
  assert.equal(ctx.main.byTag('svg').length, 0);
  installFetch({ 'GET /api/routes/15/summary': fail(422, 'ci must satisfy 0 < ci <= 0.9999') });
  ctx = ctxOf({ route: '15' });
  await (await routePage()).render(ctx);
  assert.match(text(ctx.main), /Invalid request: ci must satisfy/);
  assert.equal(ctx.main.byText('button', /Retry/).length, 1);
  installFetch({ 'GET /api/routes/15/summary': fail(502, 'server unavailable: x') });
  ctx = ctxOf({ route: '15' });
  await (await routePage()).render(ctx);
  assert.match(text(ctx.main), /bus data server is unavailable/);
});

test('route page: pagination slices stops', async () => {
  const stops = Array.from({ length: 95 }, (_, i) => stat(`s${i}`, { position: i }));
  installFetch({ 'GET /api/routes/15/summary': { route: '15', filters: filters(), order_source: 'server', n: 999, low_n: false, stops } });
  const ctx = ctxOf({ route: '15' }, { page: '3' });
  await (await routePage()).render(ctx);
  assert.match(text(ctx.main), /Stops 81–95 of 95/);
  assert.equal(ctx.main.byTag('svg')[0].all((n) => n.tag === 'g' && n.getAttribute('class').startsWith('row')).length, 15);
  const ctx2 = ctxOf({ route: '15' }, { page: '999' });
  await (await routePage()).render(ctx2);
  assert.match(text(ctx2.main), /Stops 81–95 of 95/);
});

test('filters form: apply navigates with only non-default state; invalid input blocks', async () => {
  installFetch({ 'GET /api/routes/15/summary': { route: '15', filters: filters(), order_source: 'server', n: 30, low_n: false, stops: [stat('s1')] } });
  const ctx = ctxOf({ route: '15' });
  await (await routePage()).render(ctx);
  const form = ctx.main.byTag('form')[0];
  const ci = ctx.main.byTag('input').find((i) => /-ci$/.test(i.getAttribute('id')));
  ci.value = '90';
  form.dispatch('submit');
  assert.deepEqual(ctx.gos, ['#/route/15?ci=90']);
  ci.value = '150';
  form.dispatch('submit');
  assert.equal(ctx.gos.length, 1);
  assert.match(text(form), /Confidence level must be/);
  assert.equal(form.byTag('div').find((d) => d.getAttribute('role') === 'alert').hasAttribute('hidden'), false);
  ci.value = '95';
  ctx.main.byText('button', /^Reset$/)[0].dispatch('click');
  assert.equal(ctx.gos.length, 2);
  assert.equal(ctx.gos[1], '#/route/15');
});

test('invalid link values fall back to defaults with a notice', async () => {
  installFetch({ 'GET /api/routes/15/summary': { route: '15', filters: filters(), order_source: 'server', n: 30, low_n: false, stops: [stat('s1')] } });
  const ctx = ctxOf({ route: '15' }, { ci: 'abc', wd: '9' });
  await (await routePage()).render(ctx);
  assert.match(text(ctx.main), /Ignored invalid "ci"/);
});

const hist = () => { const b = [{ lo: null, hi: -600, count: 0 }]; for (let lo = -600; lo < 1800; lo += 60) b.push({ lo, hi: lo + 60, count: lo === 0 ? 20 : lo === 60 ? 10 : 0 }); b.push({ lo: 1800, hi: null, count: 0 }); return b; };
const stopStats = (over = {}) => ({ stop_id: 's1', route: '15', filters: filters(), n: 30, low_n: false,
  delay: { mean: 90, min: -30, max: 400, std: 50, median: 80, ci_low: 60, ci_high: 120, p_low: -20, p_high: 300 }, on_time_pct: 85, reliability: 72, histogram: hist(),
  recent: [{ trip_id: 't/1', route: '15', seq: 1_700_000_000, service_date: '20231114', actual: 1_700_000_090, delay: 90, confidence: 1, scheduled: 1_700_000_000 }], ...over });

test('stop page: CI sentence, histogram, recent trips link, null-safe', async () => {
  installFetch({ 'GET /api/stops/s1/stats': stopStats(), 'GET /api/routes/15/stops': stopsResp });
  const { render } = await import('../js/pages/stop.js');
  const ctx = ctxOf({ route: '15', stop: 's1' }, { ci: '0.99' });
  await render(ctx);
  assert.match(text(ctx.main), /Usually arrives 1\.0 to 2\.0 min late vs scheduled \(95% confidence interval of the mean delay, n=30\)/);
  assert.match(text(ctx.main), /90% of buses arrive 20 to 5\.0 min|90% of buses arrive/);
  const hrefs = ctx.main.byTag('a').map((a) => a.getAttribute('href'));
  assert.ok(hrefs.some((h) => h.startsWith('#/trip/t%2F1/s1?seq=1700000000&route=15')));
  const svg = ctx.main.byTag('svg')[0];
  assert.equal(svg.byTag('rect').filter((r) => /bar/.test(r.getAttribute('class'))).length, 42);
  assert.ok(svg.byTag('line').some((l) => /zero-line/.test(l.getAttribute('class'))));
  assert.ok(!/NaN|undefined|null/.test(text(ctx.main)));
  installFetch({ 'GET /api/stops/s1/stats': stopStats({ n: 1, low_n: true, delay: { mean: 90, min: 90, max: 90, std: null, median: 90, ci_low: null, ci_high: null, p_low: 90, p_high: 90 }, reliability: null, on_time_pct: 100 }) });
  const c2 = ctxOf({ route: '15', stop: 's1' });
  await render(c2);
  assert.ok(!/NaN|undefined|null/.test(text(c2.main)));
  assert.ok(c2.main.all((n) => /tile low-n/.test(n.getAttribute?.('class') || '')).length >= 3);
  installFetch({ 'GET /api/stops/s1/stats': stopStats({ n: 0, low_n: true, histogram: hist(), recent: [], delay: Object.fromEntries(['mean', 'min', 'max', 'std', 'median', 'ci_low', 'ci_high', 'p_low', 'p_high'].map((k) => [k, null])), on_time_pct: null, reliability: null }) });
  const c3 = ctxOf({ route: '15', stop: 's1' });
  await render(c3);
  assert.match(text(c3.main), /No confirmed arrivals at this stop/);
});

test('stop page: 404-style error is shown inline', async () => {
  installFetch({ 'GET /api/stops/zz/stats': fail(422, 'since must be <= until') });
  const ctx = ctxOf({ route: '15', stop: 'zz' });
  await (await import('../js/pages/stop.js')).render(ctx);
  assert.match(text(ctx.main), /since must be/);
});

const series = (over = {}) => ({ trip_id: 't1', stop_id: 's1', truncated: false, visits: [
  { seq: 1_700_000_000, scheduled: 1_700_000_000, last_seen_ts: 1_700_000_200, arrival: { actual: 1_700_000_150, delay: 150, confidence: 1, realtime: 1, service_date: '20231114' },
    points: [{ ts: 1_699_999_900, due_min: 5, expected: 1_700_000_200, realtime: 1 }, { ts: 1_700_000_100, due_min: 1, expected: 1_700_000_150, realtime: 1 }] },
  { seq: 1_700_003_600, scheduled: 1_700_003_600, last_seen_ts: null, arrival: null, points: [] }], ...over });

test('trip page: step chart, markers, visit selector, dropped bus, empty points', async () => {
  installFetch({ 'GET /api/trips/t1/series': series() });
  const { render } = await import('../js/pages/trip.js');
  const ctx = ctxOf({ trip: 't1', stop: 's1' }, { seq: '1700000000', route: '15' });
  await render(ctx);
  const svg = ctx.main.byTag('svg')[0];
  assert.ok(svg.byTag('path').some((p) => /step-line/.test(p.getAttribute('class')) && p.getAttribute('d').startsWith('M')));
  assert.equal(svg.byTag('line').filter((l) => /marker/.test(l.getAttribute('class'))).length, 2);
  assert.match(text(ctx.main), /arrived 2[0-9]:\d\d/);
  const sel = ctx.main.byTag('select')[0];
  assert.equal(sel.byTag('option').length, 2);
  sel.value = '1';
  sel.dispatch('change', { target: sel });
  assert.match(text(ctx.main), /disappeared from the feed/);
  assert.match(text(ctx.main), /no stored prediction samples/);
  assert.equal(ctx.main.byTag('svg').length, 0);
  assert.ok(ctx.main.byText('a', /Stop s1, route 15/).length === 1);
  installFetch({ 'GET /api/trips/t1/series': fail(404, 'no such trip/stop visit') });
  const c2 = ctxOf({ trip: 't1', stop: 's1' });
  await render(c2);
  assert.match(text(c2.main), /Not found: no such trip/);
  installFetch({ 'GET /api/trips/t1/series': series({ truncated: true, visits: [series().visits[0]] }) });
  const c3 = ctxOf({ trip: 't1', stop: 's1' });
  await render(c3);
  assert.match(text(c3.main), /Some visits or samples were left out/);
  assert.equal(c3.main.byTag('select').length, 0);
});

const heat = { route: '15', filters: filters(), n: 500, cells: [{ weekday: 0, hour: 8, n: 100, low_n: false, mean_delay: 300 }, { weekday: 5, hour: 3, n: 2, low_n: true, mean_delay: -120 }] };
const acc = (groups, order_source = 'server') => ({ route: '15', filters: filters(), order_source, groups });
const stopRows = [{ stop_id: 's1', position: 0, mean_delay: 0, segment_delta: null, n: 30, n_delta: 0, low_n: false, low_n_delta: true }, { stop_id: 's2', position: 1, mean_delay: 90, segment_delta: 90, n: 28, n_delta: 25, low_n: false, low_n_delta: false }];
const fc = { route: '15', filters: filters(), horizons: [30, 20, 10, 5, 2].map((h) => ({ horizon_min: h, n: h === 2 ? 0 : 50, low_n: h === 2, mean_error: h === 2 ? null : 60, mae: h === 2 ? null : 90, p_low: h === 2 ? null : -100, p_high: h === 2 ? null : 200 })) };

test('analytics page: heatmap, accumulation groups incl. fallback, forecast', async () => {
  installFetch({ 'GET /api/routes/15/heatmap': heat, 'GET /api/routes/15/stops': stopsResp, 'GET /api/routes/15/forecast': fc,
    'GET /api/routes/15/accumulation': acc([{ direction: 'out', fallback: false, n: 40, low_n: false, n_delta: 25, stops: stopRows }, { direction: null, fallback: true, n: 7, low_n: true, n_delta: 0, stops: [] }]) });
  const ctx = ctxOf({ route: '15' });
  await (await import('../js/pages/analytics.js')).render(ctx);
  const t = text(ctx.main);
  assert.equal(ctx.main.byTag('h1').length, 1);
  assert.equal(ctx.main.byTag('svg').length, 3);
  assert.equal(ctx.main.byTag('svg')[0].all((n) => n.getAttribute?.('tabindex') === '0').length, 168);
  assert.match(t, /Direction: out · 40 trips/);
  assert.match(t, /Unmatched trips · 7 trips/);
  assert.match(t, /could not be matched/);
  assert.match(t, /30 min before/);
  assert.ok(!/NaN|undefined|null/.test(t));
  assert.equal(ctx.main.byText('button', /Show data table/).length, 3);
});

test('analytics page: each section fails independently; order none; empty', async () => {
  installFetch({ 'GET /api/routes/15/heatmap': { route: '15', filters: filters(), n: 0, cells: [] }, 'GET /api/routes/15/forecast': fail(502, 'x'),
    'GET /api/routes/15/accumulation': acc([], 'none') });
  const ctx = ctxOf({ route: '15' });
  await (await import('../js/pages/analytics.js')).render(ctx);
  const t = text(ctx.main);
  assert.match(t, /No data for this selection/);
  assert.match(t, /needs the stop order/);
  assert.match(t, /bus data server is unavailable/);
  assert.equal(ctx.main.byText('button', /^Retry$/).length, 1);
});

test('router: stale responses from a superseded navigation are ignored; unknown route; error', async () => {
  const gate = {};
  installFetch({
    'GET /api/routes/slow/summary': () => new Promise((r) => { gate.slow = () => r({ route: 'slow', filters: filters(), order_source: 'server', n: 30, low_n: false, stops: [stat('SLOWSTOP')] }); }),
    'GET /api/routes/fast/summary': { route: 'fast', filters: filters(), order_source: 'server', n: 30, low_n: false, stops: [stat('FASTSTOP')] },
  });
  const { main } = installDom();
  globalThis.location.hash = '#/route/slow';
  await import('../js/app.js');
  const fire = () => (globalThis.__listeners.hashchange || []).forEach((f) => f());
  await settle();
  globalThis.location.hash = '#/route/fast';
  fire();
  await settle(40);
  gate.slow();
  await settle(40);
  const t = text(main);
  assert.match(t, /FASTSTOP/);
  assert.ok(!t.includes('SLOWSTOP'), 'stale response must not render');
  assert.match(t, /Route fast/);
  globalThis.location.hash = '#/nothing';
  fire();
  await settle();
  assert.match(text(main), /Page not found/);
  globalThis.location.hash = '#/route/%E0%A4%A';
  fire();
  await settle();
  assert.match(text(main), /Page not found/);
});
