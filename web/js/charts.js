// Chart geometry (pure, unit-tested) and thin SVG renderers. Colours come from CSS classes, never inline.
// Delays in the data are seconds; chart axes are minutes. Negative = early (left/down), positive = late.
import { isNum, extent, linearScale, niceTicks, niceDomain, divergingBucket, fmtNum, fmtDelay, fmtCount,
  clockTime, LOW_N_NOTE, shorten, niceIntTicks, plural } from './lib.js';

const r2 = (x) => (isNum(x) ? Math.round(x * 100) / 100 : x);
const toMin = (s) => (isNum(s) ? s / 60 : null);

// ---------- range chart: mean marker, min/max whiskers, percentile band, zero line ----------
// items: [{label, href, n, low_n, mean, min, max, lo, hi}] (seconds; any of the numbers may be null)
export function rangeDomain(items) {
  const vals = [];
  for (const it of items) vals.push(it.mean, it.min, it.max, it.lo, it.hi);
  const e = extent(vals);
  return niceDomain(...(e ? e.map(toMin) : [null, null]), 6, true).map((m) => m * 60);
}

export function rangeGeometry(items, domain = rangeDomain(items), o = {}) {
  const { width = 720, left = 170, right = 56, top = 34, rowH = 22 } = o;
  const x0 = left, x1 = width - right;
  const sx = linearScale(domain, [x0, x1]);
  const X = (v) => (isNum(v) ? r2(Math.min(x1, Math.max(x0, sx(v)))) : null);
  const rows = items.map((it, i) => {
    const y = top + i * rowH;
    const g = { ...it, y, cy: y + rowH / 2, xMean: X(it.mean), xMin: X(it.min), xMax: X(it.max), xLo: X(it.lo), xHi: X(it.hi) };
    g.empty = g.xMean === null;
    g.lowN = !!it.low_n;
    return g;
  });
  const ticks = niceTicks(domain[0] / 60, domain[1] / 60, 6).map((m) => ({ x: r2(sx(m * 60)), label: fmtNum(m, Number.isInteger(m) ? 0 : 1) }));
  return { width, height: top + items.length * rowH + 10, x0, x1, top, rowH, zeroX: r2(sx(0)), ticks, rows, domain };
}

// ---------- histogram ----------
// bins: [{lo, hi, count}] with open-ended first (lo null) and last (hi null) bins; slots are equal width.
export function histogramGeometry(bins, o = {}) {
  const { width = 720, height = 260, left = 44, right = 12, top = 12, bottom = 40 } = o;
  const n = Array.isArray(bins) ? bins.length : 0;
  const plotW = width - left - right, plotH = height - top - bottom;
  const total = n ? bins.reduce((a, b) => a + (b.count || 0), 0) : 0;
  const max = n ? Math.max(0, ...bins.map((b) => b.count || 0)) : 0;
  const slot = n ? plotW / n : 0;
  const innerLo = n > 2 ? bins[1].lo : 0, innerHi = n > 2 ? bins[n - 1].lo : 1;
  const xOf = (sec) => {
    if (!n || !isNum(sec)) return null;
    if (sec < innerLo) return r2(left + slot / 2);
    if (sec > innerHi) return r2(left + plotW - slot / 2);
    return r2(left + slot + ((sec - innerLo) / (innerHi - innerLo || 1)) * slot * (n - 2));
  };
  const bars = n ? bins.map((b, i) => {
    const h = max ? (b.count / max) * plotH : 0;
    return { x: r2(left + i * slot + 0.5), w: r2(Math.max(0, slot - 1)), h: r2(h), y: r2(top + plotH - h), count: b.count, lo: b.lo, hi: b.hi,
      label: b.lo === null ? `below ${fmtNum(b.hi / 60, 0)} min` : b.hi === null ? `${fmtNum(b.lo / 60, 0)} min and above` : `${fmtNum(b.lo / 60, 0)} to ${fmtNum(b.hi / 60, 0)} min` };
  }) : [];
  const xTicks = [];
  for (let i = 1; i < n; i++) { // boundary between slot i-1 and i is the edge bins[i].lo
    const sec = bins[i].lo;
    if (isNum(sec) && sec % 300 === 0) xTicks.push({ x: r2(left + i * slot), label: fmtNum(sec / 60, 0) });
  }
  const yTicks = niceIntTicks(max, 4).map((c) => ({ y: r2(top + plotH - (max ? (c / max) * plotH : 0)), label: fmtCount(c) }));
  return { width, height, left, top, plotW, plotH, bars, xOf, xTicks, yTicks, zeroX: xOf(0), max, total, empty: total === 0 };
}

// ---------- heatmap (weekday x hour) ----------
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
// cells: API cells [{weekday, hour, n, low_n, mean_delay}]. Scale saturates at max(|mean|, minAbs) seconds.
export function heatmapGrid(cells, o = {}) {
  const { left = 44, top = 24, cw = 28, ch = 26, minAbs = 60 } = o;
  const byKey = new Map();
  for (const c of cells || []) if (c && c.weekday >= 0 && c.weekday <= 6 && c.hour >= 0 && c.hour <= 23) byKey.set(`${c.weekday}:${c.hour}`, c);
  let maxAbs = minAbs;
  for (const c of byKey.values()) if (isNum(c.mean_delay)) maxAbs = Math.max(maxAbs, Math.abs(c.mean_delay));
  const out = [];
  for (let wd = 0; wd < 7; wd++) {
    for (let hour = 0; hour < 24; hour++) {
      const c = byKey.get(`${wd}:${hour}`);
      const mean = c && isNum(c.mean_delay) ? c.mean_delay : null;
      out.push({ wd, hour, x: left + hour * cw, y: top + wd * ch, w: cw - 1, h: ch - 1, n: c ? c.n : 0, mean,
        lowN: !c || !!c.low_n, empty: mean === null, bucket: divergingBucket(mean, maxAbs) });
    }
  }
  return { cells: out, maxAbs, width: left + 24 * cw + 8, height: top + 7 * ch + 4, left, top, cw, ch };
}

// ---------- line / bars (delay accumulation) ----------
// Path for points [{x, y}] where y may be null (gap breaks the line).
export function linePath(points) {
  let d = '', pen = false;
  for (const p of points) {
    if (!p || !isNum(p.x) || !isNum(p.y)) { pen = false; continue; }
    d += `${pen ? 'L' : 'M'}${r2(p.x)} ${r2(p.y)}`;
    pen = true;
  }
  return d;
}

// stops: API accumulation stops [{stop_id, position, mean_delay, segment_delta, n, n_delta, ...}], in route order.
// x follows the route POSITION (not the list index) and the line breaks where positions are skipped or data is missing.
export function accumulationGeometry(stops, o = {}) {
  const { width = 720, left = 52, right = 16, top = 14, panelH = 170, gap = 36, bottom = 22 } = o;
  const n = stops.length;
  const x0 = left, x1 = width - right;
  const posOf = (s, i) => (isNum(s.position) ? s.position : i);
  const pe = extent(stops.map(posOf));
  const span = pe ? pe[1] - pe[0] : 0;
  const xs = (p) => (span === 0 ? (x0 + x1) / 2 : x0 + ((p - pe[0]) / span) * (x1 - x0));
  const e1 = extent(stops.map((s) => toMin(s.mean_delay)));
  const [a0, a1] = niceDomain(e1 ? e1[0] : null, e1 ? e1[1] : null, 4, true);
  const ya = linearScale([a0, a1], [top + panelH, top]);
  const e2 = extent(stops.map((s) => toMin(s.segment_delta)));
  const m2 = e2 ? Math.max(Math.abs(e2[0]), Math.abs(e2[1])) : 0;
  const [b0, b1] = niceDomain(-m2, m2, 4, true);
  const bTop = top + panelH + gap, bH = panelH * 0.7;
  const yb = linearScale([b0, b1], [bTop + bH, bTop]);
  const pts = stops.map((s, i) => ({ x: r2(xs(posOf(s, i))), y: isNum(s.mean_delay) ? r2(ya(s.mean_delay / 60)) : null, pos: posOf(s, i) }));
  const withGaps = [];
  pts.forEach((p, i) => { if (i && p.pos - pts[i - 1].pos !== 1) withGaps.push({ x: p.x, y: null }); withGaps.push(p); });
  const slot = (x1 - x0) / (span + 1);
  const bars = stops.map((s, i) => {
    const v = toMin(s.segment_delta), cx = xs(posOf(s, i));
    if (!isNum(v)) return { x: r2(cx - slot * 0.4), w: r2(slot * 0.8), y: r2(yb(0)), h: 0, delta: null };
    const y = yb(v), z = yb(0);
    return { x: r2(cx - slot * 0.4), w: r2(Math.max(1, slot * 0.8)), y: r2(Math.min(y, z)), h: r2(Math.abs(z - y)), delta: s.segment_delta };
  });
  const every = Math.max(1, Math.ceil(n / 12));
  return { width, height: bTop + bH + bottom, x0, x1, pts, path: linePath(withGaps), bars, zeroA: r2(ya(0)), zeroB: r2(yb(0)),
    ticksA: niceTicks(a0, a1, 4).map((m) => ({ y: r2(ya(m)), label: fmtNum(m, Number.isInteger(m) ? 0 : 1) })),
    ticksB: niceTicks(b0, b1, 4).map((m) => ({ y: r2(yb(m)), label: fmtNum(m, Number.isInteger(m) ? 0 : 1) })),
    xTicks: stops.map((s, i) => ({ x: r2(xs(posOf(s, i))), i, label: String(posOf(s, i) + 1) })).filter((t) => t.i % every === 0 || t.i === n - 1),
    panelA: { top, h: panelH }, panelB: { top: bTop, h: bH } };
}

// ---------- step chart: due minutes over time ----------
// steps: [{t, due}] from lib.dueSteps; markers: [{t, label, kind}]
export function stepGeometry(steps, markers = [], o = {}) {
  const { width = 720, height = 280, left = 44, right = 16, top = 16, bottom = 34 } = o;
  const x0 = left, x1 = width - right, y0 = top + (height - top - bottom), y1 = top;
  const ts = [...steps.map((s) => s.t), ...markers.map((m) => m.t)];
  const te = extent(ts);
  const sx = linearScale(te || [0, 1], [x0, x1]);
  const maxDue = Math.max(1, ...steps.map((s) => s.due).filter(isNum));
  const [, dMax] = niceDomain(0, maxDue, 4);
  const sy = linearScale([0, dMax], [y0, y1]);
  let path = '';
  steps.forEach((s, i) => {
    path += i === 0 ? `M${r2(sx(s.t))} ${r2(sy(s.due))}` : `H${r2(sx(s.t))}V${r2(sy(s.due))}`;
  });
  const last = steps[steps.length - 1];
  const tickT = !te ? [] : te[0] === te[1] ? [te[0]] : [0, 1, 2, 3].map((i) => Math.round(te[0] + ((te[1] - te[0]) * i) / 3));
  return { width, height, x0, x1, y0, y1, path, sx, sy, empty: !steps.length,
    yTicks: niceTicks(0, dMax, 4).map((d) => ({ y: r2(sy(d)), label: String(d) })),
    xTicks: tickT.map((t) => ({ x: r2(sx(t)), label: clockTime(t) })),
    endX: last ? r2(sx(last.t)) : null,
    markers: markers.map((m) => ({ ...m, x: r2(sx(m.t)) })) };
}

// ---------- alt text ----------
export function rangeTip(r) {
  if (r.empty) return `${r.label}: no data`;
  const parts = [`mean ${fmtDelay(r.mean)}`];
  if (isNum(r.min) && isNum(r.max)) parts.push(`min ${fmtDelay(r.min)}`, `max ${fmtDelay(r.max)}`);
  if (isNum(r.lo) && isNum(r.hi)) parts.push(`band ${fmtDelay(r.lo)} to ${fmtDelay(r.hi)}`);
  return `${r.label}: ${parts.join(', ')}, ${plural(r.n, 'sample')}${r.lowN ? `. ${LOW_N_NOTE}` : ''}`;
}

export function rangeSummary(items, what = 'stops') {
  const ok = items.filter((i) => isNum(i.mean));
  if (!ok.length) return `No ${what} with data.`;
  const lo = ok.reduce((a, b) => (b.mean < a.mean ? b : a)), hi = ok.reduce((a, b) => (b.mean > a.mean ? b : a));
  return `${ok.length} of ${items.length} ${what} have data. Mean delay ranges from ${fmtDelay(lo.mean)} (${lo.label}) to ${fmtDelay(hi.mean)} (${hi.label}). Negative means early.`;
}

// ---------- renderers (DOM; thin) ----------
const NS = 'http://www.w3.org/2000/svg';
function el(doc, tag, attrs = {}, text) {
  const e = doc.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, String(v));
  if (text != null) e.textContent = String(text);
  return e;
}
let uid = 0;
function root(doc, w, h, title, desc, cls) {
  const id = `c${++uid}`;
  const svg = el(doc, 'svg', { viewBox: `0 0 ${w} ${h}`, class: `chart ${cls}`, role: 'img', 'aria-labelledby': `${id}t ${id}d`, focusable: 'false' });
  svg.append(el(doc, 'title', { id: `${id}t` }, title), el(doc, 'desc', { id: `${id}d` }, desc));
  return svg;
}

export function renderRangeChart(geo, { title, desc, axisLabel = 'minutes vs schedule', zeroLabel = 'on schedule', doc = globalThis.document } = {}) {
  const svg = root(doc, geo.width, geo.height, title, desc, 'range-chart');
  for (const t of geo.ticks) {
    svg.append(el(doc, 'line', { class: 'grid', x1: t.x, x2: t.x, y1: geo.top - 6, y2: geo.height - 6 }),
      el(doc, 'text', { class: 'tick-label', x: t.x, y: geo.top - 12, 'text-anchor': 'middle' }, t.label));
  }
  svg.append(el(doc, 'line', { class: 'zero-line', x1: geo.zeroX, x2: geo.zeroX, y1: geo.top - 6, y2: geo.height - 6 }),
    el(doc, 'text', { class: 'axis-label', x: geo.x0, y: 10 }, `← early    ${axisLabel}    late →  (dashed line = on schedule)`));
  for (const r of geo.rows) {
    const g = el(doc, 'g', { class: `row${r.lowN ? ' low-n' : ''}${r.empty ? ' empty' : ''}` });
    const tip = r.empty ? `${r.label}: no data` : rangeTip(r);
    g.append(el(doc, 'title', {}, tip));
    const label = el(doc, 'text', { class: 'row-label', x: geo.x0 - 8, y: r.cy + 4, 'text-anchor': 'end' }, shorten(r.label, 24));
    if (r.href) { const a = el(doc, 'a', { href: r.href, tabindex: -1 }); a.append(label); g.append(a); } else g.append(label);
    g.append(el(doc, 'text', { class: 'n-label', x: geo.width - 4, y: r.cy + 4, 'text-anchor': 'end' }, `n=${fmtCount(r.n)}`));
    if (!r.empty) {
      if (r.xLo !== null && r.xHi !== null) g.append(el(doc, 'rect', { class: 'band', x: r.xLo, y: r.cy - 6, width: Math.max(1, r.xHi - r.xLo), height: 12, rx: 2 }));
      if (r.xMin !== null && r.xMax !== null) {
        g.append(el(doc, 'line', { class: 'whisker', x1: r.xMin, x2: r.xMax, y1: r.cy, y2: r.cy }),
          el(doc, 'line', { class: 'whisker', x1: r.xMin, x2: r.xMin, y1: r.cy - 5, y2: r.cy + 5 }),
          el(doc, 'line', { class: 'whisker', x1: r.xMax, x2: r.xMax, y1: r.cy - 5, y2: r.cy + 5 }));
      }
      g.append(el(doc, 'path', { class: `mean ${r.mean < 0 ? 'early' : r.mean > 0 ? 'late' : 'ontime'}`, d: `M${r.xMean} ${r.cy - 5}l5 5l-5 5l-5-5z` }));
    }
    svg.append(g);
  }
  return svg;
}

export function renderHistogram(geo, { title, desc, markers = [], band = null, doc = globalThis.document } = {}) {
  const svg = root(doc, geo.width, geo.height, title, desc, 'hist-chart');
  for (const t of geo.yTicks) {
    svg.append(el(doc, 'line', { class: 'grid', x1: geo.left, x2: geo.left + geo.plotW, y1: t.y, y2: t.y }),
      el(doc, 'text', { class: 'tick-label', x: geo.left - 6, y: t.y + 4, 'text-anchor': 'end' }, t.label));
  }
  if (band && band.x1 > band.x0) svg.append(el(doc, 'rect', { class: 'ci-band', x: band.x0, y: geo.top, width: band.x1 - band.x0, height: geo.plotH }));
  for (const b of geo.bars) {
    const r = el(doc, 'rect', { class: `bar${b.lo !== null && b.lo < 0 ? ' early' : b.lo !== null && b.lo >= 0 ? ' late' : ''}`, x: b.x, y: b.y, width: b.w, height: b.h });
    r.append(el(doc, 'title', {}, `${b.label}: ${fmtCount(b.count)} arrivals`));
    svg.append(r);
  }
  for (const t of geo.xTicks) svg.append(el(doc, 'text', { class: 'tick-label', x: t.x, y: geo.top + geo.plotH + 16, 'text-anchor': 'middle' }, t.label));
  svg.append(el(doc, 'text', { class: 'axis-label', x: geo.left, y: geo.height - 6 }, '← early    minutes vs schedule    late →'));
  if (geo.zeroX !== null) svg.append(el(doc, 'line', { class: 'zero-line', x1: geo.zeroX, x2: geo.zeroX, y1: geo.top, y2: geo.top + geo.plotH }));
  for (const m of markers) if (isNum(m.x)) svg.append(el(doc, 'line', { class: `marker ${m.cls || ''}`, x1: m.x, x2: m.x, y1: geo.top, y2: geo.top + geo.plotH }, null));
  return svg;
}

export function renderHeatmap(grid, { title, desc, doc = globalThis.document } = {}) {
  const svg = root(doc, grid.width, grid.height, title, desc, 'heatmap');
  for (let h = 0; h < 24; h += 2) svg.append(el(doc, 'text', { class: 'tick-label', x: grid.left + h * grid.cw + grid.cw / 2, y: grid.top - 8, 'text-anchor': 'middle' }, String(h).padStart(2, '0')));
  WEEKDAYS.forEach((d, i) => svg.append(el(doc, 'text', { class: 'tick-label', x: grid.left - 6, y: grid.top + i * grid.ch + grid.ch / 2 + 4, 'text-anchor': 'end' }, d)));
  for (const c of grid.cells) {
    const g = el(doc, 'g', { class: 'cell' });
    const cls = c.empty ? 'hm-empty' : `hm-b${c.bucket < 0 ? 'n' : ''}${Math.abs(c.bucket)}`;
    g.append(el(doc, 'title', {}, `${WEEKDAYS[c.wd]} ${String(c.hour).padStart(2, '0')}:00 - ${c.empty ? 'no data' : `mean ${fmtDelay(c.mean)}`}, n=${fmtCount(c.n)}${c.lowN && !c.empty ? `. ${LOW_N_NOTE}` : ''}`),
      el(doc, 'rect', { class: `${cls}${c.lowN ? ' low-n' : ''}`, x: c.x, y: c.y, width: c.w, height: c.h, rx: 2 }));
    if (!c.empty) g.append(el(doc, 'text', { class: `cell-text ${cls}`, x: c.x + c.w / 2, y: c.y + c.h / 2 + 3, 'text-anchor': 'middle' }, fmtNum(c.mean / 60, 1)));
    svg.append(g);
  }
  return svg;
}

export function renderAccumulation(geo, stops, { title, desc, doc = globalThis.document } = {}) {
  const svg = root(doc, geo.width, geo.height, title, desc, 'acc-chart');
  for (const [ticks, x] of [[geo.ticksA, geo.x0], [geo.ticksB, geo.x0]]) {
    for (const t of ticks) svg.append(el(doc, 'line', { class: 'grid', x1: x, x2: geo.x1, y1: t.y, y2: t.y }), el(doc, 'text', { class: 'tick-label', x: x - 6, y: t.y + 4, 'text-anchor': 'end' }, t.label));
  }
  svg.append(el(doc, 'line', { class: 'zero-line', x1: geo.x0, x2: geo.x1, y1: geo.zeroA, y2: geo.zeroA }),
    el(doc, 'line', { class: 'zero-line', x1: geo.x0, x2: geo.x1, y1: geo.zeroB, y2: geo.zeroB }),
    el(doc, 'text', { class: 'axis-label', x: geo.x0, y: geo.panelA.top - 3 }, 'Mean delay at each stop (min, up = later)'),
    el(doc, 'text', { class: 'axis-label', x: geo.x0, y: geo.panelB.top - 6 }, 'Change from previous stop (min): above zero loses time, below gains'));
  svg.append(el(doc, 'path', { class: 'acc-line', d: geo.path, fill: 'none' }));
  stops.forEach((s, i) => {
    const p = geo.pts[i], b = geo.bars[i];
    if (p.y !== null) { const c = el(doc, 'circle', { class: 'acc-dot', cx: p.x, cy: p.y, r: 3 }); c.append(el(doc, 'title', {}, `Stop ${p.pos + 1} ${s.stop_id}: mean delay ${fmtDelay(s.mean_delay)}, ${plural(s.n, 'sample')}`)); svg.append(c); }
    if (b.delta !== null) { const r = el(doc, 'rect', { class: `bar ${b.delta > 0 ? 'late' : 'early'}`, x: b.x, y: b.y, width: b.w, height: Math.max(b.h, 1) }); r.append(el(doc, 'title', {}, `Into stop ${p.pos + 1} ${s.stop_id}: ${fmtDelay(b.delta)} from the previous stop, ${plural(s.n_delta, 'trip')} seen at both`)); svg.append(r); }
  });
  for (const t of geo.xTicks) svg.append(el(doc, 'text', { class: 'tick-label', x: t.x, y: geo.height - 6, 'text-anchor': 'middle' }, t.label));
  return svg;
}

export function renderStepChart(geo, { title, desc, doc = globalThis.document } = {}) {
  const svg = root(doc, geo.width, geo.height, title, desc, 'step-chart');
  for (const t of geo.yTicks) svg.append(el(doc, 'line', { class: 'grid', x1: geo.x0, x2: geo.x1, y1: t.y, y2: t.y }), el(doc, 'text', { class: 'tick-label', x: geo.x0 - 6, y: t.y + 4, 'text-anchor': 'end' }, t.label));
  for (const t of geo.xTicks) svg.append(el(doc, 'text', { class: 'tick-label', x: t.x, y: geo.height - 16, 'text-anchor': 'middle' }, t.label));
  svg.append(el(doc, 'text', { class: 'axis-label', x: geo.x0, y: geo.height - 2 }, 'Time (Dublin local) vs minutes shown to passengers (due)'));
  for (const m of geo.markers) {
    svg.append(el(doc, 'line', { class: `marker ${m.kind}`, x1: m.x, x2: m.x, y1: geo.y1, y2: geo.y0 }),
      el(doc, 'text', { class: 'marker-label', x: m.x + 4, y: geo.y1 + 10 + (m.kind === 'arrival' ? 12 : 0) }, m.label));
  }
  if (!geo.empty) svg.append(el(doc, 'path', { class: 'step-line', d: geo.path, fill: 'none' }));
  return svg;
}
