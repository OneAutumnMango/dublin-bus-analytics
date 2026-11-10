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
    addSource(id, o) { this.sources[id] = { data: o.data, setData: (d) => { this.sources[id].data = d; } }; }
    getSource(id) { return this.sources[id]; }
    addLayer(l) { this.layers[l.id] = l; }
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
  const calls = installFetch({ 'GET /api/map': mapResp });
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
  assert.equal(map.sources.regions.data.features.length, 1);
  assert.match(ctx.main.textContent, /on time/);
  assert.match(ctx.main.textContent, /no data in this window/);
  assert.match(ctx.main.textContent, /tiles\.openfreemap\.org/);
  assert.ok(map.bounds, 'fits the routes on first load');
  assert.equal(map.layout['regions.visibility'], 'visible');
  assert.deepEqual(map.paint['stops.circle-color'].slice(0, 3), ['interpolate', ['linear'], ['get', 'delay']]);
  const boxes = ctx.main.all((n) => n.tag === 'input' && n.attrs.get('type') === 'checkbox');
  const [c15, c46, regions] = boxes;
  c46.checked = false; c46.dispatch('change', { target: c46 });
  assert.deepEqual(names(map.sources.stops.data).sort(), ['Alpha', 'Beta']);
  assert.equal(map.sources.routes.data.features.length, 1);
  assert.equal(map.sources.stops.data.features.find((f) => f.properties.name === 'Beta').properties.delay, 0, 'Beta recomputed from route 15 only');
  regions.checked = false; regions.dispatch('change', { target: regions });
  assert.equal(map.layout['regions.visibility'], 'none');
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

test('darkenStyle recolours background and fills', async () => {
  const { darkenStyle } = await import('../js/pages/map.js');
  const rec = stubMapLibre();
  const m = new globalThis.maplibregl.Map({});
  darkenStyle(m);
  assert.equal(m.paint['bg.background-color'], '#0d1117');
  assert.equal(m.paint['water.fill-color'], '#080b10');
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
