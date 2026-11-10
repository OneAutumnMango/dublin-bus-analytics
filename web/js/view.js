// Async region loader: skeleton -> data -> render, or an inline error with Retry. Stale responses are dropped.
import { isAbort } from './api.js';
import { errorBox, setStatus, skeleton } from './dom.js';
import { makeLatest } from './lib.js';

export function region(container, parentSignal) {
  const latest = makeLatest(parentSignal);
  async function load(fetcher, render, { rows = 4, announce } = {}) {
    const tok = latest.start();
    container.replaceChildren(skeleton(rows));
    container.setAttribute('aria-busy', 'true');
    setStatus('Loading…');
    try {
      const data = await fetcher(tok.signal);
      if (!tok.isCurrent()) return;
      container.replaceChildren();
      render(data, container);
      if (announce) setStatus(announce(data));
    } catch (e) {
      if (isAbort(e) || !tok.isCurrent()) return;
      container.replaceChildren(errorBox(e, () => load(fetcher, render, { rows, announce })));
      setStatus(`Error: ${e.message}`);
    } finally {
      if (tok.isCurrent()) container.removeAttribute('aria-busy');
    }
  }
  return { load };
}
