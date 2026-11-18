import test from 'node:test';
import assert from 'node:assert/strict';
import * as T from '../js/tipdata.js';

const keys = (p) => p.rows.map((r) => r.k);
const noBad = (p) => assert.ok(!/NaN|undefined|Infinity/.test(JSON.stringify(p)), JSON.stringify(p));

test('rangeTipData: full stat set, headline mean, omits missing stats', () => {
  const p = T.rangeTipData({ label: 'Heuston (2051)', n: 40, low_n: false, mean: 90, min: -60, max: 400, lo: -30, hi: 300 },
    { median: 80, std: 55, ci_low: 60, ci_high: 120, on_time_pct: 71.2 });
  assert.equal(p.title, 'Heuston (2051)');
  assert.equal(p.head.sec, 90);
  assert.deepEqual(keys(p), ['median', 'min', 'max', 'band', 'ci', 'std', 'ontime', 'n']);
  assert.deepEqual(p.range, { min: -60, max: 400, lo: -30, hi: 300, mean: 90 });
  noBad(p);
  const sparse = T.rangeTipData({ label: 'x', n: 0, mean: null, min: null, max: null, lo: null, hi: null }, {});
  assert.equal(sparse.head, null);
  assert.deepEqual(keys(sparse), ['n']);
  assert.equal(sparse.range, undefined);
  noBad(sparse);
  assert.match(T.rangeTipData({ label: 'x', n: 3, low_n: true, mean: 1 }, {}).foot, /fewer than 10/i);
});

test('accTipData / heatTipData / forecastTipData / histTipData', () => {
  const a = T.accTipData({ stop_id: 's', mean_delay: 120, segment_delta: -30, n: 20, n_delta: 18 }, 'Heuston (2051)', 4);
  assert.equal(a.title, 'Heuston (2051)');
  assert.equal(a.head.sec, 120);
  assert.deepEqual(keys(a), ['delta', 'n']);
  noBad(a);
  const h = T.heatTipData({ wd: 0, hour: 8, mean: 300, n: 12, lowN: false });
  assert.equal(h.title, 'Mon 08:00');
  assert.equal(h.head.sec, 300);
  assert.deepEqual(T.heatTipData({ wd: 6, hour: 23, mean: null, n: 0, lowN: true }).head, null);
  const f = T.forecastTipData({ horizon_min: 10, n: 50, low_n: false, mean_error: 60, mae: 90, p_low: -100, p_high: 200 });
  assert.equal(f.title, '10 min before');
  assert.deepEqual(keys(f), ['mae', 'band', 'n']);
  noBad(f);
  const b = T.histTipData({ lo: 0, hi: 60, count: 25, label: '0 to 1 min' }, 100);
  assert.equal(b.title, '0 to 1 min');
  assert.deepEqual(b.rows.map((r) => r.k), ['n', 'share']);
  assert.equal(b.rows[1].v, 25);
  assert.equal(T.histTipData({ count: 0, label: 'x' }, 0).rows[1], undefined);
});

test('fmtTipValue formats by kind and units', () => {
  assert.equal(T.fmtTipValue({ fmt: 'delay', v: 90 }, 'min'), '+1.5 min');
  assert.equal(T.fmtTipValue({ fmt: 'delay', v: 90 }, 'sec'), '+90 s');
  assert.equal(T.fmtTipValue({ fmt: 'delay', v: -30 }, 'sec'), '−30 s');
  assert.equal(T.fmtTipValue({ fmt: 'range', v: [-60, 120] }, 'min'), '−1.0 min to +2.0 min');
  assert.equal(T.fmtTipValue({ fmt: 'pct', v: 71.234 }, 'min'), '71.2%');
  assert.equal(T.fmtTipValue({ fmt: 'count', v: 1234 }, 'min'), '1,234');
  assert.equal(T.fmtTipValue({ fmt: 'delay', v: null }, 'min'), '–');
});

test('sampleTipData: shown, expected, versus timetable, movement, source', async () => {
  const { sampleTipData } = await import('../js/tipdata.js');
  const clock = (t) => `t${t}`;
  const t = sampleTipData({ ts: 100, due_min: 9, expected: 700, realtime: 1 }, 1, 3, { expected: 640 }, 600, 10, clock);
  assert.equal(t.title, 't100 reading');
  assert.equal(t.sub, 'Stored change 2 of 3');
  assert.deepEqual(t.head, { sec: 100, label: 'predicted vs timetable' });
  const r = Object.fromEntries(t.rows.map((x) => [x.k, x]));
  assert.equal(r.shown.v, '10 min');
  assert.equal(r.expected.v, 't700');
  assert.equal(r.vsched.v, 100);
  assert.equal(r.delta.v, 60);
  assert.equal(r.source.v, 'live prediction');
  const first = sampleTipData({ ts: 1, due_min: 5, expected: null, realtime: 0 }, 0, 1, null, 600, 5, clock);
  assert.equal(first.head, undefined);
  assert.ok(!first.rows.some((x) => x.k === 'delta' || x.k === 'vsched'));
  assert.equal(first.rows.find((x) => x.k === 'source').v, 'timetable only');
});
