// Hash router: one page module per view, each exporting render(ctx).
import { isAbort } from './api.js';
import { errorBox, h, link, setStatus } from './dom.js';
import { parseHash } from './lib.js';
import * as analytics from './pages/analytics.js';
import * as route from './pages/route.js';
import * as routes from './pages/routes.js';
import * as stop from './pages/stop.js';
import * as trip from './pages/trip.js';

const PAGES = { routes, route, stop, trip, analytics };
const SITE = 'Dublin Bus Analytics';
let ctl = null;
let lastKey = null;

async function show() {
  if (ctl) ctl.abort(); // anything still loading for the previous view is cancelled and ignored
  ctl = new AbortController();
  const { signal } = ctl;
  const r = parseHash(globalThis.location.hash);
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
