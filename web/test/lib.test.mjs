import test from 'node:test';
import assert from 'node:assert/strict';
import * as L from '../js/lib.js';

test('fmtNum handles null/NaN/inf/-0', () => {
  for (const v of [null, undefined, NaN, Infinity, -Infinity, 'x']) assert.equal(L.fmtNum(v), '–');
  assert.equal(L.fmtNum(1.25), '1.3');
  assert.equal(L.fmtNum(-0.04), '0.0');
  assert.equal(L.fmtNum(2, 0), '2');
});

test('fmtMinutes: seconds below a minute, minutes with one decimal', () => {
  assert.equal(L.fmtMinutes(null), '–');
  assert.equal(L.fmtMinutes(NaN), '–');
  assert.equal(L.fmtMinutes(0), '0 s');
  assert.equal(L.fmtMinutes(-0.2), '0 s');
  assert.equal(L.fmtMinutes(45), '45 s');
  assert.equal(L.fmtMinutes(-45), '-45 s');
  assert.equal(L.fmtMinutes(59.4), '59 s');
  assert.equal(L.fmtMinutes(59.6), '1.0 min');
  assert.equal(L.fmtMinutes(60), '1.0 min');
  assert.equal(L.fmtMinutes(210), '3.5 min');
  assert.equal(L.fmtMinutes(-210), '-3.5 min');
  assert.equal(L.fmtMinutes(210, { signed: true }), '+3.5 min');
  assert.equal(L.fmtMinutes(0, { signed: true }), '0 s');
  assert.equal(L.fmtMinutes(30, { signed: true }), '+30 s');
});

test('fmtMinutes huge values never produce exponent notation', () => {
  for (const v of [1e9, -1e9, 1e300, Number.MAX_VALUE]) {
    const s = L.fmtMinutes(v);
    assert.ok(!/e[+-]?\d/i.test(s), s);
  }
  assert.ok(L.fmtMinutes(1e9).startsWith('>'));
  assert.ok(L.fmtMinutes(-1e9).startsWith('<'));
});

test('fmtDelay is always signed', () => {
  assert.equal(L.fmtDelay(180), '+3.0 min');
  assert.equal(L.fmtDelay(-180), '-3.0 min');
  assert.equal(L.fmtDelay(null), '–');
});

test('describeDelay words', () => {
  assert.equal(L.describeDelay(null), '–');
  assert.equal(L.describeDelay(0), 'on time');
  assert.equal(L.describeDelay(0.4), 'on time');
  assert.equal(L.describeDelay(210), '3.5 min late');
  assert.equal(L.describeDelay(-45), '45 s early');
});

test('describeRange', () => {
  assert.equal(L.describeRange(null, 5), '–');
  assert.equal(L.describeRange(60, 180), '1.0 to 3.0 min late');
  assert.equal(L.describeRange(-180, -60), '1.0 to 3.0 min early');
  assert.equal(L.describeRange(-60, 180), '1.0 min early to 3.0 min late');
  assert.equal(L.describeRange(-30, 30), '30 s early to 30 s late');
  assert.equal(L.describeRange(30, 30), '30 s late');
});

test('fmtPct / fmtCount', () => {
  assert.equal(L.fmtPct(87.54), '87.5%');
  assert.equal(L.fmtPct(null), '–');
  assert.equal(L.fmtCount(1234567), '1,234,567');
  assert.equal(L.fmtCount(0), '0');
  assert.equal(L.fmtCount(null), '–');
  assert.equal(L.fmtCount(-1234), '-1,234');
});

test('dueAt clamps at zero and floors', () => {
  assert.equal(L.dueAt(1000, 1000), 0);
  assert.equal(L.dueAt(1000, 940), 1);
  assert.equal(L.dueAt(1000, 941), 0);
  assert.equal(L.dueAt(1000, 2000), 0);
  assert.equal(L.dueAt(1300, 1000), 5);
});

test('dueSteps: step function between samples, no extrapolation past last_seen', () => {
  const pts = [{ ts: 1000, due_min: 5, expected: 1300, realtime: 1 }, { ts: 1100, due_min: 3, expected: 1300, realtime: 1 }];
  const s = L.dueSteps(pts, 1200);
  assert.equal(s[0].t, 1000);
  assert.equal(s[0].due, 5);
  assert.ok(s.every((p) => p.t <= 1200));
  assert.equal(s[s.length - 1].t, 1200); // closes exactly at last_seen
  assert.ok(s.every((p, i) => i === 0 || p.t > s[i - 1].t));
  // monotone non-increasing inside one prediction (no change of expected)
  assert.deepEqual(s.map((p) => p.due).filter((d, i, a) => i === 0 || d !== a[i - 1]), [5, 4, 3, 2, 1]);
});

test('dueSteps: prediction change creates a jump; clamp at 0; single point; empty; unsorted', () => {
  const pts = [{ ts: 0, expected: 600 }, { ts: 100, expected: 1200 }];
  const s = L.dueSteps(pts, 200);
  const at100 = s.find((p) => p.t === 100);
  assert.equal(at100.due, 18);
  assert.equal(L.dueSteps([{ ts: 50, expected: 0 }], 90).every((p) => p.due === 0), true);
  assert.deepEqual(L.dueSteps([{ ts: 5, expected: 5000 }], 5), [{ t: 5, due: 83 }]);
  assert.deepEqual(L.dueSteps([], 5), []);
  assert.deepEqual(L.dueSteps(null, null), []);
  const u = L.dueSteps([{ ts: 100, expected: 1200 }, { ts: 0, expected: 600 }], 150);
  assert.equal(u[0].t, 0);
});

test('dueSteps: lastSeen before the last sample is ignored for that sample, null lastSeen uses last ts; null expected falls back to due_min', () => {
  const s = L.dueSteps([{ ts: 0, expected: null, due_min: 7 }, { ts: 60, expected: null, due_min: 6 }], null);
  assert.deepEqual(s, [{ t: 0, due: 7 }, { t: 60, due: 6 }]);
  const clip = L.dueSteps([{ ts: 0, expected: 600 }, { ts: 100, expected: 600 }], 50);
  assert.ok(clip.every((p) => p.t <= 50));
});

test('extent', () => {
  assert.equal(L.extent([]), null);
  assert.equal(L.extent([null, NaN, undefined]), null);
  assert.deepEqual(L.extent([3, null, -1, 2]), [-1, 3]);
  assert.deepEqual(L.extent([4]), [4, 4]);
});

test('linearScale maps and handles degenerate domains', () => {
  const s = L.linearScale([0, 10], [100, 200]);
  assert.equal(s(0), 100);
  assert.equal(s(10), 200);
  assert.equal(s(5), 150);
  assert.equal(s(-10), 0);
  const d = L.linearScale([3, 3], [0, 100]);
  assert.equal(d(3), 50);
  assert.equal(d(99), 50);
  const inv = L.linearScale([0, 10], [200, 100]);
  assert.equal(inv(10), 100);
  assert.equal(L.linearScale([0, 1], [0, 1])(NaN), NaN);
});

test('niceTicks', () => {
  assert.deepEqual(L.niceTicks(0, 10, 5), [0, 2, 4, 6, 8, 10]);
  assert.deepEqual(L.niceTicks(-3, 7, 5), [-2, 0, 2, 4, 6]);
  assert.deepEqual(L.niceTicks(null, null), []);
  assert.deepEqual(L.niceTicks(NaN, 3), []);
  const t = L.niceTicks(5, 5);
  assert.ok(t.length >= 2 && t.includes(5));
  const t2 = L.niceTicks(0, 0);
  assert.ok(t2.includes(0) && t2.length >= 2);
  assert.deepEqual(L.niceTicks(10, 0, 5), L.niceTicks(0, 10, 5)); // swapped args
  const f = L.niceTicks(0, 1, 10);
  assert.ok(f.every((x) => Number.isFinite(x) && Math.abs(x * 100 - Math.round(x * 100)) < 1e-9));
  const big = L.niceTicks(-1e6, 3e6, 4);
  assert.ok(big.length >= 2 && big.length <= 12);
});

test('niceDomain covers data and includes zero when asked', () => {
  const d = L.niceDomain(-3.2, 7.9, 5);
  assert.ok(d[0] <= -3.2 && d[1] >= 7.9);
  const z = L.niceDomain(2, 9, 5, true);
  assert.ok(z[0] <= 0);
  assert.deepEqual(L.niceDomain(null, null, 5), [-1, 1]);
  const same = L.niceDomain(4, 4, 5);
  assert.ok(same[0] < 4 && same[1] > 4);
});

test('buildQuery / parseQuery', () => {
  assert.equal(L.buildQuery({}), '');
  assert.equal(L.buildQuery({ a: 1, b: null, c: undefined, d: '' }), 'a=1');
  assert.equal(L.buildQuery({ q: 'a b&c=d/é' }), 'q=a%20b%26c%3Dd%2F%C3%A9');
  assert.equal(L.buildQuery({ weekdays: [0, 5, 6] }), 'weekdays=0%2C5%2C6');
  assert.equal(L.buildQuery({ weekdays: [] }), '');
  assert.equal(L.buildQuery({ z: 0, f: false }), 'z=0&f=false');
  assert.deepEqual(L.parseQuery('?a=1&b=x%20y&c'), { a: '1', b: 'x y', c: '' });
  assert.deepEqual(L.parseQuery(''), {});
  assert.deepEqual(L.parseQuery('a=%E0%A4%A'), { a: '%E0%A4%A' }); // malformed escape survives
  const o = { 'k&=': 'v%+ #?', u: 'ünï' };
  assert.deepEqual(L.parseQuery(L.buildQuery(o)), o);
});

test('hash routes round trip', () => {
  const cases = [
    ['routes', {}, {}],
    ['route', { route: '15/A' }, { ci: '0.9' }],
    ['route', { route: '100%' }, {}],
    ['stop', { route: '46A', stop: 'Ünï cødé/1' }, { page: '2' }],
    ['trip', { trip: 't#1?x', stop: '8220DB000002' }, { seq: '1700000000' }],
    ['analytics', { route: 'x y' }, {}],
  ];
  for (const [name, params, query] of cases) {
    const h = L.buildHash(name, params, query);
    assert.ok(h.startsWith('#/'));
    assert.ok(!h.slice(2).split('?')[0].includes(' '));
    assert.deepEqual(L.parseHash(h), { name, params, query });
  }
  assert.equal(L.buildHash('route', { route: '15/A' }), '#/route/15%2FA');
});

test('parseHash defaults and bad input', () => {
  assert.equal(L.parseHash('').name, 'routes');
  assert.equal(L.parseHash('#').name, 'routes');
  assert.equal(L.parseHash('#/').name, 'routes');
  assert.equal(L.parseHash(undefined).name, 'routes');
  assert.equal(L.parseHash('#/nope').name, 'notfound');
  assert.equal(L.parseHash('#/route').name, 'notfound');
  assert.equal(L.parseHash('#/route/a/b').name, 'notfound');
  assert.equal(L.parseHash('#/stop/onlyone').name, 'notfound');
  assert.equal(L.parseHash('#/route/%E0%A4%A').name, 'notfound'); // malformed percent escape
  assert.equal(L.parseHash('#/route/').name, 'notfound');
  assert.equal(L.parseHash('#/route/15?ci=0.9').query.ci, '0.9');
});

test('makeLatest: only the newest request is current; aborts the previous', () => {
  const parent = new AbortController();
  const l = L.makeLatest(parent.signal);
  const a = l.start();
  assert.equal(a.isCurrent(), true);
  const b = l.start();
  assert.equal(a.isCurrent(), false);
  assert.equal(a.signal.aborted, true);
  assert.equal(b.isCurrent(), true);
  parent.abort();
  assert.equal(b.isCurrent(), false);
  assert.equal(b.signal.aborted, true);
  const c = l.start(); // started after parent abort: already stale
  assert.equal(c.isCurrent(), false);
});

test('makeLatest without a parent signal', () => {
  const l = L.makeLatest();
  const a = l.start();
  assert.equal(a.isCurrent(), true);
  l.start();
  assert.equal(a.isCurrent(), false);
});

test('hoursList wraps midnight and handles blanks', () => {
  assert.equal(L.hoursList('', ''), null);
  assert.deepEqual(L.hoursList(7, 9), [7, 8, 9]);
  assert.deepEqual(L.hoursList(22, 1), [22, 23, 0, 1]);
  assert.deepEqual(L.hoursList(5, 5), [5]);
  assert.equal(L.hoursList(0, 23).length, 24);
  assert.deepEqual(L.hoursList(7, ''), [7]);
  assert.deepEqual(L.hoursList('', 7), [7]);
});

test('dateToEpoch', () => {
  assert.equal(L.dateToEpoch('1970-01-02'), 86400);
  assert.equal(L.dateToEpoch('1970-01-02', true), 86400 + 86399);
  assert.equal(L.dateToEpoch(''), null);
  assert.equal(L.dateToEpoch('garbage'), null);
  assert.equal(L.dateToEpoch('2025-02-30'), null);
});

test('controls: defaults, validation and API mapping', () => {
  const c = { ...L.DEFAULT_CONTROLS };
  assert.deepEqual(L.validateControls(c), []);
  const q = L.toApiQuery(c, 1_700_000_000);
  assert.equal(q.ci, 0.95);
  assert.equal(q.pct_lo, 0.05);
  assert.equal(q.pct_hi, 0.95);
  assert.equal(q.window_lo, -60);
  assert.equal(q.window_hi, 300);
  assert.equal(q.since, 1_700_000_000 - 90 * 86400); // explicit since by default
  assert.equal(q.min_confidence, 1);
  assert.equal(q.until, null);
  const c2 = { ...c, ci: 99.9, pLo: 10, pHi: 90, winLo: -2.5, winHi: 10, since: '2025-01-01', until: '2025-01-31',
    weekdays: [0, 4], hFrom: 22, hTo: 1, confirmed: false };
  const q2 = L.toApiQuery(c2, 1_700_000_000);
  assert.equal(q2.ci, 0.999);
  assert.equal(q2.window_lo, -150);
  assert.equal(q2.min_confidence, 0);
  assert.deepEqual(q2.weekdays, [0, 4]);
  assert.deepEqual(q2.hours, [22, 23, 0, 1]);
  assert.equal(q2.until, Date.UTC(2025, 0, 31) / 1000 + 86399);
  assert.ok(!/NaN|undefined/.test(L.buildQuery(q2)));
});

test('validateControls rejects bad values', () => {
  const bad = (patch) => L.validateControls({ ...L.DEFAULT_CONTROLS, ...patch }).length > 0;
  assert.ok(bad({ ci: 0 }));
  assert.ok(bad({ ci: 100 }));
  assert.ok(bad({ ci: 99.99 + 0.01 }));
  assert.ok(!bad({ ci: 99.99 }));
  assert.ok(bad({ ci: NaN }));
  assert.ok(bad({ pLo: 50, pHi: 50 }));
  assert.ok(bad({ pLo: 90, pHi: 10 }));
  assert.ok(bad({ pLo: -1 }));
  assert.ok(bad({ winLo: 5, winHi: -5 }));
  assert.ok(!bad({ winLo: 1, winHi: 1 }));
  assert.ok(bad({ winLo: NaN }));
  assert.ok(bad({ since: '2025-02-01', until: '2025-01-01' }));
  assert.ok(bad({ since: 'x' }));
  assert.ok(bad({ hFrom: 24, hTo: 3 }));
  assert.ok(bad({ weekdays: [7] }));
});

test('controls <-> hash query round trip; only non-defaults are stored', () => {
  assert.deepEqual(L.controlsToQuery(L.DEFAULT_CONTROLS), {});
  const c = { ...L.DEFAULT_CONTROLS, ci: 90, weekdays: [5, 6], hFrom: 7, hTo: 9, confirmed: false, since: '2025-03-01', winHi: 10 };
  const q = L.controlsToQuery(c);
  const back = L.controlsFromQuery(L.parseQuery(L.buildQuery(q)));
  assert.deepEqual(back.controls, c);
  assert.deepEqual(back.errors, []);
});

test('controlsFromQuery falls back to defaults on garbage and reports errors', () => {
  const r = L.controlsFromQuery({ ci: 'abc', wd: '9,x', hf: '99', conf: 'maybe', plo: '' });
  assert.deepEqual(r.controls, L.DEFAULT_CONTROLS);
  assert.ok(r.errors.length >= 3);
  assert.deepEqual(L.controlsFromQuery({}).controls, L.DEFAULT_CONTROLS);
});

test('paginate clamps', () => {
  assert.deepEqual(L.paginate(0, 1, 10), { page: 1, pages: 1, start: 0, end: 0 });
  assert.deepEqual(L.paginate(25, 3, 10), { page: 3, pages: 3, start: 20, end: 25 });
  assert.deepEqual(L.paginate(25, 99, 10), { page: 3, pages: 3, start: 20, end: 25 });
  assert.deepEqual(L.paginate(25, -4, 10), { page: 1, pages: 3, start: 0, end: 10 });
  assert.deepEqual(L.paginate(25, NaN, 10), { page: 1, pages: 3, start: 0, end: 10 });
  assert.deepEqual(L.paginate(10, 1, 10), { page: 1, pages: 1, start: 0, end: 10 });
});

test('diverging bucket: symmetric, monotonic, clamped, null-safe', () => {
  assert.equal(L.divergingBucket(null, 100), null);
  assert.equal(L.divergingBucket(NaN, 100), null);
  assert.equal(L.divergingBucket(0, 100), 0);
  assert.equal(L.divergingBucket(5, 0), 0);
  assert.equal(L.divergingBucket(100, 100), 5);
  assert.equal(L.divergingBucket(-100, 100), -5);
  assert.equal(L.divergingBucket(1e9, 100), 5);
  let prev = -Infinity;
  for (let v = -150; v <= 150; v += 0.5) {
    const b = L.divergingBucket(v, 100);
    assert.ok(b >= prev, `monotonic at ${v}`);
    assert.equal(b, -L.divergingBucket(-v, 100) || 0, `symmetric at ${v}`);
    prev = b;
  }
});

test('clockTime formats in Europe/Dublin (DST aware)', () => {
  assert.equal(L.clockTime(Date.UTC(2025, 0, 15, 12, 0) / 1000), '12:00'); // GMT
  assert.equal(L.clockTime(Date.UTC(2025, 6, 15, 12, 0) / 1000), '13:00'); // IST
  assert.equal(L.clockTime(null), '–');
  assert.equal(L.dateTime(Date.UTC(2025, 6, 15, 12, 0) / 1000), '2025-07-15 13:00');
});

test('shorten', () => {
  assert.equal(L.shorten('abcdef', 4), 'abc…');
  assert.equal(L.shorten('abc', 4), 'abc');
  assert.equal(L.shorten(null, 4), '');
});

test('delayClass', () => {
  assert.equal(L.delayClass(null), '');
  assert.equal(L.delayClass(0.2), 'ontime');
  assert.equal(L.delayClass(-90), 'early');
  assert.equal(L.delayClass(90), 'late');
});

test('stopNameMap tolerates odd shapes', () => {
  const m = L.stopNameMap({ out: [{ id: 'a', name: 'Alpha' }, { stopId: 'b', stopName: 'Beta' }, { id: 'c' }, null, { id: 5, name: 'x' }], back: [{ id: 'a', name: 'Other' }, 'junk'] });
  assert.deepEqual([...m], [['a', 'Alpha'], ['b', 'Beta']]);
  for (const bad of [null, undefined, 3, [], { x: 'y' }]) assert.equal(L.stopNameMap(bad).size, 0);
});
