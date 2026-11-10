import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, installFetch, settle, FNode } from './fakedom.mjs';

installDom();
const { rankRoutes, renderSidebar } = await import('../js/sidebar.js');
const R = (...n) => n.map((s, i) => ({ id: `i${i}`, shortName: s, longName: '' }));
const mount = async () => { const root = new FNode('aside'); await renderSidebar(root, { signal: new AbortController().signal }); return root; };
const type = (root, v) => { const i = root.byTag('input')[0]; i.value = v; i.dispatch('input'); return i; };
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
  assert.deepEqual(root.byTag('a').map((a) => a.getAttribute('href')), ['#/analytics/sA', '#/analytics/sA%2FB']);
  root.byText('button', /^Untrack$/)[1].dispatch('click');
  await settle();
  assert.equal(tracked.length, 2, 'first click only asks for confirmation');
  assert.equal(root.byText('button', /^Confirm$/).length, 1);
  root.byText('button', /^Cancel$/)[0].dispatch('click');
  assert.equal(root.byText('button', /^Confirm$/).length, 0, 'cancel restores the Untrack button');
  root.byText('button', /^Untrack$/)[1].dispatch('click');
  root.byText('button', /^Confirm$/)[0].dispatch('click');
  await settle();
  assert.deepEqual(root.byTag('a').map((a) => a.getAttribute('href')), ['#/analytics/sA']);
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
  assert.ok(root.byTag('a').length === 1);
});

test('sidebar: aria-current follows the hash', async () => {
  installFetch({ 'GET /api/routes/tracked': { routes: ['sA', 'sB'] } });
  const root = await mount();
  globalThis.location.hash = '#/analytics/sA';
  for (const fn of globalThis.__listeners.hashchange) fn();
  const [a, b] = root.byTag('a');
  assert.equal(a.getAttribute('aria-current'), 'page');
  assert.equal(b.getAttribute('aria-current'), null);
  globalThis.location.hash = '#/analytics/sB';
  for (const fn of globalThis.__listeners.hashchange) fn();
  assert.equal(a.getAttribute('aria-current'), null);
  assert.equal(b.getAttribute('aria-current'), 'page');
});
