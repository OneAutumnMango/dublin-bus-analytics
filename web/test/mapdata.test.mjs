import test from 'node:test';
import assert from 'node:assert/strict';
import * as M from '../js/mapdata.js';

const data = () => ({
  routes: [{ route: '15', directions: { out: ['a', 'b'], back: ['b', 'a'] } }, { route: '46A', directions: { out: ['b', 'c'] } }],
  stops: [
    { stop_id: 'a', code: '2051', name: 'Alpha', lat: 53.3, lon: -6.2, n: 30, mean_delay: 90, routes: [{ route: '15', n: 30, mean_delay: 90 }] },
    { stop_id: 'b', code: '7', name: 'Beta', lat: 53.3005, lon: -6.2005, n: 40, mean_delay: 225, routes: [{ route: '15', n: 10, mean_delay: 0 }, { route: '46A', n: 30, mean_delay: 300 }] },
    { stop_id: 'c', code: '9', name: 'Gamma', lat: 53.4, lon: -6.3, n: 0, mean_delay: null, routes: [] },
  ],
});
const noBad = (o) => assert.ok(!/NaN|undefined|Infinity/.test(JSON.stringify(o)), JSON.stringify(o).slice(0, 200));

test('delayColour: Okabe-Ito diverging scale, saturates, tolerates junk', () => {
  assert.equal(M.delayColour(-300, 300), '#0072b2');
  assert.equal(M.delayColour(300, 300), '#e69f00');
  assert.equal(M.delayColour(0, 300), M.NEUTRAL);
  assert.equal(M.delayColour(9999, 300), '#e69f00');
  assert.equal(M.delayColour(-9999, 300), '#0072b2');
  assert.notEqual(M.delayColour(150, 300), M.delayColour(300, 300));
  for (const bad of [null, undefined, NaN, 'x']) assert.equal(M.delayColour(bad, 300), M.NEUTRAL);
  assert.equal(M.delayColour(100, 0), M.NEUTRAL);
  assert.deepEqual(M.colourStops(300).map((s) => s[0]), [-300, 0, 300]);
});

test('labels', () => {
  assert.equal(M.minutesLabel(0), '00:00'); assert.equal(M.minutesLabel(1439), '23:59'); assert.equal(M.minutesLabel(365), '06:05');
  assert.equal(M.windowLabel(360, 630), '06:00 – 10:30');
  assert.match(M.windowLabel(1320, 120), /22:00 – 02:00 \(overnight\)/);
});

test('visibleStops recomputes from visible routes only and drops unlisted stops', () => {
  const all = M.visibleStops(data(), new Set(['15', '46A']));
  assert.equal(all.length, 3);
  const only15 = M.visibleStops(data(), new Set(['15']));
  assert.deepEqual(only15.map((s) => s.stop_id), ['a', 'b']);
  const b = only15.find((s) => s.stop_id === 'b');
  assert.equal(b.n, 10); assert.equal(b.mean_delay, 0); assert.equal(b.routes.length, 1);
  assert.equal(M.visibleStops(data(), new Set()).length, 0);
  assert.equal(M.visibleStops({}, new Set(['15'])).length, 0);
  assert.equal(all.find((s) => s.stop_id === 'b').mean_delay, 225);
});

test('stopFeatures / routeLines GeoJSON shapes, no NaN, colours cycle', () => {
  const f = M.stopFeatures(M.visibleStops(data(), new Set(['15', '46A'])));
  assert.equal(f.features.length, 3);
  assert.deepEqual(f.features[0].geometry.coordinates, [-6.2, 53.3]);
  assert.equal(f.features[2].properties.delay, null);
  noBad(f);
  const lines = M.routeLines(data(), new Set(['15']));
  assert.equal(lines.features.length, 2);
  assert.ok(lines.features.every((l) => l.properties.route === '15' && l.properties.colour === M.ROUTE_COLOURS[0]));
  assert.equal(M.routeLines(data(), new Set(['46A'])).features[0].properties.colour, M.ROUTE_COLOURS[1]);
  assert.equal(M.routeColour(data(), '46A'), M.ROUTE_COLOURS[1]);
  assert.equal(M.routeLines({ routes: [{ route: 'x', directions: { out: ['zz'] } }], stops: [] }, new Set(['x'])).features.length, 0);
});

test('hexCells: groups nearby stops, needs 2 stops and enough samples, closed rings', () => {
  const cells = M.hexCells(M.visibleStops(data(), new Set(['15', '46A'])));
  assert.equal(cells.features.length, 1, 'a and b are ~65 m apart; c has no data');
  const p = cells.features[0].properties;
  assert.equal(p.stops, 2); assert.equal(p.n, 70);
  assert.equal(p.delay, (30 * 90 + 40 * 225) / 70);
  const ring = cells.features[0].geometry.coordinates[0];
  assert.equal(ring.length, 7); assert.deepEqual(ring[0], ring[6]);
  noBad(cells);
  assert.equal(M.hexCells([]).features.length, 0);
  assert.equal(M.hexCells(M.visibleStops(data(), new Set(['15'])), { minN: 1000 }).features.length, 0);
  const lone = [{ stop_id: 'a', lat: 53.3, lon: -6.2, n: 500, mean_delay: 60 }];
  assert.equal(M.hexCells(lone).features.length, 0, 'a single stop never makes a region');
  const far = [{ stop_id: 'a', lat: 53.3, lon: -6.2, n: 500, mean_delay: 60 }, { stop_id: 'b', lat: 53.4, lon: -6.2, n: 500, mean_delay: 60 }];
  assert.equal(M.hexCells(far).features.length, 0, 'distant stops do not share a cell');
});

test('tooltip payloads', () => {
  const s = M.visibleStops(data(), new Set(['15', '46A'])).find((x) => x.stop_id === 'b');
  const t = M.stopTipData({ id: s.stop_id, name: s.name, code: s.code, n: s.n, delay: s.mean_delay, routes: s.routes });
  assert.equal(t.title, 'Beta (7)'); assert.equal(t.head.sec, 225);
  assert.deepEqual(t.rows.map((r) => r.l), ['Route 15', 'Route 46A', 'Samples']);
  noBad(t);
  assert.equal(M.stopTipData({ id: 'x', name: '', code: '', n: 0, delay: null, routes: [] }).head, null);
  assert.match(M.stopTipData({ id: 'x', name: 'N', n: 3, delay: 5, routes: [] }).foot, /Fewer than 10/);
  const c = M.cellTipData({ stops: 4, n: 120, delay: -30 });
  assert.equal(c.sub, '4 stops with data'); assert.equal(c.head.sec, -30);
});

test('regions never move when other stops come and go (fixed projection origin)', () => {
  const all = M.visibleStops(data(), new Set(['15', '46A']));
  const near = all.filter((s) => s.stop_id !== 'c');
  const farAway = [...near, { stop_id: 'z', lat: 53.5, lon: -6.0, n: 80, mean_delay: -90, routes: [] }];
  const ring = (g) => JSON.stringify(g.features[0].geometry);
  assert.equal(ring(M.hexCells(near)), ring(M.hexCells(farAway)), 'adding a distant stop must not shift the hexagon');
  assert.equal(M.hexCells(near).features.length, 1);
});

test('circleCells: one disc per stop with data', () => {
  const stops = M.visibleStops(data(), new Set(['15', '46A']));
  const circles = M.circleCells(stops);
  assert.equal(circles.features.length, 2);
  assert.equal(circles.features[0].geometry.coordinates[0].length, 25);
  noBad(circles);
  assert.equal(M.areaCells('off', stops).features.length, 0);
  assert.equal(M.areaCells('smooth', stops).features.length, 0, 'smooth is a raster, not GeoJSON');
  assert.equal(M.areaCells('hex', stops).features.length, 1);
  assert.deepEqual(M.AREA_MODES.map((m) => m[0]), ['off', 'hex', 'circles', 'smooth']);
});

test('smoothRaster: continuous field, colour from weighted mean, fades away from stops, bounded size', () => {
  const stops = [{ stop_id: 'a', lat: 53.3, lon: -6.2, n: 100, mean_delay: -300 }, { stop_id: 'b', lat: 53.304, lon: -6.2, n: 100, mean_delay: 300 }];
  const r = M.smoothRaster(stops, { cap: 300 });
  assert.equal(r.data.length, r.width * r.height * 4);
  assert.equal(r.coordinates.length, 4);
  const px = (lon, lat) => {
    const [[w, n], [e], [, s]] = r.coordinates;
    const i = Math.floor(((lon - w) / (e - w)) * r.width), j = Math.floor(((n - lat) / (n - s)) * r.height);
    const o = (j * r.width + i) * 4; return [...r.data.slice(o, o + 4)];
  };
  const early = px(-6.2, 53.3), late = px(-6.2, 53.304), mid = px(-6.2, 53.302);
  assert.ok(early[2] > early[0] && early[3] > 100, 'blue-ish and opaque at the early stop');
  assert.ok(late[0] > late[2] && late[3] > 100, 'orange-ish and opaque at the late stop');
  assert.ok(mid[3] > 0 && Math.abs(mid[0] - 0xc9) < 40, 'between them the colour passes through neutral (no hard edge)');
  assert.equal(r.data[3], 0, 'corner pixels are transparent');
  // smoothness: neighbouring pixels along a row never jump more than a small amount
  let worst = 0;
  const row = Math.floor(r.height / 2);
  for (let i = 1; i < r.width; i++) worst = Math.max(worst, Math.abs(r.data[(row * r.width + i) * 4 + 3] - r.data[(row * r.width + i - 1) * 4 + 3]));
  assert.ok(worst < 40, `opacity steps are gentle (${worst})`);
  assert.equal(M.smoothRaster([]), null);
  const wide = M.smoothRaster([...stops, { stop_id: 'z', lat: 53.6, lon: -5.8, n: 50, mean_delay: 0 }], { maxPx: 300 });
  assert.ok(wide.width <= 300 && wide.height <= 300 && wide.pxM > 50, 'coarsens instead of exploding');
  noBad(r.coordinates);
});

test('routeLines uses real shapes when given, stop lines otherwise', () => {
  const real = [[-6.2, 53.3], [-6.201, 53.3004], [-6.2005, 53.3005]];
  const lines = M.routeLines(data(), new Set(['15']), { 15: { out: real } });
  const out = lines.features.find((f) => f.properties.dir === 'out'), back = lines.features.find((f) => f.properties.dir === 'back');
  assert.deepEqual(out.geometry.coordinates, real);
  assert.equal(back.geometry.coordinates.length, 2, 'direction without a shape falls back to its stops');
  assert.equal(M.routeLines(data(), new Set(['15']), null).features.length, 2);
});

test('area size scales hexagons, circles and the smooth reach', () => {
  const stops = M.visibleStops(data(), new Set(['15', '46A']));
  const width = (g) => { const xs = g.features[0].geometry.coordinates[0].map((p) => p[0]); return Math.max(...xs) - Math.min(...xs); };
  assert.ok(width(M.areaCells('circles', stops, 800)) > width(M.areaCells('circles', stops, 250)) * 3);
  assert.ok(M.areaCells('hex', stops, 150).features.length <= 1, 'tiny hexagons: the two stops ~65 m apart may no longer share a cell');
  assert.equal(M.areaCells('hex', stops, 2000).features.length, 1);
  assert.equal(M.smoothReach(400), 450);
  const small = M.smoothRaster(stops, { radiusM: M.smoothReach(150) }), big = M.smoothRaster(stops, { radiusM: M.smoothReach(1200) });
  assert.ok(big.width > small.width, 'a larger spread covers more ground');
});
