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
  assert.ok(form.all((n) => /None selected = every day/.test(n.getAttribute('title') || '')).length >= 1, 'every-day rule kept as a tooltip');
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

const attrs = (n, k) => n.getAttribute(k) || '';
const inputs = (form) => form.byTag('input');
const byName = (form, name) => inputs(form).find((i) => attrs(i, 'aria-label') === name);
const norm = (t) => t.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

test('controls: round trip of every field through read/submit', () => {
  const init = { ci: 99, pLo: 2.5, pHi: 97.5, winLo: -2, winHi: 7, since: '2026-01-02', until: '2026-02-03', weekdays: [1, 4, 6], hFrom: 7, hTo: 9, confirmed: true };
  const { submit, applied } = build(init);
  submit();
  assert.deepEqual(applied[0], { ...DEFAULT_CONTROLS, ...init });
});

test('controls: three rows (period, hours, statistics) with no bordered fieldset sections', () => {
  const { form } = build();
  const rows = form.all((n) => (attrs(n, 'class')).split(' ').includes('frow'));
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map((r) => r.byTag('legend').map((l) => l.textContent)[0] || attrs(r, 'aria-label')), ['Period', 'Hours', 'Statistics']);
  const [dates, hours] = rows;
  assert.equal(dates.byTag('button').length, 0, 'dates row holds no day chips');
  assert.equal(hours.all((n) => attrs(n, 'aria-label') === 'Days of week')[0].byTag('button').length, 7, 'days group lives in the hours row');
  const tr = hours.all((n) => attrs(n, 'class').split(' ').includes('time-range'))[0];
  const cls = tr.children.map((c) => attrs(c, 'class'));
  assert.ok(cls.indexOf('tr-presets') > cls.indexOf('tr-track') && cls.includes('tr-presets'), 'presets follow the track');
  assert.ok(!/Dublin/.test(hours.all((n) => attrs(n, 'class') === 'row-label')[0].textContent));
  assert.equal(form.byTag('fieldset').length, 2);
  assert.equal(form.byTag('legend').length, 2);
  assert.ok(form.byTag('legend').every((l) => attrs(l, 'class') === 'sr-only'));
  assert.equal(form.byText('button', /^Apply$/).length, 1);
  assert.equal(form.byText('button', /^Apply$/)[0].getAttribute('type'), 'submit');
});

test('controls: inputs have the documented accessible names, containing any visible label text', () => {
  const { form } = build();
  for (const n of ['Confidence level (%)', 'Percentile band from (%)', 'Percentile band to (%)', 'On-time window from (min; negative = early)',
    'On-time window to (min; positive = late)', 'From (UTC date)', 'To (UTC date)']) assert.ok(byName(form, n), n);
  assert.ok(inputs(form).filter((i) => i.getAttribute('type') === 'checkbox').every((i) => /^Confirmed arrivals only/.test(attrs(i, 'aria-label'))));
  const ids = inputs(form).map((i) => attrs(i, 'id')).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length);
  for (const i of inputs(form).filter((x) => x.getAttribute('type') !== 'range')) {
    assert.ok(attrs(i, 'aria-label'), 'every input is named');
    for (const l of form.byTag('label').filter((l) => attrs(l, 'for') === attrs(i, 'id') || l.children.includes(i))) {
      assert.ok(norm(attrs(i, 'aria-label')).includes(norm(l.textContent)), `${attrs(i, 'aria-label')} contains ${l.textContent}`);
    }
  }
  const hint = form.all((n) => attrs(n, 'id') === attrs(byName(form, 'Confidence level (%)'), 'aria-describedby'));
  assert.equal(hint.length, 1);
});

test('controls: Apply payload equals the edited values; Reset applies defaults', () => {
  const { form, submit, applied } = build({ weekdays: [0], hFrom: 7, hTo: 9 });
  byName(form, 'Confidence level (%)').value = '90';
  byName(form, 'Percentile band from (%)').value = '10';
  byName(form, 'On-time window to (min; positive = late)').value = '8';
  submit();
  assert.deepEqual(applied[0], { ...DEFAULT_CONTROLS, ci: 90, pLo: 10, winHi: 8, weekdays: [0], hFrom: 7, hTo: 9 });
  form.byText('button', /^Reset$/)[0].dispatch('click');
  assert.deepEqual(applied[1], { ...DEFAULT_CONTROLS, weekdays: [] });
});

test('controls: validation errors show in the alert box and block apply', () => {
  const { form, submit, applied } = build();
  const alert = form.all((n) => attrs(n, 'role') === 'alert')[0];
  assert.ok(alert.hasAttribute('hidden'));
  byName(form, 'Confidence level (%)').value = '150';
  submit();
  assert.equal(applied.length, 0);
  assert.equal(alert.hasAttribute('hidden'), false);
  assert.match(alert.textContent, /Confidence level must be/);
});

test('controls: days group is named and described; hours group is named once and described', () => {
  const { form, dayBtns } = build();
  const byId = (id) => form.all((n) => attrs(n, 'id') === id);
  const days = form.all((n) => attrs(n, 'aria-label') === 'Days of week');
  assert.equal(days.length, 1);
  assert.equal(attrs(days[0], 'role'), 'group');
  assert.equal(days[0].byTag('button').length, 7);
  assert.equal(dayBtns().length, 7);
  const hours = form.all((n) => /Hours/.test(attrs(n, 'aria-label')));
  assert.deepEqual(hours.map((n) => attrs(n, 'aria-label')), ['Hours of day']);
  for (const [g, re] of [[days[0], /None selected = every day/], [hours[0], /Times are Dublin local time\. Whole hours; drag the start past the end to wrap midnight/]]) {
    const t = byId(attrs(g, 'aria-describedby'));
    assert.equal(t.length, 1);
    assert.match(t[0].textContent, re);
  }
});
