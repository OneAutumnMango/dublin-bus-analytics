// Floating tooltip for charts. Reads the text of the SVG <title> elements the renderers already emit, so screen
// readers keep their labels. While a tip is shown its <title> is detached so the browser's native tooltip
// does not double up; it is restored on leave.
const TAG = new Set(['g', 'rect', 'circle', 'path']);

// Text of the first direct <title> child of el, or of the nearest ancestor (max 3 levels, never past the svg).
export function tipText(el) {
  return titleNode(el)?.textContent || null;
}

function titleNode(el) {
  let n = el;
  for (let i = 0; n && i < 4; i++, n = n.parentNode ?? null) {
    if (n.tag === 'svg' || n.tagName === 'svg') return null;
    const name = n.tag ?? n.tagName;
    if (!TAG.has(name)) continue;
    const t = (n.children ? [...n.children] : [...(n.childNodes || [])]).find((c) => (c.tag ?? c.tagName) === 'title');
    if (t) return t;
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

export function installTooltip(doc = globalThis.document) {
  if (typeof doc?.addEventListener !== 'function') return null; // non-browser host (tests)
  const tip = doc.createElement('div');
  tip.id = 'chart-tip';
  tip.setAttribute('role', 'presentation');
  tip.hidden = true;
  doc.body.append(tip);
  let held = null; // { parent, node } title detached while its tip is visible

  const restore = () => { if (held) { held.parent.append(held.node); held = null; } };
  const hide = () => { restore(); tip.hidden = true; };
  const show = (target, x, y) => {
    const node = titleNode(target);
    if (!node) { hide(); return; }
    if (!held || held.node !== node) { restore(); tip.textContent = node.textContent; held = { parent: node.parentNode, node }; node.remove(); }
    tip.hidden = false;
    const { width, height } = tip.getBoundingClientRect();
    const p = placeTip(x, y, width, height, globalThis.innerWidth, globalThis.innerHeight);
    tip.style.left = `${p.left}px`;
    tip.style.top = `${p.top}px`;
  };
  const inChart = (t) => t?.closest?.('svg.chart');
  doc.addEventListener('pointermove', (e) => { if (inChart(e.target)) show(e.target, e.clientX, e.clientY); else hide(); });
  doc.addEventListener('pointerleave', hide);
  doc.addEventListener('focusin', (e) => {
    if (!inChart(e.target)) return;
    const r = e.target.getBoundingClientRect();
    show(e.target, r.left + r.width / 2, r.top + r.height / 2);
  });
  doc.addEventListener('focusout', hide);
  return tip;
}
