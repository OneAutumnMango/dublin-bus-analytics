// Small DOM builders. Text always goes through textContent / text nodes, never HTML parsing.
import { fmtCount } from './lib.js';
import { getSettings } from './settings.js';

export function h(tag, props, ...children) {
  const doc = globalThis.document;
  const e = doc.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'on') for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else if (k === 'text') e.textContent = String(v);
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected') e[k] = v;
    else e.setAttribute(k, v === true ? '' : String(v));
  }
  return put(e, ...children);
}

// Append children, skipping null/false and flattening arrays (a bare Node.append(null) would insert the text "null").
export function put(parent, ...children) {
  const doc = globalThis.document;
  const add = (c) => {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else parent.append(typeof c === 'object' ? c : doc.createTextNode(String(c)));
  };
  children.forEach(add);
  return parent;
}

export const link = (href, text, props) => h('a', { href, ...props }, text);

export function setStatus(text) {
  const s = globalThis.document.getElementById('status');
  if (s) s.textContent = text;
}

export const skeleton = (n = 4) => h('div', { class: 'skeleton', 'aria-hidden': 'true' }, Array.from({ length: n }, () => h('span')));
export const notice = (text, kind = 'warn') => h('div', { class: `notice ${kind}`, role: kind === 'warn' ? 'note' : null }, text);
export const emptyState = (text) => h('div', { class: 'empty' }, text);

export function errorBox(err, onRetry) {
  return h('div', { class: 'error', role: 'alert' }, h('strong', { text: 'Something went wrong. ' }), err?.message || String(err),
    onRetry ? h('div', {}, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: onRetry } }, 'Retry')) : null);
}

// n shown with every statistic; low_n greyed with an explanation in the tooltip.
export function tile(label, value, { note, lowN, n } = {}) {
  return h('div', { class: `tile${lowN ? ' low-n' : ''}`, title: lowN ? 'Fewer than 10 samples: treat this number with caution.' : null },
    h('div', { class: 'label' }, label), h('div', { class: 'value' }, value),
    note || n != null ? h('div', { class: 'note' }, [note, n != null ? `n=${fmtCount(n)}` : null].filter(Boolean).join(' · ')) : null);
}

// columns: [{label, num?, get(row) -> text|Node}]; rows may carry low_n for greying.
export function dataTable({ caption, columns, rows }) {
  return h('div', { class: 'table-wrap', tabindex: 0, role: 'region', 'aria-label': caption }, h('table', {},
    h('caption', { text: caption }),
    h('thead', {}, h('tr', {}, columns.map((c) => h('th', { scope: 'col', class: c.num ? 'num' : null }, c.label)))),
    h('tbody', {}, rows.map((r) => h('tr', { class: r.low_n || r.lowN ? 'low-n' : null, title: r.low_n || r.lowN ? 'Fewer than 10 samples: treat with caution.' : null },
      columns.map((c) => h('td', { class: c.num ? 'num' : null }, c.get(r))))))));
}

let figId = 0;
// A chart plus a toggleable data table (accessible alternative).
export function figure({ title, svg, table, legend, level = 'h2' }) {
  const id = `fig${++figId}`;
  const toggle = getSettings().charts.tableToggle;
  // toggle off: the table stays in the page for keyboard and screen reader users, just not visible
  const tableBox = toggle ? h('div', { id: `${id}-table`, hidden: true }, table) : h('div', { id: `${id}-table`, class: 'sr-only' }, table);
  const btn = h('button', { class: 'btn btn-quiet', type: 'button', 'aria-expanded': 'false', 'aria-controls': `${id}-table`, on: { click: () => {
    const open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? 'Hide data table' : 'Show data table';
    if (open) tableBox.removeAttribute('hidden'); else tableBox.setAttribute('hidden', '');
  } } }, 'Show data table');
  btn.setAttribute('title', 'The chart is a picture; the data table has the same numbers and links for keyboard and screen reader users.');
  return h('figure', { class: 'card' }, h('div', { class: 'figure-head' }, h(level, {}, title), toggle ? btn : null),
    h('div', { class: 'chart-scroll', tabindex: 0, role: 'region', 'aria-label': `${title} chart` }, svg), legend || null, tableBox);
}
