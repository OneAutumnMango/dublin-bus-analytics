// Plain-English explanations and one-line readings for the Stats lab. Pure text, no DOM.
// API delays are SECONDS (actual minus scheduled; negative = early); the UI shows minutes.
import { fmtNum, describeDelay, isNum } from './lib.js';

const NONE = 'No data for this selection.';

export const EXPLAIN = {
  ecdf: {
    title: 'Cumulative distribution (ECDF)',
    paragraphs: [
      'Each point shows the share of observed bus arrivals whose delay was at or below that value. Delay is actual minus scheduled time, so negative means early and positive means late.',
      'Read it from the y axis: where the curve crosses 0.5 is the median delay, and where it crosses 0.95 is the delay that 1 in 20 buses exceeds. A steep curve means delays are tightly bunched; a long right tail means occasional very late buses.',
      'A good shape rises sharply close to zero. A bad one climbs slowly, or sits well to the right of zero.',
    ],
  },
  qq: {
    title: 'Normal Q-Q plot',
    paragraphs: [
      'Observed delays (y, in minutes) are plotted against the values a normal distribution would give at the same ranks (x, in standard deviations). Points on a straight line mean the delays are roughly normal.',
      'Bus delays usually are not: a curve bending upward at the right means a heavy late tail, and a bend at the left means some buses run very early. The further the points bow away from a straight line, the less a mean and standard deviation describe the data.',
    ],
  },
  shape: {
    title: 'Skew and excess kurtosis',
    paragraphs: [
      'Skew measures asymmetry. 0 is symmetric; positive means a longer tail of late buses, negative a longer tail of early ones.',
      'Excess kurtosis measures tail weight relative to a normal distribution, where it is 0. Positive means more extreme delays than a normal curve would produce; negative means fewer.',
      'For buses, a strongly positive skew and kurtosis is the bad pattern: most trips are fine but a few are very late.',
    ],
  },
  buffer: {
    title: 'Reliability buffer',
    paragraphs: [
      'The buffer is the 95th percentile delay minus the median delay. It is the extra time a passenger must allow, beyond the typical journey, to arrive on time 19 times out of 20.',
      'A small buffer means the service is predictable. A large buffer means the typical bus can look fine while the bad days are costly.',
    ],
  },
  persistence: {
    title: 'Delay persistence (autocorrelation)',
    paragraphs: [
      'Each bar is the correlation between a bus delay at one stop and its delay a given number of stops (the lag) further along the same trip. Values near 1 mean a late bus stays late; near 0 means the delay at one stop tells you nothing about later ones.',
      'High correlation that decays slowly is the bad pattern: once a bus falls behind, it stays behind. Fast decay means the route absorbs delays.',
    ],
  },
  recovery: {
    title: 'Recovery slope',
    paragraphs: [
      'The slope regresses the delay at a later stop on the delay at an earlier stop of the same trip. A slope near 1 means delay persists: whatever lateness a bus has, it keeps.',
      'A slope near 0 means buses recover and the earlier delay is forgotten. Between roughly 0.3 and 0.7 the delay partly recovers.',
    ],
  },
  variance: {
    title: 'What explains the delay (omega squared)',
    paragraphs: [
      'Each bar is the share of delay variance associated with one factor: hour of day, weekday, stop, or calendar date. It is omega squared, an ANOVA effect size adjusted for the number of levels.',
      'The four bars overlap and do not add to 100%: each factor is measured on its own, and the factors are correlated (for example, hour and stop). A tall bar means that factor separates delays well; a short one means it matters little.',
    ],
  },
  ci: {
    title: 'Confidence intervals',
    paragraphs: [
      'The ranges next to each figure are bootstrap confidence intervals. Delays on the same day are not independent, so we resample whole service days with replacement (200 resamples, fixed seed) and take percentiles of the results.',
      'A wide interval means the figure depends heavily on which days happen to be included. With fewer than 5 days the interval cannot be computed and the page says "not enough days yet": collect more days or widen the date range.',
    ],
  },
};

const FACTOR = { hour: 'hour of day', weekday: 'day of week', stop: 'stop', date: 'calendar date' };

export function readDistribution(d) {
  if (!d || !d.n) return NONE;
  const t = d.tiles || {};
  const v = (k) => (t[k] && isNum(t[k].value) ? t[k].value : null);
  const parts = [];
  if (v('median') !== null) parts.push(`Half of buses are ${describeDelay(v('median'))} or better`);
  if (v('p95') !== null) parts.push(`1 in 20 are more than ${describeDelay(v('p95'))}`);
  if (v('buffer') !== null) parts.push(`allow ${describeDelay(v('buffer')).replace(/ late$/, '')} extra for the buffer`);
  if (v('skew') !== null) parts.push(`skew ${fmtNum(v('skew'), 2)}`);
  if (v('kurtosis') !== null) parts.push(`excess kurtosis ${fmtNum(v('kurtosis'), 2)}`);
  return parts.length ? `${parts.join('; ')}.` : NONE;
}

export function readPersistence(d) {
  if (!d || !d.n_trips) return NONE;
  const lag1 = (d.lags || []).find((l) => l.lag === 1 && isNum(l.r));
  const s = d.recovery && isNum(d.recovery.slope) ? d.recovery.slope : null;
  const parts = [];
  if (lag1) parts.push(`Delay at one stop correlates ${fmtNum(lag1.r, 2)} with the next`);
  if (s !== null) {
    const w = s >= 0.7 ? 'persists: late buses mostly stay late' : s <= 0.3 ? 'recovers: buses mostly recover' : 'partly recovers';
    parts.push(`delay ${w} (slope ${fmtNum(s, 2)})`);
  }
  return parts.length ? `${parts.join('; ')}.` : NONE;
}

export function readVariance(d) {
  if (!d || !d.n) return NONE;
  const fs = (d.factors || []).filter((f) => isNum(f.omega2));
  if (!fs.length) return NONE;
  const top = fs.reduce((a, b) => (b.omega2 > a.omega2 ? b : a));
  return `${FACTOR[top.factor] || top.factor} explains the most delay variation (${fmtNum(top.omega2 * 100, 0)}%).`;
}
