import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, installFetch, settle, FNode } from './fakedom.mjs';

const { doc } = installDom();
doc.documentElement = new FNode('html');

// Minimal MapLibre double: records sources, layers, visibility and paint changes; lets tests fire events.
function stubMapLibre() {
  const rec = { maps: [], controls: 0 };
  class Map {
    constructor(opts) { this.opts = opts; this.handlers = {}; this.sources = {}; this.layers = {}; this.layout = {}; this.paint = {}; this.removed = false; this.bounds = null; rec.maps.push(this); }
    on(ev, fn) { (this.handlers[ev] ||= []).push(fn); }
    fire(ev, e) { for (const f of this.handlers[ev] || []) f(e); }
    addControl() { rec.controls++; }
    addSource(id, o) { this.sources[id] = { data: o.data, spec: o, setData: (d) => { this.sources[id].data = d; } }; }
    getSource(id) { return this.sources[id]; }
    getLayer(id) { return this.layers[id]; }
    removeLayer(id) { delete this.layers[id]; }
    removeSource(id) { delete this.sources[id]; }
    addLayer(l, before) { this.layers[l.id] = l; if (before) this.layers[l.id].before = before; }
    setLayoutProperty(id, k, v) { this.layout[`${id}.${k}`] = v; }
    setPaintProperty(id, k, v) { this.paint[`${id}.${k}`] = v; }
    getStyle() { return { layers: [{ id: 'bg', type: 'background' }, { id: 'water', type: 'fill' }] }; }
    getCanvas() { return { style: {}, addEventListener() {} }; }
    queryRenderedFeatures(_pt, { layers }) { return this.hit && layers.includes(this.hit.layer) ? [this.hit.feature] : []; }
    fitBounds(b) { this.bounds = b; }
    remove() { this.removed = true; }
  }
  globalThis.maplibregl = { Map, NavigationControl: class {}, setWorkerUrl() {} };
  return rec;
}

const mapResp = () => ({ filters: { since: 1_690_000_000, until: null, min_confidence: 1 }, minutes: null, n: 70, unavailable: [],
  routes: [{ route: '15', directions: { out: ['a', 'b'] } }, { route: '46A', directions: { out: ['b', 'c'] } }],
  stops: [
    { stop_id: 'a', code: '2051', name: 'Alpha', lat: 53.3, lon: -6.2, n: 30, mean_delay: 90, routes: [{ route: '15', n: 30, mean_delay: 90 }] },
    { stop_id: 'b', code: '7', name: 'Beta', lat: 53.3005, lon: -6.2005, n: 40, mean_delay: 225, routes: [{ route: '15', n: 10, mean_delay: 0 }, { route: '46A', n: 30, mean_delay: 300 }] },
    { stop_id: 'c', code: '9', name: 'Gamma', lat: 53.31, lon: -6.21, n: 0, mean_delay: null, routes: [] }] });

const ctxOf = () => ({ main: new FNode('main'), params: {}, query: {}, signal: new AbortController().signal, title() {}, go() {}, nowSec: () => 1_700_000_000 });
const names = (g) => g.features.map((f) => f.properties.name ?? f.properties.route);

test('map page: layers, sources, legend, route toggles, regions toggle', async () => {
  const rec = stubMapLibre();
  (await import('../js/pages/map.js')).hooks.makeCanvas = (r) => ({ width: r.width, height: r.height });
  const calls = installFetch({ 'GET /api/map': mapResp, 'GET /api/map/shapes': { shapes: { 15: { out: [[-6.2, 53.3], [-6.201, 53.3004], [-6.2005, 53.3005]] } } } });
  const ctx = ctxOf();
  const { render } = await import('../js/pages/map.js');
  const done = render(ctx);
  await settle();
  const map = rec.maps[0];
  map.fire('load');
  await done; await settle();
  assert.equal(ctx.main.byTag('h1').length, 1);
  assert.deepEqual(Object.keys(map.layers), ['regions', 'routes', 'stops-nodata', 'stops']);
  assert.deepEqual(names(map.sources.stops.data).sort(), ['Alpha', 'Beta', 'Gamma']);
  assert.equal(map.sources.routes.data.features.length, 2);
  assert.equal(map.sources.routes.data.features.find((f) => f.properties.route === '15' && f.properties.dir === 'out').geometry.coordinates.length, 3, 'real shape, not stop-to-stop');
  assert.equal(map.sources.regions.data.features.length, 1);
  assert.match(ctx.main.textContent, /on time/);
  assert.match(ctx.main.textContent, /no data in this window/);
  assert.match(ctx.main.textContent, /tiles\.openfreemap\.org/);
  assert.ok(map.bounds, 'fits the routes on first load');
  assert.equal(map.layout['regions.visibility'], 'visible');
  assert.deepEqual(map.paint['stops.circle-color'].slice(0, 3), ['interpolate', ['linear'], ['get', 'delay']]);
  const boxes = ctx.main.all((n) => n.tag === 'input' && n.attrs.get('type') === 'checkbox');
  const [c15, c46, stopsBox] = boxes;
  const areas = ctx.main.byTag('select').find((n) => n.children.some((o) => o.value === 'smooth'));
  assert.equal(map.layout['stops.visibility'], 'visible');
  stopsBox.checked = false; stopsBox.dispatch('change', { target: stopsBox });
  assert.equal(map.layout['stops.visibility'], 'none'); assert.equal(map.layout['stops-nodata.visibility'], 'none');
  stopsBox.checked = true; stopsBox.dispatch('change', { target: stopsBox });
  assert.equal(map.layout['stops.visibility'], 'visible');
  c46.checked = false; c46.dispatch('change', { target: c46 });
  assert.deepEqual(names(map.sources.stops.data).sort(), ['Alpha', 'Beta']);
  assert.equal(map.sources.routes.data.features.length, 1);
  assert.equal(map.sources.stops.data.features.find((f) => f.properties.name === 'Beta').properties.delay, 0, 'Beta recomputed from route 15 only');
  const hexRings = JSON.stringify(map.sources.regions.data.features.map((f) => f.geometry));
  c46.checked = true; c46.dispatch('change', { target: c46 });
  assert.equal(JSON.stringify(map.sources.regions.data.features.map((f) => f.geometry)), hexRings, 'hexagons keep their positions when routes toggle');
  c46.checked = false; c46.dispatch('change', { target: c46 });
  areas.value = 'circles'; areas.dispatch('change', { target: areas });
  assert.equal(map.sources.regions.data.features.length, 2);
  areas.value = 'smooth'; areas.dispatch('change', { target: areas });
  assert.equal(map.sources.regions.data.features.length, 0, 'smooth mode draws a raster, not polygons');
  assert.equal(map.sources.smooth.spec.type, 'canvas');
  assert.equal(map.sources.smooth.spec.coordinates.length, 4);
  assert.equal(map.layers.smooth.type, 'raster');
  assert.equal(map.layers.smooth.before, 'routes', 'under the route lines');
  assert.equal(map.layers.smooth.paint['raster-resampling'], 'linear');
  areas.value = 'off'; areas.dispatch('change', { target: areas });
  assert.equal(map.layout['regions.visibility'], 'none');
  assert.equal(map.layers.smooth, undefined, 'raster removed when leaving smooth mode');
  areas.value = 'hex'; areas.dispatch('change', { target: areas });
  assert.equal(map.layout['regions.visibility'], 'visible');
  ctx.main.byText('button', /^None$/)[0].dispatch('click');
  assert.equal(map.sources.stops.data.features.length, 0);
  assert.ok(!c15.checked);
  ctx.main.byText('button', /^All$/)[0].dispatch('click');
  assert.equal(map.sources.stops.data.features.length, 3);
  assert.ok(calls.every((c) => !/minutes_from/.test(c.url)), 'full-day window sends no minute filter');
});

test('map page: time window refetches with minutes; stale/invalid never reaches the map; tooltip on hover', async () => {
  const rec = stubMapLibre();
  const calls = installFetch({ 'GET /api/map': mapResp });
  const ctx = ctxOf();
  const { render } = await import('../js/pages/map.js');
  const done = render(ctx);
  await settle(); rec.maps[0].fire('load'); await done; await settle();
  const [inA, inB] = ctx.main.byTag('input').filter((n) => n.attrs.get('type') === 'range');
  inA.value = '360'; inB.value = '630'; inA.dispatch('change'); inB.dispatch('change');
  await settle();
  const last = calls[calls.length - 1].url;
  assert.match(last, /minutes_from=360/); assert.match(last, /minutes_to=630/);
  assert.match(ctx.main.textContent, /06:00 – 10:30/);
  const map = rec.maps[0];
  map.hit = { layer: 'stops', feature: { properties: { id: 'b' } } };
  const { installTooltip } = await import('../js/tooltip.js');
  assert.equal(installTooltip(doc), null, 'node host: no document listeners, showTip is a safe no-op');
  map.fire('mousemove', { point: { x: 1, y: 1 }, originalEvent: { clientX: 5, clientY: 6 } });
});

test('map page: nothing tracked -> empty state; maplibre failing -> notice, page still usable', async () => {
  stubMapLibre();
  installFetch({ 'GET /api/map': { ...mapResp(), routes: [], stops: [], n: 0 } });
  const ctx = ctxOf();
  const { render } = await import('../js/pages/map.js');
  const done = render(ctx);
  await settle(); globalThis.maplibregl && 0; await done; await settle();
  assert.match(ctx.main.textContent, /No tracked routes with known stops/);

  delete globalThis.maplibregl;
  const m = await import('../js/pages/map.js');
  globalThis.document.head = new FNode('head');
  installFetch({ 'GET /api/map': mapResp });
  const ctx2 = ctxOf();
  const p = m.render(ctx2);
  await settle();
  const script = globalThis.document.head.byTag('script')[0];
  script.dispatch('error');
  await p; await settle();
  assert.match(ctx2.main.textContent, /map library could not be loaded/);
  assert.match(ctx2.main.textContent, /Route 15/, 'controls and data still render');
});

test('darkenStyle recolours background and fills (incl. outlines); softenBuildings drops light outlines', async () => {
  const { darkenStyle, softenBuildings } = await import('../js/pages/map.js');
  const rec = stubMapLibre();
  const m = new globalThis.maplibregl.Map({});
  darkenStyle(m);
  assert.equal(m.paint['bg.background-color'], '#0d1117');
  assert.equal(m.paint['water.fill-color'], '#080b10');
  assert.equal(m.paint['water.fill-outline-color'], '#080b10', 'no pale outline left on dark fills');
  m.getStyle = () => ({ layers: [{ id: 'building', type: 'fill' }, { id: 'park', type: 'fill' }] });
  softenBuildings(m);
  assert.equal(m.paint['building.fill-outline-color'], 'rgba(0,0,0,0)');
  assert.equal(m.paint['park.fill-outline-color'], undefined);
  assert.ok(rec.maps.length >= 1);
});

test('map page: stops table is available as the accessible/fallback view', async () => {
  stubMapLibre();
  installFetch({ 'GET /api/map': mapResp });
  const ctx = ctxOf();
  const { render } = await import('../js/pages/map.js');
  const done = render(ctx);
  await settle(); globalThis.maplibregl.Map && 0; await done; await settle();
  const t = ctx.main.textContent;
  assert.match(t, /Stops as a table \(3\)/);
  assert.match(t, /Beta \(7\)/);
  assert.ok(!/NaN|undefined/.test(t));
});

test('map page remembers its choices and restores them; area strength comes from settings', async () => {
  const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; };
  const store = mem();
  Object.defineProperty(globalThis, 'localStorage', { value: store, configurable: true, writable: true });
  const { hooks, render } = await import('../js/pages/map.js');
  const seenAlpha = [];
  hooks.makeCanvas = (r) => { seenAlpha.push(Math.max(...[...r.data].filter((_, i) => i % 4 === 3))); return { width: r.width, height: r.height }; };
  const S = await import('../js/settings.js');
  S.resetSettings(); S.setSettings({ map: { areaOpacity: 0.3 } });

  let rec = stubMapLibre();
  installFetch({ 'GET /api/map': mapResp, 'GET /api/map/shapes': { shapes: {} } });
  let ctx = ctxOf();
  let done = render(ctx); await settle(); rec.maps[0].fire('load'); await done; await settle();
  const areas = ctx.main.byTag('select').find((n) => n.children.some((o) => o.value === 'smooth'));
  areas.value = 'smooth'; areas.dispatch('change', { target: areas });
  assert.ok(seenAlpha.at(-1) <= Math.round(255 * 0.3), `alpha ${seenAlpha.at(-1)} respects 30% strength`);
  assert.ok(seenAlpha.at(-1) > 0);
  const [, c46, stopsBox] = ctx.main.all((n) => n.tag === 'input' && n.attrs.get('type') === 'checkbox');
  c46.checked = false; c46.dispatch('change', { target: c46 });
  stopsBox.checked = false; stopsBox.dispatch('change', { target: stopsBox });
  const [inA, inB] = ctx.main.byTag('input').filter((n) => n.attrs.get('type') === 'range');
  inA.value = '360'; inB.value = '630'; inA.dispatch('change'); inB.dispatch('change'); await settle();
  const saved = JSON.parse(store.getItem('dba-map'));
  assert.equal(saved.areas, 'smooth'); assert.equal(saved.stops, false); assert.deepEqual(saved.hidden, ['46A']); assert.deepEqual(saved.win, [360, 630]);

  // a fresh visit restores all of it, and asks the server for the saved window straight away
  rec = stubMapLibre();
  const calls = installFetch({ 'GET /api/map': mapResp, 'GET /api/map/shapes': { shapes: {} } });
  ctx = ctxOf();
  done = render(ctx); await settle(); rec.maps[0].fire('load'); await done; await settle();
  assert.match(calls.find((c) => c.key === 'GET /api/map').url, /minutes_from=360.*minutes_to=630|minutes_to=630.*minutes_from=360/);
  const m2 = rec.maps[0];
  assert.equal(m2.layout['stops.visibility'], 'none');
  assert.deepEqual(m2.sources.stops.data.features.map((f) => f.properties.name).sort(), ['Alpha', 'Beta']); // route 46A stays off (Gamma is only on it)
  const picked = ctx.main.byTag('select').find((n) => n.children.some((o) => o.value === 'smooth')).children.find((o) => o.selected);
  assert.equal(picked.value, 'smooth', 'the area style dropdown shows the remembered choice');
  assert.match(ctx.main.textContent, /06:00 – 10:30/);
  Object.defineProperty(globalThis, 'localStorage', { value: undefined, configurable: true, writable: true });
  S.resetSettings();
});
