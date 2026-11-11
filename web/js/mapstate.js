// What you picked on the map page, remembered in this browser (localStorage key dba-map): colour metric, area style, stops shown, time
// window, routes switched off, and where the map was left. Validated field by field; storage may be blocked.
import { AREA_MODES, METRICS, METRIC_DEFAULT } from './mapdata.js';

const KEY = 'dba-map';
const MAX = 1439;
export const MAP_DEFAULTS = Object.freeze({ metric: METRIC_DEFAULT, areas: 'hex', stops: true, win: [0, MAX], hidden: [], view: null });

const minute = (v) => Number.isInteger(v) && v >= 0 && v <= MAX;

export function validateMapState(raw) {
  const r = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const v = r.view;
  const view = v && typeof v === 'object' && Number.isFinite(v.lng) && Number.isFinite(v.lat) && Number.isFinite(v.zoom)
    && Math.abs(v.lng) <= 180 && Math.abs(v.lat) <= 85 && v.zoom >= 0 && v.zoom <= 22 ? { lng: v.lng, lat: v.lat, zoom: v.zoom } : null;
  return {
    metric: METRICS.some(([k]) => k === r.metric) ? r.metric : METRIC_DEFAULT,
    areas: AREA_MODES.some(([k]) => k === r.areas) ? r.areas : MAP_DEFAULTS.areas,
    stops: typeof r.stops === 'boolean' ? r.stops : true,
    win: Array.isArray(r.win) && r.win.length === 2 && r.win.every(minute) ? [r.win[0], r.win[1]] : [0, MAX],
    hidden: Array.isArray(r.hidden) ? r.hidden.filter((x) => typeof x === 'string' && x.length <= 20).slice(0, 500) : [],
    view,
  };
}

export function loadMapState() {
  try { return validateMapState(JSON.parse(globalThis.localStorage.getItem(KEY))); } catch { return validateMapState(null); }
}

// Merge `patch` into the stored state and save it; failures (private mode, quota) are ignored.
export function saveMapState(patch) {
  const next = validateMapState({ ...loadMapState(), ...patch });
  try { globalThis.localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* not remembered */ }
  return next;
}
