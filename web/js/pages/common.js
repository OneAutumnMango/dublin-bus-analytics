// Helpers shared by the filtered views (route, stop, analytics).
import { controlsForm } from '../controls.js';
import { h, notice } from '../dom.js';
import { buildHash, controlsFromQuery, controlsToQuery, dateTime, plural, toApiQuery } from '../lib.js';

export function setup(ctx) {
  const { controls, errors } = controlsFromQuery(ctx.query);
  return { controls, errors, apiQuery: toApiQuery(controls, ctx.nowSec()), linkQuery: controlsToQuery(controls) };
}

// Filter form + notices about ignored values; applying navigates (the hash holds the state, so views are linkable).
export function filterBar(ctx, name, params, s, extraQuery = {}) {
  return [s.errors.length ? notice(`${s.errors.join(' ')} Defaults were used instead.`) : null,
    controlsForm(s.controls, (c) => ctx.go(buildHash(name, params, { ...extraQuery, ...controlsToQuery(c) })))];
}

export function filterNotices(data) {
  const f = data?.filters || {};
  const out = [];
  if (f.truncated) {
    out.push(notice(`Too many arrivals matched: showing the most recent rows only (the API caps a request at 500,000). Data before ${dateTime(f.since)} (Dublin time) is not included. Narrow the date range for a complete view.`));
  }
  if (data && data.order_source === 'none') {
    out.push(notice('Stop order is unavailable (the bus data server could not be reached), so stops are listed by stop id and not in route order.', 'info'));
  }
  return out;
}

export function windowText(data) {
  const f = data?.filters || {};
  return f.since ? `Arrivals scheduled since ${dateTime(f.since)}${f.until ? ` until ${dateTime(f.until)}` : ''} · ${f.min_confidence ? 'confirmed arrivals only' : 'including unconfirmed arrivals'} · ${plural(data.n, 'arrival')}` : '';
}

export const crumbs = (...links) => h('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' }, links);
