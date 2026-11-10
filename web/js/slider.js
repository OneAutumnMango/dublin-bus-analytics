// Time-of-day window: two native range inputs layered on one track (keyboard accessible). Values are minutes since
// midnight, 0..1439; values snap to `step` minutes (23:59 is the last stop). If the start handle is after the end handle the window wraps midnight.
import { h } from './dom.js';
import { minutesLabel, windowLabel } from './mapdata.js';

export const MAX = 1439;
export const PRESETS = [['All day', 0, MAX], ['Morning peak', 420, 570], ['Daytime', 570, 960], ['Evening peak', 960, 1140], ['Night', 1320, 300]];

// Round to the step; the top of the day is 23:59 (1439), which is not a multiple of 5, so anything within half a step of it snaps there.
const snap = (v, step) => { const n = Number(v); return !Number.isFinite(n) ? 0 : n >= MAX - step / 2 ? MAX : Math.max(0, Math.round(n / step) * step); };

// Highlighted parts of the track as [leftPct, widthPct]; two parts when the window wraps midnight.
export function fillSegments(a, b) {
  const p = (m) => (m / MAX) * 100;
  return a <= b ? [[p(a), p(b) - p(a)]] : [[p(a), 100 - p(a)], [0, p(b)]];
}

export function timeRangeSlider({ from = 0, to = MAX, step = 5, onChange = () => {} } = {}) {
  let a = snap(from, step), b = snap(to, step);
  const mkInput = (cls, label, v) => h('input', { type: 'range', class: `tr-input ${cls}`, min: '0', max: String(MAX), step: '1', value: String(v), 'aria-label': label });
  const inA = mkInput('tr-from', 'Window start (time of day)', a), inB = mkInput('tr-to', 'Window end (time of day)', b);
  const label = h('output', { class: 'tr-label', 'aria-live': 'polite' });
  const track = h('div', { class: 'tr-track' });
  const fills = h('div', { class: 'tr-fills', 'aria-hidden': 'true' });
  const presetRow = h('div', { class: 'tr-presets' });

  const paint = () => {
    inA.value = String(a); inB.value = String(b);
    inA.setAttribute('aria-valuetext', minutesLabel(a)); inB.setAttribute('aria-valuetext', minutesLabel(b));
    label.textContent = windowLabel(a, b);
    fills.replaceChildren(...fillSegments(a, b).map(([l, w]) => { const f = h('span', { class: 'tr-fill' }); if (f.style) { f.style.left = `${l}%`; f.style.width = `${w}%`; } return f; }));
  };
  const read = () => { a = snap(inA.value, step); b = snap(inB.value, step); paint(); };
  inA.addEventListener('input', read); inB.addEventListener('input', read);
  const commit = () => { read(); onChange(a, b); };
  inA.addEventListener('change', commit); inB.addEventListener('change', commit);
  for (const [name, x, y] of PRESETS) presetRow.append(h('button', { type: 'button', class: 'btn btn-quiet tr-preset', on: { click: () => { a = x; b = y; paint(); onChange(a, b); } } }, name));
  track.append(fills, inA, inB);
  const ticks = h('div', { class: 'tr-ticks', 'aria-hidden': 'true' }, ['00:00', '06:00', '12:00', '18:00', '23:59'].map((t) => h('span', {}, t)));
  paint();
  const el = h('div', { class: 'time-range', role: 'group', 'aria-label': 'Time of day window' }, label, track, ticks, presetRow);
  return { el, get: () => [a, b], set: (x, y) => { a = snap(x, step); b = snap(y, step); paint(); } };
}
