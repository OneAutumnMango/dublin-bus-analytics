import test from 'node:test';
import assert from 'node:assert/strict';
import { installDom, FNode } from './fakedom.mjs';
import * as C from '../js/charts.js';
import { dueSteps } from '../js/lib.js';

installDom();
const SVG = 'http://www.w3.org/2000/svg';

function all() {
  const items = [{ label: 'a', href: '#/x', n: 30, low_n: false, mean: 60, min: -60, max: 300, lo: 0, hi: 200 }, { label: 'b', n: 2, low_n: true, mean: null, min: null, max: null, lo: null, hi: null }];
  const bins = [{ lo: null, hi: -600, count: 1 }, ...Array.from({ length: 40 }, (_, i) => ({ lo: -600 + i * 60, hi: -540 + i * 60, count: i })), { lo: 1800, hi: null, count: 2 }];
  const hg = C.histogramGeometry(bins);
  const stops = [{ stop_id: 'a', position: 0, mean_delay: 0, segment_delta: null, n: 20, n_delta: 0 }, { stop_id: 'b', position: 1, mean_delay: 90, segment_delta: 90, n: 20, n_delta: 15 }];
  const steps = dueSteps([{ ts: 0, expected: 600, due_min: 10 }], 300);
  return [
    C.renderRangeChart(C.rangeGeometry(items), { title: 't', desc: 'd' }),
    C.renderHistogram(hg, { title: 't', desc: 'd', markers: [{ x: hg.xOf(0), cls: 'mean-line' }], band: { x0: 100, x1: 200 } }),
    C.renderHeatmap(C.heatmapGrid([{ weekday: 1, hour: 2, n: 20, low_n: false, mean_delay: 100 }]), { title: 't', desc: 'd' }),
    C.renderAccumulation(C.accumulationGeometry(stops), stops, { title: 't', desc: 'd' }),
    C.renderStepChart(C.stepGeometry(steps, [{ t: 100, label: 'x', kind: 'arrival' }]), { title: 't', desc: 'd' }),
  ];
}

test('every element of every chart is created in the SVG namespace', () => {
  for (const svg of all()) {
    assert.equal(svg.tag, 'svg');
    const nodes = [...svg.walk()];
    assert.ok(nodes.length > 5);
    for (const n of nodes) assert.equal(n.ns, SVG, `<${n.tag}> in ${svg.getAttribute('class')}`);
  }
});

test('the DOM double distinguishes HTML from SVG creation', () => {
  const html = globalThis.document.createElement('div');
  const svg = globalThis.document.createElementNS(SVG, 'svg');
  assert.ok(html instanceof FNode && html.ns === 'html' && svg.ns === SVG);
});
