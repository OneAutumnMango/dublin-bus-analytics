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
  assert.equal(g.bars.length, 2);
  assert.deepEqual(g.bars.map((b) => b.label), ['a', 'c']);
  assert.ok(g.bars[0].ciY1 < g.bars[0].ciY2);
  assert.equal(g.bars[1].ciY1, null); assert.equal(g.bars[1].ciY2, null);
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
