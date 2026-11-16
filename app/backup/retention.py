"""Backup retention: the newest N backups plus the newest backup of each of the last M ISO weeks."""
from datetime import datetime, timezone
from pathlib import Path


def choose_keep(items: list[tuple[Path, int]], keep: int, weekly: int) -> set[Path]:
    """items: (path, stamp_epoch_s) oldest-first, future-dated already excluded. Returns the paths to keep."""
    kept = {p for p, _ in items[-keep:]} if keep > 0 else set()
    if weekly <= 0 or not items:
        return kept
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isocalendar()[:2]
    newest_week = iso(items[-1][1])  # the newest item's own week never takes a weekly slot
    covered = 0
    seen = {newest_week}
    for p, t in reversed(items):  # newest first, so the first unkept item seen per week is its newest
        if p in kept or iso(t) in seen:
            continue
        if covered == weekly:
            break
        seen.add(iso(t))
        kept.add(p)
        covered += 1
    return kept
