"""Nightly backup scheduling: snapshot then compaction at BACKUP_TIME (Europe/Dublin local time)."""
import asyncio
import logging
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.backup import backup

TZ = ZoneInfo("Europe/Dublin")
UTC = timezone.utc


def _local_instant(day, hh: int, mm: int) -> datetime:
    """The instant wall-clock hh:mm on `day` in Dublin. Ambiguous (fall-back) -> first occurrence (fold=0);
    nonexistent (spring-forward gap) -> the first instant after the gap."""
    wall = datetime.combine(day, dtime(hh, mm))
    dt = wall.replace(tzinfo=TZ, fold=0)
    inst = dt.astimezone(UTC)
    if inst.astimezone(TZ).replace(tzinfo=None) == wall:
        return dt
    after = wall.replace(tzinfo=TZ, fold=1).utcoffset()  # offset once the gap is over
    lo, hi = wall.replace(tzinfo=TZ, fold=1).astimezone(UTC), inst  # transition lies in [lo, hi]
    while hi - lo > timedelta(seconds=1):  # first instant whose offset is the post-gap one
        mid = lo + (hi - lo) / 2
        lo, hi = (lo, mid) if mid.astimezone(TZ).utcoffset() == after else (mid, hi)
    return hi.astimezone(TZ)


def next_run(now: datetime, backup_time: str) -> datetime:
    """Next occurrence of `backup_time` (HH:MM Europe/Dublin) strictly after `now`; tz-aware, DST-safe."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    hh, mm = int(backup_time[:2]), int(backup_time[3:])
    day = now.astimezone(TZ).date()
    for i in range(3):  # today, tomorrow, and one spare day: always enough
        cand = _local_instant(day + timedelta(days=i), hh, mm)
        if cand.astimezone(UTC) > now.astimezone(UTC):
            return cand
    raise AssertionError("unreachable")  # pragma: no cover


log = logging.getLogger(__name__)
CATCH_UP_AFTER_S = 24 * 3600
RETRY_S = 60
MIN_SLEEP_S = 1.0  # a backwards clock jump must never turn the loop into a spin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BackupScheduler:
    """Runs snapshot then compaction nightly. Both are blocking, so they run in worker threads; cancelling
    run() cancels only the awaiting task: a snapshot already running in its thread finishes in the background
    (it is atomic: temp dir then link), so shutdown never hangs on it."""

    def __init__(self, settings, *, now_fn=_utcnow, sleep_fn=asyncio.sleep,
                 snapshot_fn=backup.snapshot, compact_fn=backup.compact_closed_months):
        self.settings, self.now_fn, self.sleep_fn = settings, now_fn, sleep_fn
        self.snapshot_fn, self.compact_fn = snapshot_fn, compact_fn

    async def run_once(self) -> Path | None:
        """Snapshot, then compact. If the snapshot fails nothing is compacted (never delete originals
        without a fresh backup). A compaction failure keeps the new backup. Never raises (except cancel)."""
        s = self.settings
        try:
            path = await asyncio.to_thread(self.snapshot_fn, s.data_dir, s.backup_path, s.backup_keep)
        except Exception:
            log.exception("backup snapshot failed; compaction skipped")
            return None
        if path is None or not path.is_file() or path.stat().st_size == 0:
            log.error("snapshot returned no usable backup file (%s); compaction skipped", path)
            return None
        log.info("backup written: %s", path)
        skipped: list[Path] = []
        try:
            done = await asyncio.to_thread(
                self.compact_fn, s.data_dir, int(self.now_fn().timestamp()), skipped=skipped)
            log.info("compacted %d month file(s)", len(done))
        except Exception:
            log.exception("compaction failed; backup %s kept", path)
        if skipped:
            log.warning("compaction skipped months: %s", ", ".join(p.name for p in skipped))
        return path

    def _needs_catch_up(self) -> bool:
        now = self.now_fn().timestamp()
        try:
            stamps = [backup._stamp(p) for p in backup.list_backups(self.settings.backup_path)]
        except OSError:
            log.exception("cannot list backups in %s", self.settings.backup_path)
            stamps = []
        valid = [t for t in stamps if t <= now + backup.FUTURE_SLACK_S]  # future-dated files don't count
        return not valid or now - max(valid) > CATCH_UP_AFTER_S

    async def run(self) -> None:
        if not self.settings.backup_enabled:
            log.info("backups disabled (BACKUP_ENABLED=false)")
            return
        d = self.settings.backup_dir
        if d is not None and not d.is_absolute():
            log.warning("BACKUP_DIR %s is relative; it resolves to %s against the working directory "
                        "(use an absolute path, e.g. under /data, in containers)", d, d.resolve())
        caught_up = False
        while True:
            try:  # the whole body is guarded: the scheduler must never silently stop (cancel still propagates)
                if not caught_up:
                    caught_up = True
                    if self._needs_catch_up():
                        log.info("no backup in the last 24h; running catch-up backup")
                        await self.run_once()
                nxt = next_run(self.now_fn(), self.settings.backup_time)
                await self.sleep_fn(max(MIN_SLEEP_S, (nxt - self.now_fn()).total_seconds()))
                if self.now_fn() < nxt:  # clock went backwards while sleeping: not due yet
                    continue
                await self.run_once()
            except Exception:
                log.exception("backup scheduler iteration failed; retrying in %ds", RETRY_S)
                await self.sleep_fn(RETRY_S)
