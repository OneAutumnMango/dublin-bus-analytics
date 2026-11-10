import test from 'node:test';
import assert from 'node:assert/strict';
import { validateMapState, loadMapState, saveMapState, MAP_DEFAULTS } from '../js/mapstate.js';

const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m }; };
const use = (st) => Object.defineProperty(globalThis, 'localStorage', { value: st, configurable: true, writable: true });

test('validateMapState: per-field fallback, bounds, junk tolerated', () => {
  assert.deepEqual(validateMapState(null), MAP_DEFAULTS);
  assert.deepEqual(validateMapState('x'), MAP_DEFAULTS);
  const v = validateMapState({ areas: 'smooth', stops: false, win: [360, 630], hidden: ['15', 7, 'x'.repeat(30)], view: { lng: -6.26, lat: 53.35, zoom: 12 }, extra: 1 });
  assert.equal(v.areas, 'smooth'); assert.equal(v.stops, false); assert.deepEqual(v.win, [360, 630]);
  assert.deepEqual(v.hidden, ['15']); assert.deepEqual(v.view, { lng: -6.26, lat: 53.35, zoom: 12 }); assert.ok(!('extra' in v));
  const bad = validateMapState({ areas: 'neon', stops: 'yes', win: [0, 1440], hidden: 'a', view: { lng: 500, lat: 0, zoom: 3 } });
  assert.equal(bad.areas, 'hex'); assert.equal(bad.stops, true); assert.deepEqual(bad.win, [0, 1439]); assert.deepEqual(bad.hidden, []); assert.equal(bad.view, null);
  assert.deepEqual(validateMapState({ win: [10] }).win, [0, 1439]);
});

test('load/save round trip merges patches; throwing storage is harmless', () => {
  const st = mem(); use(st);
  assert.deepEqual(loadMapState(), MAP_DEFAULTS);
  saveMapState({ areas: 'circles' });
  saveMapState({ win: [1320, 300], hidden: ['46A'] });
  assert.deepEqual(loadMapState(), { ...MAP_DEFAULTS, areas: 'circles', win: [1320, 300], hidden: ['46A'] });
  st.setItem('dba-map', '{bad json'); assert.deepEqual(loadMapState(), MAP_DEFAULTS);
  use({ getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); } });
  assert.deepEqual(loadMapState(), MAP_DEFAULTS);
  assert.equal(saveMapState({ areas: 'off' }).areas, 'off');
});
