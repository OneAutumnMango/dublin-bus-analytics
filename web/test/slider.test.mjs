import test from 'node:test';
import assert from 'node:assert/strict';
import { timeRangeSlider, fillSegments, PRESETS, MAX } from '../js/slider.js';
import { installDom } from './fakedom.mjs';

test('fillSegments: one segment, or two when the window wraps midnight', () => {
  assert.deepEqual(fillSegments(0, MAX), [[0, 100]]);
  const [seg] = fillSegments(MAX / 4, MAX / 2);
  assert.ok(Math.abs(seg[0] - 25) < 0.01 && Math.abs(seg[1] - 25) < 0.01);
  const wrap = fillSegments(1320, 300);
  assert.equal(wrap.length, 2);
  assert.ok(wrap[0][0] + wrap[0][1] > 99.99 && wrap[1][0] === 0);
});

test('slider: label, snapping, change fires onChange, presets, wrap label', () => {
  installDom();
  const seen = [];
  const s = timeRangeSlider({ from: 362, to: 633, onChange: (a, b) => seen.push([a, b]) });
  assert.deepEqual(s.get(), [360, 635]);
  assert.match(s.el.textContent, /06:00 – 10:35/);
  const [inA, inB] = s.el.byTag('input');
  assert.equal(inA.getAttribute('aria-valuetext'), '06:00');
  inA.value = '1337'; inA.dispatch('input'); // dragging: label updates, no callback yet
  assert.match(s.el.textContent, /22:15 – 10:35 \(overnight\)/);
  assert.equal(seen.length, 0);
  inA.dispatch('change');
  assert.deepEqual(seen, [[1335, 635]]);
  inB.value = '99999'; inB.dispatch('change');
  assert.equal(s.get()[1], MAX);
  assert.equal(s.el.byTag('input').every((i) => i.attrs.get('type') === 'range'), true);
  const night = s.el.byText('button', /^Night$/)[0];
  night.dispatch('click');
  assert.deepEqual(seen[seen.length - 1], [1320, 300]);
  assert.equal(PRESETS.length, 5);
  assert.ok(!/NaN|undefined/.test(s.el.textContent));
  s.set(0, MAX);
  assert.match(s.el.textContent, /00:00 – 23:59/);
});
