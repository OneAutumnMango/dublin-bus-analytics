from datetime import datetime, timezone
from pathlib import Path

from app.backup.retention import choose_keep


def items(*days):
    return [(Path(d), int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())) for d in days]


def names(s):
    return sorted(p.name for p in s)


def days(start, n):  # n consecutive days from 2026-06-<start>
    return [f"2026-06-{start + i:02d}" for i in range(n)]


def test_keeps_newest_n_only_when_weekly_zero():
    it = items(*days(1, 10))
    assert names(choose_keep(it, 3, 0)) == days(8, 3)


def test_weekly_picks_newest_per_week():
    # 2026-06-01 is a Monday: weeks are 1-7, 8-14 (items 1..14)
    it = items(*days(1, 14))
    # keep 12,13,14 (week 2); weeks walking back: week 2 has unkept 8-11 -> 11; week 1 -> 7
    assert names(choose_keep(it, 3, 2)) == ["2026-06-07", "2026-06-11", "2026-06-12", "2026-06-13", "2026-06-14"]


def test_year_end_iso_week():
    # 2026-12-28..2027-01-03 are all 2026-W53; 2027-01-04 starts 2027-W01; 12-27 is 2026-W52
    it = items("2026-12-27", "2026-12-28", "2026-12-31", "2027-01-03", "2027-01-04")
    assert names(choose_keep(it, 1, 1)) == ["2027-01-03", "2027-01-04"]
    assert names(choose_keep(it, 1, 2)) == ["2026-12-27", "2027-01-03", "2027-01-04"]


def test_weekly_skips_weeks_already_covered_by_daily():
    it = items(*days(1, 14))
    # keep=8 covers all of week 2 (8-14) and 06-07; week 1 is partly kept, so weekly=1 takes the
    # newest unkept item of week 1 (06-06), not another week-2 item
    assert names(choose_keep(it, 8, 1)) == days(6, 9)


def test_fewer_items_than_keep_keeps_all():
    it = items(*days(1, 2))
    assert choose_keep(it, 5, 3) == {p for p, _ in it}


def test_keep_one_weekly_zero_keeps_exactly_newest():
    it = items(*days(1, 5))
    assert choose_keep(it, 1, 0) == {Path("2026-06-05")}
