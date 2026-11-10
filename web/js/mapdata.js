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

export function routeLines(data, visible) {
  const at = new Map((data.stops || []).map((s) => [s.stop_id, [s.lon, s.lat]]));
  const features = [];
  (data.routes || []).forEach((r, i) => {
    if (!visible.has(r.route)) return;
    for (const [dir, ids] of Object.entries(r.directions || {})) {
      const coordinates = ids.map((id) => at.get(id)).filter(Boolean);
      if (coordinates.length > 1) features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates }, properties: { route: r.route, dir, colour: ROUTE_COLOURS[i % ROUTE_COLOURS.length] } });
    }
  });
  return { type: 'FeatureCollection', features };
}

export const routeColour = (data, route) => ROUTE_COLOURS[Math.max(0, (data.routes || []).findIndex((r) => r.route === route)) % ROUTE_COLOURS.length];

// Regions: pointy-top hex grid (cell radius sizeM metres) over stops with data; cells need >= minStops stops and >= minN samples.
export function hexCells(stops, { sizeM = 400, minStops = 2, minN = 20 } = {}) {
  const used = stops.filter((s) => s.n > 0 && isNum(s.mean_delay) && isNum(s.lat) && isNum(s.lon));
  if (!used.length) return { type: 'FeatureCollection', features: [] };
  const lat0 = used.reduce((a, s) => a + s.lat, 0) / used.length, lon0 = used.reduce((a, s) => a + s.lon, 0) / used.length;
  const kx = 111320 * Math.cos((lat0 * Math.PI) / 180), ky = 110540;
  const toXY = (s) => [(s.lon - lon0) * kx, (s.lat - lat0) * ky];
  const toLL = (x, y) => [lon0 + x / kx, lat0 + y / ky];
  const cells = new Map();
  for (const s of used) {
    const [x, y] = toXY(s);
    let q = ((Math.sqrt(3) / 3) * x - y / 3) / sizeM, r = ((2 / 3) * y) / sizeM, qs = -q - r; // cube rounding
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
  return { type: 'FeatureCollection', features };
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
