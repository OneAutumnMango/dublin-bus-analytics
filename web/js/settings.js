// User settings: one validated JSON object in localStorage (key dba-settings). Works in memory when storage is blocked.
const KEY = 'dba-settings';
const STATS = ['mean', 'median', 'min', 'max', 'band', 'ci', 'std', 'ontime', 'n'];

export const DEFAULTS = Object.freeze({
  density: 'comfortable', // theme lives in theme.js (dba-theme) so it applies before first paint
  tip: { on: true, units: 'min', stats: Object.fromEntries(STATS.map((k) => [k, true])) },
  charts: { heatCap: 0, pageSize: 40, tableToggle: true },
});

const obj = (x) => (x && typeof x === 'object' && !Array.isArray(x) ? x : {});
const pick = (v, allowed, d) => (allowed.includes(v) ? v : d);
const bool = (v, d) => (typeof v === 'boolean' ? v : d);

export function validate(raw) {
  const r = obj(raw), tip = obj(r.tip), stats = obj(tip.stats), ch = obj(r.charts);
  const cap = ch.heatCap;
  return {
    density: pick(r.density, ['comfortable', 'compact'], DEFAULTS.density),
    tip: { on: bool(tip.on, true), units: pick(tip.units, ['min', 'sec'], 'min'),
      stats: Object.fromEntries(STATS.map((k) => [k, bool(stats[k], true)])) },
    charts: { heatCap: cap === 0 || (Number.isFinite(cap) && cap >= 1 && cap <= 60) ? cap : 0,
      pageSize: pick(ch.pageSize, [20, 40, 80], 40), tableToggle: bool(ch.tableToggle, true) },
  };
}

let ok = true;
const read = () => { try { const t = globalThis.localStorage.getItem(KEY); ok = true; return t ? JSON.parse(t) : null; } catch { ok = false; return null; } };
const write = (s) => { try { globalThis.localStorage.setItem(KEY, JSON.stringify(s)); ok = true; } catch { ok = false; } };

let current = validate(read());
const subs = new Set();
const notify = () => subs.forEach((fn) => fn(current));
const merge = (a, b) => { const o = { ...a }; for (const [k, v] of Object.entries(obj(b))) o[k] = obj(v) && obj(a[k]) === a[k] && typeof a[k] === 'object' ? merge(a[k], v) : v; return o; };

export const getSettings = () => current;
export const storageWorks = () => ok;
export function reload() { current = validate(read()); }
export function setSettings(patch) { current = validate(merge(current, patch)); write(current); notify(); return current; }
export function resetSettings() { current = validate(null); try { globalThis.localStorage.removeItem(KEY); } catch { ok = false; } notify(); return current; }
export function onSettings(fn) { subs.add(fn); return () => subs.delete(fn); }
