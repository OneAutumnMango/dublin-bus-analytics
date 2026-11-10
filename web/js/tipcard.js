// Builds the tooltip card DOM from a tipdata payload. Text only (textContent); positions are set through the style
// object (CSSOM), which the page CSP allows. Returns null when detailed tooltips are switched off.
import { fmtTipValue } from './tipdata.js';
import { isNum } from './lib.js';

const ALWAYS = new Set(['delta', 'mae', 'share']); // not user-toggleable
const mk = (doc, tag, cls, text) => { const e = doc.createElement(tag); if (cls) e.setAttribute('class', cls); if (text != null) e.textContent = text; return e; };
const place = (e, o) => { if (e.style) Object.assign(e.style, o); };

export function visibleRows(payload, settings) {
  return (payload.rows || []).filter((r) => ALWAYS.has(r.k) || settings.tip.stats[r.k] !== false);
}

function rangeBar(doc, rg, settings) {
  const st = settings.tip.stats;
  const vals = [0, rg.min, rg.max, rg.lo, rg.hi, rg.mean].filter(isNum);
  const a = Math.min(...vals), b = Math.max(...vals), span = b - a || 1;
  const pct = (v) => `${(((v - a) / span) * 100).toFixed(2)}%`;
  const bar = mk(doc, 'div', 'tip-range');
  bar.setAttribute('aria-hidden', 'true');
  const zero = mk(doc, 'span', 'tr-zero');
  place(zero, { left: pct(0) });
  bar.append(zero);
  const add = (cls, from, to) => { const s = mk(doc, 'span', cls); place(s, { left: pct(from), width: `${Math.max(1, ((to - from) / span) * 100).toFixed(2)}%` }); bar.append(s); };
  if (st.min !== false && st.max !== false && isNum(rg.min) && isNum(rg.max)) add('tr-whisker', rg.min, rg.max);
  if (st.band !== false && isNum(rg.lo) && isNum(rg.hi)) add('tr-band', rg.lo, rg.hi);
  if (isNum(rg.mean)) { const m = mk(doc, 'span', `tr-mean ${rg.mean < 0 ? 'early' : rg.mean > 0 ? 'late' : ''}`); place(m, { left: pct(rg.mean) }); bar.append(m); }
  return bar;
}

export function renderCard(doc, payload, settings) {
  if (!settings.tip.on || !payload) return null;
  const units = settings.tip.units;
  const card = mk(doc, 'div', 'tip-card');
  card.append(mk(doc, 'div', 'tip-title', payload.title));
  if (payload.sub) card.append(mk(doc, 'div', 'tip-sub', payload.sub));
  if (payload.head && settings.tip.stats.mean !== false) {
    const sec = payload.head.sec;
    const h = mk(doc, 'div', 'tip-head');
    h.append(mk(doc, 'span', `tip-value ${sec < 0 ? 'early' : sec > 0 ? 'late' : ''}`, fmtTipValue({ fmt: 'delay', v: sec }, units)), mk(doc, 'span', 'tip-unit', 'mean'));
    card.append(h);
  }
  const st = settings.tip.stats;
  if (payload.range && (st.min !== false || st.band !== false || st.mean !== false)) card.append(rangeBar(doc, payload.range, settings));
  const rows = visibleRows(payload, settings);
  if (rows.length) {
    const grid = mk(doc, 'dl', 'tip-grid');
    for (const r of rows) grid.append(mk(doc, 'dt', null, r.l), mk(doc, 'dd', null, fmtTipValue(r, units)));
    card.append(grid);
  }
  if (payload.foot) card.append(mk(doc, 'div', 'tip-foot', payload.foot));
  return card;
}
