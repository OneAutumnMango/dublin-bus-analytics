import test from 'node:test';
import assert from 'node:assert/strict';
import { renderCard, visibleRows } from '../js/tipcard.js';
import { DEFAULTS, validate } from '../js/settings.js';
import { rangeTipData } from '../js/tipdata.js';
import { installDom } from './fakedom.mjs';

const payload = () => rangeTipData({ label: 'Heuston (2051)', n: 40, mean: 90, min: -60, max: 400, lo: -30, hi: 300 }, { median: 80, std: 55, ci_low: 60, ci_high: 120, on_time_pct: 71.2 });
const text = (n) => n.textContent;

test('card shows title, headline, stat grid; honours stat toggles and units', () => {
  const { doc } = installDom();
  const full = renderCard(doc, payload(), DEFAULTS);
  const t = text(full);
  assert.match(t, /Heuston \(2051\)/); assert.match(t, /\+1\.5 min/); assert.match(t, /Median/); assert.match(t, /71\.2%/);
  assert.ok(!/NaN|undefined/.test(t));
  const s = validate({ tip: { units: 'sec', stats: { median: false, std: false } } });
  const sec = text(renderCard(doc, payload(), s));
  assert.match(sec, /\+90 s/); assert.ok(!/Median/.test(sec)); assert.ok(!/Std deviation/.test(sec)); assert.match(sec, /Max/);
  assert.deepEqual(visibleRows(payload(), s).map((r) => r.k), ['min', 'max', 'band', 'ci', 'ontime', 'n']);
});

test('detailed tooltips off -> null; mean off hides the headline; foot for low n', () => {
  const { doc } = installDom();
  assert.equal(renderCard(doc, payload(), validate({ tip: { on: false } })), null);
  assert.ok(!/\+1\.5 min/.test(text(renderCard(doc, payload(), validate({ tip: { stats: { mean: false } } })))));
  const low = rangeTipData({ label: 'x', n: 3, low_n: true, mean: 5 }, {});
  assert.match(text(renderCard(doc, low, DEFAULTS)), /Fewer than 10/);
  assert.ok(!/NaN|undefined/.test(text(renderCard(doc, rangeTipData({ label: 'x', n: 0, mean: null }, {}), DEFAULTS))));
});

test('headline unit label defaults to "mean" and can be overridden by the payload', () => {
  const { doc } = installDom();
  const p = { title: 'S', head: { sec: 60 }, rows: [] };
  assert.match(text(renderCard(doc, p, DEFAULTS)), /mean/);
  assert.match(text(renderCard(doc, { ...p, head: { sec: 60, label: 'change vs previous stop' } }, DEFAULTS)), /change vs previous stop/);
});
