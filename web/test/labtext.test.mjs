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
