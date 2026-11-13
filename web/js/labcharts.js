// Stats-lab chart geometry (pure, minutes in) and thin SVG renderers. Colours come from CSS classes, never inline.
// The page converts seconds to minutes before calling these.
import { isNum, extent, linearScale, niceTicks, niceDomain, fmtNum } from './lib.js';
import { linePath } from './charts.js';

const r2 = (x) => Math.round(x * 100) / 100;
const lab = (m) => fmtNum(m, Number.isInteger(m) ? 0 : 1);

function frame(o, dw, dh) {
  const { width = dw, height = dh, left = 64, right = 16, top = 14, bottom = 52 } = o;
  return { width, height, left, top, x0: left, x1: width - right, y0: height - bottom, y1: top };
}

// points: [{x minutes, p 0..1}] sorted by x. Markers sit on the first point reaching p50 / p95.
export function ecdfGeometry(points, o = {}) {
  const f = frame(o, 720, 280);
  const pts = (points || []).filter((q) => q && isNum(q.x) && isNum(q.p));
  const e = extent(pts.map((q) => q.x));
  const domain = niceDomain(e ? e[0] : null, e ? e[1] : null, 6, true);
  const sx = linearScale(domain, [f.x0, f.x1]), sy = linearScale([0, 1], [f.y0, f.y1]);
  const at = (lvl) => { const q = pts.find((s) => s.p >= lvl); return q ? { x: r2(sx(q.x)), y: r2(sy(q.p)), minute: q.x } : null; };
  return { ...f, domain, sx, sy, path: linePath(pts.map((q) => ({ x: sx(q.x), y: sy(q.p) }))), p50: at(0.5), p95: at(0.95),
    zeroX: r2(sx(0)),
    xTicks: niceTicks(domain[0], domain[1], 6).map((m) => ({ x: r2(sx(m)), label: lab(m) })),
    yTicks: [0, 0.25, 0.5, 0.75, 1].map((p) => ({ y: r2(sy(p)), label: `${p * 100}%` })) };
}

// points: [{z standard-normal quantile, y minutes}]. The y domain is widened to hold both reference-line endpoints so the slope stays exact.
export function qqGeometry(points, o = {}) {
  const f = frame(o, 420, 320);
  const pts = (points || []).filter((q) => q && isNum(q.z) && isNum(q.y)).sort((a, b) => a.z - b.z);
  const ze = extent(pts.map((q) => q.z));
  let ref = null; // line in data units
  if (pts.length >= 4) {
    const a = pts[Math.floor(pts.length * 0.25)], b = pts[Math.floor(pts.length * 0.75)];
    if (b.z > a.z) { const k = (b.y - a.y) / (b.z - a.z), zl = pts[0].z, zr = pts[pts.length - 1].z; ref = [zl, a.y + k * (zl - a.z), zr, a.y + k * (zr - a.z)]; }
  }
  const ye = extent([...pts.map((q) => q.y), ...(ref ? [ref[1], ref[3]] : [])]);
  const zDomain = niceDomain(ze && ze[0], ze && ze[1], 5), yDomain = niceDomain(ye && ye[0], ye && ye[1], 5);
  const sx = linearScale(zDomain, [f.x0, f.x1]), sy = linearScale(yDomain, [f.y0, f.y1]);
  return { ...f, zDomain, yDomain, dots: pts.map((q) => ({ cx: r2(sx(q.z)), cy: r2(sy(q.y)) })),
    line: ref && { x1: sx(ref[0]), y1: sy(ref[1]), x2: sx(ref[2]), y2: sy(ref[3]) }, // unrounded: rounding would bend the slope
    xTicks: niceTicks(zDomain[0], zDomain[1], 5).map((m) => ({ x: r2(sx(m)), label: lab(m) })),
    yTicks: niceTicks(yDomain[0], yDomain[1], 5).map((m) => ({ y: r2(sy(m)), label: lab(m) })) };
}

// items: [{label, value in [min, max], ci_low, ci_high}]. Bars grow from value 0 (up if positive, down if negative); values are clamped to [min, max].
// A null value keeps its slot as {missing: true, h: 0}; a null CI draws no whisker.
export function barGeometry(items, o = {}) {
  const { min = 0, max = 1 } = o;
  const f = frame(o, 720, 260);
  const n = (items || []).length, slot = n ? (f.x1 - f.x0) / n : 0, w = Math.min(slot * 0.6, 80);
  const sy = linearScale([min, max], [f.y0, f.y1]);
  const Y = (v) => sy(Math.min(max, Math.max(min, v))), zeroY = Y(0);
  const bars = (items || []).map((it, i) => {
    const x = r2(f.x0 + slot * i + (slot - w) / 2), label = it && it.label;
    if (!it || !isNum(it.value)) return { label, x, y: r2(zeroY), w: r2(w), h: 0, ciY1: null, ciY2: null, missing: true };
    const y = Y(it.value), hasCi = isNum(it.ci_low) && isNum(it.ci_high);
    return { label, x, y: r2(Math.min(y, zeroY)), w: r2(w), h: r2(Math.abs(zeroY - y)),
      ciY1: hasCi ? r2(Y(it.ci_high)) : null, ciY2: hasCi ? r2(Y(it.ci_low)) : null };
  });
  const yTicks = niceTicks(min, max, 4).map((m) => ({ y: r2(sy(m)), label: max - min <= 2 && max <= 1 && min >= -1 ? `${r12(m * 100)}%` : lab(m) }));
  return { ...f, bars, zeroY: r2(zeroY), yTicks };
}
const r12 = (x) => Number(x.toPrecision(12));

// ---------- renderers ----------
const NS = 'http://www.w3.org/2000/svg';
function el(doc, tag, attrs = {}, text) {
  const e = doc.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, String(v));
  if (text != null) e.textContent = String(text);
  return e;
}
let uid = 0;
function root(doc, g, title, desc, cls) {
  const id = `lc${++uid}d`; // no root <title> element (charts.js convention: it would be a hover tooltip over the whole chart); aria-label carries the title
  const svg = el(doc, 'svg', { viewBox: `0 0 ${g.width} ${g.height}`, class: `chart ${cls}`, role: 'img', 'aria-label': title, 'aria-describedby': id, focusable: 'false' });
  svg.append(el(doc, 'desc', { id }, desc));
  return svg;
}
const yAxis = (doc, svg, g) => { for (const t of g.yTicks) svg.append(el(doc, 'line', { class: 'grid', x1: g.x0, x2: g.x1, y1: t.y, y2: t.y }), el(doc, 'text', { class: 'tick-label', x: g.x0 - 6, y: t.y + 4, 'text-anchor': 'end' }, t.label)); };
// optional axis titles: x centred under the tick labels, y rotated -90 in the left margin
const axisTitles = (doc, svg, g, { xLabel, yLabel }) => {
  const cy = (g.y0 + g.y1) / 2;
  if (xLabel) svg.append(el(doc, 'text', { class: 'axis-label', x: (g.x0 + g.x1) / 2, y: g.height - 8, 'text-anchor': 'middle' }, xLabel));
  if (yLabel) svg.append(el(doc, 'text', { class: 'axis-label', x: 14, y: cy, transform: `rotate(-90 14 ${cy})`, 'text-anchor': 'middle' }, yLabel));
};
const xAxis = (doc, svg, g) => { for (const t of g.xTicks) svg.append(el(doc, 'text', { class: 'tick-label', x: t.x, y: g.y0 + 16, 'text-anchor': 'middle' }, t.label)); };

export function renderEcdf(geo, { title, desc, band = null, xLabel, yLabel, doc = globalThis.document } = {}) {
  const svg = root(doc, geo, title, desc, 'ecdf-chart');
  yAxis(doc, svg, geo); xAxis(doc, svg, geo);
  if (band && band.x1 > band.x0) svg.append(el(doc, 'rect', { class: 'ci-band', x: band.x0, y: geo.y1, width: band.x1 - band.x0, height: geo.y0 - geo.y1 }));
  svg.append(el(doc, 'line', { class: 'zero-line', x1: geo.zeroX, x2: geo.zeroX, y1: geo.y1, y2: geo.y0 }));
  axisTitles(doc, svg, geo, { xLabel, yLabel });
  if (geo.path) svg.append(el(doc, 'path', { class: 'ecdf-line', d: geo.path, fill: 'none' }));
  for (const [m, k] of [[geo.p50, 'p50'], [geo.p95, 'p95']]) {
    if (m) svg.append(el(doc, 'circle', { class: `ecdf-mark ${k}`, cx: m.x, cy: m.y, r: 4 }), el(doc, 'text', { class: 'marker-label', x: m.x + 6, y: m.y + 14 }, k.toUpperCase()));
  }
  return svg;
}

export function renderQq(geo, { title, desc, xLabel, yLabel, doc = globalThis.document } = {}) {
  const svg = root(doc, geo, title, desc, 'qq-chart');
  yAxis(doc, svg, geo); xAxis(doc, svg, geo);
  axisTitles(doc, svg, geo, { xLabel, yLabel });
  if (geo.line) svg.append(el(doc, 'line', { class: 'qq-line', ...geo.line }));
  for (const d of geo.dots) svg.append(el(doc, 'circle', { class: 'qq-dot', cx: d.cx, cy: d.cy, r: 2.5 }));
  return svg;
}

export function renderBars(geo, { title, desc, xLabel, yLabel, doc = globalThis.document } = {}) {
  const svg = root(doc, geo, title, desc, 'bar-chart');
  yAxis(doc, svg, geo);
  for (const b of geo.bars) {
    const g = el(doc, 'g', { class: 'bar-group' });
    if (b.missing) g.append(el(doc, 'text', { class: 'tick-label n-label', x: r2(b.x + b.w / 2), y: geo.zeroY - 4, 'text-anchor': 'middle' }, 'n/a'));
    else g.append(el(doc, 'rect', { class: 'lab-bar', x: b.x, y: b.y, width: b.w, height: b.h }));
    if (b.ciY1 !== null) {
      const cx = r2(b.x + b.w / 2);
      g.append(el(doc, 'line', { class: 'whisker', x1: cx, x2: cx, y1: b.ciY1, y2: b.ciY2 }),
        el(doc, 'line', { class: 'whisker', x1: cx - 4, x2: cx + 4, y1: b.ciY1, y2: b.ciY1 }),
        el(doc, 'line', { class: 'whisker', x1: cx - 4, x2: cx + 4, y1: b.ciY2, y2: b.ciY2 }));
    }
    g.append(el(doc, 'text', { class: 'tick-label', x: r2(b.x + b.w / 2), y: geo.y0 + 16, 'text-anchor': 'middle' }, b.label));
    svg.append(g);
  }
  axisTitles(doc, svg, geo, { xLabel, yLabel });
  return svg;
}
