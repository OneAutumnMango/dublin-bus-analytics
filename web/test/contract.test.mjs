// Contract test against a running server. Skipped unless BASE_URL is set:
//   BASE_URL=http://127.0.0.1:8765 ROUTE=15 STOP=15-S02 node --test web/test/contract.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';

const BASE = process.env.BASE_URL;
const ROUTE = process.env.ROUTE || '15';
const opts = { skip: BASE ? false : 'BASE_URL not set' };
const get = async (p) => {
  const r = await fetch(BASE + p);
  assert.equal(r.status, 200, `${p} -> ${r.status}`);
  return r.json();
};
const has = (o, keys, what) => keys.forEach((k) => assert.ok(k in o, `${what} lacks ${k}`));
const FILT = ['ci', 'pct_lo', 'pct_hi', 'window_lo', 'window_hi', 'since', 'until', 'weekdays', 'hours', 'min_confidence', 'truncated'];
const q = '?since=0';
const enc = encodeURIComponent;

test('static assets and headers', opts, async () => {
  for (const p of ['/', '/js/app.js', '/css/style.css']) {
    const r = await fetch(BASE + p);
    assert.equal(r.status, 200, p);
    assert.match(r.headers.get('content-security-policy'), /default-src 'self'/);
    assert.equal(r.headers.get('x-content-type-options'), 'nosniff');
  }
});

test('health, tracked, routes search', opts, async () => {
  assert.equal((await get('/api/health')).ok, true);
  assert.ok(Array.isArray((await get('/api/routes/tracked')).routes));
  const rs = await fetch(`${BASE}/api/routes?q=1`);
  assert.ok([200, 502].includes(rs.status));
});

test('summary', opts, async () => {
  const d = await get(`/api/routes/${enc(ROUTE)}/summary${q}`);
  has(d, ['route', 'filters', 'order_source', 'n', 'low_n', 'stops'], 'summary');
  has(d.filters, FILT, 'filters');
  assert.ok(['server', 'none'].includes(d.order_source));
  for (const s of d.stops) has(s, ['stop_id', 'position', 'direction', 'directions', 'n', 'low_n', 'mean', 'min', 'max', 'p_low', 'p_high', 'ci_low', 'ci_high', 'on_time_pct', 'reliability'], 'stop');
});

test('stop stats, trip series', opts, async () => {
  const sum = await get(`/api/routes/${enc(ROUTE)}/summary${q}`);
  if (!sum.stops.length) return;
  const stop = process.env.STOP || sum.stops[0].stop_id;
  const d = await get(`/api/stops/${enc(stop)}/stats${q}&route=${enc(ROUTE)}`);
  has(d, ['stop_id', 'filters', 'n', 'low_n', 'delay', 'on_time_pct', 'reliability', 'histogram', 'recent'], 'stop stats');
  has(d.delay, ['mean', 'min', 'max', 'median', 'ci_low', 'ci_high', 'p_low', 'p_high'], 'delay');
  has(d.histogram[0], ['lo', 'hi', 'count'], 'bin');
  assert.equal(d.histogram[0].lo, null);
  if (!d.recent.length) return;
  const r = d.recent[0];
  has(r, ['trip_id', 'seq', 'scheduled', 'actual', 'delay', 'confidence'], 'recent');
  const s = await get(`/api/trips/${enc(r.trip_id)}/series?stop=${enc(stop)}&seq=${r.seq}`);
  has(s, ['trip_id', 'stop_id', 'truncated', 'visits'], 'series');
  has(s.visits[0], ['seq', 'scheduled', 'arrival', 'last_seen_ts', 'points'], 'visit');
  if (s.visits[0].points.length) has(s.visits[0].points[0], ['ts', 'due_min', 'expected', 'realtime'], 'point');
});

test('heatmap, accumulation, forecast', opts, async () => {
  const h = await get(`/api/routes/${enc(ROUTE)}/heatmap${q}`);
  has(h, ['cells', 'n', 'filters'], 'heatmap');
  if (h.cells.length) has(h.cells[0], ['weekday', 'hour', 'n', 'low_n', 'mean_delay'], 'cell');
  const a = await get(`/api/routes/${enc(ROUTE)}/accumulation${q}`);
  has(a, ['order_source', 'groups', 'filters'], 'accumulation');
  for (const g of a.groups) {
    has(g, ['direction', 'fallback', 'n', 'low_n', 'n_delta', 'stops'], 'group');
    for (const s of g.stops) has(s, ['stop_id', 'position', 'mean_delay', 'segment_delta', 'n', 'n_delta', 'low_n'], 'acc stop');
  }
  const f = await get(`/api/routes/${enc(ROUTE)}/forecast${q}`);
  assert.deepEqual(f.horizons.map((x) => x.horizon_min), [30, 20, 10, 5, 2]);
  has(f.horizons[0], ['n', 'low_n', 'mean_error', 'mae', 'p_low', 'p_high'], 'horizon');
});

test('invalid parameters give 422 with a detail string', opts, async () => {
  const r = await fetch(`${BASE}/api/routes/${enc(ROUTE)}/summary?ci=5`);
  assert.equal(r.status, 422);
  assert.equal(typeof (await r.json()).detail, 'string');
});
