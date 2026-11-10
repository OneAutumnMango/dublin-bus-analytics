// Routes page: search server routes, choose which routes to track, per-route status, backups.
import { api, backupDownloadUrl, isAbort } from '../api.js';
import { emptyState, errorBox, h, link, put, setStatus } from '../dom.js';
import { buildHash, fmtCount } from '../lib.js';

const WEEK = 7 * 86400;
const MAX_RESULTS = 50;

export async function render(ctx) {
  const { main, signal } = ctx;
  ctx.title('Routes');
  const banner = h('div', { id: 'api-banner' });
  const alertBox = h('div', { role: 'alert' });
  const trackedBox = h('div', { 'aria-live': 'polite' });
  const resultsBox = h('div', { 'aria-live': 'polite' });
  const backupBox = h('div', { class: 'row' });
  let tracked = [];
  let results = null;

  const fail = (e) => { if (!isAbort(e)) { alertBox.replaceChildren(errorBox(e)); setStatus(e.message); } };

  async function act(fn, okText) {
    alertBox.replaceChildren();
    try {
      const r = await fn();
      if (signal.aborted) return;
      tracked = r.routes;
      setStatus(okText);
      draw();
    } catch (e) { fail(e); }
  }

  function trackedRow(r) {
    const status = h('span', { class: 'grow' }, 'Checking status…');
    api.summary(r, { since: ctx.nowSec() - WEEK, min_confidence: 0 }, { signal })
      .then((s) => { status.textContent = s.n ? `${fmtCount(s.n)} arrivals logged in the last 7 days across ${fmtCount(s.stops.length)} stops` : 'No arrivals logged yet in the last 7 days (the poller logs every stop of a tracked route)'; })
      .catch((e) => { if (!isAbort(e)) status.textContent = `Status unavailable: ${e.message}`; });
    return h('li', {}, h('span', { class: 'name' }, link(buildHash('route', { route: r }), `Route ${r}`)), status,
      link(buildHash('analytics', { route: r }), 'Analytics', { 'aria-label': `Analytics for route ${r}` }),
      h('button', { class: 'btn btn-danger', type: 'button', 'aria-label': `Stop tracking route ${r}`, on: { click: () => act(() => api.untrack(r, { signal }), `Stopped tracking ${r}`) } }, 'Untrack'));
  }

  function draw() {
    trackedBox.replaceChildren(tracked.length
      ? h('ul', { class: 'route-list' }, tracked.map(trackedRow))
      : emptyState('No routes tracked yet. Search for a route below and press Track: the poller then logs every stop of that route.'));
    if (results) drawResults();
  }

  function drawResults() {
    if (!results.length) return resultsBox.replaceChildren(emptyState('No routes match that search.'));
    resultsBox.replaceChildren(h('ul', { class: 'route-list' }, results.slice(0, MAX_RESULTS).map((r) => {
      const name = String(r.shortName ?? r.id ?? '');
      const on = tracked.includes(name);
      return h('li', {}, h('span', { class: 'name' }, name), h('span', { class: 'grow' }, r.longName || ''),
        on ? h('span', { class: 'badge' }, 'Tracked')
          : h('button', { class: 'btn', type: 'button', 'aria-label': `Track route ${name}`, on: { click: () => act(() => api.track(name, { signal }), `Tracking ${name}`) } }, 'Track'));
    })), results.length > MAX_RESULTS ? h('p', { class: 'hint' }, `Showing the first ${MAX_RESULTS} of ${results.length} routes. Refine the search.`) : null);
  }

  async function search(q) {
    resultsBox.replaceChildren(h('p', { class: 'muted' }, 'Searching…'));
    try {
      results = await api.routes(q, { signal });
      if (!Array.isArray(results)) results = [];
      if (!signal.aborted) drawResults();
    } catch (e) { if (!isAbort(e)) resultsBox.replaceChildren(errorBox(e, () => search(q))); }
  }

  const q = h('input', { id: 'route-q', type: 'search', autocomplete: 'off', placeholder: 'e.g. 15 or 46A' });
  const form = h('form', { class: 'row', role: 'search', on: { submit: (ev) => { ev.preventDefault(); search(q.value.trim()); } } },
    h('label', { for: 'route-q' }, 'Search routes on the server', q), h('button', { class: 'btn', type: 'submit' }, 'Search'));

  const backupMsg = h('span', { role: 'status', class: 'muted' });
  const dl = link(backupDownloadUrl(), 'Download latest backup', { class: 'btn btn-quiet', download: '' });
  backupBox.append(dl, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    backupMsg.textContent = 'Creating backup…';
    try {
      const r = await api.runBackup({ signal });
      backupMsg.textContent = `Backup ${r.file} created (${fmtCount(Math.round(r.size / 1024))} KiB).`;
      dl.setAttribute('href', backupDownloadUrl(r.file));
    } catch (e) { if (!isAbort(e)) backupMsg.textContent = `Backup failed: ${e.message}`; } finally { btn.disabled = false; }
  } } }, 'Run backup now'), backupMsg);

  put(main, h('h1', {}, 'Routes'),
    h('p', { class: 'subtitle' }, 'Choose which bus routes to track. For every tracked route the analytics service logs each stop, then compares actual with scheduled arrivals.'),
    banner, alertBox,
    h('section', { class: 'card', 'aria-labelledby': 'h-tracked' }, h('h2', { id: 'h-tracked' }, 'Tracked routes'), trackedBox),
    h('section', { class: 'card', 'aria-labelledby': 'h-find' }, h('h2', { id: 'h-find' }, 'Find a route'), form, resultsBox),
    h('section', { class: 'card', 'aria-labelledby': 'h-backup' }, h('h2', { id: 'h-backup' }, 'Backup'),
      h('p', { class: 'hint' }, 'A backup is a tar of the stored data. Scheduled backups also run daily.'), backupBox));

  trackedBox.replaceChildren(h('div', { class: 'skeleton', 'aria-hidden': 'true' }, h('span'), h('span')));
  try {
    await api.health({ signal });
    tracked = (await api.tracked({ signal })).routes || [];
    if (!signal.aborted) draw();
  } catch (e) {
    if (isAbort(e)) return;
    banner.replaceChildren(h('div', { class: 'error', role: 'alert' }, h('strong', {}, e.kind === 'network' ? 'The analytics API is unreachable. ' : 'The analytics API reported a problem. '), e.message,
      h('div', {}, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => ctx.reload() } }, 'Retry'))));
    trackedBox.replaceChildren(emptyState('Tracked routes are unavailable while the API cannot be reached.'));
  }
}
