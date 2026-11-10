import test from 'node:test';
import assert from 'node:assert/strict';
import * as S from '../js/settings.js';

const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m }; };
const withStore = (store) => { Object.defineProperty(globalThis, 'localStorage', { value: store, configurable: true, writable: true }); S.reload(); };

test('defaults on empty storage', () => {
  withStore(mem());
  assert.deepEqual(S.getSettings(), S.DEFAULTS);
});

test('garbage JSON and wrong types fall back per field; unknown keys dropped', () => {
  const st = mem(); st.setItem('dba-settings', '{nope'); withStore(st);
  assert.deepEqual(S.getSettings(), S.DEFAULTS);
  const v = S.validate({ density: 'compact', tip: { on: 'yes', units: 'sec', stats: { median: false, mean: 5 } }, charts: { heatCap: -3, pageSize: 40.5, tableToggle: false }, extra: 1 });
  assert.equal(v.density, 'compact');
  assert.equal(v.tip.on, true);
  assert.equal(v.tip.units, 'sec');
  assert.equal(v.tip.stats.median, false);
  assert.equal(v.tip.stats.mean, true);
  assert.equal(v.charts.heatCap, 0);
  assert.equal(v.charts.pageSize, 40);
  assert.equal(v.charts.tableToggle, false);
  assert.ok(!('extra' in v));
  assert.equal(S.validate(null).density, 'comfortable');
  assert.equal(S.validate({ charts: { pageSize: 80, heatCap: 12 } }).charts.pageSize, 80);
  assert.equal(S.validate({ charts: { heatCap: 12 } }).charts.heatCap, 12);
  assert.equal(S.validate({ charts: { heatCap: 99 } }).charts.heatCap, 0);
});

test('setSettings merges, persists, notifies; reset restores', () => {
  const st = mem(); withStore(st);
  const seen = [];
  const off = S.onSettings((s) => seen.push(s.density));
  S.setSettings({ density: 'compact', tip: { stats: { std: false } } });
  assert.equal(S.getSettings().density, 'compact');
  assert.equal(S.getSettings().tip.stats.std, false);
  assert.equal(S.getSettings().tip.stats.mean, true);
  assert.equal(JSON.parse(st.getItem('dba-settings')).density, 'compact');
  S.resetSettings();
  assert.deepEqual(S.getSettings(), S.DEFAULTS);
  assert.deepEqual(seen, ['compact', 'comfortable']);
  off(); S.setSettings({ density: 'compact' });
  assert.equal(seen.length, 2);
});

test('throwing storage keeps working in memory', () => {
  const bad = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); } };
  withStore(bad);
  assert.equal(S.storageWorks(), false);
  S.setSettings({ density: 'compact' });
  assert.equal(S.getSettings().density, 'compact');
});

test('settings page: routes, renders controls, applies and resets', async () => {
  const { installDom, FNode } = await import('./fakedom.mjs').then((m) => ({ ...m }));
  const { doc } = installDom();
  doc.documentElement = new FNode('html');
  withStore(mem());
  const { parseHash } = await import('../js/lib.js');
  assert.equal(parseHash('#/settings').name, 'settings');
  const { render } = await import('../js/pages/settings.js');
  const main = new FNode('main');
  await render({ main, title() {} });
  assert.equal(main.byTag('h1').length, 1);
  const boxes = main.all((n) => n.tag === 'input' && n.attrs.get('type') === 'checkbox');
  assert.ok(boxes.length >= 11, 'stat checkboxes + toggles');
  const dens = main.byTag('select').find((s) => s.children.some((o) => o.value === 'compact'));
  dens.value = 'compact'; dens.dispatch('change', { target: dens });
  assert.equal(S.getSettings().density, 'compact');
  assert.equal(doc.documentElement.getAttribute('data-density'), 'compact');
  main.byText('button', /Reset/)[0].dispatch('click');
  assert.equal(S.getSettings().density, 'comfortable');
});

test('figure honours the table-toggle setting (table stays for screen readers)', async () => {
  const { installDom, FNode } = await import('./fakedom.mjs');
  installDom(); withStore(mem());
  const { figure } = await import('../js/dom.js');
  const mkFig = () => figure({ title: 'T', svg: new FNode('svg'), table: new FNode('table') });
  assert.equal(mkFig().byText('button', /Show data table/).length, 1);
  S.setSettings({ charts: { tableToggle: false } });
  const f = mkFig();
  assert.equal(f.byTag('button').length, 0);
  assert.equal(f.byTag('table').length, 1);
  assert.equal(f.all((n) => n.getAttribute?.('class') === 'sr-only').length, 1);
});

test('map.areaOpacity: default, bounds and persistence', () => {
  assert.equal(S.DEFAULTS.map.areaOpacity, 0.5);
  assert.equal(S.validate({ map: { areaOpacity: 0.3 } }).map.areaOpacity, 0.3);
  for (const bad of [0, 0.05, 1.5, 'x', null, NaN]) assert.equal(S.validate({ map: { areaOpacity: bad } }).map.areaOpacity, 0.5, String(bad));
  withStore(mem());
  S.setSettings({ map: { areaOpacity: 0.8 } });
  assert.equal(S.getSettings().map.areaOpacity, 0.8);
  assert.equal(S.getSettings().tip.on, true, 'other settings untouched');
});

test('map.areaSize: default 400 m, only the offered sizes are accepted', () => {
  assert.equal(S.DEFAULTS.map.areaSize, 400);
  assert.equal(S.validate({ map: { areaSize: 600 } }).map.areaSize, 600);
  for (const bad of [0, 123, -400, '400', null, 1e9]) assert.equal(S.validate({ map: { areaSize: bad } }).map.areaSize, 400, String(bad));
});
