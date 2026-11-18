// Route text box with a dropdown of suggestions (the tracked routes): type to filter, or click / arrow keys + Enter to pick.
// The box holds a comma separated list; a pick replaces the route being typed and leaves ", " so the next one can follow.
import { h } from './dom.js';

const split = (text) => String(text).split(/[\s,]+/);

// Tracked routes not yet in the box that start with what is being typed (the text after the last separator).
export function suggest(tracked, text) {
  const parts = split(text);
  const cur = (parts.pop() || '').toLowerCase();
  const chosen = new Set(parts.filter(Boolean));
  return tracked.filter((r) => !chosen.has(r) && r.toLowerCase().startsWith(cur));
}

export function applyChoice(text, route) {
  const parts = split(text);
  parts.pop();
  return `${[...parts.filter(Boolean), route].join(', ')}, `;
}

let uid = 0;
export function routePicker(input) {
  const id = `rp${++uid}`;
  const list = h('ul', { class: 'suggest', id, role: 'listbox', 'aria-label': 'Tracked routes', hidden: true });
  const el = h('div', { class: 'suggest-wrap' }, input, list);
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-controls', id);
  input.setAttribute('aria-expanded', 'false');
  let tracked = [], items = [], active = -1;
  const hide = () => { list.setAttribute('hidden', ''); input.setAttribute('aria-expanded', 'false'); active = -1; };
  const mark = () => items.forEach((li, i) => li.setAttribute('aria-selected', String(i === active)));
  const choose = (r) => { input.value = applyChoice(input.value, r); refresh(); input.focus?.(); };
  function refresh() {
    const found = suggest(tracked, input.value);
    items = found.map((r) => h('li', { role: 'option', 'aria-selected': 'false', on: { mousedown: (e) => { e.preventDefault?.(); choose(r); } } }, `Route ${r}`));
    active = -1;
    list.replaceChildren(...items);
    if (!items.length) { hide(); return; }
    list.removeAttribute('hidden');
    input.setAttribute('aria-expanded', 'true');
  }
  input.addEventListener('focus', refresh);
  input.addEventListener('click', refresh);
  input.addEventListener('input', refresh);
  input.addEventListener('blur', hide);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { hide(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (list.hasAttribute('hidden')) refresh();
      if (!items.length) return;
      e.preventDefault?.();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      mark();
    } else if (e.key === 'Enter' && active >= 0) { e.preventDefault?.(); choose(items[active].textContent.replace('Route ', '')); } // else Enter submits the form
  });
  return { el, setTracked: (routes) => { tracked = [...routes]; if (globalThis.document?.activeElement === input) refresh(); } };
}
