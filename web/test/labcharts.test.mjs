import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from './fakedom.mjs';
import { ecdfGeometry, qqGeometry, barGeometry, renderEcdf, renderQq, renderBars, ecdfAtPercentile, percentileAtY, ecdfReadout, qqReadout, normCdf } from '../js/labcharts.js';

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
  const b = renderBars(barGeometry([{ label: 'a', value: 0.5, ci_low: 0.5, ci_high: 0.5 }, { label: 'b', value: null }]), { title: 'T', desc: 'D', doc });
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

const rotated = (svg) => svg.byTag('text').filter((t) => (t.getAttribute('transform') || '').startsWith('rotate(-90'));
const axisTexts = (svg) => svg.all((n) => n.tag === 'text' && n.getAttribute('class') === 'axis-label');

test('renderers draw the given axis labels, y rotated, and none by default', () => {
  const lbl = { xLabel: 'X axis', yLabel: 'Y axis' };
  const cases = [
    [renderEcdf(ecdfGeometry([{ x: -1, p: 0.2 }, { x: 3, p: 1 }]), { title: 'T', desc: 'D', doc, ...lbl }), renderEcdf(ecdfGeometry([{ x: 1, p: 1 }]), { title: 'T', desc: 'D', doc })],
    [renderQq(qqGeometry([1, 2, 3, 4].map((i) => ({ z: i, y: i * 2 }))), { title: 'T', desc: 'D', doc, ...lbl }), renderQq(qqGeometry([]), { title: 'T', desc: 'D', doc })],
    [renderBars(barGeometry([{ label: 'a', value: 0.5 }]), { title: 'T', desc: 'D', doc, ...lbl }), renderBars(barGeometry([]), { title: 'T', desc: 'D', doc })],
  ];
  for (const [withL, without] of cases) {
    assert.deepEqual(axisTexts(withL).map((t) => t.textContent).sort(), ['X axis', 'Y axis']);
    assert.equal(rotated(withL).length, 1);
    assert.equal(rotated(withL)[0].textContent, 'Y axis');
    finiteAttrs(withL);
    assert.equal(axisTexts(without).length, 0);
  }
});

test('axis labels are finite on degenerate input and clear the tick labels', () => {
  for (const g of [ecdfGeometry([]), qqGeometry([]), barGeometry([])]) assert.ok(g.x0 >= 60 && g.height - g.y0 >= 50);
  finiteAttrs(renderEcdf(ecdfGeometry([]), { title: 'T', desc: 'D', doc, xLabel: 'x', yLabel: 'y' }));
});

const P = [{ x: -60, p: 0.1 }, { x: 0, p: 0.5 }, { x: 120, p: 0.9 }];

test('ecdfAtPercentile: interpolates, clamps at the edges, empty and duplicates are safe', () => {
  const near = (r, x) => { assert.ok(Math.abs(r.x - x) < 1e-9 && r.clamp === null); };
  near(ecdfAtPercentile(P, 0.3), -30);
  near(ecdfAtPercentile(P, 0.7), 60);
  assert.deepEqual(ecdfAtPercentile(P, 0.5), { x: 0, clamp: null });
  assert.deepEqual(ecdfAtPercentile(P, 0.05), { x: -60, clamp: 'low' });
  assert.deepEqual(ecdfAtPercentile(P, 0.99), { x: 120, clamp: 'high' });
  assert.equal(ecdfAtPercentile([], 0.5), null);
  const dupP = ecdfAtPercentile([{ x: 0, p: 0.5 }, { x: 5, p: 0.5 }, { x: 9, p: 1 }], 0.5);
  assert.ok(Number.isFinite(dupP.x));
  assert.deepEqual(ecdfAtPercentile([{ x: 2, p: 0.2 }, { x: 2, p: 0.6 }, { x: 2, p: 1 }], 0.4), { x: 2, clamp: null });
});

test('percentileAtY: pointer y to percentile, clamped to the plot', () => {
  const g = ecdfGeometry(P);
  assert.equal(percentileAtY(g, g.y0), 0);
  assert.equal(percentileAtY(g, g.y1), 1);
  assert.ok(Math.abs(percentileAtY(g, (g.y0 + g.y1) / 2) - 0.5) < 1e-9);
  assert.equal(percentileAtY(g, g.y0 + 500), 0);
  assert.equal(percentileAtY(g, -500), 1);
  assert.equal(percentileAtY(g, NaN), 0);
});

test('readouts: ecdf and q-q wording', () => {
  assert.equal(ecdfReadout(P, 0.7), '70th percentile: 60.0 min late');
  assert.equal(ecdfReadout(P, 0.3), '30th percentile: 30.0 min early');
  assert.equal(ecdfReadout(P, 0.5), '50th percentile: on time');
  assert.match(ecdfReadout(P, 0.99), /^99th percentile: .*beyond/);
  assert.equal(ecdfReadout([], 0.5), '');
  assert.ok(Math.abs(normCdf(0) - 0.5) < 1e-6 && Math.abs(normCdf(1.645) - 0.95) < 1e-3);
  assert.equal(qqReadout(1.64, 9), 'Normal quantile 1.64 (standard deviations) — observed delay 9.0 min late (95th percentile)');
  assert.match(qqReadout(0, 0), /on time \(50th percentile\)$/);
  assert.doesNotMatch(qqReadout(-8, -1), /NaN|undefined/);
});

test('renderEcdf hover: overlay tracks the pointer, marker on the curve, hides on leave', () => {
  const g = ecdfGeometry(P);
  const svg = renderEcdf(g, { title: 'T', desc: 'D', doc });
  svg.getBoundingClientRect = () => ({ top: 100, left: 0, height: g.height / 2, width: g.width / 2 }); // rendered at half size: pointer pixels must be scaled back to viewBox units
  const area = svg.all((n) => n.getAttribute('class') === 'hover-area')[0];
  const grp = svg.all((n) => n.getAttribute('class') === 'hover-group')[0];
  assert.equal(grp.getAttribute('display'), 'none');
  area.dispatch('pointermove', { clientX: 50, clientY: 100 + (g.y0 + g.y1) / 4 });
  assert.equal(grp.getAttribute('display'), null);
  assert.match(area.getAttribute('data-tip'), /^50th percentile: on time$/);
  const circle = grp.byTag('circle')[0];
  assert.ok(Math.abs(Number(circle.getAttribute('cx')) - g.sx(0)) < 0.01 && Math.abs(Number(circle.getAttribute('cy')) - (g.y0 + g.y1) / 2) < 0.01);
  finiteAttrs(svg);
  area.dispatch('pointermove', { clientX: 50, clientY: 100 + g.y1 / 2 });
  assert.equal(circle.getAttribute('visibility'), 'hidden'); // beyond the data: no marker
  assert.match(area.getAttribute('data-tip'), /beyond/);
  area.dispatch('pointerleave');
  assert.equal(grp.getAttribute('display'), 'none'); // children with an explicit visibility would override a hidden parent
  assert.equal(grp.byTag('circle')[0].getAttribute('visibility'), 'hidden');
  assert.ok(grp.byTag('line').every((l) => l.getAttribute('visibility') === 'hidden'));
  area.dispatch('pointermove', { clientX: 50, clientY: 100 + (g.y0 + g.y1) / 4 });
  assert.equal(grp.getAttribute('display'), null);
  assert.equal(renderEcdf(ecdfGeometry([]), { title: 'T', desc: 'D', doc }).all((n) => n.getAttribute('class') === 'hover-area').length, 0);
});

test('renderQq: every point has a larger invisible hit circle carrying its tooltip text', () => {
  const pts = [{ z: -1, y: -2 }, { z: 0, y: 0 }, { z: 1.64, y: 9 }, { z: 2, y: 12 }];
  const svg = renderQq(qqGeometry(pts), { title: 'T', desc: 'D', doc });
  const hits = svg.all((n) => n.getAttribute('class') === 'qq-hit');
  assert.equal(hits.length, 4);
  assert.ok(hits.every((c) => Number(c.getAttribute('r')) >= 8));
  assert.ok(hits.some((c) => c.getAttribute('data-tip') === 'Normal quantile 1.64 (standard deviations) — observed delay 9.0 min late (95th percentile)'));
  assert.equal(svg.all((n) => n.getAttribute('class') === 'qq-dot').length, 4);
});
