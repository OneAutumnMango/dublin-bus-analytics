import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from './fakedom.mjs';
import { ecdfGeometry, qqGeometry, barGeometry, renderEcdf, renderQq, renderBars } from '../js/labcharts.js';

installDom();
const doc = globalThis.document;
const finiteAttrs = (svg) => {
  for (const n of svg.walk()) for (const [k, v] of n.attrs) assert.ok(!/NaN|Infinity|undefined|null/.test(v), `${n.tag}@${k}=${v}`);
};

test('ecdfGeometry: path, markers, empty', () => {
  const g = ecdfGeometry([{ x: -60, p: 0.1 }, { x: 0, p: 0.5 }, { x: 300, p: 1 }]);
  assert.match(g.path, /^M/);
  assert.ok(g.p50 && g.p95 && g.p50.minute === 0);
  assert.ok(g.p50.x > g.xTicks[0].x && g.p50.y > g.p95.y - 1e-9);
  assert.ok(g.domain[0] <= -60 && g.domain[1] >= 300);
  const none = ecdfGeometry([{ x: 1, p: 0.2 }, { x: 2, p: 0.4 }]);
  assert.equal(none.p50, null); assert.equal(none.p95, null);
  const e = ecdfGeometry([]);
  assert.equal(e.path, ''); assert.equal(e.p50, null); assert.equal(e.p95, null);
});

test('qqGeometry: line needs 4 points', () => {
  const pts = Array.from({ length: 100 }, (_, i) => ({ z: (i - 50) / 25, y: 2 * ((i - 50) / 25) + 1 }));
  const g = qqGeometry(pts);
  assert.equal(g.dots.length, 100);
  assert.ok(g.line && [g.line.x1, g.line.y1, g.line.x2, g.line.y2].every(Number.isFinite));
  assert.equal(qqGeometry(pts.slice(0, 3)).line, null);
  assert.equal(qqGeometry([]).dots.length, 0);
});

test('barGeometry: null value no bar, null CI no whisker', () => {
  const g = barGeometry([{ label: 'a', value: 0.5, ci_low: 0.4, ci_high: 0.6 }, { label: 'b', value: null, ci_low: null, ci_high: null }, { label: 'c', value: 0.2, ci_low: null, ci_high: null }]);
  assert.equal(g.bars.length, 3);
  assert.deepEqual(g.bars.map((b) => b.label), ['a', 'b', 'c']);
  assert.equal(g.bars[1].missing, true); assert.equal(g.bars[1].h, 0); assert.ok(Number.isFinite(g.bars[1].x) && g.bars[1].w > 0);
  assert.ok(g.bars[0].ciY1 < g.bars[0].ciY2);
  assert.equal(g.bars[1].ciY1, null); assert.equal(g.bars[2].ciY1, null); assert.ok(!g.bars[0].missing);
  assert.ok(g.yTicks.length >= 2);
  assert.deepEqual(barGeometry([]).bars, []);
});

const hasTitle = (svg) => assert.ok(svg.getAttribute('role') === 'img' && svg.getAttribute('aria-label') && svg.byTag('desc')[0].textContent);

test('renderers: role, labels, finite output, zero range', () => {
  const same = [{ x: 5, p: 0.5 }, { x: 5, p: 1 }];
  const e = renderEcdf(ecdfGeometry(same), { title: 'T', desc: 'D', band: { x0: 100, x1: 200 }, doc });
  hasTitle(e); finiteAttrs(e);
  finiteAttrs(renderEcdf(ecdfGeometry([]), { title: 'T', desc: 'D', doc }));
  const q = renderQq(qqGeometry([1, 2, 3, 4, 5].map(() => ({ z: 0, y: 3 }))), { title: 'T', desc: 'D', doc });
  hasTitle(q); finiteAttrs(q);
  const b = renderBars(barGeometry([{ label: 'a', value: 0.5, ci_low: 0.5, ci_high: 0.5 }, { label: 'b', value: null }]), { title: 'T', desc: 'D', axisLabel: 'share', doc });
  hasTitle(b); finiteAttrs(b);
  finiteAttrs(renderQq(qqGeometry([]), { title: 'T', desc: 'D', doc }));
});

test('single-point and zero-range geometry is finite', () => {
  const gs = [ecdfGeometry([{ x: 5, p: 1 }]), qqGeometry([{ z: 0, y: 0 }]), qqGeometry([1, 2, 3, 4].map(() => ({ z: 1, y: 1 }))), barGeometry([{ label: 'a', value: 0 }])];
  assert.equal(gs[2].line, null); // z3 == z1: no defined slope
  const fin = (v) => (typeof v === "number" ? Number.isFinite(v) : v && typeof v === "object" ? Object.values(v).every(fin) : true);
  for (const g of gs) assert.ok(Object.values(g).every(fin));
});

test('barGeometry: signed range, clamping', () => {
  const g = barGeometry([{ label: 'p', value: 0.5, ci_low: 0.2, ci_high: 0.8 }, { label: 'n', value: -0.5 }, { label: 'big', value: 5 }], { min: -1, max: 1 });
  const [p, n, big] = g.bars, zero = g.yTicks.find((t) => t.label === '0%' || t.label === '0').y;
  assert.ok(Math.abs(p.y + p.h - zero) < 0.02, 'positive bar rises from zero');
  assert.ok(Math.abs(n.y - zero) < 0.02 && n.h > 0, 'negative bar hangs below zero');
  assert.ok(p.ciY1 < p.ciY2 && p.ciY2 < zero);
  assert.ok(Math.abs(big.y - g.y1) < 0.02 && Math.abs(big.h - (zero - g.y1)) < 0.02, 'clamped to max');
  assert.ok(g.yTicks.some((t) => Math.abs(t.y - g.y0) < 0.02) && g.yTicks.some((t) => Math.abs(t.y - g.y1) < 0.02));
});

test('renderBars draws n/a for a missing slot', () => {
  const svg = renderBars(barGeometry([{ label: 'a', value: 0.5 }, { label: 'b', value: null }]), { title: 'T', desc: 'D', doc });
  assert.equal(svg.byTag('rect').length, 1);
  assert.equal(svg.byText('text', /^n\/a$/).length, 1);
  assert.equal(svg.byText('text', /^b$/).length, 1);
});

test('qqGeometry: reference line keeps the exact Q1-Q3 slope on heavy tails', () => {
  const pts = Array.from({ length: 101 }, (_, i) => { const z = (i - 50) / 20; return { z, y: Math.sign(z) * z ** 4 * 0.5 + 0.1 * z }; });
  const g = qqGeometry(pts);
  const a = pts[25], b = pts[75], k = (b.y - a.y) / (b.z - a.z);
  const [z0, z1] = g.zDomain, [y0, y1] = g.yDomain;
  const dz = ((g.line.x2 - g.line.x1) / (g.x1 - g.x0)) * (z1 - z0), dy = ((g.line.y1 - g.line.y2) / (g.y0 - g.y1)) * (y1 - y0);
  assert.ok(Math.abs(dy / dz - k) < 1e-9);
  for (const v of [g.line.y1, g.line.y2]) assert.ok(v >= g.y1 - 1e-9 && v <= g.y0 + 1e-9);
});
