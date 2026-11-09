from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.scheduler import next_run

DUB = ZoneInfo("Europe/Dublin")
UTC = timezone.utc


def d(y, mo, da, h=0, mi=0, s=0):
    return datetime(y, mo, da, h, mi, s, tzinfo=DUB)


def test_next_run_later_today_when_before_time():
    assert next_run(d(2026, 6, 10, 2, 59, 59), "03:00") == d(2026, 6, 10, 3, 0)


def test_next_run_tomorrow_when_after_time():
    assert next_run(d(2026, 6, 10, 3, 0, 1), "03:00") == d(2026, 6, 11, 3, 0)


def test_next_run_exactly_at_time_is_tomorrow():
    assert next_run(d(2026, 6, 10, 3, 0), "03:00") == d(2026, 6, 11, 3, 0)


def test_next_run_accepts_any_timezone_and_returns_dublin_aware():
    r = next_run(datetime(2026, 6, 10, 1, 0, tzinfo=UTC), "03:00")  # 02:00 IST
    assert r == d(2026, 6, 10, 3, 0) and r.tzinfo is not None and r.utcoffset() == timedelta(hours=1)


def test_next_run_naive_rejected():
    with pytest.raises(ValueError):
        next_run(datetime(2026, 6, 10), "03:00")


def test_next_run_midnight_and_year_end():
    assert next_run(d(2026, 12, 31, 23, 59), "00:00") == d(2027, 1, 1, 0, 0)


def test_spring_forward_nonexistent_time_runs_at_first_instant_after_gap():
    # 2026-03-29: 01:00 GMT -> 02:00 IST, so 01:30 local does not exist
    r = next_run(d(2026, 3, 28, 2, 0), "01:30")
    assert r.astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert (r.hour, r.minute, r.utcoffset()) == (2, 0, timedelta(hours=1))
    # and it is not repeated that same day, the next run is the next day at 01:30 IST
    assert next_run(r, "01:30") == d(2026, 3, 30, 1, 30)


def test_spring_forward_existing_time_unaffected():
    assert next_run(d(2026, 3, 28, 12), "02:30") == d(2026, 3, 29, 2, 30)


def test_fall_back_ambiguous_time_runs_once_first_occurrence():
    # 2026-10-25: 02:00 IST -> 01:00 GMT, so 01:30 occurs twice
    r = next_run(d(2026, 10, 24, 2, 0), "01:30")
    assert r.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # first (IST) occurrence
    r2 = next_run(r, "01:30")  # the second 01:30 (GMT) must NOT fire
    assert r2.astimezone(UTC) == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)


def test_fall_back_now_in_second_occurrence_goes_to_next_day():
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 01:30 GMT, the repeated hour
    assert next_run(second, "01:30").astimezone(UTC) == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)
    # a target inside the repeated hour whose first pass is already over also moves to tomorrow
    assert next_run(second, "01:45").astimezone(UTC) == datetime(2026, 10, 26, 1, 45, tzinfo=UTC)


@pytest.mark.parametrize("t", ["00:30", "01:00", "01:30", "02:00", "02:30", "03:00"])
@pytest.mark.parametrize("start", [datetime(2026, 3, 25, 12, tzinfo=UTC), datetime(2026, 10, 21, 12, tzinfo=UTC)])
def test_consecutive_days_across_transitions_never_skip_or_double(start, t):
    now, runs = start, []
    for _ in range(10):
        now = next_run(now, t)
        runs.append(now.astimezone(DUB))
    days = [r.date() for r in runs]
    assert len(set(days)) == 10 and days == sorted(days)
    assert [(b.date() - a.date()).days for a, b in zip(runs, runs[1:])] == [1] * 9
    for a, b in zip(runs, runs[1:]):
        assert timedelta(hours=22) <= b.astimezone(UTC) - a.astimezone(UTC) <= timedelta(hours=26)


@given(
    now=st.datetimes(datetime(2024, 1, 1), datetime(2028, 12, 31), timezones=st.sampled_from([UTC, DUB, ZoneInfo("Asia/Tokyo")])),
    h=st.integers(0, 23), m=st.integers(0, 59),
)
@hsettings(max_examples=300, deadline=None)
def test_next_run_property(now, h, m):
    t = f"{h:02d}:{m:02d}"
    r = next_run(now, t)
    delta = r.astimezone(UTC) - now.astimezone(UTC)
    assert delta > timedelta(0)
    assert delta <= timedelta(hours=26)
    local = r.astimezone(DUB)
    if (local.hour, local.minute) != (h, m):  # only legal in the spring-forward gap: lands on the post-gap hour
        assert (local.hour, local.minute) == (h + 1 if m else h, 0) or (local.hour, local.minute) == (2, 0)
        assert local.month == 3 and local.day >= 25
    # nothing earlier qualifies: running again from r gives a later day
    assert next_run(r, t).astimezone(UTC) > r.astimezone(UTC)
