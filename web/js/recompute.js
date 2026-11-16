// Background recompute of a page's cached endpoints. Pure logic: no DOM, no fetch (the caller injects run/now).
const MIN_ESTIMATE = 500;
const CAP = 0.95;

// Heavy requests are serialised by the server gates, so the times add up. null = unknown.
export function estimateMs(requests) {
  if (!requests.length || requests.some((r) => r.ms == null)) return null;
  return requests.reduce((a, r) => a + r.ms, 0);
}

export function progressFraction(elapsedMs, estimate) {
  if (estimate == null) return null;
  return Math.min(CAP, Math.max(0, elapsedMs) / Math.max(estimate, MIN_ESTIMATE));
}

export function createRecomputer({ paths, run, now, onState }) {
  let st = { phase: 'idle', progress: null, elapsedMs: 0, failed: 0 };
  let ctl = null, t0 = 0, est = null, gen = 0;
  const set = (s) => { st = s; onState?.(st); };

  function start() {
    if (st.phase !== 'idle' && st.phase !== 'failed') return;
    const list = paths();
    if (!list.length) return;
    const mine = ++gen;
    ctl = new AbortController();
    const { signal } = ctl;
    t0 = now();
    est = estimateMs(list);
    set({ phase: 'running', progress: progressFraction(0, est), elapsedMs: 0, failed: 0 });
    Promise.allSettled(list.map((r) => run(r.path, signal))).then((rs) => {
      if (mine !== gen) return; // cancelled or superseded: an abort is not a failure
      const failed = rs.filter((x) => x.status === 'rejected').length;
      set({ phase: failed ? 'failed' : 'done', progress: failed ? st.progress : 1, elapsedMs: now() - t0, failed });
    });
  }
  function cancel() {
    if (st.phase !== 'running') return;
    gen++;
    ctl.abort();
    set({ phase: 'idle', progress: null, elapsedMs: 0, failed: 0 });
  }
  function reset() {
    cancel();
    if (st.phase !== 'idle') set({ phase: 'idle', progress: null, elapsedMs: 0, failed: 0 });
  }
  function tick() {
    if (st.phase !== 'running') return;
    const elapsedMs = now() - t0;
    set({ ...st, elapsedMs, progress: progressFraction(elapsedMs, est) });
  }
  return { start, cancel, reset, tick, state: () => st };
}
