// Round "i" button + popover panel. One popover is open at a time; Escape or an outside click closes it.
import { h } from './dom.js';
import { hideTip } from './tooltip.js';

let uid = 0, open = null, inside = false;
const hooked = new WeakSet();

function close(restoreFocus) {
  if (!open) return;
  const { btn, panel } = open;
  open = null;
  panel.setAttribute('hidden', '');
  btn.setAttribute('aria-expanded', 'false');
  if (restoreFocus && btn.isConnected !== false) btn.focus();
}

// document listeners are added once per document, so re-rendering pages cannot pile them up
function hook(doc) {
  if (hooked.has(doc)) return;
  hooked.add(doc);
  doc.addEventListener('click', () => { if (!inside) close(false); inside = false; });
  doc.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(true); });
}

// entries: [{title, paragraphs}] -> [button, panel]; place both inside a position: relative host
export function infoPopover(label, entries) {
  hook(globalThis.document);
  const id = `info${++uid}`;
  const panel = h('div', { id: `${id}-pop`, class: 'info-pop', role: 'region', 'aria-labelledby': `${id}-t`, hidden: true, on: { click: () => { inside = true; } } },
    entries.map((e, i) => [h('h4', i ? {} : { id: `${id}-t` }, e.title), e.paragraphs.map((p) => h('p', {}, p))]));
  const btn = h('button', { type: 'button', class: 'info-btn', 'aria-label': label, 'aria-expanded': 'false', 'aria-controls': panel.getAttribute('id'), on: { click: () => {
    inside = true;
    const was = open && open.btn === btn;
    close(false);
    if (was) return;
    open = { btn, panel };
    hideTip(); // a chart tooltip must not sit over the panel
    panel.removeAttribute('hidden');
    btn.setAttribute('aria-expanded', 'true');
  } } }, 'i');
  return [btn, panel];
}
