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

test('slider can reach 23:59 even though 1439 is not a multiple of the step', () => {
  installDom();
  const seen = [];
  const s = timeRangeSlider({ from: 0, to: 1439, onChange: (a, b) => seen.push([a, b]) });
  assert.deepEqual(s.get(), [0, 1439]);
  assert.match(s.el.textContent, /00:00 – 23:59/);
  const [, inB] = s.el.byTag('input');
  assert.equal(inB.attrs.get('step'), '1', 'native step 1: the browser would otherwise stop at 1435');
  for (const [typed, want] of [[1439, 1439], [1437, 1439], [1436, 1435], [1435, 1435], [1000, 1000], [1003, 1005]]) {
    inB.value = String(typed); inB.dispatch('change');
    assert.equal(s.get()[1], want, `typed ${typed}`);
  }
  assert.equal(seen.at(-1)[1], 1005);
});
