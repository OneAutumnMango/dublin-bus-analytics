// fetch wrappers: timeout, abort, error mapping. No DOM.
import { buildQuery } from './lib.js';

export class ApiError extends Error {
  constructor(kind, message, status = 0, body = null) {
    super(message);
    this.body = body; // parsed JSON error body, when there was one
    this.kind = kind; // network | timeout | abort | http | parse
    this.status = status;
  }
}
export const isAbort = (e) => e instanceof ApiError && e.kind === 'abort';

export function errorMessage(status, bodyText) {
  let detail = '';
  try {
    const d = JSON.parse(bodyText).detail;
    detail = Array.isArray(d) ? d.map((x) => x.msg ?? JSON.stringify(x)).join('; ') : typeof d === 'string' ? d : '';
  } catch { /* body is not JSON */ }
  if (status === 422) return `Invalid request: ${detail || 'check the filter values'}`;
  if (status === 403 || status === 429) return detail || `The API refused the request (HTTP ${status})`;
  if (status === 409) return detail || 'The request conflicts with another operation; try again shortly';
  if (status === 404) return `Not found${detail ? `: ${detail}` : ''}`;
  if (status === 502) return `The bus data server is unavailable${detail ? ` (${detail})` : ''}`;
  return `The API answered HTTP ${status}`;
}

export async function request(path, { method = 'GET', signal, timeout = 15000, fetchFn = globalThis.fetch } = {}) {
  const ctl = new AbortController();
  let timedOut = false;
  const onAbort = () => ctl.abort();
  if (signal) {
    if (signal.aborted) throw new ApiError('abort', 'Cancelled');
    signal.addEventListener('abort', onAbort, { once: true });
  }
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, timeout);
  try {
    let res;
    try {
      res = await fetchFn(path, { method, signal: ctl.signal, headers: { Accept: 'application/json' } });
    } catch (e) {
      if (timedOut) throw new ApiError('timeout', `The API did not answer within ${Math.round(timeout / 1000)} s`);
      if (signal?.aborted || e?.name === 'AbortError') throw new ApiError('abort', 'Cancelled');
      throw new ApiError('network', 'Cannot reach the analytics API');
    }
    const text = await res.text();
    if (!res.ok) {
      let body = null;
      try { body = JSON.parse(text); } catch { /* not JSON */ }
      throw new ApiError('http', errorMessage(res.status, text), res.status, body && typeof body === 'object' ? body : null);
    }
    try { return JSON.parse(text); } catch { throw new ApiError('parse', 'The API returned an unreadable response'); }
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }
}

const enc = encodeURIComponent;
const qs = (q) => { const s = buildQuery(q); return s ? `?${s}` : ''; };

export function makeApi(fetchFn = globalThis.fetch) {
  const get = (path, o) => request(path, { ...o, fetchFn });
  return {
    health: (o) => get('/api/health', o),
    routes: (q, o) => get(`/api/routes${qs({ q })}`, o),
    tracked: (o) => get('/api/routes/tracked', o),
    track: (r, o) => request(`/api/routes/tracked/${enc(r)}`, { ...o, method: 'PUT', fetchFn }),
    untrack: (r, o) => request(`/api/routes/tracked/${enc(r)}`, { ...o, method: 'DELETE', fetchFn }),
    map: (q, o) => get(`/api/map${qs(q)}`, o),
    mapShapes: (o) => get('/api/map/shapes', o),
    stopsOf: (r, o) => get(`/api/routes/${enc(r)}/stops`, o),
    summary: (r, q, o) => get(`/api/routes/${enc(r)}/summary${qs(q)}`, o),
    heatmap: (r, q, o) => get(`/api/routes/${enc(r)}/heatmap${qs(q)}`, o),
    accumulation: (r, q, o) => get(`/api/routes/${enc(r)}/accumulation${qs(q)}`, o),
    forecast: (r, q, o) => get(`/api/routes/${enc(r)}/forecast${qs(q)}`, o),
    stopStats: (s, route, q, o) => get(`/api/stops/${enc(s)}/stats${qs({ route, ...q })}`, o),
    series: (trip, stop, q, o) => get(`/api/trips/${enc(trip)}/series${qs({ stop, ...q })}`, o),
    runBackup: (o) => request('/api/backup', { ...o, method: 'POST', timeout: 120000, fetchFn }),
    dayPreview: ({ date, end, route } = {}, o) => get(`/api/data/day${qs({ date, end, route })}`, o),
    removeDay: ({ date, end, route } = {}, o) => request(`/api/data/day/remove${qs({ date, end, route, confirm: end ? `${date}..${end}` : date })}`, { ...o, method: 'POST', fetchFn }),
    removeJob: (jobId, o) => get(`/api/data/day/remove/${enc(jobId)}`, o),
  };
}

export const api = makeApi((...a) => globalThis.fetch(...a)); // late-bound so tests can stub fetch
export const exportUrl = ({ route, gzip } = {}) => `/api/export.json${qs({ route, gzip: gzip ? 1 : '' })}`;
export const backupDownloadUrl = (file) => `/api/backup/download${qs({ file })}`;

// true: a backup can be downloaded; false: none yet (404); null: unknown. Reads headers only (Range 0-0, then aborts).
export async function backupAvailable({ signal, fetchFn = globalThis.fetch } = {}) {
  const ctl = new AbortController();
  const onAbort = () => ctl.abort();
  if (signal) signal.addEventListener('abort', onAbort, { once: true });
  try {
    const r = await fetchFn(backupDownloadUrl(), { method: 'GET', headers: { Range: 'bytes=0-0' }, signal: ctl.signal });
    ctl.abort();
    if (r.status === 404) return false;
    return r.status === 200 || r.status === 206 ? true : null;
  } catch {
    return null;
  } finally {
    if (signal) signal.removeEventListener('abort', onAbort);
  }
}
