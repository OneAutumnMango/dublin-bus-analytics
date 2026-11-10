// Filter form shared by the route, stop and analytics views.
import { DEFAULT_CONTROLS, DEFAULT_DAYS, validateControls } from './lib.js';
import { h } from './dom.js';

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
  const hour = (name, label, v) => {
    const sel = h('select', { id: f(name) }, h('option', { value: '' }, 'any'),
      Array.from({ length: 24 }, (_, i) => h('option', { value: String(i), selected: String(v) === String(i) }, String(i).padStart(2, '0'))));
    return { sel, node: h('label', { for: f(name) }, label, sel) };
  };
  const hf = hour('hf', 'From hour (Dublin time)', initial.hFrom);
  const ht = hour('ht', 'To hour (Dublin time, inclusive)', initial.hTo);
  const days = DAYS.map((d, i) => ({ i, box: h('input', { type: 'checkbox', id: f(`wd${i}`), checked: initial.weekdays.includes(i) }) }));
  const confirmed = h('input', { type: 'checkbox', id: f('conf'), checked: initial.confirmed });
  const errors = h('div', { class: 'error', role: 'alert', hidden: true });

  function read() {
    const n = (i) => (i.value === '' ? NaN : Number(i.value));
    return { ci: n(parts.ci.input), pLo: n(parts.pLo.input), pHi: n(parts.pHi.input), winLo: n(parts.winLo.input), winHi: n(parts.winHi.input),
      since: since.value, until: until.value, weekdays: days.filter((d) => d.box.checked).map((d) => d.i),
      hFrom: hf.sel.value, hTo: ht.sel.value === '' ? '' : Number(ht.sel.value), confirmed: !!confirmed.checked };
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
    h('fieldset', {}, h('legend', {}, 'Weekdays (none = all)'), h('div', { class: 'fields' },
      days.map((d) => h('label', { class: 'inline', for: f(`wd${d.i}`) }, d.box, DAYS[d.i])))),
    h('fieldset', {}, h('legend', {}, 'Hours of day'), h('div', { class: 'fields' }, hf.node, ht.node),
      h('span', { class: 'hint' }, 'e.g. 22 to 01 wraps midnight')),
    h('fieldset', {}, h('legend', {}, 'Data quality'), h('label', { class: 'inline', for: f('conf') }, confirmed, 'Only arrivals confirmed by a due-now reading'))),
  errors,
  h('div', { class: 'row' }, h('button', { class: 'btn', type: 'submit' }, 'Apply filters'),
    h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => apply({ ...DEFAULT_CONTROLS, weekdays: [] }) } }, 'Reset')));
  return form;
}
