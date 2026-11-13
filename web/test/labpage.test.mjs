import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, installFetch, fail, settle, FNode } from './fakedom.mjs';

installDom();
const { render } = await import('../js/pages/lab.js');

const ctxOf = (route = '15') => ({ main: new FNode('main'), params: { route }, query: {}, signal: new AbortController().signal, title() {}, go() {}, reload() {}, nowSec: () => 1_700_000_000 });
const filters = (over = {}) => ({ ci: 0.95, since: 1_690_000_000, until: null, min_confidence: 1, truncated: false, ...over });
const T = (value, ci = true) => ({ value, ci_low: ci ? value - 1 : null, ci_high: ci ? value + 1 : null });
const DIST = { n: 50, days: 10, low_n: false, enough_days: true, filters: filters(),
  ecdf: [{ x: -60, p: 0.1 }, { x: 0, p: 0.5 }, { x: 120, p: 0.96 }], qq: [{ z: -1, y: -60 }, { z: 0, y: 0 }, { z: 1, y: 100 }, { z: 2, y: 200 }],
  tiles: { median: T(0), p95: T(300), buffer: T(240), skew: T(1.2), kurtosis: T(3) } };
const PERS = { order_source: 'server', n_trips: 40, days: 10, low_n: false, enough_days: true, filters: filters(),
  lags: [1, 2, 3].map((lag) => ({ lag, r: 0.5 / lag, n: 30, low_n: false, ci_low: 0.1, ci_high: 0.6 })), recovery: { slope: 0.5, n: 30, low_n: false, ci_low: 0.3, ci_high: 0.7 } };
const VAR = { n: 50, days: 10, low_n: false, enough_days: true, filters: filters(),
  factors: ['hour', 'weekday', 'stop', 'date'].map((factor, i) => ({ factor, omega2: 0.1 * (i + 1), ci_low: 0.05, ci_high: 0.5, levels: 5 })) };
const url = (route, k) => `GET /api/routes/${encodeURIComponent(route)}/lab/${k}`;
const stubs = (over = {}, route = '15') => installFetch({ [url(route, 'distribution')]: DIST, [url(route, 'persistence')]: PERS, [url(route, 'variance')]: VAR, ...over });
const mount = async (route) => { const ctx = ctxOf(route); await render(ctx); await settle(); return ctx; };
const sections = (ctx) => ctx.main.all((n) => n.tag === 'section' && n.getAttribute('class') === 'analytics-section');

test('lab renders three sections with explanations', async () => {
  stubs();
  const ctx = await mount();
  const s = sections(ctx);
  assert.equal(s.length, 3);
  for (const sec of s) {
    assert.equal(sec.byTag('h2').length, 1);
    const d = sec.byTag('details');
    assert.equal(d.length, 1);
    assert.ok(d[0].hasAttribute('open'));
    assert.ok(sec.byTag('svg').length >= 1);
    assert.ok(sec.byTag('table').length >= 1);
    assert.ok(sec.byTag('p').some((p) => p.getAttribute('class') === 'reading' && p.textContent.length > 10));
  }
});

test('lab not enough days message', async () => {
  const noCi = { ...DIST, enough_days: false, tiles: Object.fromEntries(Object.entries(DIST.tiles).map(([k, v]) => [k, T(v.value, false)])) };
  stubs({ [url('15', 'distribution')]: noCi, [url('15', 'persistence')]: { ...PERS, enough_days: false, lags: PERS.lags.map((l) => ({ ...l, ci_low: null, ci_high: null })), recovery: { ...PERS.recovery, ci_low: null, ci_high: null } },
    [url('15', 'variance')]: { ...VAR, enough_days: false, factors: VAR.factors.map((f) => ({ ...f, ci_low: null, ci_high: null })) } });
  const ctx = await mount();
  assert.match(ctx.main.textContent, /not enough days yet/);
  assert.equal(ctx.main.all((n) => n.getAttribute('class') === 'whisker').length, 0);
});

test('lab empty selection', async () => {
  stubs({ [url('15', 'distribution')]: { ...DIST, n: 0, ecdf: [], qq: [] }, [url('15', 'persistence')]: { ...PERS, n_trips: 0, lags: [], recovery: null }, [url('15', 'variance')]: { ...VAR, n: 0, factors: [] } });
  const ctx = await mount();
  const s = sections(ctx);
  assert.equal(s.length, 3);
  for (const sec of s) { assert.equal(sec.all((n) => n.getAttribute('class') === 'empty').length, 1); assert.equal(sec.byTag('svg').length, 0); }
});

test('lab persistence without order', async () => {
  stubs({ [url('15', 'persistence')]: { ...PERS, order_source: 'none', lags: [], recovery: null } });
  const ctx = await mount();
  const sec = sections(ctx)[1];
  assert.match(sec.textContent, /Persistence needs the stop order from the bus data server, which is unavailable right now\./);
  assert.doesNotMatch(sec.textContent, /listed by stop id/);
  assert.equal(sec.byTag('svg').length, 0);
});

test('lab one section failing leaves others', async () => {
  stubs({ [url('15', 'variance')]: fail(500, 'boom') });
  const ctx = await mount();
  const s = sections(ctx);
  assert.match(s[2].textContent, /HTTP 500/);
  assert.doesNotMatch(s[0].textContent + s[1].textContent, /HTTP 500/);
  assert.ok(s[0].byTag('svg').length && s[1].byTag('svg').length);
});

test('lab text is escaped', async () => {
  const r = '<img src=x>';
  stubs({}, r);
  const ctx = await mount(r);
  assert.match(ctx.main.byTag('h1')[0].textContent, /<img src=x>/);
  assert.equal(ctx.main.byTag('img').length, 0);
});

const tileText = (ctx, label) => ctx.main.all((n) => /^tile( |$)/.test(n.getAttribute('class') ?? '') && n.byText('div', new RegExp(`^${label}`)).length)[0]?.textContent ?? '';
const bad = /null|undefined|NaN/;

test('lab converts seconds to minutes only for delays', async () => {
  stubs({ [url('15', 'distribution')]: { ...DIST, tiles: { ...DIST.tiles, median: T(90, false), skew: T(120, false) } } });
  const ctx = await mount();
  assert.match(tileText(ctx, 'Median delay'), /\+1\.5 min/);
  assert.match(tileText(ctx, 'Skew'), /120\.00/);
  assert.doesNotMatch(tileText(ctx, 'Skew'), /min|2\.00/);
});

test('lab draws the on-time window band on the ECDF', async () => {
  stubs();
  const ctx = await mount();
  assert.equal(sections(ctx)[0].all((n) => n.getAttribute('class') === 'ci-band').length, 1);
});

test('lab null values render placeholders, never null/NaN', async () => {
  const lags = PERS.lags.map((l) => ({ ...l, r: l.lag === 1 ? null : l.r }));
  stubs({ [url('15', 'variance')]: { ...VAR, factors: VAR.factors.map((f) => ({ ...f, omega2: f.factor === 'date' ? null : f.omega2 })) },
    [url('15', 'persistence')]: { ...PERS, lags, recovery: null } });
  const ctx = await mount();
  const s = sections(ctx);
  assert.match(s[2].textContent, /Calendar date/);
  assert.ok(s[2].all((n) => n.tag === 'td' && n.textContent === '–').length >= 1);
  assert.ok(s[1].all((n) => n.tag === 'td' && n.textContent === '–').length >= 1);
  assert.match(tileText(ctx, 'Recovery slope'), /–/);
  assert.match(tileText(ctx, 'Recovery slope'), /No data for this selection/);
  for (const sec of s) assert.doesNotMatch(sec.textContent.replace(/\bnull\b(?=\s*$)/, ''), bad);
});

test('lab renders negative correlations and early delays', async () => {
  stubs({ [url('15', 'persistence')]: { ...PERS, lags: PERS.lags.map((l) => ({ ...l, r: -0.4, ci_low: -0.6, ci_high: -0.1 })) },
    [url('15', 'distribution')]: { ...DIST, tiles: { ...DIST.tiles, median: T(-120), p95: T(-60) } } });
  const ctx = await mount();
  assert.match(sections(ctx)[1].textContent, /−0\.40/);
  assert.match(sections(ctx)[0].textContent, /95% of buses are at least 1\.0 min early/);
  assert.doesNotMatch(ctx.main.textContent, bad);
});

test('lab escapes malicious route in links and the stop-free tables', async () => {
  // the lab page renders only numeric table cells and fixed factor labels; the route string is the sole free-text input, and it also appears in chart titles and links
  const r = '<script>x</script>';
  stubs({}, r);
  const ctx = await mount(r);
  assert.equal(ctx.main.byTag('script').length, 0);
  assert.equal(ctx.main.all((n) => n.tag === 'svg' || n.tag === 'title').filter((n) => n.byTag('script').length).length, 0);
});

test('lab buffer tile is unsigned', async () => {
  stubs({ [url('15', 'distribution')]: DIST });
  const t = tileText(await mount(), 'Buffer');
  assert.match(t, /4\.0 min/);
  assert.doesNotMatch(t, /\+/);
});
