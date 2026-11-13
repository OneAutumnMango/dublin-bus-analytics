// Stats lab view: distribution, persistence along the route, and variance explained. API delays are seconds; charts take minutes.
import { api } from '../api.js';
import { barGeometry, ecdfGeometry, qqGeometry, renderBars, renderEcdf, renderQq } from '../labcharts.js';
import { EXPLAIN, readDistribution, readPersistence, readVariance } from '../labtext.js';
import { dataTable, emptyState, figure, h, link, put, tile } from '../dom.js';
import { buildHash, fmtCount, fmtDelay, fmtMinutes, fmtNum, isNum, plural } from '../lib.js';
import { region } from '../view.js';
import { infoPopover } from '../infopop.js';
import { crumbs, filterBar, filterNotices, setup, windowText } from './common.js';

const NO_DATA = 'No data for this selection.';
const NO_DAYS = 'not enough days yet';
const CHART = { width: 560, height: 340 }; // every chart shares one size so each side-by-side pair renders at equal height
const FACTORS = { hour: 'Hour of day', weekday: 'Day of week', stop: 'Stop', date: 'Calendar date', route: 'Route' };

// route undefined = the all-routes view (#/lab), pooled from /api/all/lab/*
export async function render(ctx) {
  const { route } = ctx.params;
  const s = setup(ctx);
  const who = route ? `route ${route}` : 'all routes';
  const Who = `${who[0].toUpperCase()}${who.slice(1)}`;
  ctx.title(`Stats lab · ${who}`);
  const sections = ['dist', 'pers', 'var'].map(() => h('section', { class: 'analytics-section' }));
  put(ctx.main,
    route
      ? crumbs(link('#/', '← All routes'), link(buildHash('route', { route }, s.linkQuery), `Route ${route} stops`), link(buildHash('analytics', { route }, s.linkQuery), 'Analytics for this route'))
      : crumbs(link(buildHash('all', {}, s.linkQuery), '← All tracked routes')),
    h('h1', {}, `Stats lab for ${who}`),
    h('p', { class: 'subtitle' }, 'Delay = actual minus scheduled: negative = early, positive = late. Statistics use confirmed arrivals unless you change the filter.'),
    ...filterBar(ctx, 'lab', route ? { route } : {}, s), sections[0], h('div', { class: 'lab-pair' }, sections[1], sections[2]));
  const [dist, pers, vari] = sections.map((el) => region(el, ctx.signal));
  const q = s.apiQuery, o = (signal) => ({ signal });
  await Promise.all([
    dist.load((sig) => (route ? api.labDistribution(route, q, o(sig)) : api.allLabDistribution(q, o(sig))), (d, el) => drawDist(who, d, el, s, !route), { announce: (d) => `${Who} delay distribution loaded: ${fmtCount(d.n)} arrivals` }),
    pers.load((sig) => (route ? api.labPersistence(route, q, o(sig)) : api.allLabPersistence(q, o(sig))), (d, el) => drawPers(who, d, el, !route), { announce: (d) => `${Who} delay persistence loaded: ${fmtCount(d.n_trips)} trips` }),
    vari.load((sig) => (route ? api.labVariance(route, q, o(sig)) : api.allLabVariance(q, o(sig))), (d, el) => drawVar(who, d, el, !route), { announce: (d) => `${Who} variance explained loaded: ${fmtCount(d.n)} arrivals` }),
  ]);
}

const head = (title, d) => [h('h2', {}, title), ...filterNotices(d)];
// host for a chart (or tile block) with an "i" button in its top-right corner; keys pick the EXPLAIN entries shown in the popover
const withInfo = (label, keys, ...content) => h('div', { class: 'info-host' }, content, infoPopover(label, keys.map((k) => EXPLAIN[k])));
const reading = (text) => h('p', { class: 'reading' }, text);
const pct = (v) => (isNum(v) ? `${fmtNum(v * 100, 0)}%` : '–');
const ciOf = (x, enough) => (enough && x && isNum(x.ci_low) && isNum(x.ci_high) ? [x.ci_low, x.ci_high] : null);

// value text plus its CI (or the not-enough-days note); fmt converts a raw API number to text
function ciTile(label, x, enough, fmt, n) {
  if (!x || !isNum(x.value ?? x.slope)) return tile(label, '–', { note: NO_DATA });
  const ci = ciOf(x, enough);
  return tile(label, fmt(x.value ?? x.slope), { n, lowN: x.low_n, note: ci ? `CI ${fmt(ci[0])} to ${fmt(ci[1])}` : NO_DAYS });
}

function drawDist(who, d, el, s, pooled) {
  put(el, head('How are delays distributed?', d));
  if (!d.n) { put(el, emptyState(NO_DATA)); return; }
  if (pooled) put(el, h('p', { class: 'hint' }, `${plural(d.routes, 'route')} pooled; every arrival counts once, so busy routes weigh more.`));
  const pts = d.ecdf.map((p) => ({ x: p.x / 60, p: p.p }));
  const geo = ecdfGeometry(pts, CHART);
  const lo = Math.max(geo.x0, geo.sx(s.controls.winLo)), hi = Math.min(geo.x1, geo.sx(s.controls.winHi));
  const svg = renderEcdf(geo, { xLabel: 'Delay (minutes; negative = early)', yLabel: 'Share of arrivals at or below this delay', title: `Cumulative delay distribution, ${who}`, band: { x0: lo, x1: hi },
    desc: `Share of ${fmtCount(d.n)} arrivals at or below each delay in minutes. The shaded band is the on-time window (${s.controls.winLo} to ${s.controls.winHi} min); circles mark the median (p50) and the 95th percentile (p95).` });
  const table = dataTable({ caption: 'Share of arrivals at or below each delay (negative = early)', rows: d.ecdf, columns: [
    { label: 'Delay', num: true, get: (p) => fmtDelay(p.x) }, { label: 'Share at or below', num: true, get: (p) => pct(p.p) }] });
  const qg = qqGeometry(d.qq.map((p) => ({ z: p.z, y: p.y / 60 })), CHART);
  const qsvg = renderQq(qg, { xLabel: 'Normal quantile (standard deviations)', yLabel: 'Observed delay (minutes)', title: `Normal Q-Q plot of delays, ${who}`, desc: `${d.qq.length} quantile points of observed delay in minutes against a normal distribution; a straight line would mean normal delays.` });
  const qtable = dataTable({ caption: 'Observed delay against the normal-distribution quantile', rows: d.qq, columns: [
    { label: 'Std. deviations (normal)', num: true, get: (p) => fmtNum(p.z, 2) }, { label: 'Observed delay', num: true, get: (p) => fmtDelay(p.y) }] });
  const t = d.tiles || {}, e = d.enough_days;
  put(el, reading(readDistribution(d)),
    h('div', { class: 'lab-pair' },
      withInfo('About the cumulative distribution chart', ['ecdf', 'ci'], figure({ title: 'Cumulative distribution', svg, table, level: 'h3', legend: h('p', { class: 'legend' }, 'Shaded band: on-time window · circles: median (p50) and 95th percentile (p95) · dashed line: on time.') })),
      withInfo('About the Q-Q plot', ['qq'], figure({ title: 'Normal Q-Q plot', svg: qsvg, table: qtable, level: 'h3' }))),
    withInfo('About the distribution statistics', ['shape', 'buffer', 'ci'], h('div', { class: 'tiles' }, ciTile('Median delay', t.median, e, fmtDelay, d.n), ciTile('95th percentile', t.p95, e, fmtDelay, d.n), ciTile('Buffer (p95 minus median)', t.buffer, e, fmtMinutes, d.n),
      ciTile('Skew', t.skew, e, (v) => fmtNum(v, 2), d.n), ciTile('Excess kurtosis', t.kurtosis, e, (v) => fmtNum(v, 2), d.n))),
    h('p', { class: 'hint' }, windowText(d)));
}

function barFigure(info, keys, title, svgOpts, items, geoOpts, columns, rows, legend) {
  const svg = renderBars(barGeometry(items, geoOpts), svgOpts);
  return withInfo(info, [...keys, 'ci'], figure({ title, svg, table: dataTable({ caption: svgOpts.title, rows, columns }), level: 'h3', legend: h('p', { class: 'legend' }, legend) }));
}

function drawPers(who, d, el, pooled) {
  const along = pooled ? 'along each route' : 'along the route';
  // the shared "listed by stop id" notice does not apply here, so mask order_source for head()
  put(el, head(`Do late buses stay late ${along}?`, { ...d, order_source: 'server' }));
  if (d.routes_skipped?.length) put(el, h('p', { class: 'hint' }, `${plural(d.routes_skipped.length, 'route')} left out: stop order unavailable`));
  if (d.order_source === 'none') { put(el, emptyState('Persistence needs the stop order from the bus data server, which is unavailable right now.')); return; }
  if (!d.n_trips) { put(el, emptyState(NO_DATA)); return; }
  const e = d.enough_days, lags = d.lags || [];
  const items = lags.map((l) => { const ci = ciOf(l, e); return { label: `${l.lag} stop${l.lag === 1 ? '' : 's'} on`, value: l.r, ci_low: ci && ci[0], ci_high: ci && ci[1] }; });
  const range = (l) => { const ci = ciOf(l, e); return ci ? `${fmtNum(ci[0], 2)} to ${fmtNum(ci[1], 2)}` : NO_DAYS; };
  put(el, reading(readPersistence(d)),
    barFigure('About the delay correlation chart', ['persistence', 'recovery'], `Correlation of delay by distance ${along}`, { title: `Delay correlation by lag, ${who}`, xLabel: 'Lag (stops apart)', yLabel: 'Autocorrelation (r)',
      desc: `Correlation of delay between stops ${lags.length} different distances apart, from -1 to 1. ${lags.map((l) => `${l.lag} stops: ${fmtNum(l.r, 2)}`).join('; ')}.` },
    items, { min: -1, max: 1, ...CHART }, [{ label: 'Lag (stops)', num: true, get: (l) => String(l.lag) }, { label: 'Correlation (r)', num: true, get: (l) => fmtNum(l.r, 2) },
      { label: 'Trips (n)', num: true, get: (l) => fmtCount(l.n) }, { label: 'Confidence interval', num: true, get: range }], lags, 'Bar: correlation r (1 = delay carries over fully, 0 = no link) · whisker: confidence interval.'),
    h('div', { class: 'tiles' }, ciTile('Recovery slope', d.recovery, e, (v) => fmtNum(v, 2), d.recovery?.n)),
    h('p', { class: 'hint' }, windowText({ ...d, n: d.n_trips })));
}

function drawVar(who, d, el, pooled) {
  put(el, head('What explains delay variation?', d));
  const fs = d.factors || [];
  if (!d.n || !fs.length) { put(el, emptyState(NO_DATA)); return; }
  const e = d.enough_days;
  const items = fs.map((f) => { const ci = ciOf(f, e); return { label: FACTORS[f.factor] || f.factor, value: f.omega2, ci_low: ci && ci[0], ci_high: ci && ci[1] }; });
  put(el, reading(readVariance(d)),
    barFigure('About the variance explained chart', [pooled ? 'varianceAll' : 'variance'], 'Share of delay variation explained', { title: `Variance explained by factor, ${who}`, xLabel: 'Factor', yLabel: 'Adjusted ω² (share of variance explained)',
      desc: `Share of delay variation explained by each factor: ${fs.map((f) => `${FACTORS[f.factor] || f.factor} ${pct(f.omega2)}`).join('; ')}.` },
    items, CHART, [{ label: 'Factor', get: (f) => FACTORS[f.factor] || f.factor }, { label: 'Share explained', num: true, get: (f) => pct(f.omega2) },
      { label: 'Groups', num: true, get: (f) => fmtCount(f.levels) }, { label: 'Confidence interval', num: true, get: (f) => { const ci = ciOf(f, e); return ci ? `${pct(ci[0])} to ${pct(ci[1])}` : NO_DAYS; } }], fs,
    'Bar: share of the variation in delay that the factor explains · whisker: confidence interval.'),
    h('p', { class: 'hint' }, windowText(d)));
}
