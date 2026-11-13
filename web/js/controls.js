// Filter form shared by the route, stop and analytics views.
import { DEFAULT_CONTROLS, DEFAULT_DAYS, validateControls, hoursToWindow, windowToHours } from './lib.js';
import { h } from './dom.js';
import { timeRangeSlider, HOUR_PRESETS } from './slider.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let uid = 0;

export function controlsForm(initial, onApply) {
  const id = `ctl${++uid}`;
  const f = (name) => `${id}-${name}`;
  // Compact number input: short visible label (optional), the input, a unit suffix. aria-label carries the full name.
  const mk = (name, aria, value, attrs) => h('input', { id: f(name), type: 'number', step: 'any', value: String(value), inputmode: 'decimal', 'aria-label': aria, ...attrs });
  const fld = (label, id, ...rest) => h('span', { class: 'fld' }, label && h('label', { for: f(id) }, label), ...rest);
  const unit = (t) => h('span', { class: 'unit', 'aria-hidden': 'true' }, t);
  const parts = {
    ci: { input: mk('ci', 'Confidence level (%)', initial.ci, { min: '0.1', max: '99.99', 'aria-describedby': f('ci-hint') }) },
    pLo: { input: mk('plo', 'Percentile band from (%)', initial.pLo, { min: '0', max: '100' }) },
    pHi: { input: mk('phi', 'Percentile band to (%)', initial.pHi, { min: '0', max: '100' }) },
    winLo: { input: mk('wlo', 'On-time window from (min; negative = early)', initial.winLo) },
    winHi: { input: mk('whi', 'On-time window to (min; positive = late)', initial.winHi) },
  };
  const since = h('input', { id: f('since'), type: 'date', value: initial.since, 'aria-label': 'From (UTC date)' });
  const until = h('input', { id: f('until'), type: 'date', value: initial.until, 'aria-label': 'To (UTC date)' });
  // Whole-hour window. An on-hour start==end window would read as 23 hours, so collapse it to that single hour.
  const collapse = () => {
    const [a, b] = slider.get();
    if (a === b) slider.set(a, Math.min(a + 60, 1439));
  };
  const [w0, w1] = hoursToWindow(initial.hFrom, initial.hTo);
  const slider = timeRangeSlider({ step: 60, presets: HOUR_PRESETS, from: w0, to: w1, onChange: collapse });
  const days = DAYS.map((d, i) => {
    const btn = h('button', { type: 'button', class: 'day-btn', 'aria-pressed': String(initial.weekdays.includes(i)),
      on: { click: () => btn.setAttribute('aria-pressed', String(btn.getAttribute('aria-pressed') !== 'true')) } }, d);
    return { i, btn };
  });
  const confirmed = h('input', { type: 'checkbox', id: f('conf'), checked: initial.confirmed, 'aria-label': 'Confirmed arrivals only (confirmed by a due-now reading)' });
  slider.el.removeAttribute('role'); slider.el.removeAttribute('aria-label'); // the labelled hours row is the group
  const errors = h('div', { class: 'error', role: 'alert', hidden: true });

  function read() {
    const n = (i) => (i.value === '' ? NaN : Number(i.value));
    return { ci: n(parts.ci.input), pLo: n(parts.pLo.input), pHi: n(parts.pHi.input), winLo: n(parts.winLo.input), winHi: n(parts.winHi.input),
      since: since.value, until: until.value, weekdays: days.filter((d) => d.btn.getAttribute('aria-pressed') === 'true').map((d) => d.i),
      ...windowToHours(...slider.get()), confirmed: !!confirmed.checked };
  }
  function apply(c) {
    const errs = validateControls({ ...c, hFrom: c.hFrom === '' ? '' : Number(c.hFrom) });
    if (errs.length) {
      errors.replaceChildren(h('ul', {}, errs.map((e) => h('li', { text: e }))));
      errors.removeAttribute('hidden');
      return false;
    }
    errors.setAttribute('hidden', '');
    onApply({ ...c, hFrom: c.hFrom === '' ? '' : Number(c.hFrom) });
    return true;
  }
  const form = h('form', { class: 'controls card', novalidate: true, 'aria-label': 'Filters', on: {
    submit: (ev) => { ev.preventDefault(); apply(read()); },
  } },
  h('fieldset', { class: 'frow' }, h('legend', { class: 'sr-only' }, 'Period'),
    fld('From (UTC)', 'since', since), fld('To (UTC)', 'until', until),
    h('span', { class: 'hint' }, `Blank start = last ${DEFAULT_DAYS} days`)),
  h('div', { class: 'frow', role: 'group', 'aria-label': 'Hours of day', 'aria-describedby': f('hours-hint'), title: 'Times are Dublin local time. Whole hours; drag the start past the end to wrap midnight' },
    h('span', { class: 'row-label' }, 'Hours'), slider.el,
    h('span', { id: f('hours-hint'), class: 'sr-only' }, 'Times are Dublin local time. Whole hours; drag the start past the end to wrap midnight'),
    h('span', { class: 'day-btns', role: 'group', 'aria-label': 'Days of week', 'aria-describedby': f('days-hint'), title: 'None selected = every day' }, days.map((d) => d.btn)),
    h('span', { id: f('days-hint'), class: 'sr-only' }, 'None selected = every day')),
  h('fieldset', { class: 'frow' }, h('legend', { class: 'sr-only' }, 'Statistics'),
    fld('Confidence', 'ci', parts.ci.input, unit('%')),
    h('span', { id: f('ci-hint'), class: 'sr-only' }, 'Confidence level of the interval around the mean delay, e.g. 90, 95 or 99'),
    h('span', { class: 'fld', title: 'Percentile band of arrivals shown in the distribution charts' }, h('label', { for: f('plo') }, 'Percentile band'),
      parts.pLo.input, unit('–'), parts.pHi.input, unit('%')),
    h('span', { class: 'fld', title: 'An arrival counts as on time between these minutes: negative = early, positive = late' }, h('label', { for: f('wlo') }, 'On-time window'),
      parts.winLo.input, unit('…'), parts.winHi.input, unit('min')),
    h('label', { class: 'inline', for: f('conf'), title: 'Only arrivals confirmed by a due-now reading' }, confirmed, 'Confirmed arrivals only'),
    h('span', { class: 'actions' }, h('button', { class: 'btn', type: 'submit' }, 'Apply'),
      h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => {
        slider.set(0, 1439);
        days.forEach((d) => d.btn.setAttribute('aria-pressed', 'false'));
        apply({ ...DEFAULT_CONTROLS, weekdays: [] });
      } } }, 'Reset'))),
  errors);
  return form;
}
