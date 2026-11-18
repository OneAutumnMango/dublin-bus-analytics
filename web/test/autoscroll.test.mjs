import test from 'node:test';
import assert from 'node:assert/strict';
import { scrollStep, installAutoscroll } from '../js/autoscroll.js';

test('scrollStep: dead zone, direction, faster further out', () => {
  assert.equal(scrollStep(0), 0);
  assert.equal(scrollStep(12), 0);
  assert.equal(scrollStep(-12), 0);
  assert.ok(scrollStep(50) > 0 && scrollStep(-50) < 0);
  assert.equal(scrollStep(50), -scrollStep(-50));
  assert.ok(scrollStep(200) > scrollStep(50));
});

test('install is a no-op without a browser document', () => {
  assert.doesNotThrow(() => installAutoscroll({}));
});

// a tiny event target is enough: the handlers only need addEventListener, body and the chart box
function setup() {
  const handlers = {};
  const classes = new Set();
  const dots = [];
  const frames = [];
  const doc = { addEventListener: (t, f) => { (handlers[t] ||= []).push(f); },
    createElement: () => { const d = { style: {}, setAttribute() {}, remove() { dots.splice(dots.indexOf(d), 1); } }; return d; },
    body: { append: (d) => dots.push(d), classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) } } };
  globalThis.requestAnimationFrame = (f) => frames.push(f);
  globalThis.cancelAnimationFrame = () => {};
  globalThis.scrollBy = () => {};
  const orig = globalThis.addEventListener;
  globalThis.addEventListener = () => {};
  installAutoscroll(doc);
  globalThis.addEventListener = orig;
  const box = { scrollLeft: 0, scrollTop: 0, scrollHeight: 100, clientHeight: 100 };
  const target = { closest: (s) => (s === '.chart-scroll' ? box : null) };
  const fire = (t, e) => (handlers[t] || []).forEach((f) => f(e));
  return { box, target, fire, classes, dots, frames };
}

test('middle click starts, pointer offset scrolls, next click stops', () => {
  const { box, target, fire, classes, dots, frames } = setup();
  let prevented = 0;
  fire('mousedown', { button: 1, target, clientX: 100, clientY: 100, preventDefault: () => prevented++ });
  assert.equal(prevented, 1);
  assert.ok(classes.has('autoscrolling'));
  assert.equal(dots.length, 1);
  fire('mousemove', { clientX: 200, clientY: 100 });
  frames.shift()();
  assert.ok(box.scrollLeft > 0);
  fire('mousemove', { clientX: 0, clientY: 100 });
  const before = box.scrollLeft;
  frames.shift()();
  assert.ok(box.scrollLeft < before);
  fire('mousedown', { button: 0, target, clientX: 0, clientY: 0, preventDefault() {} });
  assert.equal(dots.length, 0);
  assert.ok(!classes.has('autoscrolling'));
});

test('other buttons and non-chart targets do nothing; Escape stops', () => {
  const { target, fire, dots } = setup();
  fire('mousedown', { button: 0, target, clientX: 1, clientY: 1, preventDefault() {} });
  fire('mousedown', { button: 1, target: { closest: () => null }, clientX: 1, clientY: 1, preventDefault() {} });
  assert.equal(dots.length, 0);
  fire('mousedown', { button: 1, target, clientX: 1, clientY: 1, preventDefault() {} });
  assert.equal(dots.length, 1);
  fire('keydown', { key: 'Escape' });
  assert.equal(dots.length, 0);
});
