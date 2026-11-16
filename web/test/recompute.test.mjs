import test from 'node:test';
import assert from 'node:assert/strict';
import { createRecomputer, estimateMs, needsRebuild, progressFraction } from '../js/recompute.js';

test('estimateMs sums, null on any null or empty', () => {
  assert.equal(estimateMs([{ path: 'a', ms: 100 }, { path: 'b', ms: 250 }]), 350);
  assert.equal(estimateMs([{ path: 'a', ms: 100 }, { path: 'b', ms: null }]), null);
  assert.equal(estimateMs([]), null);
});

test('progressFraction caps at 0.95, floors small estimates, null when unknown', () => {
  assert.equal(progressFraction(500, 1000), 0.5);
  assert.equal(progressFraction(5000, 1000), 0.95);
  assert.equal(progressFraction(250, 0), 0.5);
  assert.equal(progressFraction(100, 100), 0.2);
  assert.equal(progressFraction(10, null), null);
  let last = -1;
  for (let t = 0; t < 3000; t += 100) { const p = progressFraction(t, 2000); assert.ok(p >= last && p <= 0.95); last = p; }
});

function setup(paths = [{ path: '/a', ms: 1000 }, { path: '/b', ms: 1000 }], failing = new Set()) {
  let t = 0;
  const calls = [], pend = [], states = [];
  const run = (p, signal) => new Promise((res, rej) => {
    calls.push({ p, signal });
    pend.push({ p, res, rej: () => rej(new Error('x')) });
    signal.addEventListener('abort', () => rej(new Error('abort')));
  });
  const r = createRecomputer({ paths: () => paths, run, now: () => t, onState: (s) => states.push(s) });
  const settle = async () => { for (const x of pend.splice(0)) (failing.has(x.p) ? x.rej : x.res)(); await new Promise((r2) => setTimeout(r2, 0)); };
  return { r, calls, states, pend, settle, at: (v) => { t = v; } };
}

test('idle -> running -> done with progress 1; start while running ignored; tick after done is inert', async () => {
  const { r, calls, states, settle, at } = setup();
  r.start(); r.start();
  assert.equal(calls.length, 2);
  assert.equal(r.state().phase, 'running');
  at(1000);
  r.tick();
  assert.equal(r.state().progress, 0.5); // estimate 2000, elapsed 1000
  await settle();
  assert.deepEqual([r.state().phase, r.state().progress], ['done', 1]);
  const n = states.length;
  at(9000); r.tick();
  assert.equal(states.length, n);
});

test('one failure -> failed with count; start again re-runs', async () => {
  const { r, calls, settle } = setup(undefined, new Set(['/a']));
  r.start(); await settle();
  assert.equal(r.state().phase, 'failed'); assert.equal(r.state().failed, 1);
  r.start();
  assert.equal(calls.length, 4);
  assert.equal(r.state().phase, 'running');
});

test('cancel aborts, returns to idle without failed; empty paths stay idle', async () => {
  const { r, calls, states, settle } = setup();
  r.start(); r.cancel();
  assert.ok(calls.every((c) => c.signal.aborted));
  await settle();
  assert.equal(r.state().phase, 'idle');
  assert.ok(!states.some((s) => s.phase === 'failed'));
  const e = setup([]);
  e.r.start();
  assert.equal(e.r.state().phase, 'idle'); assert.equal(e.calls.length, 0);
});

test('needsRebuild: only on phase or determinacy change', () => {
  const r = (phase, progress) => ({ phase, progress });
  assert.equal(needsRebuild(null, r('idle', null)), true);
  assert.equal(needsRebuild(r('running', 0.1), r('running', 0.2)), false);
  assert.equal(needsRebuild(r('running', null), r('running', null)), false);
  assert.equal(needsRebuild(r('running', null), r('running', 0.2)), true);
  assert.equal(needsRebuild(r('running', 0.2), r('done', 1)), true);
});

test('a throwing onState does not stop the recompute', async () => {
  const orig = console.error; console.error = () => {};
  try {
    let calls = 0;
    const r = createRecomputer({ paths: () => [{ path: '/a', ms: 1 }], run: async () => { calls++; }, now: () => 0, onState: () => { throw new Error('boom'); } });
    r.start();
    await new Promise((x) => setTimeout(x, 0));
    assert.equal(calls, 1);
    assert.equal(r.state().phase, 'done');
  } finally { console.error = orig; }
});

test('old runs settling after cancel+start do not touch the new run', async () => {
  const { r, calls, settle } = setup(undefined, new Set(['/a']));
  r.start();
  const old = calls.length;
  r.cancel();
  r.start();
  await new Promise((x) => setTimeout(x, 0)); // the aborted old runs reject now
  assert.equal(r.state().phase, 'running');
  await settle(); // settles old (aborted, rejected) and new runs; /a fails in the new run only
  assert.equal(calls.length, old * 2);
  assert.equal(r.state().phase, 'failed');
  assert.equal(r.state().failed, 1); // not 2: the aborted old /a is ignored
});

test('cancel after a failure returns to idle; reset from done and failed returns to idle', async () => {
  const a = setup([{ path: '/a', ms: 1 }, { path: '/b', ms: 1 }], new Set(['/a']));
  a.r.start();
  a.pend[0].rej(); // /a already failed, /b still in flight
  await new Promise((x) => setTimeout(x, 0));
  a.r.cancel();
  await a.settle();
  assert.equal(a.r.state().phase, 'idle');
  assert.ok(!a.states.some((s) => s.phase === 'failed'));
  const d = setup(); d.r.start(); await d.settle(); d.r.reset();
  assert.equal(d.r.state().phase, 'idle');
  const f = setup(undefined, new Set(['/a'])); f.r.start(); await f.settle(); assert.equal(f.r.state().phase, 'failed'); f.r.reset();
  assert.equal(f.r.state().phase, 'idle');
});
