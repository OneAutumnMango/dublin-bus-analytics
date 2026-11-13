import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, installFetch, settle, FNode, fail } from './fakedom.mjs';

installDom();
const { rankRoutes, renderSidebar, onTrackedChange } = await import('../js/sidebar.js');
const R = (...n) => n.map((s, i) => ({ id: `i${i}`, shortName: s, longName: '' }));
const mount = async () => { const root = new FNode('aside'); await renderSidebar(root, { signal: new AbortController().signal }); return root; };
const type = (root, v) => { const i = root.byTag('input')[0]; i.value = v; i.dispatch('input'); return i; };
const trackedHrefs = (root) => root.byTag('a').map((a) => a.getAttribute('href')).filter((x) => x !== '#/all' && x !== '#/lab' && !x.startsWith('#/lab/'));
const names = (l) => l.map((r) => r.shortName);

test('rankRoutes: exact, prefixes, rest; stable; case-insensitive', () => {
  assert.deepEqual(names(rankRoutes(R('104', '46A', '24', '40', '4'), '4')), ['4', '46A', '40', '104', '24']);
  assert.deepEqual(names(rankRoutes(R('46a', '46A'), ' 46A ')), ['46a', '46A']);
  assert.deepEqual(names(rankRoutes(R('x46A', '46a'), '46A')), ['46a', 'x46A']);
});

test('sidebar: tracked links are url-encoded, untrack works', async () => {
  let tracked = ['sA', 'sA/B'];
  installFetch({ 'GET /api/routes/tracked': () => ({ routes: [...tracked] }),
    'DELETE /api/routes/tracked/sA%2FB': () => { tracked = ['sA']; return { routes: tracked }; } });
  const root = await mount();
  assert.deepEqual(trackedHrefs(root), ['#/analytics/sA', '#/analytics/sA%2FB']);
  root.byText('button', /^Untrack$/)[1].dispatch('click');
  await settle();
  assert.equal(tracked.length, 2, 'first click only asks for confirmation');
  assert.equal(root.byText('button', /^Confirm$/).length, 1);
  root.byText('button', /^Cancel$/)[0].dispatch('click');
  assert.equal(root.byText('button', /^Confirm$/).length, 0, 'cancel restores the Untrack button');
  root.byText('button', /^Untrack$/)[1].dispatch('click');
  root.byText('button', /^Confirm$/)[0].dispatch('click');
  await settle();
  assert.deepEqual(trackedHrefs(root), ['#/analytics/sA']);
});

test('sidebar: each tracked route has a Stats lab link', async () => {
  installFetch({ 'GET /api/routes/tracked': { routes: ['15', 'sA/B'] } });
  const root = await mount();
  for (const [r, href] of [['15', '#/lab/15'], ['sA/B', '#/lab/sA%2FB']]) {
    const a = root.byTag('a').filter((x) => x.getAttribute('href') === href);
    assert.equal(a.length, 1);
    assert.equal(a[0].getAttribute('aria-label'), `Stats lab for route ${r}`);
    assert.equal(a[0].textContent, 'Stats lab');
  }
});

test('sidebar: typing searches after debounce without submit; ranked; literal text', async () => {
  const calls = installFetch({ 'GET /api/routes/tracked': { routes: [] },
    'GET /api/routes': [{ id: '1', shortName: '46A', longName: 'Park <b>x</b>' }, { id: '2', shortName: '4', longName: '' }] });
  const root = await mount();
  type(root, '4');
  await settle(50);
  assert.ok(!calls.some((c) => c.key === 'GET /api/routes'), 'not before debounce');
  await settle(250);
  assert.ok(calls.some((c) => c.url === '/api/routes?q=4'));
  const li = root.byTag('li');
  assert.match(li[0].textContent, /^4/);
  assert.match(root.textContent, /Park <b>x<\/b>/);
});

test('sidebar: submit tracks only an exact untracked match', async () => {
  let tracked = [];
  const puts = [];
  const calls = installFetch({ 'GET /api/routes/tracked': () => ({ routes: [...tracked] }),
    'GET /api/routes': (url) => (new URL(url, 'http://x').searchParams.get('q') === '4' ? R('46A', '104') : R('46A', '104', '4')),
    'PUT /api/routes/tracked/46A': () => { puts.push('46A'); tracked = ['46A']; return { routes: tracked }; } });
  const root = await mount();
  const form = root.byTag('form')[0];
  type(root, '4'); form.dispatch('submit'); await settle(); // no exact '4'
  assert.ok(!calls.some((c) => c.init.method === 'PUT'));
  type(root, '46a'); form.dispatch('submit'); await settle();
  assert.deepEqual(puts, ['46A']);
  assert.ok(root.byTag('a').some((a) => a.getAttribute('href') === '#/analytics/46A'));
  form.dispatch('submit'); await settle(); // already tracked
  assert.equal(puts.length, 1);
});

test('sidebar: two searches resolving in reverse order show the newer results', async () => {
  const gates = {};
  installFetch({ 'GET /api/routes/tracked': { routes: [] },
    'GET /api/routes': (url) => new Promise((r) => { gates[new URL(url, 'http://x').searchParams.get('q')] = r; }) });
  const root = await mount();
  const form = root.byTag('form')[0];
  type(root, 'a'); form.dispatch('submit');
  type(root, 'b'); form.dispatch('submit');
  await settle();
  gates.b(R('NEWER'));
  await settle();
  gates.a(R('OLDER'));
  await settle();
  assert.match(root.textContent, /NEWER/);
  assert.ok(!/OLDER/.test(root.textContent));
});

test('sidebar: double click Track sends one PUT and disables buttons meanwhile', async () => {
  let puts = 0;
  installFetch({ 'GET /api/routes/tracked': { routes: [] }, 'GET /api/routes': R('e1', 'e2'),
    'PUT /api/routes/tracked/e1': async () => { puts++; await settle(20); return { routes: ['e1'] }; } });
  const root = await mount();
  type(root, 'e'); await settle(250);
  const [b1, b2] = root.byText('button', /^Track$/);
  b1.dispatch('click'); b1.dispatch('click');
  assert.equal(b1.disabled, true);
  assert.equal(b2.disabled, true);
  await settle(60);
  assert.equal(puts, 1);
  assert.equal(trackedHrefs(root).length, 1);
});

test('sidebar: aria-current follows the hash', async () => {
  installFetch({ 'GET /api/routes/tracked': { routes: ['sA', 'sB'] } });
  const root = await mount();
  globalThis.location.hash = '#/analytics/sA';
  for (const fn of globalThis.__listeners.hashchange) fn();
  const [a, b] = root.byTag('a').filter((x) => x.getAttribute('href') !== '#/all' && x.getAttribute('href') !== '#/lab' && !x.getAttribute('href').startsWith('#/lab/'));
  assert.equal(a.getAttribute('aria-current'), 'page');
  assert.equal(b.getAttribute('aria-current'), null);
  globalThis.location.hash = '#/analytics/sB';
  for (const fn of globalThis.__listeners.hashchange) fn();
  assert.equal(a.getAttribute('aria-current'), null);
  assert.equal(b.getAttribute('aria-current'), 'page');
});

test('sidebar: All routes link exists once, even with no tracked routes', async () => {
  installFetch({ 'GET /api/routes/tracked': { routes: [] } });
  const root = await mount();
  const all = root.byText('a', /^All routes$/);
  assert.equal(all.length, 1);
  assert.equal(all[0].getAttribute('href'), '#/all');
});

test('sidebar: All routes aria-current only on #/all', async () => {
  installFetch({ 'GET /api/routes/tracked': { routes: ['sA'] } });
  const root = await mount();
  const fire = (h) => { globalThis.location.hash = h; for (const fn of globalThis.__listeners.hashchange) fn(); };
  const all = root.byText('a', /^All routes$/)[0];
  fire('#/all');
  assert.equal(all.getAttribute('aria-current'), 'page');
  assert.ok(root.byTag('a').filter((a) => a !== all).every((a) => a.getAttribute('aria-current') === null));
  fire('#/analytics/sA');
  assert.equal(all.getAttribute('aria-current'), null);
});

const trackFlow = async (routes) => {
  const root = await mount();
  type(root, 'e'); await settle(250);
  root.byText('button', /^Track$/)[0].dispatch('click');
  await settle();
  return root;
};

test('sidebar: onTrackedChange fires once per successful track and untrack', async () => {
  let n = 0;
  const off = onTrackedChange(() => { n++; });
  installFetch({ 'GET /api/routes/tracked': { routes: [] }, 'GET /api/routes': R('e1'),
    'PUT /api/routes/tracked/e1': { routes: ['e1'] }, 'DELETE /api/routes/tracked/e1': { routes: [] } });
  const root = await trackFlow();
  assert.equal(n, 1);
  assert.deepEqual(trackedHrefs(root), ['#/analytics/e1'], 'listener runs after the sidebar was redrawn');
  root.byText('button', /^Untrack$/)[0].dispatch('click');
  root.byText('button', /^Confirm$/)[0].dispatch('click');
  await settle();
  assert.equal(n, 2);
  off();
});

test('sidebar: onTrackedChange is not called when the request fails', async () => {
  let n = 0;
  const off = onTrackedChange(() => { n++; });
  installFetch({ 'GET /api/routes/tracked': { routes: ['e1'] }, 'GET /api/routes': R('e2'),
    'PUT /api/routes/tracked/e2': () => fail(500, 'boom'),
    'DELETE /api/routes/tracked/e1': () => fail(500, 'boom') });
  const root = await trackFlow();
  root.byText('button', /^Untrack$/)[0].dispatch('click');
  root.byText('button', /^Confirm$/)[0].dispatch('click');
  await settle();
  assert.equal(n, 0);
  off();
});

test('sidebar: unsubscribe stops calls; a throwing listener does not break others or the sidebar', async () => {
  let a = 0, b = 0;
  const offA = onTrackedChange(() => { a++; });
  const offBad = onTrackedChange(() => { throw new Error('bad'); });
  const offB = onTrackedChange(() => { b++; });
  installFetch({ 'GET /api/routes/tracked': { routes: [] }, 'GET /api/routes': R('e1', 'e2'),
    'PUT /api/routes/tracked/e1': { routes: ['e1'] }, 'PUT /api/routes/tracked/e2': { routes: ['e1', 'e2'] } });
  const root = await trackFlow();
  assert.equal(a, 1); assert.equal(b, 1);
  assert.deepEqual(trackedHrefs(root), ['#/analytics/e1']);
  assert.equal(root.byTag('div').some((d) => d.getAttribute('role') === 'alert' && d.textContent), false, 'no error shown');
  offA();
  root.byText('button', /^Track$/)[0].dispatch('click');
  await settle();
  assert.equal(a, 1); assert.equal(b, 2);
  offBad(); offB();
});

test('sidebar: all-routes Stats lab link sits inline with All routes and tracks the hash', async () => {
  installFetch({ 'GET /api/routes/tracked': () => ({ routes: ['15'] }) });
  globalThis.location = { hash: '#/lab' };
  const root = await mount();
  const hrefs = root.byTag('a').map((a) => a.getAttribute('href'));
  assert.equal(hrefs[hrefs.indexOf('#/all') + 1], '#/lab');
  const a = root.byTag('a').find((x) => x.getAttribute('href') === '#/lab');
  assert.equal(a.getAttribute('aria-label'), 'Stats lab for all routes');
  assert.equal(a.getAttribute('aria-current'), 'page');
  assert.equal(root.byText('a', /^All routes$/)[0].getAttribute('aria-current'), null);
  globalThis.location = { hash: '#/lab/15' };
  const root2 = await mount();
  assert.equal(root2.byTag('a').find((x) => x.getAttribute('href') === '#/lab').getAttribute('aria-current'), null);
});
