import test from 'node:test';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { installDom, addDocEvents } from './fakedom.mjs';

const { doc } = installDom();
addDocEvents(doc);
const { infoPopover } = await import('../js/infopop.js');
const E = (t) => [{ title: `Title ${t}`, paragraphs: [`Para ${t}`] }];
// a real click bubbles to the document, so simulate both
const click = (n) => { n.dispatch('click'); doc.dispatch('click'); };
const make = (t) => { const [btn, pop] = infoPopover(`About chart ${t}`, E(t)); doc.body.append(btn, pop); return { btn, pop }; };

test('infoPopover: hidden at start, accessible wiring', () => {
  const { btn, pop } = make('a');
  assert.equal(btn.tag, 'button');
  assert.equal(btn.getAttribute('type'), 'button');
  assert.equal(btn.getAttribute('aria-label'), 'About chart a');
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(btn.getAttribute('aria-controls'), pop.getAttribute('id'));
  assert.ok(pop.hasAttribute('hidden'));
  assert.equal(pop.getAttribute('role'), 'region');
  const title = pop.byTag('h4')[0];
  assert.equal(pop.getAttribute('aria-labelledby'), title.getAttribute('id'));
  assert.match(pop.textContent, /Title a.*Para a/);
});

test('infoPopover: click toggles, Escape closes and refocuses, outside click closes', () => {
  const { btn, pop } = make('b');
  click(btn);
  assert.ok(!pop.hasAttribute('hidden'));
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  click(btn);
  assert.ok(pop.hasAttribute('hidden'));
  click(btn);
  btn.focused = false;
  doc.dispatch('keydown', { key: 'Escape' });
  assert.ok(pop.hasAttribute('hidden'));
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(btn.focused, true);
  click(btn);
  click(pop); // a click inside the panel keeps it open
  assert.ok(!pop.hasAttribute('hidden'));
  doc.dispatch('click'); // outside
  assert.ok(pop.hasAttribute('hidden'));
});

test('infoPopover: only one open at a time, document listeners registered once', () => {
  const a = make('c'), b = make('d');
  click(a.btn); click(b.btn); doc.dispatch('click');
  assert.ok(a.pop.hasAttribute('hidden') && b.pop.hasAttribute('hidden'));
  click(a.btn); click(b.btn);
  assert.ok(a.pop.hasAttribute('hidden') && !b.pop.hasAttribute('hidden'));
  assert.equal(doc.listeners.click.length, 1);
  assert.equal(doc.listeners.keydown.length, 1);
});

// `main > *` runs a rise animation (fill: both), which makes every page section its own stacking context: a popover can only
// beat later sections if the section holding the open popover is raised above its siblings (below the sticky header, z 40).
test('css: a section with an open popover is raised above later sections and the popover stays inside its host', () => {
  const css = readFileSync(new URL('../css/style.css', import.meta.url), 'utf8');
  const rule = css.match(/main > \*:has\(\.info-pop:not\(\[hidden\]\)\)\s*\{([^}]*)\}/);
  assert.ok(rule, 'raise rule exists');
  assert.match(rule[1], /position:\s*relative/);
  const z = Number(rule[1].match(/z-index:\s*(\d+)/)[1]);
  assert.ok(z > 10 && z < 40);
  assert.match(css.match(/\.info-pop \{[^}]*\}/)[0], /max-width:\s*min\(22rem, calc\(100% /);
});
