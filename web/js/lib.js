// Pure helpers (no DOM, no network): formatting, due reconstruction, scales, query/hash routing, controls.
// Delays are SECONDS in the API; negative = early, positive = late.

export const DASH = '–';
export const LOW_N = 10;
export const LOW_N_NOTE = 'Fewer than 10 samples: treat this number with caution.';
export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

// ---------- formatting ----------
export function fmtNum(v, digits = 1) {
  if (!isNum(v)) return DASH;
  const s = v.toFixed(digits);
  return /^-0(\.0*)?$/.test(s) ? s.slice(1) : s;
}

export function fmtMinutes(sec, { signed = false } = {}) {
  if (!isNum(sec)) return DASH;
  const r = Math.round(Math.abs(sec));
  const sign = r === 0 ? '' : sec < 0 ? '-' : signed ? '+' : '';
  if (r < 60) return `${sign}${r} s`;
  const m = Math.abs(sec) / 60;
  if (m >= 1e6) return sec < 0 ? '<-1,000,000 min' : '>1,000,000 min';
  return `${sec < 0 ? '-' : signed ? '+' : ''}${m.toFixed(1)} min`;
}

export const fmtDelay = (sec) => fmtMinutes(sec, { signed: true });

export function describeDelay(sec) {
  if (!isNum(sec)) return DASH;
  if (Math.abs(sec) < 1) return 'on time';
  return `${fmtMinutes(Math.abs(sec))} ${sec > 0 ? 'late' : 'early'}`;
}

export function delayClass(sec) {
  if (!isNum(sec)) return '';
  const r = Math.round(sec);
  return r === 0 ? 'ontime' : r < 0 ? 'early' : 'late';
}

// "1.0 to 3.0 min late", "1.0 min early to 3.0 min late"
export function describeRange(lo, hi) {
  if (!isNum(lo) || !isNum(hi)) return DASH;
  if (lo > hi) [lo, hi] = [hi, lo];
  if (lo === hi) return describeDelay(lo);
  if (lo < 0 && hi > 0) return `${describeDelay(lo)} to ${describeDelay(hi)}`;
  const side = lo >= 0 ? 'late' : 'early';
  const [a, b] = lo >= 0 ? [lo, hi] : [-hi, -lo];
  const secs = b < 60;
  const f = (x) => (secs ? String(Math.round(x)) : (x / 60).toFixed(1));
  return `${f(a)} to ${f(b)} ${secs ? 's' : 'min'} ${side}`;
}

export function fmtPct(v) {
  return isNum(v) ? `${v.toFixed(1)}%` : DASH;
}

export function fmtCount(n) {
  if (!isNum(n)) return DASH;
  const r = Math.round(n);
  return (r < 0 ? '-' : '') + String(Math.abs(r)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

export function shorten(text, max) {
  const s = text == null ? '' : String(text);
  return s.length <= max ? s : `${s.slice(0, Math.max(0, max - 1))}…`;
}

function dublinParts(epoch) {
  const f = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Dublin', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  return Object.fromEntries(f.formatToParts(new Date(epoch * 1000)).map((p) => [p.type, p.value]));
}
export function clockTime(epoch) {
  if (!isNum(epoch)) return DASH;
  const p = dublinParts(epoch);
  return `${p.hour}:${p.minute}`;
}
export function dateTime(epoch) {
  if (!isNum(epoch)) return DASH;
  const p = dublinParts(epoch);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

// ---------- trip series ----------
export const dueAt = (expected, t) => Math.max(0, Math.floor((expected - t) / 60));

// Step function of the displayed due minutes: [{t, due}] where each entry holds until the next one.
// Within a sample the due counts down at minute boundaries (expected fixed); a new sample resets it.
// Nothing is emitted after lastSeen (no extrapolated countdown).
export function dueSteps(points, lastSeen) {
  if (!Array.isArray(points) || !points.length) return [];
  const sorted = [...points].sort((a, b) => a.ts - b.ts);
  const end = isNum(lastSeen) ? lastSeen : sorted[sorted.length - 1].ts;
  const pts = sorted.filter((p) => p.ts <= end);
  const out = [];
  const push = (t, due) => {
    const last = out[out.length - 1];
    if (last && last.t === t) last.due = due;
    else if (!last || last.due !== due || t === end) out.push({ t, due });
  };
  pts.forEach((p, i) => {
    const b = i + 1 < pts.length ? pts[i + 1].ts : end;
    if (!isNum(p.expected)) return push(p.ts, p.due_min);
    let d = dueAt(p.expected, p.ts);
    push(p.ts, d);
    for (; d > 0; d--) {
      const t = p.expected - d * 60 + 1; // first second at which floor((expected - t) / 60) == d - 1
      if (t >= b) break;
      push(t, d - 1);
    }
  });
  const last = out[out.length - 1];
  if (last && last.t < end) out.push({ t: end, due: last.due });
  return out;
}

// ---------- scales ----------
export function extent(values) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) if (isNum(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
  return lo === Infinity ? null : [lo, hi];
}

export function linearScale([d0, d1], [r0, r1]) {
  if (d0 === d1) return () => (r0 + r1) / 2;
  const k = (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
}

const r12 = (x) => Number(x.toPrecision(12));
function niceStep(raw) {
  const e = Math.floor(Math.log10(raw));
  const f = raw / 10 ** e;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * 10 ** e;
}
function orderPad(min, max) {
  if (min > max) [min, max] = [max, min];
  return min === max ? [min - 1, max + 1] : [min, max];
}

export function niceTicks(min, max, count = 5) {
  if (!isNum(min) || !isNum(max)) return [];
  const [lo, hi] = orderPad(min, max);
  const step = niceStep((hi - lo) / Math.max(1, count));
  const out = [];
  for (let k = Math.ceil(lo / step - 1e-9); k <= Math.floor(hi / step + 1e-9); k++) out.push(r12(k * step));
  return out;
}

export function niceDomain(min, max, count = 5, includeZero = false) {
  if (!isNum(min) || !isNum(max)) return [-1, 1];
  if (includeZero) [min, max] = [Math.min(min, max, 0), Math.max(min, max, 0)];
  const [lo, hi] = orderPad(min, max);
  const step = niceStep((hi - lo) / Math.max(1, count));
  return [r12(Math.floor(lo / step + 1e-9) * step), r12(Math.ceil(hi / step - 1e-9) * step)];
}

// Diverging colour bucket -steps..steps (negative = early). Null for missing values. CSS maps buckets to colours.
export function divergingBucket(v, maxAbs, steps = 5) {
  if (!isNum(v)) return null;
  if (!(maxAbs > 0)) return 0;
  const b = Math.round(Math.min(1, Math.abs(v) / maxAbs) * steps);
  return b === 0 ? 0 : v < 0 ? -b : b;
}

// ---------- query string and hash routing ----------
const enc = encodeURIComponent;
export function buildQuery(obj) {
  const parts = [];
  for (const [k, v] of Object.entries(obj || {})) {
    if (v == null || v === '' || (Array.isArray(v) && !v.length)) continue;
    parts.push(`${enc(k)}=${enc(Array.isArray(v) ? v.join(',') : String(v))}`);
  }
  return parts.join('&');
}

function decode(s) {
  try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch { return s; }
}
export function parseQuery(str) {
  const out = {};
  for (const part of String(str || '').replace(/^\?/, '').split('&')) {
    if (!part) continue;
    const i = part.indexOf('=');
    out[decode(i < 0 ? part : part.slice(0, i))] = i < 0 ? '' : decode(part.slice(i + 1));
  }
  return out;
}

const ROUTES = { routes: [], route: ['route'], stop: ['route', 'stop'], trip: ['trip', 'stop'], analytics: ['route'] };

export function buildHash(name, params = {}, query = {}) {
  const segs = [name === 'routes' ? '' : name, ...(ROUTES[name] || []).map((k) => enc(params[k] ?? ''))];
  const q = buildQuery(query);
  return `#/${name === 'routes' ? '' : segs.join('/')}${q ? `?${q}` : ''}`;
}

export function parseHash(hash) {
  const s = String(hash || '').replace(/^#\/?/, '');
  const qi = s.indexOf('?');
  const path = qi < 0 ? s : s.slice(0, qi);
  const query = qi < 0 ? {} : parseQuery(s.slice(qi + 1));
  const notfound = { name: 'notfound', params: {}, query: {} };
  if (path === '') return { name: 'routes', params: {}, query };
  const [name, ...rest] = path.split('/');
  const keys = ROUTES[name];
  if (!keys || name === 'routes' || rest.length !== keys.length || rest.some((x) => x === '')) return notfound;
  try {
    return { name, params: Object.fromEntries(keys.map((k, i) => [k, decodeURIComponent(rest[i])])), query };
  } catch {
    return notfound;
  }
}

// ---------- stale-response guard ----------
// start() aborts the previous request; isCurrent() is false once superseded or once the parent aborted.
export function makeLatest(parentSignal) {
  let current = null;
  return {
    start() {
      if (current) current.ctl.abort();
      const ctl = new AbortController();
      if (parentSignal) {
        if (parentSignal.aborted) ctl.abort();
        else parentSignal.addEventListener('abort', () => ctl.abort(), { once: true });
      }
      const me = { ctl };
      current = me;
      return { signal: ctl.signal, isCurrent: () => current === me && !ctl.signal.aborted };
    },
  };
}

// ---------- controls (confidence level, percentiles, window, dates, weekday/hour filters) ----------
export const DEFAULT_CONTROLS = Object.freeze({ ci: 95, pLo: 5, pHi: 95, winLo: -1, winHi: 5, since: '', until: '',
  weekdays: [], hFrom: '', hTo: '', confirmed: true });
export const DEFAULT_DAYS = 90;
const blank = (v) => v === '' || v == null;

export function dateToEpoch(str, endOfDay = false) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str || '');
  if (!m) return null;
  const ms = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  const d = new Date(ms);
  if (d.getUTCFullYear() !== +m[1] || d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3]) return null;
  return ms / 1000 + (endOfDay ? 86399 : 0);
}

export function hoursList(from, to) {
  if (blank(from) && blank(to)) return null;
  if (blank(from)) return [Number(to)];
  if (blank(to)) return [Number(from)];
  const out = [];
  for (let h = Number(from); ; h = (h + 1) % 24) {
    out.push(h);
    if (h === Number(to) || out.length > 24) break;
  }
  return out;
}

export function validateControls(c) {
  const e = [];
  if (!(isNum(c.ci) && c.ci > 0 && c.ci <= 99.99)) e.push('Confidence level must be above 0 and at most 99.99 %.');
  if (!(isNum(c.pLo) && isNum(c.pHi) && c.pLo >= 0 && c.pLo < c.pHi && c.pHi <= 100)) {
    e.push('Percentile band: lower must be below upper, both within 0 to 100.');
  }
  if (!(isNum(c.winLo) && isNum(c.winHi) && c.winLo <= c.winHi)) e.push('On-time window: from must not exceed to (minutes).');
  const s = blank(c.since) ? null : dateToEpoch(c.since);
  const u = blank(c.until) ? null : dateToEpoch(c.until);
  if ((!blank(c.since) && s === null) || (!blank(c.until) && u === null)) e.push('Dates must be valid (YYYY-MM-DD).');
  else if (s !== null && u !== null && s > u) e.push('The start date must not be after the end date.');
  for (const h of [c.hFrom, c.hTo]) {
    if (!blank(h) && !(Number.isInteger(Number(h)) && h >= 0 && h <= 23)) e.push('Hours must be whole numbers from 0 to 23.');
  }
  if (!(c.weekdays || []).every((d) => Number.isInteger(d) && d >= 0 && d <= 6)) e.push('Weekdays must be 0 (Mon) to 6 (Sun).');
  return [...new Set(e)];
}

// Always sends an explicit `since` (default: now - 90 days) so unwindowed heavy requests are avoided.
export function toApiQuery(c, nowSec) {
  const p100 = (x) => Number((x / 100).toPrecision(10));
  const since = blank(c.since) ? null : dateToEpoch(c.since);
  const until = blank(c.until) ? null : dateToEpoch(c.until, true);
  return {
    ci: p100(c.ci), pct_lo: p100(c.pLo), pct_hi: p100(c.pHi),
    window_lo: Math.round(c.winLo * 60), window_hi: Math.round(c.winHi * 60),
    since: since ?? Math.floor(nowSec) - DEFAULT_DAYS * 86400, until,
    weekdays: c.weekdays && c.weekdays.length ? c.weekdays : null, hours: hoursList(c.hFrom, c.hTo),
    min_confidence: c.confirmed ? 1 : 0,
  };
}

const D = DEFAULT_CONTROLS;
export function controlsToQuery(c) {
  const q = {};
  const num = [['ci', 'ci'], ['pLo', 'plo'], ['pHi', 'phi'], ['winLo', 'wlo'], ['winHi', 'whi']];
  for (const [k, qk] of num) if (c[k] !== D[k]) q[qk] = String(c[k]);
  if (c.since !== D.since) q.since = c.since;
  if (c.until !== D.until) q.until = c.until;
  if (c.weekdays.length) q.wd = c.weekdays.join(',');
  if (!blank(c.hFrom)) q.hf = String(c.hFrom);
  if (!blank(c.hTo)) q.ht = String(c.hTo);
  if (!c.confirmed) q.conf = '0';
  return q;
}

export function controlsFromQuery(q) {
  const c = { ...D, weekdays: [] };
  const errors = [];
  const bad = (k) => errors.push(`Ignored invalid "${k}" in the link.`);
  const present = (k) => q[k] !== undefined && q[k] !== '';
  const number = (k, field) => {
    if (!present(k)) return;
    const n = Number(q[k]);
    if (Number.isFinite(n)) c[field] = n; else bad(k);
  };
  number('ci', 'ci'); number('plo', 'pLo'); number('phi', 'pHi'); number('wlo', 'winLo'); number('whi', 'winHi');
  for (const k of ['since', 'until']) {
    if (!present(k)) continue;
    if (dateToEpoch(q[k]) === null) bad(k); else c[k] = q[k];
  }
  if (present('wd')) {
    const days = q.wd.split(',').map((x) => (/^[0-6]$/.test(x) ? Number(x) : NaN));
    if (days.some(Number.isNaN)) bad('wd'); else c.weekdays = [...new Set(days)].sort((a, b) => a - b);
  }
  for (const [k, field] of [['hf', 'hFrom'], ['ht', 'hTo']]) {
    if (!present(k)) continue;
    if (/^([0-9]|1[0-9]|2[0-3])$/.test(q[k])) c[field] = Number(q[k]); else bad(k);
  }
  if (present('conf')) {
    if (q.conf === '0' || q.conf === '1') c.confirmed = q.conf === '1'; else bad('conf');
  }
  const cross = validateControls(c);
  if (cross.length) return { controls: { ...D, weekdays: [] }, errors: [...errors, ...cross] };
  return { controls: c, errors };
}

export function paginate(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  const p = Math.min(pages, Math.max(1, Number.isFinite(page) ? Math.floor(page) : 1));
  const start = (p - 1) * size;
  return { page: p, pages, start, end: Math.min(total, start + size) };
}

// {direction: [{id|stopId, name|stopName}]} -> Map(stop id -> display name) for labelling.
export function stopNameMap(directions) {
  const m = new Map();
  for (const list of Object.values(directions && typeof directions === 'object' ? directions : {})) {
    for (const s of Array.isArray(list) ? list : []) {
      const id = s?.id ?? s?.stopId;
      const name = s?.name ?? s?.stopName ?? s?.stop_name;
      if (typeof id === 'string' && typeof name === 'string' && name && !m.has(id)) m.set(id, name);
    }
  }
  return m;
}
