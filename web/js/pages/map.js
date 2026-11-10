// Map page: tracked routes on an OpenFreeMap basemap (like the Android app), stops coloured by mean delay
// (Okabe-Ito blue early .. orange late), optional hex regions, route toggles, time-of-day window, stat-card tooltips.
import { api, isAbort } from '../api.js';
import { dataTable, emptyState, errorBox, h, link, notice, put } from '../dom.js';
import { fmtCount, fmtDelay, fmtNum, makeLatest } from '../lib.js';
import { AREA_MODES, areaCells, autoCap, BLUE, cellTipData, colourStops, NEUTRAL, ORANGE, routeColour, routeLines, smoothRaster, smoothReach, stopFeatures, stopTipData, visibleStops, windowLabel } from '../mapdata.js';
import { loadMapState, saveMapState } from '../mapstate.js';
import { getSettings, onSettings } from '../settings.js';
import { hideTip, showTip } from '../tooltip.js';
import { timeRangeSlider, MAX } from '../slider.js';
import { setup, windowText } from './common.js';

const STYLE_URL = 'https://tiles.openfreemap.org/styles/positron'; // same basemap as the Android app
const LIB = '/vendor/maplibre/';
const CENTRE = [-6.26, 53.35];
const empty = { type: 'FeatureCollection', features: [] };

let libPromise = null;
// MapLibre GL JS is vendored (CSP build) and loaded on first use; tests pre-set globalThis.maplibregl.
export function loadMapLibre() {
  if (globalThis.maplibregl) return Promise.resolve(globalThis.maplibregl);
  libPromise ||= new Promise((resolve, reject) => {
    const d = globalThis.document;
    d.head.append(h('link', { rel: 'stylesheet', href: `${LIB}maplibre-gl.css` }));
    const s = h('script', { src: `${LIB}maplibre-gl-csp.js` });
    s.addEventListener('load', () => { globalThis.maplibregl.setWorkerUrl(`${LIB}maplibre-gl-csp-worker.js`); resolve(globalThis.maplibregl); });
    s.addEventListener('error', () => { libPromise = null; reject(new Error('The map library could not be loaded.')); });
    d.head.append(s);
  });
  return libPromise;
}

const isDark = () => {
  const t = globalThis.document?.documentElement?.getAttribute?.('data-theme');
  return t ? t === 'dark' : !!globalThis.matchMedia?.('(prefers-color-scheme: dark)').matches;
};

// The positron style is light; for the dark theme recolour its background, fills, lines and labels.
// Light theme: buildings keep their fill but lose the hard outline that gets harsh when zoomed in.
export function softenBuildings(map) {
  for (const l of map.getStyle()?.layers || []) {
    if (l.type === 'fill' && /building/.test(l.id)) { try { map.setPaintProperty(l.id, 'fill-outline-color', 'rgba(0,0,0,0)'); } catch { /* none */ } }
  }
}

export function darkenStyle(map) {
  for (const l of map.getStyle()?.layers || []) {
    try {
      if (l.type === 'background') map.setPaintProperty(l.id, 'background-color', '#0d1117');
      else if (l.type === 'fill') {
        const c = /water/.test(l.id) ? '#080b10' : /building/.test(l.id) ? '#1a222d' : '#151c26';
        map.setPaintProperty(l.id, 'fill-color', c);
        map.setPaintProperty(l.id, 'fill-outline-color', c); // the light style's pale outlines would glare on dark fills (buildings)
      }
      else if (l.type === 'line') map.setPaintProperty(l.id, 'line-color', '#27313f');
      else if (l.type === 'symbol') { map.setPaintProperty(l.id, 'text-color', '#8a97a8'); map.setPaintProperty(l.id, 'text-halo-color', '#0d1117'); }
      else if (l.type === 'raster') map.setPaintProperty(l.id, 'raster-opacity', 0.08);
    } catch { /* a layer without that paint property */ }
  }
}

// Seams for tests (no canvas in node): turn a smoothRaster into a canvas element for MapLibre's canvas source, which draws
// from the element directly (no fetch, so the page CSP needs nothing extra).
export const hooks = {
  makeCanvas(r) {
    const c = globalThis.document.createElement('canvas');
    c.width = r.width; c.height = r.height;
    const g = c.getContext?.('2d');
    if (!g) return null;
    g.putImageData(new globalThis.ImageData(r.data, r.width, r.height), 0, 0);
    return c;
  },
};

const colourExpr = (cap) => ['interpolate', ['linear'], ['get', 'delay'], ...colourStops(cap).flat()];

export async function render(ctx) {
  const { main, signal } = ctx;
  ctx.title('Map');
  const s = setup(ctx);
  const panel = h('aside', { class: 'map-panel', 'aria-label': 'Map controls' });
  const status = h('div', { class: 'map-status', role: 'status' });
  const tableHost = h('div', { class: 'map-table-host' });
  const canvas = h('div', { class: 'map-canvas', 'data-tip-host': '' });
  const legend = h('div', { class: 'map-legend' });
  put(main, h('h1', {}, 'Map'),
    h('p', { class: 'subtitle' }, 'Your tracked routes. Stops are coloured by how early (blue) or late (orange) buses arrive there.'),
    h('div', { class: 'map-layout' }, panel, h('div', { class: 'map-wrap' }, canvas, legend)), tableHost);

  const saved = loadMapState(); // what you left on the map last time
  const st = { data: null, visible: new Set(), hidden: new Set(saved.hidden), areas: saved.areas, shapes: {}, stops: saved.stops, win: saved.win, cap: 300, map: null, ready: false, stopsById: new Map() };
  const persist = () => saveMapState({ areas: st.areas, stops: st.stops, win: st.win, hidden: [...st.hidden] });
  const latest = makeLatest(signal);
  const query = () => ({ ...s.apiQuery, ...(st.win[0] === 0 && st.win[1] === MAX ? {} : { minutes_from: st.win[0], minutes_to: st.win[1] }) });

  function capNow(stops) {
    const fixed = getSettings().charts.heatCap;
    return fixed > 0 ? fixed * 60 : autoCap(stops);
  }

  function drawLegend() {
    const m = fmtNum(st.cap / 60, st.cap % 60 ? 1 : 0);
    const bar = h('div', { class: 'lg-bar', 'aria-hidden': 'true' });
    if (bar.style) bar.style.background = `linear-gradient(90deg, ${BLUE}, ${NEUTRAL}, ${ORANGE})`; // CSSOM: allowed by the CSP
    legend.replaceChildren(h('div', { class: 'lg-title' }, 'Mean delay vs schedule'), bar,
      h('div', { class: 'lg-ticks' }, h('span', {}, `−${m} min or earlier`), h('span', {}, 'on time'), h('span', {}, `+${m} min or later`)),
      h('div', { class: 'lg-nodata' }, h('span', { class: 'lg-dot' }), 'no data in this window'));
  }

  // Push the current selection into the map sources.
  function refresh() {
    if (!st.data) return;
    const stops = visibleStops(st.data, st.visible);
    st.stopsById = new Map(stops.map((x) => [x.stop_id, x]));
    st.cap = capNow(stops);
    drawLegend();
    drawSummary(stops);
    const m = st.map;
    if (!m || !st.ready) return;
    m.getSource('stops').setData(stopFeatures(stops));
    m.getSource('routes').setData(routeLines(st.data, st.visible, st.shapes));
    m.getSource('regions').setData(areaCells(st.areas, stops, getSettings().map.areaSize));
    drawSmooth(stops);
    m.setPaintProperty('regions', 'fill-opacity', (getSettings().map.areaOpacity * (st.areas === 'circles' ? 0.6 : 0.8)));
    for (const id of ['stops', 'regions']) m.setPaintProperty(id, id === 'stops' ? 'circle-color' : 'fill-color', colourExpr(st.cap));
    m.setLayoutProperty('regions', 'visibility', st.areas === 'hex' || st.areas === 'circles' ? 'visible' : 'none');
    for (const id of ['stops', 'stops-nodata']) m.setLayoutProperty(id, 'visibility', st.stops ? 'visible' : 'none');
  }

  // Smooth mode: a continuous colour raster under the route lines; rebuilt on each change (cheap, and a fresh canvas source
  // always draws its first frame).
  function drawSmooth(stops) {
    const m = st.map;
    if (m.getLayer('smooth')) m.removeLayer('smooth');
    if (m.getSource('smooth')) m.removeSource('smooth');
    if (st.areas !== 'smooth') return;
    const r = smoothRaster(stops, { cap: st.cap, alpha: getSettings().map.areaOpacity, radiusM: smoothReach(getSettings().map.areaSize) });
    const canvas = r && hooks.makeCanvas(r);
    if (!canvas) return;
    m.addSource('smooth', { type: 'canvas', canvas, coordinates: r.coordinates, animate: false });
    m.addLayer({ id: 'smooth', type: 'raster', source: 'smooth', paint: { 'raster-opacity': 1, 'raster-resampling': 'linear', 'raster-fade-duration': 0 } }, 'routes');
  }

  const summary = h('p', { class: 'hint' });
  const tableBox = h('details', { class: 'map-table' });
  function drawSummary(stops) {
    const withData = stops.filter((x) => x.n > 0).length;
    drawTable(stops);
    summary.textContent = `${stops.length} stops on ${st.visible.size} route${st.visible.size === 1 ? '' : 's'}, ${withData} with data · ${windowLabel(...st.win)} · ${windowText(st.data)}`;
  }

  // Accessible alternative to the canvas, and the fallback when the map cannot load.
  function drawTable(stops) {
    const rows = [...stops].sort((a, b) => (b.mean_delay ?? -Infinity) - (a.mean_delay ?? -Infinity)).slice(0, 300);
    tableBox.replaceChildren(h('summary', {}, `Stops as a table (${stops.length}${stops.length > 300 ? ', first 300 by mean delay' : ''})`),
      dataTable({ caption: 'Mean delay per stop for the routes shown (negative = early)', rows, columns: [
        { label: 'Stop', get: (x) => (x.code ? `${x.name || x.stop_id} (${x.code})` : x.name || x.stop_id) },
        { label: 'Routes', get: (x) => x.routes.map((r) => r.route).join(', ') || '–' },
        { label: 'n', num: true, get: (x) => fmtCount(x.n) }, { label: 'Mean delay', num: true, get: (x) => fmtDelay(x.mean_delay) }] }));
  }

  function drawPanel() {
    const routes = st.data?.routes || [];
    const slider = timeRangeSlider({ from: st.win[0], to: st.win[1], onChange: (a, b) => { st.win = [a, b]; persist(); load(); } });
    const boxes = [];
    const chips = h('div', { class: 'route-chips', role: 'group', 'aria-label': 'Routes shown' }, routes.map((r) => {
      const box = h('input', { type: 'checkbox', checked: st.visible.has(r.route), on: { change: (e) => { if (e.target.checked) { st.visible.add(r.route); st.hidden.delete(r.route); } else { st.visible.delete(r.route); st.hidden.add(r.route); } persist(); refresh(); } } });
      const sw = h('span', { class: 'chip-swatch' });
      if (sw.style) sw.style.background = routeColour(st.data, r.route);
      boxes.push([r.route, box]);
      return h('label', { class: 'chip' }, box, sw, `Route ${r.route}`);
    }));
    const setAll = (on) => { st.visible = new Set(on ? routes.map((r) => r.route) : []); st.hidden = new Set(on ? [] : routes.map((r) => r.route)); persist(); for (const [, b] of boxes) b.checked = on; refresh(); };
    panel.replaceChildren();
    put(panel,
      h('h2', {}, 'Routes'), chips,
      h('div', { class: 'row' }, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => setAll(true) } }, 'All'), h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => setAll(false) } }, 'None'),
        h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => fit() } }, 'Fit to routes')),
      h('h2', {}, 'Layers'),
      h('label', { class: 'inline' }, h('input', { type: 'checkbox', checked: st.stops, on: { change: (e) => { st.stops = e.target.checked; persist(); refresh(); } } }), 'Show stops'),
      h('label', { for: 'areas' }, 'Colour areas where nearby stops run early or late',
        h('select', { id: 'areas', on: { change: (e) => { st.areas = e.target.value; persist(); refresh(); } } }, AREA_MODES.map(([v, l]) => h('option', { value: v, selected: v === st.areas }, l)))),
      h('h2', {}, 'Time of day'), slider.el, status, summary,
      h('p', { class: 'hint attribution' }, 'Basemap loaded from tiles.openfreemap.org · © OpenMapTiles · data © OpenStreetMap contributors. Date range follows the default window; change it on a route’s analytics page.'));
    if (st.data && !routes.length) put(panel, st.data.unavailable?.length ? notice(`The bus data server could not list stops for ${st.data.unavailable.join(', ')}.`) : null);
  }

  function fit() {
    const stops = [...st.stopsById.values()];
    if (!st.map || !stops.length) return;
    const lons = stops.map((x) => x.lon), lats = stops.map((x) => x.lat);
    st.map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: 48, duration: 0, maxZoom: 15 });
  }

  async function load(first = false) {
    const tok = latest.start();
    status.replaceChildren(h('span', { class: 'muted' }, 'Loading…'));
    try {
      const [data, sh] = await Promise.all([api.map(query(), { signal: tok.signal }),
        first ? api.mapShapes({ signal: tok.signal }).catch((e) => { if (isAbort(e)) throw e; return null; }) : null]);
      if (first) st.shapes = sh?.shapes || {}; // no shapes: lines through the stops instead
      if (!tok.isCurrent()) return;
      const routes = data.routes.map((r) => r.route);
      st.visible = new Set(routes.filter((r) => !st.hidden.has(r))); // routes you switched off stay off; new ones start on
      st.data = data;
      status.replaceChildren();
      if (first) { drawPanel(); tableHost.replaceChildren(tableBox); }
      if (first && !routes.length) { canvas.replaceChildren(); put(canvas, emptyState('No tracked routes with known stops yet. Track a route in the sidebar and let the poller collect arrivals.')); return; }
      refresh();
      if (first && !saved.view) fit();
    } catch (e) {
      if (isAbort(e) || !tok.isCurrent()) return;
      status.replaceChildren(errorBox(e, () => load(first)));
    }
  }

  function wireTips(m) {
    m.on('mousemove', (e) => {
      const { x: px, y: py } = e.point, pad = 7; // padded hover box so small stops are easy to hit
      const hit = m.queryRenderedFeatures([[px - pad, py - pad], [px + pad, py + pad]], { layers: ['stops', 'stops-nodata'] })[0];
      const cell = hit ? null : m.queryRenderedFeatures(e.point, { layers: ['regions'] })[0];
      m.getCanvas().style.cursor = hit || cell ? 'pointer' : '';
      const x = e.originalEvent?.clientX ?? 0, y = e.originalEvent?.clientY ?? 0;
      if (hit) { const stop = st.stopsById.get(hit.properties.id); if (stop) { showTip(stopTipData({ id: stop.stop_id, name: stop.name, code: stop.code, n: stop.n, delay: stop.mean_delay, routes: stop.routes }), x, y); return; } }
      if (cell) { showTip(cellTipData(cell.properties), x, y); return; }
      hideTip();
    });
    m.getCanvas().addEventListener?.('mouseleave', hideTip);
  }

  try {
    const lib = await loadMapLibre();
    if (signal.aborted) return;
    const dark = isDark();
    const m = new lib.Map({ container: canvas, style: STYLE_URL, center: saved.view ? [saved.view.lng, saved.view.lat] : CENTRE, zoom: saved.view ? saved.view.zoom : 11, attributionControl: { compact: true }, dragRotate: false });
    st.map = m;
    m.addControl(new lib.NavigationControl({ showCompass: false }), 'top-right');
    m.on('load', () => {
      if (dark) darkenStyle(m); else softenBuildings(m);
      m.addSource('regions', { type: 'geojson', data: empty }); m.addSource('routes', { type: 'geojson', data: empty }); m.addSource('stops', { type: 'geojson', data: empty });
      m.addLayer({ id: 'regions', type: 'fill', source: 'regions', paint: { 'fill-color': colourExpr(st.cap), 'fill-opacity': dark ? 0.42 : 0.38, 'fill-outline-color': 'rgba(0,0,0,0)' } });
      m.addLayer({ id: 'routes', type: 'line', source: 'routes', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': ['get', 'colour'], 'line-width': ['interpolate', ['linear'], ['zoom'], 9, 1.2, 15, 3], 'line-opacity': 0.75 } });
      const radius = ['interpolate', ['linear'], ['zoom'], 9, ['*', ['get', 'r'], 0.55], 14, ['get', 'r'], 17, ['*', ['get', 'r'], 1.7]];
      m.addLayer({ id: 'stops-nodata', type: 'circle', source: 'stops', filter: ['==', ['get', 'delay'], null], paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 9, 2, 15, 4], 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': '#8190a4', 'circle-stroke-width': 1.2 } });
      m.addLayer({ id: 'stops', type: 'circle', source: 'stops', filter: ['!=', ['get', 'delay'], null], paint: { 'circle-radius': radius, 'circle-color': colourExpr(st.cap), 'circle-stroke-color': dark ? '#0d1117' : '#ffffff', 'circle-stroke-width': 1.2 } });
      st.ready = true;
      wireTips(m);
      refresh();
      if (st.data && !saved.view) fit();
    });
    let moveTimer = null;
    m.on('moveend', () => { clearTimeout(moveTimer); moveTimer = setTimeout(() => { const c = m.getCenter?.(); if (c) saveMapState({ view: { lng: c.lng, lat: c.lat, zoom: m.getZoom() } }); }, 400); });
    const off = onSettings(() => refresh()); // e.g. area strength changed on the Settings page
    signal.addEventListener('abort', () => { off(); clearTimeout(moveTimer); });
    m.on('error', (e) => { if (e?.error && /tiles|style|fetch|Failed/i.test(String(e.error.message))) status.replaceChildren(notice('Some map tiles could not be loaded. Stops and routes are still shown.', 'info')); });
    signal.addEventListener('abort', () => { hideTip(); try { m.remove(); } catch { /* already gone */ } });
  } catch (e) {
    canvas.replaceChildren();
    put(canvas, notice(`${e.message} Stops are listed on each route’s page instead.`), h('p', {}, link('#/', 'Back to routes')));
  }
  await load(true);
}
