import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from './fakedom.mjs';

installDom();
const { suggest, applyChoice, routePicker } = await import('../js/routepick.js');
const { h } = await import('../js/dom.js');

test('suggest: prefix of the route being typed, minus the ones already chosen, case-insensitive', () => {
  const t = ['15', '46A', '4', '40', 'N4'];
  assert.deepEqual(suggest(t, ''), t);
  assert.deepEqual(suggest(t, '4'), ['46A', '4', '40']);
  assert.deepEqual(suggest(t, '4, '), ['15', '46A', '40', 'N4']);
  assert.deepEqual(suggest(t, '15, 4'), ['46A', '4', '40']);
  assert.deepEqual(suggest(t, 'n'), ['N4']);
  assert.deepEqual(suggest(t, 'zzz'), []);
  assert.deepEqual(suggest([], ''), []);
});

test('applyChoice: replaces the route being typed and leaves a separator', () => {
  assert.equal(applyChoice('', '15'), '15, ');
  assert.equal(applyChoice('4', '46A'), '46A, ');
  assert.equal(applyChoice('15, 4', '46A'), '15, 46A, ');
  assert.equal(applyChoice('15, ', '4'), '15, 4, ');
});

function setup(routes = ['15', '46A', '4']) {
  const input = h('input', { type: 'text', value: '' });
  const p = routePicker(input);
  p.setTracked(routes);
  const list = p.el.children[1];
  const fire = (t, e = {}) => input.dispatch(t, { preventDefault() {}, ...e });
  return { input, list, fire, p };
}

test('picker: opens on focus with the tracked routes, a click picks one, the box keeps typing', () => {
  const { input, list, fire } = setup();
  assert.ok(list.hasAttribute('hidden'));
  fire('focus');
  assert.ok(!list.hasAttribute('hidden'));
  assert.equal(input.getAttribute('aria-expanded'), 'true');
  assert.deepEqual(list.children.map((li) => li.textContent), ['Route 15', 'Route 46A', 'Route 4']);
  list.children[1].dispatch('mousedown', { preventDefault() {} });
  assert.equal(input.value, '46A, ');
  assert.deepEqual(list.children.map((li) => li.textContent), ['Route 15', 'Route 4']); // the picked one is gone
  list.children[0].dispatch('mousedown', { preventDefault() {} });
  assert.equal(input.value, '46A, 15, ');
});

test('picker: typing filters; no match hides; Escape and blur hide', () => {
  const { input, list, fire } = setup();
  input.value = '4'; fire('input');
  assert.deepEqual(list.children.map((li) => li.textContent), ['Route 46A', 'Route 4']);
  input.value = 'x'; fire('input');
  assert.ok(list.hasAttribute('hidden'));
  input.value = ''; fire('input');
  fire('keydown', { key: 'Escape' });
  assert.ok(list.hasAttribute('hidden'));
  fire('focus'); fire('blur');
  assert.ok(list.hasAttribute('hidden'));
});

test('picker: arrows move, Enter picks the highlighted route, Enter with none highlighted is left to the form', () => {
  const { input, list, fire } = setup();
  fire('focus');
  let prevented = 0;
  fire('keydown', { key: 'Enter', preventDefault: () => prevented++ });
  assert.equal(prevented, 0);
  assert.equal(input.value, '');
  fire('keydown', { key: 'ArrowDown' });
  fire('keydown', { key: 'ArrowDown' });
  assert.deepEqual(list.children.map((li) => li.getAttribute('aria-selected')), ['false', 'true', 'false']);
  fire('keydown', { key: 'ArrowUp' });
  fire('keydown', { key: 'ArrowUp' });
  assert.equal(list.children[2].getAttribute('aria-selected'), 'true'); // wraps round
  fire('keydown', { key: 'Enter', preventDefault: () => prevented++ });
  assert.equal(prevented, 1);
  assert.equal(input.value, '4, ');
});

test('picker: no tracked routes means no dropdown, typing still works', () => {
  const { input, list, fire } = setup([]);
  fire('focus');
  assert.ok(list.hasAttribute('hidden'));
  input.value = '15'; fire('input');
  assert.ok(list.hasAttribute('hidden'));
});
