// Routes (home) page: welcome text and backups. Route search and tracking live in the persistent sidebar.
import { ApiError, api, backupAvailable, backupDownloadUrl, exportUrl, isAbort } from '../api.js';
import { h, link, put } from '../dom.js';
import { fmtCount } from '../lib.js';

function todayDublin() {
  try { return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Dublin' }).format(new Date()); } catch {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
}

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

  const routeSel = h('select', { id: 'export-route', on: { change: () => update() } }, h('option', { value: '' }, 'All routes'));
  const gzBox = h('input', { type: 'checkbox', id: 'export-gz', on: { change: () => update() } });
  const exportLink = link(exportUrl(), 'Download JSON', { class: 'btn btn-quiet', download: '' });
  const update = () => exportLink.setAttribute('href', exportUrl({ route: routeSel.value, gzip: gzBox.checked }));
  const daySel = h('select', { id: 'day-route', on: { change: () => reset() } }, h('option', { value: '' }, 'All routes'));
  api.tracked({ signal }).then((r) => {
    if (signal.aborted) return;
    for (const name of r.routes || []) { routeSel.append(h('option', { value: name }, name)); daySel.append(h('option', { value: name }, name)); }
  }).catch(() => { /* keep just "All routes" */ });

  // Remove a day (or range) of data: preview first, the removal is tied to the exact dates + route that were previewed.
  // The server removes in the background; we poll the job and show progress.
  const dateIn = h('input', { type: 'date', id: 'day-date', value: todayDublin(), on: { change: () => reset(), input: () => reset() } });
  const endIn = h('input', { type: 'date', id: 'day-end', on: { change: () => reset(), input: () => reset() } });
  const dayStatus = h('div', { id: 'day-status', role: 'status', class: 'muted' });
  const rmSlot = h('span', {});
  const previewBtn = h('button', { class: 'btn btn-quiet', type: 'button', on: { click: () => preview() } }, 'Preview');
  const pollMs = ctx.pollMs ?? 1000;
  let token = 0; // bumped on every change/preview/remove so late answers are dropped
  const say = (...lines) => dayStatus.replaceChildren(...lines.filter(Boolean).map((l) => (typeof l === 'string' ? h('p', {}, l) : l)));
  const counts = (r) => `${fmtCount(r.arrivals)} arrivals, ${fmtCount(r.forecasts)} forecasts, ${fmtCount(r.samples)} samples, ${fmtCount(r.gaps)} gaps`;
  const sum = (r) => (r ? (r.arrivals || 0) + (r.forecasts || 0) + (r.samples || 0) + (r.gaps || 0) : 0);
  const liveNote = (date) => (date >= todayDublin() ? 'This day is still being recorded. Buses in flight right now may end up with incomplete forecast data and the day may not be completely empty afterwards. Removing a finished day is safest.' : '');
  const skipNote = (r) => (r.skipped_months?.length ? `Archived (gzipped) months cannot be edited and will be skipped: ${r.skipped_months.join(', ')}. Samples there stay.` : '');
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  let following = false; // true while a removal job is being polled
  function reset() {
    const was = following;
    following = false;
    token++; rmSlot.replaceChildren(); dayStatus.replaceChildren(); previewBtn.disabled = false;
    if (was) say('A removal is still running in the background; press Remove again after Preview to follow it.');
  }
  async function preview() {
    const date = dateIn.value, end = endIn.value, route = daySel.value;
    if (!date) { say('Choose a date first.'); return; }
    if (end && end < date) { say('The end date must not be before the start date.'); return; }
    const my = ++token;
    rmSlot.replaceChildren();
    say('Checking…');
    try {
      const r = await api.dayPreview({ date, end, route }, { signal });
      if (signal.aborted || my !== token) return;
      const last = end || date;
      if (!sum(r)) { say(end ? 'Nothing recorded for those days.' : 'Nothing recorded for that day.', skipNote(r)); return; }
      const days = r.days || [];
      const list = days.length > 1 ? h('ul', { class: 'day-list' }, days.map((d) => h('li', {}, `${d.date}: ${counts(d)}`))) : null;
      const span = end && end !== date ? `${date} to ${end}` : date;
      say(`Would remove ${counts(r)} for ${span}${route ? ` (route ${route})` : ''}.`, list, skipNote(r), liveNote(last));
      const label = `Remove these rows (${fmtCount(r.arrivals)} arrivals${days.length > 1 ? `, ${days.length} days` : ''})`;
      rmSlot.replaceChildren(h('button', { class: 'btn btn-danger solid', type: 'button', 'data-label': label, on: { click: (ev) => remove({ date, end, route }, ev.currentTarget) } }, label));
    } catch (e) { if (!isAbort(e) && !signal.aborted && my === token) say(e.message); }
  }
  function showProgress(r) {
    const done = sum(r.done), total = sum(r.total);
    const props = { id: 'day-progress', max: total || 1 };
    if (total) props.value = done;
    const dayNo = Math.min((r.day_index || 0) + 1, r.day_count || 1);
    const where = r.day_count ? `day ${dayNo} of ${r.day_count}${r.current_day ? ` (${r.current_day})` : ''}: ` : '';
    say(h('p', {}, h('progress', props), ` ${where}${fmtCount(r.done?.samples || 0)} samples removed…`));
  }
  // Polls a removal job until it finishes, fails, is lost, or this view is superseded.
  async function follow(jobId, my, last, finish) {
    let fails = 0;
    following = true;
    const stop = () => { following = false; finish(); };
    for (;;) {
      let r;
      try { r = await api.removeJob(jobId, { signal }); fails = 0; } catch (e) {
        if (isAbort(e) || signal.aborted || my !== token) return;
        if (e instanceof ApiError && e.status === 404) { stop(); say('Status lost; run Preview to check. Running the removal again finishes it.'); return; }
        if (e instanceof ApiError && (e.kind === 'network' || e.kind === 'timeout') && ++fails <= 3) {
          await sleep(pollMs);
          if (signal.aborted || my !== token) return;
          continue;
        }
        stop(); say(`${e.message}. The removal may still be running on the server; press Remove again to re-attach to it, or run Preview to check what is left.`); return;
      }
      if (signal.aborted || my !== token) return;
      if (r.state === 'done') { rmSlot.replaceChildren(); stop(); say(`Removed ${counts(r.done || {})}.`, skipNote(r), liveNote(last)); return; }
      if (r.state === 'error') {
        stop();
        say(r.error || 'The removal failed.', sum(r.done) ? `Part of the day was removed: ${counts({ arrivals: 0, forecasts: 0, samples: 0, gaps: 0, ...r.done })}; try again.` : '');
        return;
      }
      showProgress(r);
      await sleep(pollMs);
      if (signal.aborted || my !== token) return;
    }
  }
  async function remove({ date, end, route }, rmBtn) {
    const my = ++token;
    const label = rmBtn?.getAttribute('data-label') || '';
    const busy = (v) => {
      previewBtn.disabled = v;
      if (!rmBtn) return;
      rmBtn.disabled = v;
      rmBtn.setAttribute('class', v ? 'btn btn-danger solid busy' : 'btn btn-danger solid');
      if (v) { rmBtn.setAttribute('aria-busy', 'true'); rmBtn.textContent = 'Removing…'; } else { rmBtn.removeAttribute('aria-busy'); rmBtn.textContent = label; }
    };
    const finish = () => { following = false; if (!signal.aborted) busy(false); };
    busy(true);
    say('Starting…');
    try {
      let jobId = null;
      try {
        jobId = (await api.removeDay({ date, end, route }, { signal })).job_id;
      } catch (e) {
        if (isAbort(e) || signal.aborted || my !== token) return;
        if (e instanceof ApiError && e.status === 409 && e.body?.job_id) jobId = e.body.job_id;
        else { finish(); say(e instanceof ApiError ? e.message : `Unexpected error: ${e.message}`); return; }
      }
      if (signal.aborted || my !== token) return;
      await follow(jobId, my, end || date, finish);
    } catch (e) {
      if (isAbort(e) || signal.aborted || my !== token) return;
      try { finish(); say(`Unexpected error: ${e?.message ?? e}`); } catch { /* nothing more to do */ }
    }
  }

  put(main, h('h1', {}, 'Routes'),
    h('p', { class: 'subtitle' }, 'Use the sidebar to find a route and start tracking it. For every tracked route the analytics service logs each stop, then compares actual with scheduled arrivals. Pick a tracked route to see its analytics.'),
    h('section', { class: 'card', 'aria-labelledby': 'h-backup' }, h('h2', { id: 'h-backup' }, 'Backup'),
      h('p', { class: 'hint' }, 'A backup is a tar of the stored data. Scheduled backups also run daily.'), backupBox,
      h('h3', {}, 'Export as JSON'),
      h('p', { class: 'hint' }, 'A readable copy of arrivals, forecasts and samples. Choose one route or all, then download.'),
      h('div', { class: 'row' }, h('label', { for: 'export-route' }, 'Route'), routeSel,
        h('label', { for: 'export-gz', class: 'inline' }, gzBox, ' compressed (.gz)'), exportLink),
      h('h3', {}, 'Remove a day of data'),
      h('p', { class: 'hint' }, 'Permanently deletes the arrivals, forecasts, samples and gaps recorded on one Europe/Dublin day, or on a range of days (leave To empty for a single day; up to 62 days). There is no undo and no automatic backup: run a backup first. Preview shows what would go; nothing is deleted until you press the remove button. Removal runs in the background and progress is shown here.'),
      h('div', { class: 'row' }, h('label', { for: 'day-date' }, 'From'), dateIn, h('label', { for: 'day-end' }, 'To (optional)'), endIn, h('label', { for: 'day-route' }, 'Route'), daySel, previewBtn, rmSlot),
      dayStatus));
}
