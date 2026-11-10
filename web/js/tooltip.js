// One tooltip for everything hoverable. On first hover, native tooltips are converted to data-tip so the browser's
// own bubble never shows alongside ours: inner SVG <title> elements (the chart root keeps its title/desc for screen
// readers) and HTML title attributes.
const MAX_UP = 5;

// Text to show for el: nearest data-tip, else a direct <title> child (SVG), within a few ancestors.
export function tipText(el) {
  let n = el;
  for (let i = 0; n && i < MAX_UP; i++, n = n.parentNode ?? null) {
    const own = n.getAttribute?.('data-tip');
    if (own) return own;
    const t = (n.children ? [...n.children] : [...(n.childNodes || [])]).find((c) => (c.tag ?? c.tagName) === 'title');
    if (t?.textContent) return t.textContent;
    if ((n.tag ?? n.tagName) === 'svg') break;
  }
  return null;
}

// 14px right/below the pointer; flips left/up near the right/bottom edge; never closer than 8px to the top/left.
export function placeTip(x, y, w, h, vw, vh) {
  let left = x + 14, top = y + 14;
  if (left + w > vw - 8) left = x - 14 - w;
  if (top + h > vh - 8) top = y - 14 - h;
  return { left: Math.max(8, left), top: Math.max(8, top) };
}

// Moves native tooltip sources inside a chart svg to data-tip (idempotent); keeps the chart's own accessible title.
export function stripChart(svg) {
  for (const t of svg.querySelectorAll('title')) {
    const p = t.parentNode;
    if (!p || p === svg) continue;
    p.setAttribute('data-tip', t.textContent);
    t.remove();
  }
}

// HTML title="" attributes on the hovered element or its ancestors become data-tip.
export function stripAttr(el) {
  for (let n = el; n && n.getAttribute && n.tagName !== 'BODY'; n = n.parentNode) {
    const t = n.getAttribute('title');
    if (t) { n.setAttribute('data-tip', t); n.removeAttribute('title'); }
  }
}

export function installTooltip(doc = globalThis.document) {
  if (typeof doc?.addEventListener !== 'function') return null; // non-browser host (tests)
  const tip = doc.createElement('div');
  tip.id = 'chart-tip';
  tip.setAttribute('role', 'presentation');
  tip.hidden = true;
  doc.body.append(tip);

  const hide = () => { tip.hidden = true; };
  const show = (target, x, y) => {
    const text = tipText(target);
    if (!text) { hide(); return; }
    if (tip.textContent !== text) tip.textContent = text;
    tip.hidden = false;
    const { width, height } = tip.getBoundingClientRect();
    const p = placeTip(x, y, width, height, globalThis.innerWidth, globalThis.innerHeight);
    tip.style.left = `${p.left}px`;
    tip.style.top = `${p.top}px`;
  };
  const prep = (t) => { const svg = t?.closest?.('svg.chart'); if (svg) stripChart(svg); stripAttr(t); };
  doc.addEventListener('pointerover', (e) => prep(e.target), true);
  doc.addEventListener('pointermove', (e) => show(e.target, e.clientX, e.clientY));
  doc.addEventListener('mouseout', (e) => { if (!e.relatedTarget) hide(); }); // pointer left the window
  doc.addEventListener('focusin', (e) => {
    prep(e.target);
    const r = e.target.getBoundingClientRect?.();
    if (r) show(e.target, r.left + r.width / 2, r.top + r.height / 2);
  });
  doc.addEventListener('focusout', hide);
  return tip;
}
