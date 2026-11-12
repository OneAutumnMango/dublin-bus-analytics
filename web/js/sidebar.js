// Persistent left sidebar: live route search on top, tracked routes below. Mounted once by app.js, so it
// survives navigation between pages.
import { api, isAbort } from './api.js';
import { emptyState, errorBox, h, link, setStatus } from './dom.js';
import { buildHash, makeLatest, parseHash } from './lib.js';

const MAX_RESULTS = 50;
const DEBOUNCE_MS = 200;
let mark = () => {}; // set by the latest render
const listeners = [];
let hooked = false; // the hashchange listener is added once, even if the sidebar is re-rendered (Retry)

// Registers fn to run after every successful track/untrack; returns an unsubscribe function.
export function onTrackedChange(fn) {
  listeners.push(fn);
  return () => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
}
function notifyTrackedChange() {
  for (const fn of [...listeners]) { try { fn(); } catch { /* a faulty listener must not break the sidebar */ } }
}

// Exact short-name match first, then prefix matches, then the rest; server order within a group.
export function rankRoutes(list, q) {
  const k = q.trim().toLowerCase();
  const rank = (r) => { const n = String(r.shortName ?? r.id ?? '').toLowerCase(); return n === k ? 0 : n.startsWith(k) ? 1 : 2; };
  return list.map((r, i) => [rank(r), i, r]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => x[2]);
}

export async function renderSidebar(root, { signal } = {}) {
  const alertBox = h('div', { role: 'alert' });
  const trackedBox = h('div', {});
  const resultsBox = h('div', { 'aria-live': 'polite' });
  const searchLatest = makeLatest(signal);
  let tracked = [];
  let results = null;
  let busy = false;
  let timer = null;
  let mutButtons = [];
  let confirming = null; // route whose Untrack awaits confirmation
  let confirmTimer = null;

  const nameOf = (r) => String(r.shortName ?? r.id ?? '');
  const fail = (e) => { if (!isAbort(e)) { alertBox.replaceChildren(errorBox(e)); setStatus(e.message); } };
  const current = () => parseHash(globalThis.location?.hash).params.route;
  let links = [];
  const allLink = link(buildHash('all'), 'All routes', { 'data-all': '' });
  const markActive = mark = () => {
    const cur = current();
    if (parseHash(globalThis.location?.hash).name === 'all') allLink.setAttribute('aria-current', 'page'); else allLink.removeAttribute('aria-current');
    for (const a of links) { if (a.getAttribute('data-route') === cur) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); }
  };

  async function act(fn, okText) {
    if (busy) { setStatus('Another change is in progress'); return; }
    alertBox.replaceChildren();
    busy = true; mutButtons.forEach((x) => { x.disabled = true; });
    try {
      const r = await fn();
      tracked = r.routes;
      setStatus(okText);
      draw();
      input.focus(); // the clicked button was rebuilt; keep keyboard users in the sidebar
      notifyTrackedChange();
    } catch (e) { fail(e); } finally { busy = false; mutButtons.forEach((x) => { x.disabled = false; }); }
  }
  const track = (name) => act(() => api.track(name, { signal }), `Tracking ${name}`);
  const untrack = (name) => act(() => api.untrack(name, { signal }), `Stopped tracking ${name}`);

  function draw() {
    mutButtons = [];
    links = [];
    trackedBox.replaceChildren(tracked.length
      ? h('ul', { class: 'route-list' }, tracked.map((r) => {
        const ask = (on) => {
          clearTimeout(confirmTimer);
          confirming = on ? r : null;
          if (on) confirmTimer = setTimeout(() => { confirming = null; draw(); }, 5000); // an unanswered prompt reverts
          draw();
          if (on) globalThis.document.getElementById(`confirm-${r}`)?.focus?.();
        };
        const btns = confirming === r
          ? [h('button', { id: `confirm-${r}`, class: 'btn btn-danger solid', type: 'button', 'aria-label': `Confirm untracking route ${r}`, disabled: busy, on: { click: () => { clearTimeout(confirmTimer); confirming = null; untrack(r); } } }, 'Confirm'),
            h('button', { class: 'btn btn-quiet', type: 'button', 'aria-label': `Cancel untracking route ${r}`, on: { click: () => ask(false) } }, 'Cancel')]
          : [h('button', { class: 'btn btn-danger', type: 'button', 'aria-label': `Untrack route ${r}`, disabled: busy, on: { click: () => ask(true) } }, 'Untrack')];
        mutButtons.push(...btns.slice(0, 1));
        const btn = h('span', { class: 'row-actions' }, btns);
        const a = link(buildHash('analytics', { route: r }), `Route ${r}`, { 'data-route': r });
        links.push(a);
        return h('li', {}, h('span', { class: 'name grow' }, a), btn);
      }))
      : emptyState('No routes tracked yet. Search above and press Track.'));
    markActive();
    if (results) drawResults();
  }

  function drawResults() {
    if (!results.length) return resultsBox.replaceChildren(emptyState('No routes match that search.'));
    resultsBox.replaceChildren(h('ul', { class: 'route-list' }, results.slice(0, MAX_RESULTS).map((r) => {
      const name = nameOf(r);
      const label = h('span', { class: 'grow' }, h('span', { class: 'name' }, name), ' ', h('span', { class: 'muted' }, r.longName || ''));
      if (tracked.includes(name)) return h('li', {}, label, h('span', { class: 'badge' }, 'Tracked'));
      const btn = h('button', { class: 'btn', type: 'button', 'aria-label': `Track route ${name}`, disabled: busy, on: { click: () => track(name) } }, 'Track');
      mutButtons.push(btn);
      return h('li', {}, label, btn);
    })), results.length > MAX_RESULTS ? h('p', { class: 'hint' }, `Showing the first ${MAX_RESULTS} of ${results.length} routes. Keep typing to narrow.`) : null);
  }

  async function search(q) {
    q = q.trim();
    const tok = searchLatest.start(); // a newer search supersedes (aborts and outranks) this one
    if (!q) { results = null; resultsBox.replaceChildren(); return []; }
    try {
      const r = await api.routes(q, { signal: tok.signal });
      if (!tok.isCurrent()) return null;
      results = rankRoutes(Array.isArray(r) ? r : [], q);
      drawResults();
      return results;
    } catch (e) {
      if (tok.isCurrent() && !isAbort(e)) results = null;
      if (tok.isCurrent() && !isAbort(e)) resultsBox.replaceChildren(errorBox(e, () => search(q)));
      return null;
    }
  }

  const input = h('input', { id: 'route-q', type: 'search', autocomplete: 'off', placeholder: 'Search routes, e.g. 15 or 46A',
    on: { input: () => { clearTimeout(timer); timer = setTimeout(() => search(input.value), DEBOUNCE_MS); } } });
  const form = h('form', { role: 'search', on: { submit: async (ev) => {
    ev.preventDefault();
    clearTimeout(timer);
    const sent = input.value;
    const q = sent.trim().toLowerCase();
    const list = await search(sent);
    const hit = q && list?.find((r) => nameOf(r).toLowerCase() === q);
    if (hit && !tracked.includes(nameOf(hit))) track(nameOf(hit));
    else if (hit) setStatus(`Route ${nameOf(hit)} is already tracked`);
    // An exact match consumes the query; otherwise keep the results to pick from, and never wipe text typed meanwhile.
    if (hit && input.value === sent) { input.value = ''; resultsBox.replaceChildren(); }
  } } }, h('label', { for: 'route-q' }, 'Route number', input));

  root.replaceChildren(h('h2', {}, 'Find a route'), form, alertBox, resultsBox, h('h2', {}, 'Tracked routes'), h('p', { class: 'all-routes' }, allLink), trackedBox);
  markActive();
  if (!hooked) { hooked = true; globalThis.addEventListener?.('hashchange', () => mark()); }
  trackedBox.replaceChildren(h('div', { class: 'skeleton', 'aria-hidden': 'true' }, h('span'), h('span')));
  try {
    tracked = (await api.tracked({ signal })).routes || [];
    draw();
  } catch (e) {
    if (!isAbort(e)) trackedBox.replaceChildren(errorBox(e, () => renderSidebar(root, { signal })));
  }
}
