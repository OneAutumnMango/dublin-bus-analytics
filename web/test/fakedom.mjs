// Minimal DOM double: enough for the h() builder, SVG renderers and pages. No HTML parsing on purpose.
export class FText { constructor(t) { this.textContent = String(t); this.nodeType = 3; } }
export class FNode {
  constructor(tag) { this.tag = tag; if (tag === 'select') this.value = ''; this.attrs = new Map(); this.children = []; this.listeners = {}; this._t = null; this.nodeType = 1; }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  removeAttribute(k) { this.attrs.delete(k); }
  hasAttribute(k) { return this.attrs.has(k); }
  append(...kids) { for (const k of kids) this.children.push(k instanceof FNode || k instanceof FText ? k : new FText(k)); } // like browsers: null -> "null"
  appendChild(k) { this.append(k); return k; }
  replaceChildren(...kids) { this.children = []; this._t = null; this.append(...kids); }
  set textContent(t) { this.children = []; this._t = String(t); }
  get textContent() { return this._t ?? this.children.map((c) => c.textContent).join(''); }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  removeEventListener() {}
  focus() { this.focused = true; }
  dispatch(ev, extra = {}) {
    const e = { type: ev, preventDefault() { this.prevented = true; }, target: this, currentTarget: this, ...extra };
    for (const fn of this.listeners[ev] || []) fn(e);
    return e;
  }
  *walk() { yield this; for (const c of this.children) if (c instanceof FNode) yield* c.walk(); }
  all(pred) { return [...this.walk()].filter(pred); }
  byTag(tag) { return this.all((n) => n.tag === tag); }
  byText(tag, re) { return this.all((n) => n.tag === tag && re.test(n.textContent)); }
}

export function installDom() {
  const body = new FNode('body');
  const status = new FNode('div'); status.setAttribute('id', 'status');
  const main = new FNode('main'); main.setAttribute('id', 'main');
  body.append(status, main);
  const doc = {
    createElement: (t) => new FNode(t), createElementNS: (ns, t) => new FNode(t), createTextNode: (t) => new FText(t),
    getElementById: (id) => body.all((n) => n.getAttribute('id') === id)[0] || null, title: '', body,
  };
  const listeners = {};
  Object.assign(globalThis, { document: doc, scrollTo() {}, history: { replaceState(_a, _b, u) { globalThis.location.hash = u; } },
    addEventListener: (ev, fn) => { (listeners[ev] ||= []).push(fn); }, __listeners: listeners });
  globalThis.location = { hash: '', get href() { return `http://x/${this.hash}`; } };
  return { doc, main, status };
}

// fetch stub: handlers keyed by "METHOD /path" (query ignored) -> (url, init) => body | {status, body}
export function installFetch(handlers) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const path = url.split('?')[0];
    const key = `${init.method || 'GET'} ${path}`;
    calls.push({ key, url, init });
    const h = handlers[key];
    if (!h) return { ok: false, status: 404, text: async () => '{"detail":"Not Found"}' };
    const r = await (typeof h === 'function' ? h(url, init) : h);
    const status = r && r.__status ? r.__status : 200;
    const body = r && r.__status ? r.body : r;
    return { ok: status < 300, status, text: async () => (typeof body === 'string' ? body : JSON.stringify(body)) };
  };
  return calls;
}
export const fail = (status, detail) => ({ __status: status, body: { detail } });
export const settle = (ms = 15) => new Promise((r) => setTimeout(r, ms));
