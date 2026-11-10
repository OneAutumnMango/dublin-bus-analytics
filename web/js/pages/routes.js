// Routes (home) page: welcome text and backups. Route search and tracking live in the persistent sidebar.
import { api, backupAvailable, backupDownloadUrl, isAbort } from '../api.js';
import { h, link, put } from '../dom.js';
import { fmtCount } from '../lib.js';

export async function render(ctx) {
  const { main, signal } = ctx;
  ctx.title('Routes');
  const backupBox = h('div', { class: 'row' });
  const backupMsg = h('span', { role: 'status', class: 'muted' });
  const dlSlot = h('span', {});
  const showDownload = (href) => dlSlot.replaceChildren(link(href, 'Download latest backup', { class: 'btn btn-quiet', download: '' }));
  const noBackup = () => dlSlot.replaceChildren(h('span', { class: 'muted' }, 'No backup yet. Use “Run backup now”.'));
  put(backupBox, dlSlot, h('button', { class: 'btn btn-quiet', type: 'button', on: { click: async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    backupMsg.textContent = 'Creating backup…';
    try {
      const r = await api.runBackup({ signal });
      backupMsg.textContent = `Backup ${r.file} created (${fmtCount(Math.round(r.size / 1024))} KiB).`;
      showDownload(backupDownloadUrl(r.file));
    } catch (e) { if (!isAbort(e)) backupMsg.textContent = `Backup failed: ${e.message}`; } finally { btn.disabled = false; }
  } } }, 'Run backup now'), backupMsg);
  noBackup();
  backupAvailable({ signal }).then((ok) => { if (signal.aborted) return; if (ok === false) noBackup(); else showDownload(backupDownloadUrl()); });

  put(main, h('h1', {}, 'Routes'),
    h('p', { class: 'subtitle' }, 'Use the sidebar to find a route and start tracking it. For every tracked route the analytics service logs each stop, then compares actual with scheduled arrivals. Pick a tracked route to see its analytics.'),
    h('section', { class: 'card', 'aria-labelledby': 'h-backup' }, h('h2', { id: 'h-backup' }, 'Backup'),
      h('p', { class: 'hint' }, 'A backup is a tar of the stored data. Scheduled backups also run daily.'), backupBox));
}
