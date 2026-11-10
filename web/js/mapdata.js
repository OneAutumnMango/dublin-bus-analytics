// Pure helpers for the map page: Okabe-Ito delay scale, stop/route/region GeoJSON, labels, tooltip payloads.
import { isNum, LOW_N_NOTE } from './lib.js';

export const BLUE = '#0072B2', ORANGE = '#E69F00', NEUTRAL = '#c9ced6'; // Okabe-Ito blue / orange, neutral between
export const ROUTE_COLOURS = ['#009E73', '#CC79A7', '#F0E442', '#999999']; // Okabe-Ito bluish green, reddish purple, yellow, grey
export const colourStops = (cap) => [[-cap, BLUE], [0, NEUTRAL], [cap, ORANGE]];

const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const hex = (c) => `#${c.map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')}`;
const mix = (a, b, t) => hex(rgb(a).map((v, i) => v + (rgb(b)[i] - v) * t));

// Colour for a delay in seconds, saturating at +/- capSec.
export function delayColour(sec, capSec) {
  if (!isNum(sec) || !isNum(capSec) || capSec <= 0) return NEUTRAL;
  const t = Math.max(-1, Math.min(1, sec / capSec));
  return t < 0 ? mix(NEUTRAL, BLUE, -t) : mix(NEUTRAL, ORANGE, t);
}

export const minutesLabel = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
export const windowLabel = (a, b) => `${minutesLabel(a)} – ${minutesLabel(b)}${a > b ? ' (overnight)' : ''}`;

// Stops of the visible routes, with n / mean recomputed from those routes only. data = /api/map response.
export function visibleStops(data, visible) {
  const listed = new Set();
  for (const r of data.routes || []) if (visible.has(r.route)) for (const ids of Object.values(r.directions || {})) for (const id of ids) listed.add(id);
  const out = [];
  for (const s of data.stops || []) {
    if (!listed.has(s.stop_id)) continue;
    const rs = (s.routes || []).filter((x) => visible.has(x.route));
    const n = rs.reduce((a, x) => a + x.n, 0);
    out.push({ ...s, routes: rs, n, mean_delay: n ? rs.reduce((a, x) => a + x.n * x.mean_delay, 0) / n : null });
  }
  return out;
}

const point = (s) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
  properties: { id: s.stop_id, name: s.name || '', code: s.code || '', n: s.n, delay: isNum(s.mean_delay) ? s.mean_delay : null,
    r: s.n ? 4 + Math.min(4, Math.log10(s.n + 1)) * 1.2 : 3 } });
export const stopFeatures = (stops) => ({ type: 'FeatureCollection', features: stops.map(point) });

// shapes: {route: {direction: [[lon, lat], ...]}} from /api/map/shapes (real road geometry); a direction without one
// falls back to a straight line through its stops.
export function routeLines(data, visible, shapes = {}) {
  const at = new Map((data.stops || []).map((s) => [s.stop_id, [s.lon, s.lat]]));
  const features = [];
  (data.routes || []).forEach((r, i) => {
    if (!visible.has(r.route)) return;
    for (const [dir, ids] of Object.entries(r.directions || {})) {
      const real = shapes?.[r.route]?.[dir];
      const coordinates = Array.isArray(real) && real.length > 1 ? real : ids.map((id) => at.get(id)).filter(Boolean);
      if (coordinates.length > 1) features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates }, properties: { route: r.route, dir, colour: ROUTE_COLOURS[i % ROUTE_COLOURS.length] } });
    }
  });
  return { type: 'FeatureCollection', features };
}

export const routeColour = (data, route) => ROUTE_COLOURS[Math.max(0, (data.routes || []).findIndex((r) => r.route === route)) % ROUTE_COLOURS.length];

// Fixed projection origin (central Dublin): cell positions never depend on which stops are showing, so toggling
// routes changes colours only, never where the hexagons / grid cells are.
export const ORIGIN = { lat: 53.35, lon: -6.26 };
const KX = 111320 * Math.cos((ORIGIN.lat * Math.PI) / 180), KY = 110540;
const toXY = (s) => [(s.lon - ORIGIN.lon) * KX, (s.lat - ORIGIN.lat) * KY];
const toLL = (x, y) => [ORIGIN.lon + x / KX, ORIGIN.lat + y / KY];
const withData = (stops) => stops.filter((s) => s.n > 0 && isNum(s.mean_delay) && isNum(s.lat) && isNum(s.lon));
const fc = (features) => ({ type: 'FeatureCollection', features });

// Regions, mode "hex": pointy-top hex grid (cell radius sizeM metres); cells need >= minStops stops and >= minN samples.
export function hexCells(stops, { sizeM = 400, minStops = 2, minN = 20 } = {}) {
  const cells = new Map();
  for (const s of withData(stops)) {
    const [x, y] = toXY(s);
    const q = ((Math.sqrt(3) / 3) * x - y / 3) / sizeM, r = ((2 / 3) * y) / sizeM, qs = -q - r; // cube rounding
    let rq = Math.round(q), rr = Math.round(r), rs = Math.round(qs);
    const dq = Math.abs(rq - q), dr = Math.abs(rr - r), ds = Math.abs(rs - qs);
    if (dq > dr && dq > ds) rq = -rr - rs; else if (dr > ds) rr = -rq - rs;
    const key = `${rq},${rr}`;
    const c = cells.get(key) || { q: rq, r: rr, n: 0, sum: 0, stops: 0 };
    c.n += s.n; c.sum += s.n * s.mean_delay; c.stops += 1;
    cells.set(key, c);
  }
  const features = [];
  for (const c of cells.values()) {
    if (c.stops < minStops || c.n < minN) continue;
    const cx = sizeM * Math.sqrt(3) * (c.q + c.r / 2), cy = sizeM * 1.5 * c.r;
    const ring = Array.from({ length: 7 }, (_, i) => { const a = (Math.PI / 180) * (60 * (i % 6) - 30); return toLL(cx + sizeM * Math.cos(a), cy + sizeM * Math.sin(a)); });
    features.push({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] }, properties: { n: c.n, stops: c.stops, delay: c.sum / c.n } });
  }
  return fc(features);
}

// Regions, mode "circles": a translucent disc of radiusM around every stop with data (overlaps blend).
export function circleCells(stops, { radiusM = 300 } = {}) {
  return fc(withData(stops).map((s) => {
    const [x, y] = toXY(s);
    const ring = Array.from({ length: 25 }, (_, i) => { const a = (2 * Math.PI * (i % 24)) / 24; return toLL(x + radiusM * Math.cos(a), y + radiusM * Math.sin(a)); });
    return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] }, properties: { n: s.n, stops: 1, delay: s.mean_delay } };
  }));
}

// Regions, mode "smooth": a fine grid (cellM) of distance-weighted mean delay (Gaussian, reach radiusM) that fades out
// away from stops (`support` 0..1 drives the opacity). Grid is aligned to the fixed origin, so it never shifts.
export function smoothCells(stops, { cellM = 120, radiusM = 450, minSupport = 0.06, maxCells = 40000 } = {}) {
  const used = withData(stops);
  if (!used.length) return fc([]);
  let cell = cellM;
  const build = () => {
    const sigma = radiusM / 2, acc = new Map(), reach = Math.ceil(radiusM / cell);
    for (const s of used) {
      const [x, y] = toXY(s), ci = Math.floor(x / cell), cj = Math.floor(y / cell), w0 = Math.sqrt(s.n);
      for (let i = ci - reach; i <= ci + reach; i++) {
        for (let j = cj - reach; j <= cj + reach; j++) {
          const dx = (i + 0.5) * cell - x, dy = (j + 0.5) * cell - y, d2 = dx * dx + dy * dy;
          if (d2 > radiusM * radiusM) continue;
          const k = Math.exp(-d2 / (sigma * sigma)), key = `${i},${j}`;
          const a = acc.get(key) || { i, j, w: 0, wd: 0, k: 0 };
          a.w += w0 * k; a.wd += w0 * k * s.mean_delay; a.k += k;
          acc.set(key, a);
        }
      }
    }
    return acc;
  };
  let acc = build();
  while (acc.size > maxCells && cell < 2000) { cell *= 2; acc = build(); } // keep the layer light for very large networks
  const features = [];
  for (const a of acc.values()) {
    const support = Math.min(1, a.k);
    if (support < minSupport || !(a.w > 0)) continue;
    const x0 = a.i * cell, y0 = a.j * cell;
    features.push({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [[toLL(x0, y0), toLL(x0 + cell, y0), toLL(x0 + cell, y0 + cell), toLL(x0, y0 + cell), toLL(x0, y0)]] },
      properties: { delay: a.wd / a.w, support: Math.round(support * 100) / 100, n: 0, stops: 0 } });
  }
  return fc(features);
}

export const AREA_MODES = [['off', 'Off'], ['hex', 'Hex grid'], ['circles', 'Circles'], ['smooth', 'Smooth gradient']];
export function areaCells(mode, stops) {
  return mode === 'hex' ? hexCells(stops) : mode === 'circles' ? circleCells(stops) : mode === 'smooth' ? smoothCells(stops) : fc([]);
}

const head = (sec) => (isNum(sec) ? { sec } : null);

export function stopTipData(p) { // p = feature properties of a stop plus `routes` rows
  const rows = (p.routes || []).map((r) => ({ k: 'route', l: `Route ${r.route}`, fmt: 'delay', v: r.mean_delay }));
  rows.push({ k: 'n', l: 'Samples', fmt: 'count', v: p.n ?? 0 });
  const name = p.name || p.id;
  return { title: p.code ? `${name} (${p.code})` : name, head: head(p.delay), rows, foot: p.n > 0 && p.n < 10 ? LOW_N_NOTE : undefined };
}

export function cellTipData(p) {
  return { title: 'Area', sub: `${p.stops} stops with data`, head: head(p.delay), rows: [{ k: 'n', l: 'Samples', fmt: 'count', v: p.n }] };
}

// Colour saturation in seconds when the user has not fixed one: the 90th percentile of |mean| over stops with
// n >= 10, kept between 2 and 10 minutes (5 minutes when nothing qualifies).
export function autoCap(stops) {
  const v = stops.filter((s) => s.n >= 10 && isNum(s.mean_delay)).map((s) => Math.abs(s.mean_delay)).sort((a, b) => a - b);
  if (!v.length) return 300;
  return Math.max(120, Math.min(600, v[Math.min(v.length - 1, Math.floor(v.length * 0.9))]));
}
