import test from 'node:test';
import assert from 'node:assert/strict';
import { tipText, placeTip } from '../js/tooltip.js';
import { FNode, FText } from './fakedom.mjs';

test('placeTip: default offset, edge flips, never off the top-left', () => {
  assert.deepEqual(placeTip(100, 100, 120, 40, 1000, 800), { left: 114, top: 114 });
  assert.deepEqual(placeTip(950, 780, 120, 40, 1000, 800), { left: 816, top: 726 });
  const p = placeTip(2, 2, 120, 40, 100, 30);
  assert.ok(p.left >= 8 && p.top >= 8);
});

test('tipText: title of the element or nearest chart ancestor, null when none', () => {
  const t = new FNode('title'); t.append(new FText('Mon 08:00 - mean 2 min'));
  const rect = new FNode('rect'); const g = new FNode('g'); g.append(t, rect);
  assert.equal(tipText(g), 'Mon 08:00 - mean 2 min');
  const bare = new FNode('rect');
  assert.equal(tipText(bare), null);
  assert.equal(tipText(null), null);
});

test('tipText: data-tip attribute wins and is read from the element itself', () => {
  const r = new FNode('rect'); r.setAttribute('data-tip', 'exact value');
  assert.equal(tipText(r), 'exact value');
});
