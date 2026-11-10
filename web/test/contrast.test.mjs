import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'css', 'style.css'), 'utf8');

function block(open) {
  const i = css.indexOf(open);
  assert.ok(i >= 0, `missing ${open}`);
  let depth = 0, j = css.indexOf('{', i);
  const start = j + 1;
  for (; j < css.length; j++) { if (css[j] === '{') depth++; if (css[j] === '}' && --depth === 0) break; }
  return css.slice(start, j);
}
const vars = (b) => Object.fromEntries([...b.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\b/g)].map((m) => [m[1], m[2]]));
const light = vars(block(':root {'));
const darkMedia = vars(block(':root:not([data-theme="light"]) {'));
const darkManual = vars(block(':root[data-theme="dark"] {'));
const dark = { ...light, ...darkMedia };

const lum = (hex) => {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
export const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

const TEXT = [['text', 'bg'], ['text', 'surface'], ['text', 'surface-2'], ['muted', 'bg'], ['muted', 'surface'], ['muted', 'surface-2'],
  ['accent', 'surface'], ['accent', 'bg'], ['accent-contrast', 'accent'], ['early-text', 'surface'], ['late-text', 'surface'], ['early-text', 'bg'], ['late-text', 'bg'],
  ['err', 'err-bg'], ['warn', 'warn-bg'], ['ok', 'surface']];
const GRAPHICS = [['control-border', 'surface'], ['control-border', 'bg'], ['focus', 'bg'], ['focus', 'surface'], ['early', 'surface'], ['text', 'surface']];
// Okabe-Ito orange (#E69F00) is 2.1:1 on white: chart marks in the light theme rely on labels, the tooltip and the data table
// for their values (WCAG 1.4.11 exemption is not claimed; this is a deliberate palette choice). Dark theme is held to 3:1.
const LATE_GRAPHIC = [['late', 'surface'], ['late', 'bg']];
const HEAT = ['n5', 'n4', 'n3', 'n2', 'n1', '0', '1', '2', '3', '4', '5'];

for (const [name, v] of [['light', light], ['dark', dark]]) {
  test(`${name}: text pairs reach 4.5:1`, () => {
    for (const [f, b] of TEXT) { assert.ok(v[f] && v[b], `${f}/${b} defined`); assert.ok(ratio(v[f], v[b]) >= 4.5, `${f} on ${b}: ${ratio(v[f], v[b]).toFixed(2)}`); }
  });
  test(`${name}: borders and graphics reach 3:1`, () => {
    for (const [f, b] of GRAPHICS) { assert.ok(v[f] && v[b], `${f}/${b} defined`); assert.ok(ratio(v[f], v[b]) >= 3, `${f} on ${b}: ${ratio(v[f], v[b]).toFixed(2)}`); }
  });
  if (name === 'dark') test('dark: orange chart marks reach 3:1', () => { for (const [f, b] of LATE_GRAPHIC) assert.ok(ratio(v[f], v[b]) >= 3, `${f} on ${b}`); });
  test(`${name}: heatmap text on every bucket reaches 4.5:1`, () => {
    for (const k of HEAT) assert.ok(ratio(v[`hm-${k}`], v[`hm-${k}t`]) >= 4.5, `bucket ${k}: ${ratio(v[`hm-${k}`], v[`hm-${k}t`]).toFixed(2)}`);
  });
}

test('manual dark theme block equals the media-query dark block', () => {
  assert.deepEqual(darkManual, darkMedia);
});

test('low-n styling never reduces text opacity', () => {
  assert.ok(!/low-n[^{]*\{[^}]*opacity/.test(css), 'opacity on .low-n selectors');
});
