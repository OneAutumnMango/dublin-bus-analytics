// Stats lab view (stub): heading and filter bar; the plots are added by later tasks.
import { h, link, put } from '../dom.js';
import { buildHash } from '../lib.js';
import { crumbs, filterBar, setup } from './common.js';

export async function render(ctx) {
  const { route } = ctx.params;
  const s = setup(ctx);
  ctx.title(`Stats lab · route ${route}`);
  put(ctx.main,
    crumbs(link('#/', '← All routes'), link(buildHash('route', { route }, s.linkQuery), `Route ${route} stops`), link(buildHash('analytics', { route }, s.linkQuery), 'Analytics for this route')),
    h('h1', {}, `Stats lab for route ${route}`),
    h('p', { class: 'subtitle' }, 'Delay = actual minus scheduled: negative = early, positive = late. Statistics use confirmed arrivals unless you change the filter.'),
    ...filterBar(ctx, 'lab', { route }, s));
}
