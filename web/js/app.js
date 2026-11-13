// Hash router: one page module per view, each exporting render(ctx).
import { isAbort } from './api.js';
import { errorBox, h, link, setStatus } from './dom.js';
import { parseHash } from './lib.js';
import { onTrackedChange, renderSidebar } from './sidebar.js';
import { installTooltip } from './tooltip.js';
import * as all from './pages/all.js';
import * as analytics from './pages/analytics.js';
import * as lab from './pages/lab.js';
import * as route from './pages/route.js';
import * as routes from './pages/routes.js';
import * as stop from './pages/stop.js';
import * as trip from './pages/trip.js';
import * as settings from './pages/settings.js';
import * as map from './pages/map.js';
import { onSettings } from './settings.js';

const PAGES = { routes, route, stop, trip, analytics, lab, settings, map, all };
const SITE = 'Dublin Bus Analytics';
let ctl = null;
let lastKey = null;

const NAV = { routes: 'routes', route: 'routes', stop: 'routes', trip: 'routes', analytics: 'routes', lab: 'routes', map: 'map', settings: 'settings', all: 'routes' };
function markNav(name) { // highlight the current section in the top bar
  const links = globalThis.document.querySelectorAll?.('.main-nav a') || [];
  for (const a of links) { if (a.getAttribute('data-nav') === NAV[name]) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); }
}

async function show() {
  if (ctl) ctl.abort(); // anything still loading for the previous view is cancelled and ignored
  ctl = new AbortController();
  const { signal } = ctl;
  const r = parseHash(globalThis.location.hash);
  markNav(r.name);
  const main = globalThis.document.getElementById('main');
  main.replaceChildren();
  main.removeAttribute('aria-busy');
  const ctx = {
    main, params: r.params, query: r.query, signal,
    title: (t) => { globalThis.document.title = t ? `${t} · ${SITE}` : SITE; },
    go: (hash) => {
      const before = globalThis.location.href;
      globalThis.location.hash = hash;
      if (globalThis.location.href === before) show(); // same hash: no hashchange event fires
    },
    reload: () => show(),
    nowSec: () => Math.floor(Date.now() / 1000),
  };
  const page = PAGES[r.name];
  const key = r.name + JSON.stringify(r.params);
  try {
    let done;
    if (page) done = page.render(ctx);
    else {
      ctx.title('Not found');
      main.append(h('h1', {}, 'Page not found'), h('p', {}, 'That address is not a page of this app. ', link('#/', 'Go to the routes list')));
    }
    if (key !== lastKey) { main.focus(); globalThis.scrollTo(0, 0); lastKey = key; }
    await done;
  } catch (e) {
    if (!isAbort(e) && !signal.aborted) {
      main.append(errorBox(e, () => show()));
      setStatus(`Error: ${e.message}`);
    }
  }
}

globalThis.addEventListener('hashchange', show);
show();
installTooltip();
settings.applyAppearance();
onSettings(() => settings.applyAppearance());
const sidebar = globalThis.document.getElementById('sidebar');
if (sidebar) renderSidebar(sidebar);
onTrackedChange(() => { if (parseHash(globalThis.location.hash).name === 'all') show(); }); // the all-routes page depends on the tracked set
