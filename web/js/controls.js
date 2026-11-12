// Filter form shared by the route, stop and analytics views.
import { DEFAULT_CONTROLS, DEFAULT_DAYS, validateControls, hoursToWindow, windowToHours } from './lib.js';
import { h } from './dom.js';
import { timeRangeSlider, HOUR_PRESETS } from './slider.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let uid = 0;

export function controlsForm(initial, onApply) {
  const id = `ctl${++uid}`;
  const f = (name) => `${id}-${name}`;
  const mk = (name, label, value, attrs) => {
    const input = h('input', { id: f(name), type: 'number', step: 'any', value: String(value), inputmode: 'decimal', ...attrs });
    return { input, node: h('label', { for: f(name) }, label, input) };
  };
  const parts = {
    ci: mk('ci', 'Confidence level (%)', initial.ci, { min: '0.1', max: '99.99', 'aria-describedby': f('ci-hint') }),
    pLo: mk('plo', 'From percentile (%)', initial.pLo, { min: '0', max: '100' }),
    pHi: mk('phi', 'To percentile (%)', initial.pHi, { min: '0', max: '100' }),
    winLo: mk('wlo', 'From (min; negative = early)', initial.winLo),
    winHi: mk('whi', 'To (min; positive = late)', initial.winHi),
  };
  const since = h('input', { id: f('since'), type: 'date', value: initial.since });
  const until = h('input', { id: f('until'), type: 'date', value: initial.until });
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
  const confirmed = h('input', { type: 'checkbox', id: f('conf'), checked: initial.confirmed });
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
  h('div', { class: 'grid' },
    h('fieldset', {}, h('legend', {}, 'Confidence interval of the mean'), h('div', { class: 'fields' }, parts.ci.node),
      h('span', { id: f('ci-hint'), class: 'hint' }, 'e.g. 90, 95, 99')),
    h('fieldset', {}, h('legend', {}, 'Percentile band of arrivals'), h('div', { class: 'fields' }, parts.pLo.node, parts.pHi.node)),
    h('fieldset', {}, h('legend', {}, 'On-time window'), h('div', { class: 'fields' }, parts.winLo.node, parts.winHi.node)),
    h('fieldset', {}, h('legend', {}, 'Date range (UTC)'), h('div', { class: 'fields' },
      h('label', { for: f('since') }, 'From (UTC date)', since), h('label', { for: f('until') }, 'To (UTC date)', until)),
    h('span', { class: 'hint' }, `Blank start = last ${DEFAULT_DAYS} days`)),
    h('fieldset', { class: 'span-2' }, h('legend', {}, 'Days of week'), h('div', { class: 'day-btns' }, days.map((d) => d.btn)),
      h('span', { class: 'hint' }, 'None selected = every day')),
    h('fieldset', { class: 'span-2' }, h('legend', {}, 'Hours of day (Dublin time)'), slider.el,
      h('span', { class: 'hint' }, 'Whole hours; drag the start past the end to wrap midnight')),
    h('fieldset', {}, h('legend', {}, 'Data quality'), h('label', { class: 'inline', for: f('conf') }, confirmed, 'Only arrivals confirmed by a due-now reading'))),
  errors,
  h('div', { class: 'row' }, h('button', { class: 'btn', type: 'submit' }, 'Apply filters'),
    h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => {
      slider.set(0, 1439);
      days.forEach((d) => d.btn.setAttribute('aria-pressed', 'false'));
      apply({ ...DEFAULT_CONTROLS, weekdays: [] });
    } } }, 'Reset')));
  return form;
}
