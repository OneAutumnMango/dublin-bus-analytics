// Middle-click autoscroll for charts: click the middle button on a chart, then move the mouse; the further from the
// click point, the faster it scrolls (sideways inside the chart, up/down on the page). Click again or press Esc to stop;
// holding the button and releasing after a drag also stops. The browser's own autoscroll is switched off on Linux by default.
const DEAD = 12, GAIN = 0.15;

// Pixels to scroll per frame for a pointer `d` pixels from the click point (nothing inside the dead zone).
export const scrollStep = (d) => (Math.abs(d) <= DEAD ? 0 : (d - Math.sign(d) * DEAD) * GAIN);

export function installAutoscroll(doc = globalThis.document) {
  if (typeof doc?.addEventListener !== 'function') return;
  let st = null; // { box, ox, oy, x, y, t, moved, raf, dot }
  const stop = () => {
    if (!st) return;
    globalThis.cancelAnimationFrame(st.raf);
    st.dot.remove();
    doc.body.classList.remove('autoscrolling');
    st = null;
  };
  const tick = () => {
    if (!st) return;
    const dx = scrollStep(st.x - st.ox), dy = scrollStep(st.y - st.oy);
    if (dx) st.box.scrollLeft += dx;
    if (dy) { if (st.box.scrollHeight > st.box.clientHeight + 1) st.box.scrollTop += dy; else globalThis.scrollBy(0, dy); }
    st.raf = globalThis.requestAnimationFrame(tick);
  };
  doc.addEventListener('mousedown', (e) => {
    if (st) { stop(); e.preventDefault(); return; } // any click ends it
    if (e.button !== 1) return;
    const box = e.target?.closest?.('.chart-scroll');
    if (!box) return;
    e.preventDefault(); // no browser autoscroll or paste-on-middle-click
    const dot = doc.createElement('div');
    dot.className = 'autoscroll-dot';
    dot.setAttribute('aria-hidden', 'true');
    dot.style.left = `${e.clientX}px`;
    dot.style.top = `${e.clientY}px`;
    doc.body.append(dot);
    doc.body.classList.add('autoscrolling');
    st = { box, ox: e.clientX, oy: e.clientY, x: e.clientX, y: e.clientY, t: Date.now(), moved: false, dot, raf: 0 };
    st.raf = globalThis.requestAnimationFrame(tick);
  });
  doc.addEventListener('mousemove', (e) => {
    if (!st) return;
    st.x = e.clientX; st.y = e.clientY;
    if (Math.abs(st.x - st.ox) > DEAD || Math.abs(st.y - st.oy) > DEAD) st.moved = true;
  });
  doc.addEventListener('mouseup', (e) => { if (st && e.button === 1 && st.moved && Date.now() - st.t > 250) stop(); }); // hold, drag, release
  doc.addEventListener('auxclick', (e) => { if (e.button === 1 && e.target?.closest?.('.chart-scroll')) e.preventDefault(); });
  doc.addEventListener('keydown', (e) => { if (e.key === 'Escape') stop(); });
  globalThis.addEventListener('blur', stop);
}
