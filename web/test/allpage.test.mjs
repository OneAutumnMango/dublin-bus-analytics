import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, installFetch, fail, settle, FNode } from './fakedom.mjs';

installDom();
const { sortRoutes, render } = await import('../js/pages/all.js');
const { buildHash } = await import('../js/lib.js');

const ctxOf = (query = {}) => ({ main: new FNode('main'), params: {}, query, signal: new AbortController().signal, title() {}, go() {}, reload() {}, nowSec: () => 1_700_000_000 });
const filters = (over = {}) => ({ ci: 0.95, pct_lo: 0.05, pct_hi: 0.95, since: 1_690_000_000, until: null, min_confidence: 1, truncated: false, ...over });
const R = (route, median, over = {}) => ({ route, n: median === null ? 0 : 30, low_n: false, mean: median, median, on_time_pct: 80, reliability: 70, p_low: -60, p_high: 300, ...over });
const ROUTES = { filters: filters(), routes: [R('15', 60), R('46A', 180), R('empty', null)] };
const HEAT = { filters: filters(), n: 30, cells: [{ weekday: 0, hour: 8, n: 30, mean_delay: 120 }] };
const FC = { filters: filters(), horizons: [{ horizon_min: 5, n: 30, low_n: false, mean_error: 10, mae: 20, p_low: -60, p_high: 90 }] };
const stubs = (over = {}) => installFetch({ 'GET /api/all/routes': ROUTES, 'GET /api/all/heatmap': HEAT, 'GET /api/all/forecast': FC, ...over });
const mount = async (query) => { const ctx = ctxOf(query); await render(ctx); await settle(); return ctx; };
const rowsOf = (ctx) => ctx.main.byTag('tbody')[0].byTag('tr').map((tr) => tr.byTag('a')[0]?.textContent);

test('sortRoutes: nulls last both ways, numeric route order, no mutation', () => {
  const l = [R('a', 5), R('b', null), R('c', 9)];
  assert.deepEqual(sortRoutes(l, 'median', 'desc').map((r) => r.route), ['c', 'a', 'b']);
  assert.deepEqual(sortRoutes(l, 'median', 'asc').map((r) => r.route), ['a', 'c', 'b']);
  const rs = [R('46A', 1), R('9', 1), R('15', 1)];
  assert.deepEqual(sortRoutes(rs, 'route', 'asc').map((r) => r.route), ['9', '15', '46A']);
  assert.deepEqual(rs.map((r) => r.route), ['46A', '9', '15']);
});

test('all page: heading, hint, default sort, no data row, links, header toggle', async () => {
  const data = { ...ROUTES, routes: [R('15', 60), R('sA/B', 120), R('empty', null)] };
  stubs({ 'GET /api/all/routes': data });
  const ctx = await mount();
  assert.equal(ctx.main.byTag('h1')[0].textContent, 'All tracked routes');
  assert.match(ctx.main.textContent, /Pooled by arrival, not by route\./);
  assert.deepEqual(rowsOf(ctx), ['sA/B', '15', 'empty']);
  const emptyRow = ctx.main.byTag('tbody')[0].byTag('tr')[2];
  assert.match(emptyRow.textContent, /no data/);
  const q = {};
  const hrefs = ctx.main.byTag('a').map((a) => a.getAttribute('href'));
  assert.ok(hrefs.includes(buildHash('analytics', { route: 'sA/B' }, q)));
  assert.ok(hrefs.includes('#/analytics/sA%2FB'));
  const th = () => ctx.main.byTag('th').find((t) => t.getAttribute('aria-sort') !== null && t.getAttribute('aria-sort') !== 'none');
  assert.equal(th().getAttribute('aria-sort'), 'descending');
  ctx.main.byText('button', /^Median/)[0].dispatch('click');
  assert.equal(th().getAttribute('aria-sort'), 'ascending');
  assert.deepEqual(rowsOf(ctx), ['15', 'sA/B', 'empty']);
});

test('all page: no tracked routes', async () => {
  stubs({ 'GET /api/all/routes': { filters: filters(), routes: [] } });
  const ctx = await mount();
  assert.match(ctx.main.textContent, /No routes tracked/);
});

test('all page: heatmap and forecast requests drop their own params', async () => {
  const calls = stubs();
  await mount({ wd: '1,2', hf: '8', ht: '9' });
  const q = (k) => new URL(calls.find((c) => c.key === k).url, 'http://x').searchParams;
  assert.ok(!q('GET /api/all/heatmap').has('weekdays') && !q('GET /api/all/heatmap').has('hours'));
  assert.ok(!q('GET /api/all/forecast').has('pct_lo') && !q('GET /api/all/forecast').has('pct_hi'));
});

test('all page: truncated routes named in a notice', async () => {
  stubs({ 'GET /api/all/routes': { ...ROUTES, filters: filters({ truncated_routes: ['15'] }) } });
  const ctx = await mount();
  assert.match(ctx.main.textContent, /500,000[^]*15|15[^]*500,000/);
  assert.ok(ctx.main.all((n) => n.tag === 'div' && /per-route/.test(n.textContent) && /15/.test(n.textContent)).length);
});

test('all page: one failing endpoint only breaks its own section', async () => {
  stubs({ 'GET /api/all/heatmap': fail(500, 'boom') });
  const ctx = await mount();
  assert.equal(ctx.main.byText('button', /^Retry$/).length, 1);
  assert.match(ctx.main.textContent, /HTTP 500/);
  assert.equal(ctx.main.byText('button', /^Median/).length, 1);
  assert.match(ctx.main.textContent, /How accurate are the live predictions/);
});
