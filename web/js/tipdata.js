// Pure builders for tooltip payloads (JSON in data-tip-json). Delay values are seconds; rows carry a key (k) so the
// settings can hide statistics, a label (l), a format (fmt) and the raw value (v). Missing stats are left out.
import { fmtCount, fmtDelay, fmtPct, isNum, LOW_N_NOTE } from './lib.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const LOW = LOW_N_NOTE;
const row = (k, l, fmt, v) => ({ k, l, fmt, v });
const num = (...vs) => vs.every(isNum);
const head = (sec) => (isNum(sec) ? { sec } : null);
const out = (o) => { for (const k of Object.keys(o)) if (o[k] === undefined) delete o[k]; return o; };

export function rangeTipData(it, raw = {}) {
  const rows = [];
  if (isNum(raw.median)) rows.push(row('median', 'Median', 'delay', raw.median));
  if (isNum(it.min)) rows.push(row('min', 'Min', 'delay', it.min));
  if (isNum(it.max)) rows.push(row('max', 'Max', 'delay', it.max));
  if (num(it.lo, it.hi)) rows.push(row('band', 'Percentile band', 'range', [it.lo, it.hi]));
  if (num(raw.ci_low, raw.ci_high)) rows.push(row('ci', 'Confidence interval', 'range', [raw.ci_low, raw.ci_high]));
  if (isNum(raw.std)) rows.push(row('std', 'Std deviation', 'delay0', raw.std));
  if (isNum(raw.on_time_pct)) rows.push(row('ontime', 'On time', 'pct', raw.on_time_pct));
  rows.push(row('n', 'Samples', 'count', it.n ?? 0));
  const hasRange = isNum(it.min) || isNum(it.lo) || isNum(it.mean);
  return out({ title: it.label, head: head(it.mean), rows,
    range: hasRange ? { min: isNum(it.min) ? it.min : undefined, max: isNum(it.max) ? it.max : undefined, lo: isNum(it.lo) ? it.lo : undefined, hi: isNum(it.hi) ? it.hi : undefined, mean: isNum(it.mean) ? it.mean : undefined } : undefined,
    foot: it.low_n ? LOW : undefined });
}

export function accTipData(s, label, pos) {
  const rows = [];
  if (isNum(s.segment_delta)) rows.push(row('delta', 'Change from previous stop', 'delay', s.segment_delta));
  rows.push(row('n', 'Samples', 'count', s.n ?? 0));
  return out({ title: label, head: head(s.mean_delay), rows });
}

export function heatTipData(c) {
  return out({ title: `${DAYS[c.wd]} ${String(c.hour).padStart(2, '0')}:00`, head: head(c.mean), rows: [row('n', 'Samples', 'count', c.n ?? 0)], foot: c.lowN && isNum(c.mean) ? LOW : undefined });
}

export function forecastTipData(x) {
  const rows = [];
  if (isNum(x.mae)) rows.push(row('mae', 'Mean absolute error', 'delay0', x.mae));
  if (num(x.p_low, x.p_high)) rows.push(row('band', 'Percentile band', 'range', [x.p_low, x.p_high]));
  rows.push(row('n', 'Samples', 'count', x.n ?? 0));
  return out({ title: `${x.horizon_min} min before`, sub: 'Actual minus predicted', head: head(x.mean_error), rows, foot: x.low_n ? LOW : undefined });
}

export function histTipData(b, total) {
  const rows = [row('n', 'Arrivals', 'count', b.count ?? 0)];
  if (total > 0 && isNum(b.count)) rows.push(row('share', 'Share of arrivals', 'pct', (b.count / total) * 100));
  return { title: b.label, rows };
}

// units: 'min' (default look) or 'sec'. delay0 = unsigned spread value.
export function fmtTipValue(r, units = 'min') {
  const d = (s, signed) => {
    if (!isNum(s)) return '–';
    if (units === 'sec') { const v = Math.round(s); return `${v < 0 ? '−' : signed && v > 0 ? '+' : ''}${Math.abs(v).toLocaleString('en-IE')} s`; }
    return signed ? fmtDelay(s) : fmtDelay(Math.abs(s)).replace(/^\+/, '');
  };
  switch (r.fmt) {
    case 'delay': return d(r.v, true);
    case 'delay0': return d(r.v, false);
    case 'range': return Array.isArray(r.v) ? `${d(r.v[0], true)} to ${d(r.v[1], true)}` : '–';
    case 'pct': return fmtPct(r.v);
    case 'count': return isNum(r.v) ? fmtCount(r.v) : '–';
    default: return String(r.v ?? '–');
  }
}
