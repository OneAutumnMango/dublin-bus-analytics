// Routes page: search server routes, choose which routes to track, per-route status, backups.
import { api, backupAvailable, backupDownloadUrl, isAbort } from '../api.js';
import { emptyState, errorBox, h, link, put, setStatus } from '../dom.js';
import { buildHash, fmtCount, makeLatest, makeLimiter, plural } from '../lib.js';

const WEEK = 7 * 86400;
const MAX_RESULTS = 50;
const limiter = makeLimiter(2); // status requests are heavy: at most two at once, across page visits
const statusCache = new Map(); // route -> status text, kept for the session (only successes)

export const _resetForTests = () => statusCache.clear();

export async function render(ctx) {
  const { main, signal } = ctx;
  ctx.title('Routes');
  const banner = h('div', { id: 'api-banner' });
  const alertBox = h('div', { role: 'alert' });
  const trackedBox = h('div', { tabindex: -1 });
  const resultsBox = h('div', { 'aria-live': 'polite' });
  const backupBox = h('div', { class: 'row' });
  const searchLatest = makeLatest(signal);
  const pending = new Map(); // route -> AbortController of an in-flight status request
  let tracked = [];
  let results = null;
  let busy = false;
  let mutButtons = [];

  const fail = (e) => { if (!isAbort(e)) { alertBox.replaceChildren(errorBox(e)); setStatus(e.message); } };
  const setBusy = (b) => { busy = b; mutButtons.forEach((x) => { x.disabled = b; }); };

  // One mutation at a time: buttons are disabled while it runs, so responses cannot land out of order.
  async function act(fn, okText, changed) {
    if (busy) return;
    alertBox.replaceChildren();
    setBusy(true);
    try {
      const r = await fn();
      if (signal.aborted) return;
      tracked = r.routes;
      statusCache.delete(changed); // a newly (re)tracked route starts with a fresh status
      setStatus(okText);
      draw();
      trackedBox.focus();
    } catch (e) { fail(e); } finally { if (!signal.aborted) setBusy(false); }
  }

  function loadStatus(route) {
    if (statusCache.has(route)) return Promise.resolve(statusCache.get(route));
    const ctl = new AbortController();
    signal.addEventListener('abort', () => ctl.abort(), { once: true });
    pending.set(route, ctl);
    return limiter.run(() => api.summary(route, { since: ctx.nowSec() - WEEK, min_confidence: 0 }, { signal: ctl.signal }), ctl.signal)
      .then((s) => {
        const text = s.n ? `${plural(s.n, 'realtime arrival')} (last 7 days) across ${plural(s.stops.length, 'stop')}`
          : 'No realtime arrivals yet (last 7 days). The poller logs every stop of a tracked route.';
        statusCache.set(route, text);
        return text;
      }).finally(() => { if (pending.get(route) === ctl) pending.delete(route); });
  }

  function trackedRow(r) {
    const status = h('span', { class: 'grow' }, 'Checking status…');
    loadStatus(r).then((t) => { status.textContent = t; })
      .catch((e) => { if (!isAbort(e)) status.textContent = `Status unavailable: ${e.message}`; });
    const btn = h('button', { class: 'btn btn-danger', type: 'button', 'aria-label': `Stop tracking route ${r}`, disabled: busy, on: { click: () => act(() => api.untrack(r, { signal }), `Stopped tracking ${r}`, r) } }, 'Untrack');
    mutButtons.push(btn);
    return h('li', {}, h('span', { class: 'name' }, link(buildHash('route', { route: r }), `Route ${r}`)), status,
      link(buildHash('analytics', { route: r }), 'Analytics', { 'aria-label': `Analytics for route ${r}` }), btn);
  }

  function draw() {
    for (const [r, ctl] of pending) if (!tracked.includes(r)) { ctl.abort(); pending.delete(r); } // untracked: stop checking
    mutButtons = [];
    trackedBox.replaceChildren(tracked.length
      ? h('ul', { class: 'route-list' }, tracked.map(trackedRow))
      : emptyState('No routes tracked yet. Search for a route below and press Track: the poller then logs every stop of that route.'));
    if (results) drawResults();
  }

  function drawResults() {
    if (!results.length) return resultsBox.replaceChildren(emptyState('No routes match that search.'));
    resultsBox.replaceChildren(h('ul', { class: 'route-list' }, results.slice(0, MAX_RESULTS).map((r) => {
      const name = String(r.shortName ?? r.id ?? '');
      if (tracked.includes(name)) return h('li', {}, h('span', { class: 'name' }, name), h('span', { class: 'grow' }, r.longName || ''), h('span', { class: 'badge' }, 'Tracked'));
      const btn = h('button', { class: 'btn', type: 'button', 'aria-label': `Track route ${name}`, disabled: busy, on: { click: () => act(() => api.track(name, { signal }), `Tracking ${name}`, name) } }, 'Track');
      mutButtons.push(btn);
      return h('li', {}, h('span', { class: 'name' }, name), h('span', { class: 'grow' }, r.longName || ''), btn);
    })), results.length > MAX_RESULTS ? h('p', { class: 'hint' }, `Showing the first ${MAX_RESULTS} of ${results.length} routes. Refine the search.`) : null);
  }

  async function search(q) {
    const tok = searchLatest.start(); // a newer search supersedes (aborts and outranks) this one
    resultsBox.replaceChildren(h('p', { class: 'muted' }, 'Searching…'));
    try {
      const r = await api.routes(q, { signal: tok.signal });
      if (!tok.isCurrent()) return;
      results = Array.isArray(r) ? r : [];
      drawResults();
    } catch (e) { if (tok.isCurrent() && !isAbort(e)) resultsBox.replaceChildren(errorBox(e, () => search(q))); }
  }

  const q = h('input', { id: 'route-q', type: 'search', autocomplete: 'off', placeholder: 'e.g. 15 or 46A' });
  const form = h('form', { class: 'row', role: 'search', on: { submit: (ev) => { ev.preventDefault(); search(q.value.trim()); } } },
    h('label', { for: 'route-q' }, 'Search routes on the server', q), h('button', { class: 'btn', type: 'submit' }, 'Search'));

  const backupMsg = h('span', { role: 'status', class: 'muted' });
  const dlSlot = h('span', {});
  const showDownload = (href) => dlSlot.replaceChildren(link(href, 'Download latest backup', { class: 'btn btn-quiet', download: '' }));
  const noBackup = () => dlSlot.replaceChildren(h('span', { class: 'muted' }, 'No backup yet. Use “Run backup now”.'));
  put(backupBox, dlSlot, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    backupMsg.textContent = 'Creating backup…';
    try {
      const r = await api.runBackup({ signal });
      backupMsg.textContent = `Backup ${r.file} created (${fmtCount(Math.round(r.size / 1024))} KiB).`;
      showDownload(backupDownloadUrl(r.file));
    } catch (e) { if (!isAbort(e)) backupMsg.textContent = `Backup failed: ${e.message}`; } finally { btn.disabled = false; }
  } } }, 'Run backup now'), backupMsg);
  noBackup();
  backupAvailable({ signal }).then((ok) => { if (signal.aborted) return; if (ok === false) noBackup(); else showDownload(backupDownloadUrl()); });

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
    if (!signal.aborted) { draw(); setStatus(`Routes: ${plural(tracked.length, 'tracked route')}`); }
  } catch (e) {
    if (isAbort(e)) return;
    banner.replaceChildren(h('div', { class: 'error', role: 'alert' }, h('strong', {}, e.kind === 'network' ? 'The analytics API is unreachable. ' : 'The analytics API reported a problem. '), e.message,
      h('div', {}, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => ctx.reload() } }, 'Retry'))));
    trackedBox.replaceChildren(emptyState('Tracked routes are unavailable while the API cannot be reached.'));
  }
}
