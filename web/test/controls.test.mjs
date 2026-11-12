import test from 'node:test';
import assert from 'node:assert/strict';
import { controlsForm } from '../js/controls.js';
import { DEFAULT_CONTROLS } from '../js/lib.js';
import { installDom } from './fakedom.mjs';

const build = (over = {}) => {
  installDom();
  const applied = [];
  const form = controlsForm({ ...DEFAULT_CONTROLS, ...over }, (c) => applied.push(c));
  const dayBtns = () => form.all((n) => n.tag === 'button' && (n.getAttribute('class') || '').includes('day-btn'));
  const pressed = () => dayBtns().filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent);
  return { form, applied, dayBtns, pressed, submit: () => form.dispatch('submit') };
};

test('controls: seven day buttons, pressed state from initial weekdays, no selects', () => {
  const { form, dayBtns, pressed } = build({ weekdays: [0, 2], hFrom: 7, hTo: 9 });
  assert.equal(dayBtns().length, 7);
  assert.ok(dayBtns().every((b) => b.getAttribute('type') === 'button'));
  assert.deepEqual(pressed(), ['Mon', 'Wed']);
  assert.equal(form.byTag('select').length, 0);
  assert.match(form.textContent, /Days of week/);
  assert.match(form.textContent, /None selected = every day/);
});

test('controls: clicking a day toggles it and submit applies weekdays and hours', () => {
  const { dayBtns, pressed, submit, applied } = build({ weekdays: [0, 2], hFrom: 7, hTo: 9 });
  const tue = dayBtns()[1];
  tue.dispatch('click');
  assert.equal(tue.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(pressed(), ['Mon', 'Tue', 'Wed']);
  submit();
  assert.equal(applied.length, 1);
  assert.deepEqual(applied[0].weekdays, [0, 1, 2]);
  assert.equal(applied[0].hFrom, 7);
  assert.equal(applied[0].hTo, 9);
  tue.dispatch('click');
  assert.equal(tue.getAttribute('aria-pressed'), 'false');
});

test('controls: default controls submit blank hours', () => {
  const { submit, applied } = build();
  submit();
  assert.equal(applied[0].hFrom, '');
  assert.equal(applied[0].hTo, '');
  assert.deepEqual(applied[0].weekdays, []);
});

test('controls: reset clears pressed days and applies blanks', () => {
  const { form, pressed, applied } = build({ weekdays: [0, 2], hFrom: 7, hTo: 9 });
  form.byText('button', /^Reset$/)[0].dispatch('click');
  assert.deepEqual(pressed(), []);
  assert.equal(applied.length, 1);
  assert.equal(applied[0].hFrom, '');
  assert.equal(applied[0].hTo, '');
  assert.deepEqual(applied[0].weekdays, []);
  assert.match(form.textContent, /00:00 – 23:59/);
});

test('controls: both handles on the same hour select that single hour, not the whole day', () => {
  const { form, submit, applied } = build();
  const [inA, inB] = form.byTag('input').filter((i) => i.getAttribute('type') === 'range');
  inA.value = '120'; inB.value = '120';
  inA.dispatch('change');
  assert.match(form.textContent, /02:00 – 03:00/);
  submit();
  assert.equal(applied[0].hFrom, 2);
  assert.equal(applied[0].hTo, 2);
});

test('controls: untouched legacy 23-hour link (hf=7, ht=6) submits unchanged', () => {
  const { submit, applied } = build({ hFrom: 7, hTo: 6 });
  submit();
  assert.equal(applied[0].hFrom, 7);
  assert.equal(applied[0].hTo, 6);
});
