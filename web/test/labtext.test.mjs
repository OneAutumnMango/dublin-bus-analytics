import test from 'node:test';
import assert from 'node:assert/strict';
import { EXPLAIN, readDistribution, readPersistence, readVariance } from '../js/labtext.js';

const T = (v) => ({ value: v, ci_low: null, ci_high: null });
const bad = /null|undefined|NaN/;

test('every EXPLAIN entry has title and paragraphs', () => {
  const keys = ['ecdf', 'qq', 'shape', 'buffer', 'persistence', 'recovery', 'variance', 'ci'];
  assert.deepEqual(Object.keys(EXPLAIN).sort(), [...keys].sort());
  for (const k of keys) {
    assert.ok(EXPLAIN[k].title);
    assert.ok(EXPLAIN[k].paragraphs.length >= 1 && EXPLAIN[k].paragraphs.every((p) => p));
  }
  assert.match(EXPLAIN.variance.paragraphs.join(' '), /100%/);
  assert.match(EXPLAIN.ci.paragraphs.join(' '), /days/);
  assert.match(EXPLAIN.ci.paragraphs.join(' '), /not enough days yet/);
});

test('readDistribution', () => {
  assert.equal(readDistribution({ n: 0 }), 'No data for this selection.');
  const s = readDistribution({ n: 50, tiles: { median: T(90), p95: T(540), skew: T(null) } });
  assert.match(s, /1\.5/);
  assert.match(s, /9\.0/);
  assert.doesNotMatch(s, bad);
  assert.doesNotMatch(readDistribution({ n: 5, tiles: {} }), bad);
});

test('readPersistence', () => {
  const mk = (slope) => ({ n_trips: 20, lags: [], recovery: { slope, n: 20 } });
  assert.match(readPersistence(mk(0.9)), /persist/);
  assert.match(readPersistence(mk(0.2)), /recover/);
  assert.match(readPersistence(mk(0.5)), /partly recovers/);
  assert.equal(readPersistence({ n_trips: 0, lags: [], recovery: null }), 'No data for this selection.');
  assert.doesNotMatch(readPersistence({ n_trips: 9, lags: [{ lag: 1, r: null }], recovery: null }), bad);
});

test('readVariance', () => {
  const f = (factor, omega2) => ({ factor, omega2, levels: 3 });
  const s = readVariance({ n: 100, factors: [f('hour', 0.05), f('stop', 0.3), f('date', null)] });
  assert.match(s, /stop/);
  assert.match(s, /30%/);
  assert.doesNotMatch(s, bad);
  assert.equal(readVariance({ n: 0, factors: [] }), 'No data for this selection.');
  assert.doesNotMatch(readVariance({ n: 5, factors: [f('hour', null)] }), bad);
});

test('buffer explanation is relative to the median', () => {
  const p = EXPLAIN.buffer.paragraphs.join(' ');
  assert.match(p, /beyond the median delay/);
  assert.doesNotMatch(p, /typical journey/);
});

test('readDistribution handles early and near-zero values naturally', () => {
  const early = readDistribution({ n: 50, tiles: { median: T(-120), p95: T(-60), buffer: T(60) } });
  assert.match(early, /1 in 20 buses are less than 1\.0 min early/);
  assert.doesNotMatch(early, /more than|allow.*late/);
  assert.match(early, /allow 1\.0 min beyond the median delay/);
  const zero = readDistribution({ n: 50, tiles: { median: T(0), p95: T(0.4), buffer: T(0.2) } });
  assert.doesNotMatch(zero, /more than on time|allow on time/);
  assert.match(zero, /allow 0 s beyond the median delay/);
  const late = readDistribution({ n: 50, tiles: { p95: T(540), buffer: T(450) } });
  assert.match(late, /1 in 20 buses are later than 9\.0 min/);
  assert.match(late, /allow 7\.5 min beyond/);
  assert.doesNotMatch(late, /7\.5 min late/);
});

test('readDistribution gives direction words for skew and kurtosis', () => {
  const r = (skew, kurtosis) => readDistribution({ n: 50, tiles: { skew: T(skew), kurtosis: T(kurtosis) } });
  assert.match(r(1.2, 3), /long tail of late buses/);
  assert.match(r(1.2, 3), /heavier tails than normal/);
  assert.match(r(-1.2, -1), /long tail of early buses/);
  assert.match(r(-1.2, -1), /lighter tails than normal/);
  assert.match(r(0.01, 0.01), /symmetric/);
  assert.match(r(0.01, 0.01), /normal-like tails/);
});

test('readDistribution cautions on low_n and missing intervals', () => {
  const base = { n: 50, tiles: { median: T(90) } };
  assert.match(readDistribution({ ...base, low_n: true }), /Based on few observations; treat as rough\./);
  assert.doesNotMatch(readDistribution({ ...base, low_n: false, enough_days: true }), /few observations|not shown/);
  assert.match(readDistribution({ ...base, enough_days: false }), /intervals are not shown yet/);
  assert.doesNotMatch(readDistribution({ n: 0, low_n: true, enough_days: false }), /few observations|not shown/);
});

test('readDistribution tolerates missing tiles', () => {
  assert.doesNotMatch(readDistribution({ n: 5 }), bad);
});

test('readPersistence slope boundaries and growth', () => {
  const mk = (slope) => ({ n_trips: 20, lags: [], recovery: { slope, n: 20 } });
  assert.match(readPersistence(mk(0.7)), /persists/);
  assert.match(readPersistence(mk(0.3)), /recovers/);
  assert.doesNotMatch(readPersistence(mk(0.3)), /partly/);
  const g = readPersistence(mk(1.4));
  assert.match(g, /grows/);
  assert.doesNotMatch(g, /persists|recovers/);
  assert.match(EXPLAIN.recovery.paragraphs.join(' '), /above 1/);
});

test('readPersistence tolerates omitted recovery and flags caution', () => {
  const s = readPersistence({ n_trips: 9, lags: [{ lag: 1, r: 0.4 }] });
  assert.doesNotMatch(s, bad);
  assert.match(s, /0\.40/);
  assert.match(readPersistence({ n_trips: 9, lags: [], recovery: { slope: 0.5 }, low_n: true }), /few observations/);
  assert.match(readPersistence({ n_trips: 9, lags: [], recovery: { slope: 0.5 }, enough_days: false }), /not shown yet/);
});

test('readVariance ignores non-positive omega2', () => {
  const f = (factor, omega2) => ({ factor, omega2, levels: 3 });
  const s = readVariance({ n: 100, factors: [f('hour', -0.02), f('stop', 0.1), f('date', 0)] });
  assert.match(s, /stop/);
  assert.match(s, /10%/);
  const none = readVariance({ n: 100, factors: [f('hour', -0.02), f('stop', 0)] });
  assert.match(none, /no factor explains a meaningful share/i);
  assert.doesNotMatch(none, bad);
  assert.match(readVariance({ n: 100, low_n: true, factors: [f('stop', 0.1)] }), /few observations/);
  assert.match(readVariance({ n: 100, enough_days: false, factors: [f('stop', 0.1)] }), /not shown yet/);
});
