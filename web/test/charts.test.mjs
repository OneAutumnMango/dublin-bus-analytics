import test from 'node:test';
import assert from 'node:assert/strict';
import * as C from '../js/charts.js';

const item = (label, mean, min, max, lo, hi, n = 50) => ({ label, mean, min, max, lo, hi, n, low_n: n < 10 });

test('rangeDomain includes zero, covers data, tolerates all-null', () => {
  const d = C.rangeDomain([item('a', 120, -60, 600, -30, 400), item('b', 60, 0, 300, 0, 200)]);
  assert.ok(d[0] <= -60 && d[1] >= 600 && d[0] <= 0);
  assert.deepEqual(C.rangeDomain([]), [-60, 60]);
  assert.deepEqual(C.rangeDomain([item('x', null, null, null, null, null, 0)]), [-60, 60]);
  const only = C.rangeDomain([item('x', 300, 300, 300, 300, 300)]);
  assert.ok(only[0] <= 0 && only[1] >= 300);
});

test('rangeGeometry: ordering, zero line, clamping, empty rows', () => {
  const items = [item('a', 120, -60, 600, -30, 400), item('b', null, null, null, null, null, 0), item('c', -300, -900, 0, -600, -10, 5)];
  const g = C.rangeGeometry(items);
  assert.equal(g.rows.length, 3);
  const [a, b, c] = g.rows;
  assert.ok(a.xMin <= a.xLo && a.xLo <= a.xMean && a.xMean <= a.xHi && a.xHi <= a.xMax);
  assert.equal(b.empty, true);
  assert.equal(b.xMean, null);
  assert.equal(c.lowN, true);
  assert.ok(c.xMean < g.zeroX && a.xMean > g.zeroX, 'early left of zero, late right of zero');
  for (const r of g.rows) for (const k of ['xMean', 'xMin', 'xMax', 'xLo', 'xHi']) if (r[k] !== null) assert.ok(r[k] >= g.x0 && r[k] <= g.x1);
  assert.ok(g.zeroX > g.x0 && g.zeroX < g.x1);
  assert.ok(a.y < b.y && b.y < c.y);
  assert.ok(g.height > c.y);
  assert.ok(g.ticks.length >= 2 && g.ticks.some((t) => t.label === '0' && t.x === g.zeroX));
});

test('rangeGeometry with no items and with forecast-style rows lacking min/max', () => {
  const g0 = C.rangeGeometry([]);
  assert.deepEqual(g0.rows, []);
  assert.ok(Number.isFinite(g0.height));
  const g = C.rangeGeometry([item('30 min', 30, null, null, -90, 150)]);
  assert.equal(g.rows[0].xMin, null);
  assert.ok(g.rows[0].xLo < g.rows[0].xHi);
});

test('rangeGeometry honours an external domain (shared scale across pages)', () => {
  const g1 = C.rangeGeometry([item('a', 60, 0, 120, 0, 100)], [-600, 600]);
  const g2 = C.rangeGeometry([item('a', 60, 0, 120, 0, 100)], [-60, 120]);
  assert.ok(g1.rows[0].xMean < g2.rows[0].xMean);
  assert.equal(g1.zeroX, (g1.x0 + g1.x1) / 2);
});

function bins() {
  const out = [{ lo: null, hi: -600, count: 1 }];
  for (let lo = -600, i = 0; lo < 1800; lo += 60, i++) out.push({ lo, hi: lo + 60, count: i === 10 ? 40 : i % 3 });
  out.push({ lo: 1800, hi: null, count: 2 });
  return out;
}

test('histogramGeometry: bars, zero at the 0 edge, markers clamp, ticks', () => {
  const g = C.histogramGeometry(bins());
  assert.equal(g.bars.length, 42);
  const tallest = g.bars.reduce((a, b) => (b.h > a.h ? b : a));
  assert.equal(tallest.count, 40);
  assert.ok(Math.abs(tallest.h - g.plotH) < 0.01);
  for (const b of g.bars) { assert.ok(b.x >= g.left && b.x + b.w <= g.left + g.plotW + 0.01); assert.ok(b.h >= 0 && b.y + b.h <= g.top + g.plotH + 0.01); }
  const zeroBin = g.bars.find((b) => b.lo === 0);
  assert.ok(Math.abs(g.zeroX - zeroBin.x + 0.5) < 0.02, 'zero line on the left edge of the bin starting at 0');
  assert.ok(g.xOf(-5000) < g.xOf(-600) && g.xOf(5000) > g.xOf(1800));
  assert.ok(g.xOf(-100) < g.xOf(0) && g.xOf(0) < g.xOf(100));
  assert.equal(g.xOf(null), null);
  assert.ok(g.xTicks.some((t) => t.label === '0' && t.x === g.zeroX));
  assert.equal(g.empty, false);
  assert.match(g.bars[0].label, /below \u221210/);
  assert.match(g.bars[41].label, /30 min and above/);
});

test('histogramGeometry degenerate inputs', () => {
  for (const b of [null, undefined, []]) {
    const g = C.histogramGeometry(b);
    assert.deepEqual(g.bars, []);
    assert.equal(g.empty, true);
    assert.equal(g.zeroX, null);
  }
  const z = bins().map((b) => ({ ...b, count: 0 }));
  const g = C.histogramGeometry(z);
  assert.equal(g.empty, true);
  assert.ok(g.bars.every((b) => b.h === 0 && Number.isFinite(b.y)));
  const tiny = C.histogramGeometry([{ lo: null, hi: 0, count: 1 }, { lo: 0, hi: null, count: 1 }]);
  assert.equal(tiny.bars.length, 2);
  assert.ok(tiny.bars.every((b) => Number.isFinite(b.x) && Number.isFinite(b.h)));
});

test('heatmapGrid: 168 cells, empty distinct, symmetric colours, saturation floor', () => {
  const g = C.heatmapGrid([{ weekday: 0, hour: 8, n: 30, low_n: false, mean_delay: 300 }, { weekday: 6, hour: 23, n: 3, low_n: true, mean_delay: -300 },
    { weekday: 9, hour: 1, n: 5, low_n: false, mean_delay: 5 }, { weekday: 1, hour: 99, n: 5, mean_delay: 5 }, null]);
  assert.equal(g.cells.length, 168);
  const a = g.cells.find((c) => c.wd === 0 && c.hour === 8);
  const b = g.cells.find((c) => c.wd === 6 && c.hour === 23);
  assert.equal(a.bucket, 5);
  assert.equal(b.bucket, -5);
  assert.equal(b.lowN, true);
  assert.equal(g.cells.filter((c) => c.empty).length, 166);
  assert.ok(g.cells.filter((c) => c.empty).every((c) => c.bucket === null && c.n === 0));
  const small = C.heatmapGrid([{ weekday: 0, hour: 0, n: 20, low_n: false, mean_delay: 5 }]);
  assert.equal(small.cells[0].bucket, 0, 'a 5 s mean must not look extreme');
  assert.equal(C.heatmapGrid([]).cells.every((c) => c.empty), true);
  assert.equal(C.heatmapGrid(null).cells.length, 168);
  const xs = new Set(g.cells.map((c) => `${c.x},${c.y}`));
  assert.equal(xs.size, 168);
});

test('linePath breaks at gaps', () => {
  assert.equal(C.linePath([]), '');
  assert.equal(C.linePath([{ x: 0, y: 1 }, { x: 1, y: 2 }]), 'M0 1L1 2');
  assert.equal(C.linePath([{ x: 0, y: 1 }, { x: 1, y: null }, { x: 2, y: 3 }, { x: 3, y: 4 }]), 'M0 1M2 3L3 4');
  assert.equal(C.linePath([{ x: 0, y: NaN }]), '');
  assert.equal(C.linePath([{ x: 5, y: 1 }]), 'M5 1');
});

test('accumulationGeometry: points, bars up/down, null deltas, single stop, empty', () => {
  const stops = [{ stop_id: 'a', mean_delay: 0, segment_delta: null, n: 20, n_delta: 0 },
    { stop_id: 'b', mean_delay: 120, segment_delta: 120, n: 20, n_delta: 18 },
    { stop_id: 'c', mean_delay: 60, segment_delta: -60, n: 20, n_delta: 18 }];
  const g = C.accumulationGeometry(stops);
  assert.equal(g.pts.length, 3);
  assert.ok(g.pts[1].y < g.pts[0].y, 'later is higher');
  assert.ok(g.pts[0].x < g.pts[1].x && g.pts[1].x < g.pts[2].x);
  assert.equal(g.pts[0].y, g.zeroA);
  assert.equal(g.bars[0].delta, null);
  assert.equal(g.bars[0].h, 0);
  assert.ok(g.bars[1].y < g.zeroB && g.bars[1].y + g.bars[1].h <= g.zeroB + 0.01);
  assert.ok(g.bars[2].y >= g.zeroB - 0.01);
  assert.match(g.path, /^M/);
  const one = C.accumulationGeometry([stops[1]]);
  assert.equal(one.pts.length, 1);
  assert.ok(Number.isFinite(one.pts[0].x) && Number.isFinite(one.pts[0].y));
  const none = C.accumulationGeometry([]);
  assert.equal(none.path, '');
  assert.ok(Number.isFinite(none.height));
  const nulls = C.accumulationGeometry([{ stop_id: 'x', mean_delay: null, segment_delta: null, n: 0, n_delta: 0 }]);
  assert.equal(nulls.pts[0].y, null);
  const many = C.accumulationGeometry(Array.from({ length: 80 }, (_, i) => ({ stop_id: String(i), mean_delay: i, segment_delta: 1, n: 5, n_delta: 5 })));
  assert.ok(many.xTicks.length <= 14);
});

test('stepGeometry: path closes at last sample, markers inside, degenerate', () => {
  const steps = [{ t: 1000, due: 5 }, { t: 1060, due: 4 }, { t: 1120, due: 4 }];
  const g = C.stepGeometry(steps, [{ t: 1100, label: 'arrived', kind: 'arrival' }]);
  assert.match(g.path, /^M[\d.]+ [\d.]+H/);
  assert.equal(g.endX, g.x1);
  assert.ok(g.markers[0].x > g.x0 && g.markers[0].x < g.x1);
  assert.ok(g.sy(5) < g.sy(0));
  const single = C.stepGeometry([{ t: 5, due: 0 }]);
  assert.ok(Number.isFinite(single.endX));
  assert.equal(C.stepGeometry([]).empty, true);
  assert.equal(C.stepGeometry([]).path, '');
  const wide = C.stepGeometry([{ t: 1000, due: 1 }], [{ t: 2000, label: 's', kind: 'scheduled' }]);
  assert.ok(wide.markers[0].x === wide.x1 && wide.endX === wide.x0);
});

test('rangeSummary text', () => {
  assert.match(C.rangeSummary([]), /No stops/);
  const s = C.rangeSummary([item('A', 60, 0, 0, 0, 0), item('B', -120, 0, 0, 0, 0), item('C', null, 0, 0, 0, 0, 0)]);
  assert.match(s, /2 of 3 stops/);
  assert.match(s, /\u22122\.0 min \(B\)/);
  assert.match(s, /\+1\.0 min \(A\)/);
});

test('histogram y ticks are integers without duplicates (tiny counts)', () => {
  const g = C.histogramGeometry([{ lo: null, hi: -600, count: 1 }, { lo: -600, hi: null, count: 0 }]);
  const labels = g.yTicks.map((t) => t.label);
  assert.deepEqual(labels, ['0', '1']);
});

test('accumulation geometry follows positions and breaks the line at gaps', () => {
  const mk = (position, mean) => ({ stop_id: `s${position}`, position, mean_delay: mean, segment_delta: mean === null ? null : 10, n: 20, n_delta: 15 });
  const g = C.accumulationGeometry([mk(0, 0), mk(1, 60), mk(3, 120), mk(4, null), mk(5, 30)]);
  assert.equal((g.path.match(/M/g) || []).length, 3, 'gap at position 2 and a null mean both break the line');
  assert.ok(g.pts[2].x - g.pts[1].x > g.pts[1].x - g.pts[0].x, 'x follows position, not index');
  assert.deepEqual(g.xTicks.map((t) => t.label).slice(0, 3), ['1', '2', '4']);
});

test('rangeTip omits missing min/max and shows the band', () => {
  const g = C.rangeGeometry([{ label: '30 min before', mean: 60, min: null, max: null, lo: -60, hi: 120, n: 1, low_n: true }]);
  const tip = C.rangeTip(g.rows[0]);
  assert.ok(!/min –|max –/.test(tip));
  assert.match(tip, /band/);
  assert.match(tip, /1 sample\b/);
  assert.match(tip, /Fewer than 10/);
});

test('areaPath: closed fill per contiguous run, empty when nothing to fill', async () => {
  assert.equal(C.areaPath([{ x: 0, y: 10 }, { x: 10, y: 20 }], 100), 'M0 10L10 20V100H0Z');
  assert.equal(C.areaPath([{ x: 0, y: null }, { x: 5, y: 3 }], 100), '');
  assert.equal(C.areaPath([], 100), '');
  const d = C.areaPath([{ x: 0, y: 1 }, { x: 1, y: null }, { x: 2, y: 2 }, { x: 3, y: 3 }], 9);
  assert.equal((d.match(/M/g) || []).length, 1);
  assert.ok(!/NaN/.test(d));
});

test('renderAccumulation draws an area fill with a gradient and no NaN attributes', async () => {
  const { installDom } = await import('./fakedom.mjs');
  const { doc } = installDom();
  const stops = [0, 1, 2].map((i) => ({ stop_id: `s${i}`, position: i, mean_delay: 60 * i, segment_delta: 30, n: 20, n_delta: 20 }));
  const svg = C.renderAccumulation(C.accumulationGeometry(stops), stops, { title: 't', desc: 'd', doc });
  assert.equal(svg.all((n) => n.getAttribute?.('class') === 'acc-area').length, 1);
  assert.equal(svg.byTag('linearGradient').length, 1);
  assert.ok(svg.all((n) => [...(n.attrs?.values?.() || [])].some((v) => /NaN/.test(v))).length === 0);
  const one = [{ stop_id: 'a', position: 0, mean_delay: null, segment_delta: null, n: 0, n_delta: 0 }];
  const svg1 = C.renderAccumulation(C.accumulationGeometry(one), one, { title: 't', desc: 'd', doc });
  assert.equal(svg1.all((n) => n.getAttribute?.('class') === 'acc-area').length, 0);
});

test('chart root has an aria-label and no <title> child (browsers show a root title as a hover tooltip)', async () => {
  const { installDom } = await import('./fakedom.mjs');
  const { doc } = installDom();
  const stops = [0, 1].map((i) => ({ stop_id: `s${i}`, position: i, mean_delay: 60 * i, segment_delta: 30, n: 20, n_delta: 20 }));
  const svg = C.renderAccumulation(C.accumulationGeometry(stops), stops, { title: 'My chart', desc: 'about it', doc });
  assert.equal(svg.getAttribute('aria-label'), 'My chart');
  assert.ok(svg.getAttribute('aria-describedby'));
  assert.equal(svg.children.filter((c) => c.tag === 'title').length, 0);
});

test('marks carry rich tip payloads and padded hit targets', async () => {
  const { installDom } = await import('./fakedom.mjs');
  const { doc } = installDom();
  const stops = [0, 1, 2].map((i) => ({ stop_id: `s${i}`, position: i, mean_delay: 60 * i, segment_delta: 30, n: 20, n_delta: 20 }));
  const acc = C.renderAccumulation(C.accumulationGeometry(stops), stops, { title: 't', desc: 'd', stopLabel: (s) => `L${s.stop_id}`, doc });
  const hits = acc.all((n) => n.getAttribute?.('class') === 'hit');
  assert.ok(hits.some((n) => n.tag === 'circle' && n.getAttribute('r') === '9'));
  assert.ok(hits.every((n) => { try { JSON.parse(n.getAttribute('data-tip-json')); return true; } catch { return false; } }));
  assert.match(acc.all((n) => n.getAttribute?.('class') === 'acc-dot')[0].getAttribute('data-tip-json'), /Stop 1 · Ls0/);
  const items = [{ label: 'A', n: 5, mean: 60, min: 0, max: 120, lo: 0, hi: 100, tip: { title: 'A', rows: [] } }];
  const geo = C.rangeGeometry(items);
  const rc = C.renderRangeChart(geo, { title: 't', desc: 'd', doc });
  const row = rc.all((n) => n.tag === 'g' && n.getAttribute('data-tip-json'))[0];
  assert.ok(row);
  const hit = row.all((n) => n.getAttribute?.('class') === 'hit')[0];
  assert.equal(hit.getAttribute('height'), String(geo.rowH));
  const hm = C.renderHeatmap(C.heatmapGrid([{ weekday: 0, hour: 8, n: 12, mean_delay: 300 }]), { title: 't', desc: 'd', doc });
  assert.match(hm.all((n) => /cell/.test(n.getAttribute?.('class') || '') && /Mon 08:00/.test(n.getAttribute('data-tip-json') || ''))[0].getAttribute('data-tip-json'), /Mon 08:00/);
});
