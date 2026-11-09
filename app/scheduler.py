"""Nightly backup scheduling: snapshot then compaction at BACKUP_TIME (Europe/Dublin local time)."""
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

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
