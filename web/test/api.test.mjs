import test from 'node:test';
import assert from 'node:assert/strict';
import * as A from '../js/api.js';

const resp = (status, body, text) => ({ ok: status >= 200 && status < 300, status, text: async () => text ?? JSON.stringify(body) });

test('errorMessage maps statuses to readable text', () => {
  assert.equal(A.errorMessage(422, '{"detail":"ci must be a number"}'), 'Invalid request: ci must be a number');
  assert.equal(A.errorMessage(422, '{"detail":[{"msg":"a"},{"msg":"b"}]}'), 'Invalid request: a; b');
  assert.match(A.errorMessage(404, '{"detail":"no such trip/stop visit"}'), /Not found.*no such trip/);
  assert.match(A.errorMessage(502, '{"detail":"server unavailable: x"}'), /bus data server/);
  assert.equal(A.errorMessage(429, '{"detail":"try again in 30 s"}'), 'try again in 30 s');
  assert.match(A.errorMessage(403, ''), /HTTP 403/);
  assert.match(A.errorMessage(500, 'oops'), /HTTP 500/);
  assert.match(A.errorMessage(404, ''), /Not found/);
  assert.match(A.errorMessage(422, 'not json'), /Invalid request/);
  assert.match(A.errorMessage(418, null), /HTTP 418/);
});

test('request returns parsed JSON and encodes nothing itself', async () => {
  let seen;
  const f = async (url, init) => { seen = [url, init.method]; return resp(200, { ok: true }); };
  assert.deepEqual(await A.request('/api/health', { fetchFn: f }), { ok: true });
  assert.deepEqual(seen, ['/api/health', 'GET']);
});

test('request maps http errors, network errors, bad json', async () => {
  await assert.rejects(A.request('/x', { fetchFn: async () => resp(422, { detail: 'bad ci' }) }),
    (e) => e instanceof A.ApiError && e.kind === 'http' && e.status === 422 && /bad ci/.test(e.message));
  await assert.rejects(A.request('/x', { fetchFn: async () => { throw new TypeError('Failed to fetch'); } }),
    (e) => e.kind === 'network' && /reach/.test(e.message));
  await assert.rejects(A.request('/x', { fetchFn: async () => resp(200, null, '<html>') }),
    (e) => e.kind === 'parse');
});

test('request times out and aborts the fetch', async () => {
  let aborted = false;
  const f = (url, { signal }) => new Promise((_, rej) => signal.addEventListener('abort', () => { aborted = true; rej(new DOMException('x', 'AbortError')); }));
  await assert.rejects(A.request('/x', { fetchFn: f, timeout: 20 }), (e) => e.kind === 'timeout');
  assert.ok(aborted);
});

test('request honours caller abort (before and during) as kind abort', async () => {
  const ctl = new AbortController();
  const f = (url, { signal }) => new Promise((_, rej) => signal.addEventListener('abort', () => rej(new DOMException('x', 'AbortError'))));
  const p = A.request('/x', { fetchFn: f, signal: ctl.signal, timeout: 5000 });
  ctl.abort();
  await assert.rejects(p, (e) => A.isAbort(e));
  await assert.rejects(A.request('/x', { fetchFn: f, signal: ctl.signal }), (e) => A.isAbort(e));
  assert.equal(A.isAbort(new Error('x')), false);
});

test('endpoint helpers encode path components and build queries', async () => {
  const urls = [];
  const f = async (url, init) => { urls.push([init.method, url]); return resp(200, {}); };
  const api = A.makeApi(f);
  await api.summary('15/A', { ci: 0.9, weekdays: [0, 1], since: 5 });
  await api.stopStats('stop 1/2', '15/A', { ci: 0.9 });
  await api.series('t#1', 's?', { seq: 7 });
  await api.routes('a&b');
  await api.track('15/A');
  await api.untrack('46A');
  await api.stopsOf('15/A');
  await api.heatmap('x', {});
  await api.accumulation('x', {});
  await api.forecast('x', {});
  await api.runBackup();
  assert.deepEqual(urls, [
    ['GET', '/api/routes/15%2FA/summary?ci=0.9&weekdays=0%2C1&since=5'],
    ['GET', '/api/stops/stop%201%2F2/stats?route=15%2FA&ci=0.9'],
    ['GET', '/api/trips/t%231/series?stop=s%3F&seq=7'],
    ['GET', '/api/routes?q=a%26b'],
    ['PUT', '/api/routes/tracked/15%2FA'],
    ['DELETE', '/api/routes/tracked/46A'],
    ['GET', '/api/routes/15%2FA/stops'],
    ['GET', '/api/routes/x/heatmap'],
    ['GET', '/api/routes/x/accumulation'],
    ['GET', '/api/routes/x/forecast'],
    ['POST', '/api/backup'],
  ]);
  assert.equal(A.backupDownloadUrl('a b.tar'), '/api/backup/download?file=a%20b.tar');
  assert.equal(A.backupDownloadUrl(), '/api/backup/download');
});

test('backupAvailable probes with a Range header and maps statuses', async () => {
  const seen = [];
  const f = (status) => async (url, init) => { seen.push(init.headers.Range); return { status }; };
  assert.equal(await A.backupAvailable({ fetchFn: f(200) }), true);
  assert.equal(await A.backupAvailable({ fetchFn: f(206) }), true);
  assert.equal(await A.backupAvailable({ fetchFn: f(404) }), false);
  assert.equal(await A.backupAvailable({ fetchFn: f(500) }), null);
  assert.equal(await A.backupAvailable({ fetchFn: async () => { throw new TypeError('x'); } }), null);
  assert.ok(seen.every((r) => r === 'bytes=0-0'));
});

test('exportUrl builds the query', () => {
  assert.equal(A.exportUrl(), '/api/export.json');
  assert.equal(A.exportUrl({ route: '15/A' }), '/api/export.json?route=15%2FA');
  assert.equal(A.exportUrl({ gzip: true }), '/api/export.json?gzip=1');
  assert.equal(A.exportUrl({ route: '15', gzip: true }), '/api/export.json?route=15&gzip=1');
  assert.equal(A.exportUrl({ route: '', gzip: false }), '/api/export.json');
});

test('dayPreview and removeDay build their URLs', async () => {
  const urls = [];
  const api = A.makeApi(async (url, init) => { urls.push([init.method, url]); return resp(200, {}); });
  await api.dayPreview({ date: '2026-11-11' });
  await api.dayPreview({ date: '2026-11-11', route: '15/A' });
  await api.dayPreview({ date: '2026-11-11', route: '' });
  await api.removeDay({ date: '2026-11-11', route: '15/A' });
  await api.removeDay({ date: '2026-11-11' });
  assert.deepEqual(urls, [
    ['GET', '/api/data/day?date=2026-11-11'],
    ['GET', '/api/data/day?date=2026-11-11&route=15%2FA'],
    ['GET', '/api/data/day?date=2026-11-11'],
    ['POST', '/api/data/day/remove?date=2026-11-11&route=15%2FA&confirm=2026-11-11'],
    ['POST', '/api/data/day/remove?date=2026-11-11&confirm=2026-11-11'],
  ]);
});

test('range dayPreview/removeDay and removeJob', async () => {
  const urls = [];
  const api = A.makeApi(async (url, init) => { urls.push([init.method, url]); return resp(200, {}); });
  await api.dayPreview({ date: '2026-11-10', end: '2026-11-12' });
  await api.removeDay({ date: '2026-11-10', end: '2026-11-12', route: '15' });
  await api.removeJob('a/b');
  assert.deepEqual(urls, [
    ['GET', '/api/data/day?date=2026-11-10&end=2026-11-12'],
    ['POST', '/api/data/day/remove?date=2026-11-10&end=2026-11-12&route=15&confirm=2026-11-10..2026-11-12'],
    ['GET', '/api/data/day/remove/a%2Fb'],
  ]);
});

test('409 errors carry the server detail and parsed body', async () => {
  const api = A.makeApi(async () => ({ ok: false, status: 409, text: async () => JSON.stringify({ detail: 'busy', done: { arrivals: 3 } }) }));
  await assert.rejects(api.removeDay({ date: '2026-11-11' }), (e) => e.status === 409 && e.message === 'busy' && e.body.done.arrivals === 3);
});

test('all-routes helpers request the pooled endpoints', async () => {
  const urls = [];
  const f = async (url, init) => { urls.push([init.method, url]); return resp(200, {}); };
  const api = A.makeApi(f);
  await api.allRoutes({ since: 5 });
  await api.allHeatmap({ since: 5 });
  await api.allForecast({ since: 5 });
  assert.deepEqual(urls, [
    ['GET', '/api/all/routes?since=5'],
    ['GET', '/api/all/heatmap?since=5'],
    ['GET', '/api/all/forecast?since=5'],
  ]);
});

test('lab endpoints request /lab/{distribution|variance|persistence} with the query', async () => {
  const urls = [];
  const api = A.makeApi(async (url, init) => { urls.push([init.method, url]); return resp(200, {}); });
  await api.labDistribution('15/A', { since: 5 });
  await api.labVariance('15/A', { since: 5 });
  await api.labPersistence('15/A', { since: 5 });
  assert.deepEqual(urls, ['distribution', 'variance', 'persistence'].map((x) => ['GET', `/api/routes/15%2FA/lab/${x}?since=5`]));
});
