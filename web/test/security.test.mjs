import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const walk = (d) => readdirSync(d).flatMap((n) => (statSync(join(d, n)).isDirectory() ? walk(join(d, n)) : [join(d, n)]));
const BAD = /innerHTML|outerHTML|insertAdjacentHTML|\beval\s*\(|new Function|document\.write|setAttribute\(\s*['"]on|\.on[a-z]+\s*=[^=]|srcdoc|javascript:/;

test('no HTML-injection sinks, eval or inline handlers under web/js', () => {
  const files = walk(join(root, 'js')).filter((f) => f.endsWith('.js'));
  assert.ok(files.length >= 10);
  for (const f of files) readFileSync(f, 'utf8').split('\n').forEach((l, i) => assert.ok(!BAD.test(l), `${f}:${i + 1}: ${l.trim()}`));
});

test('index.html has no inline script, style attribute or handler', () => {
  const html = readFileSync(join(root, 'index.html'), 'utf8');
  assert.ok(!/<script(?![^>]*\bsrc=)/i.test(html));
  assert.ok(!/<style/i.test(html) && !/\sstyle=/i.test(html) && !/\son[a-z]+=/i.test(html));
});
