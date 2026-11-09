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


# ---------------------------------------------------------------- BackupScheduler
import asyncio
import logging
import sqlite3
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import backup
from app.config import Settings
from app.db import open_meta, open_samples
from app.main import create_app
from app.scheduler import BackupScheduler


class Stop(BaseException):  # BaseException: must escape the loop's `except Exception`
    pass


class Clock:
    """Fake clock + fake sleep: sleeping advances the clock; raises Stop after `limit` sleeps."""

    def __init__(self, now, limit=3, drift=None):
        self.now, self.limit, self.sleeps, self.drift = now, limit, [], drift

    def __call__(self):
        return self.now

    async def sleep(self, secs):
        self.sleeps.append(secs)
        if len(self.sleeps) > self.limit:
            raise Stop
        self.now += timedelta(seconds=self.drift if self.drift is not None else secs)


def S(tmp_path, **kw):
    kw.setdefault("backup_dir", tmp_path / "bk")
    return Settings("http://x", tmp_path / "data", **kw)


def stamp_name(dt):
    return f"analytics-{dt.astimezone(UTC):%Y%m%dT%H%M%SZ}.tar"


def put_backup(settings, dt, suffix=""):
    settings.backup_path.mkdir(parents=True, exist_ok=True)
    p = settings.backup_path / stamp_name(dt)
    p.write_bytes(b"x")
    return p


NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


class Spy:
    def __init__(self, fail_snapshot=False, fail_compact=False, skip=()):
        self.calls, self.fail_snapshot, self.fail_compact, self.skip = [], fail_snapshot, fail_compact, skip

    def snap(self, data_dir, backup_dir, keep):
        self.calls.append(("snapshot", data_dir, backup_dir, keep))
        if self.fail_snapshot:
            raise OSError("disk full")
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        p = Path(backup_dir) / "analytics-x.tar"
        p.write_bytes(b"x")
        return p

    def compact(self, data_dir, now, skipped=None):
        self.calls.append(("compact", data_dir, now, skipped))
        skipped.extend(self.skip)
        if self.fail_compact:
            raise RuntimeError("boom")
        return [Path("a.gz")]


def sched(tmp_path, spy, clock=None, **kw):
    clock = clock or Clock(NOW)
    return BackupScheduler(S(tmp_path, **kw), now_fn=clock, sleep_fn=clock.sleep,
                           snapshot_fn=spy.snap, compact_fn=spy.compact), clock


async def test_run_once_snapshot_then_compaction_order(tmp_path, caplog):
    spy = Spy(skip=[Path("samples-2020-01.db")])
    sc, _ = sched(tmp_path, spy, backup_keep=4)
    with caplog.at_level(logging.INFO):
        path = await sc.run_once()
    assert [c[0] for c in spy.calls] == ["snapshot", "compact"]
    assert spy.calls[0][1:] == (tmp_path / "data", tmp_path / "bk", 4)
    assert spy.calls[1][2] == int(NOW.timestamp())
    assert path == tmp_path / "bk" / "analytics-x.tar"
    assert any(r.levelno == logging.WARNING and "samples-2020-01.db" in r.message for r in caplog.records)


async def test_run_once_no_skipped_no_warning(tmp_path, caplog):
    sc, _ = sched(tmp_path, Spy())
    with caplog.at_level(logging.INFO):
        await sc.run_once()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_snapshot_failure_skips_compaction_and_loop_continues(tmp_path, caplog):
    spy = Spy(fail_snapshot=True)
    sc, clock = sched(tmp_path, spy, Clock(NOW, limit=3))
    put_backup(sc.settings, NOW)  # recent: no catch-up, only scheduled runs
    with pytest.raises(Stop):
        await sc.run()
    assert [c[0] for c in spy.calls] == ["snapshot"] * 3  # each night retried, never compacted
    assert any(r.exc_info and "snapshot failed" in r.message for r in caplog.records)


async def test_compaction_failure_is_logged_backup_kept(tmp_path, caplog):
    spy = Spy(fail_compact=True)
    sc, _ = sched(tmp_path, spy)
    path = await sc.run_once()
    assert path is not None
    assert any(r.exc_info and "compaction failed" in r.message for r in caplog.records)


async def test_run_survives_unexpected_exception_from_run_once(tmp_path, caplog):
    sc, _ = sched(tmp_path, Spy(), Clock(NOW, limit=3))
    put_backup(sc.settings, NOW)
    n = []

    async def boom():
        n.append(1)
        raise RuntimeError("unexpected")

    sc.run_once = boom
    with pytest.raises(Stop):
        await sc.run()
    assert len(n) == 2 and any("iteration failed" in r.message for r in caplog.records)


async def test_disabled_scheduler_never_runs(tmp_path, caplog):
    spy = Spy()
    sc, clock = sched(tmp_path, spy, backup_enabled=False)
    with caplog.at_level(logging.INFO):
        await sc.run()
    assert spy.calls == [] and clock.sleeps == []
    assert [r for r in caplog.records if "disabled" in r.message]


async def test_scheduled_runs_happen_at_backup_time(tmp_path):
    spy = Spy()
    clock = Clock(d(2026, 6, 10, 12, 0), limit=2)
    sc, _ = sched(tmp_path, spy, clock, backup_time="03:00")
    put_backup(sc.settings, clock.now)
    ran_at = []
    orig = sc.run_once

    async def rec():
        ran_at.append(clock.now.astimezone(DUB))
        return await orig()

    sc.run_once = rec
    with pytest.raises(Stop):
        await sc.run()
    assert [(r.day, r.hour, r.minute) for r in ran_at] == [(11, 3, 0), (12, 3, 0)]
    assert clock.sleeps[0] == 15 * 3600


async def test_catch_up_when_last_backup_older_than_24h(tmp_path):
    spy = Spy()
    sc, clock = sched(tmp_path, spy, Clock(NOW, limit=0))
    put_backup(sc.settings, NOW - timedelta(hours=24, seconds=1))
    with pytest.raises(Stop):
        await sc.run()
    assert [c[0] for c in spy.calls] == ["snapshot", "compact"]  # ran before the first sleep


async def test_no_catch_up_when_recent_backup(tmp_path):
    spy = Spy()
    sc, _ = sched(tmp_path, spy, Clock(NOW, limit=0))
    put_backup(sc.settings, NOW - timedelta(hours=24))  # exactly 24h: not older
    with pytest.raises(Stop):
        await sc.run()
    assert spy.calls == []


async def test_catch_up_ignores_future_dated_backups(tmp_path):
    spy = Spy()
    sc, _ = sched(tmp_path, spy, Clock(NOW, limit=0))
    put_backup(sc.settings, NOW + timedelta(days=30))
    with pytest.raises(Stop):
        await sc.run()
    assert len(spy.calls) == 2


async def test_catch_up_when_no_backups(tmp_path):
    spy = Spy()
    sc, _ = sched(tmp_path, spy, Clock(NOW, limit=0))
    with pytest.raises(Stop):
        await sc.run()
    assert len(spy.calls) == 2


async def test_catch_up_when_backup_dir_unlistable(tmp_path, monkeypatch, caplog):
    spy = Spy()
    sc, _ = sched(tmp_path, spy, Clock(NOW, limit=0))

    def bad(_):
        raise PermissionError("no")

    monkeypatch.setattr(backup, "list_backups", bad)
    with pytest.raises(Stop):
        await sc.run()
    assert len(spy.calls) == 2 and any("cannot list" in r.message for r in caplog.records)


async def test_clock_jump_backwards_does_not_spin(tmp_path):
    spy = Spy()
    # every sleep moves the clock BACK an hour: the loop must still sleep >= 1s and never run early
    clock = Clock(d(2026, 6, 10, 2, 59, 59), limit=5, drift=-3600)
    sc, _ = sched(tmp_path, spy, clock, backup_time="03:00")
    put_backup(sc.settings, clock.now)
    with pytest.raises(Stop):
        await sc.run()
    assert min(clock.sleeps) >= 1.0 and len(clock.sleeps) == 6
    assert spy.calls == []  # never due, never ran early


async def test_sleep_never_below_one_second_when_due_now(tmp_path):
    clock = Clock(d(2026, 6, 10, 2, 59, 59, ), limit=0)
    clock.now += timedelta(microseconds=999_999)
    sc, _ = sched(tmp_path, Spy(), clock, backup_time="03:00")
    put_backup(sc.settings, clock.now)
    with pytest.raises(Stop):
        await sc.run()
    assert clock.sleeps == [1.0]


async def test_run_cancellation_is_clean(tmp_path):
    started = asyncio.Event()

    async def slow(_):
        started.set()
        await asyncio.sleep(3600)

    spy = Spy()
    sc = BackupScheduler(S(tmp_path), now_fn=lambda: NOW, sleep_fn=slow, snapshot_fn=spy.snap,
                         compact_fn=spy.compact)
    put_backup(sc.settings, NOW)
    t = asyncio.create_task(sc.run())
    await started.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert [x for x in asyncio.all_tasks() if x is not asyncio.current_task()] == []


async def test_cancel_during_blocking_snapshot_returns_promptly(tmp_path):
    import threading
    release, entered = threading.Event(), threading.Event()

    def blocking(data_dir, backup_dir, keep):
        entered.set()
        release.wait(5)
        return Path("x")

    sc = BackupScheduler(S(tmp_path), now_fn=lambda: NOW, snapshot_fn=blocking, compact_fn=Spy().compact)
    t = asyncio.create_task(sc.run())  # no backups -> catch-up -> blocks in the worker thread
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t, 2)  # does not wait for the thread
    release.set()


# ------------------------------------------------------------- real snapshot / compaction
def mk_data(data_dir, month=(2020, 1)):
    data_dir.mkdir(parents=True, exist_ok=True)
    c = open_meta(data_dir)
    c.execute("insert into routes_tracked values('15', 1)")
    c.commit()
    c.close()
    ts = int(datetime(*month, 5, tzinfo=UTC).timestamp())
    c = open_samples(data_dir, ts)
    c.execute("insert into sample values(?,?,?,?,?,?,?)", (ts, "t", "s", 1, 3, 100, 1))
    c.commit()
    c.close()


async def test_retention_uses_backup_keep_and_backup_dir(tmp_path):
    mk_data(tmp_path / "data")
    sc = BackupScheduler(S(tmp_path, backup_keep=3))
    paths = [await sc.run_once() for _ in range(6)]
    assert all(p is not None and p.parent == tmp_path / "bk" for p in paths)
    kept = backup.list_backups(tmp_path / "bk")
    assert len(kept) == 3 and kept == paths[-3:]  # creation order (name order equals it, even within a second)
    assert not (tmp_path / "data" / "backups").exists()


async def test_end_to_end_real_run_once_tar_and_gz(tmp_path):
    data = tmp_path / "data"
    mk_data(data)
    clock = Clock(datetime(2026, 6, 10, tzinfo=UTC))  # 2020-01 ended years ago: past the grace
    sc = BackupScheduler(S(tmp_path), now_fn=clock)
    path = await sc.run_once()
    assert path.suffix == ".tar"
    with tarfile.open(path) as t:
        assert sorted(t.getnames()) == ["meta.db", "samples-2020-01.db"]  # snapshot taken BEFORE compaction
    assert (data / "samples-2020-01.db.gz").exists() and not (data / "samples-2020-01.db").exists()
    assert (data / "meta.db").exists()


async def test_end_to_end_month_inside_grace_not_compacted(tmp_path):
    data = tmp_path / "data"
    mk_data(data, (2026, 6))
    sc = BackupScheduler(S(tmp_path), now_fn=Clock(datetime(2026, 7, 2, 23, tzinfo=UTC)))  # < 2 days after
    await sc.run_once()
    assert (data / "samples-2026-06.db").exists()


async def test_backup_dir_unwritable_logged_not_fatal(tmp_path, caplog):
    mk_data(tmp_path / "data")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    sc = BackupScheduler(S(tmp_path, backup_dir=blocker / "sub"), now_fn=Clock(datetime(2026, 6, 10, tzinfo=UTC)))
    assert await sc.run_once() is None
    assert any(r.exc_info and "snapshot failed" in r.message for r in caplog.records)
    assert (tmp_path / "data" / "samples-2020-01.db").exists()  # originals untouched without a backup


async def test_backup_dir_inside_data_dir_not_recursively_backed_up(tmp_path):
    mk_data(tmp_path / "data")
    sc = BackupScheduler(S(tmp_path, backup_dir=tmp_path / "data" / "backups"),
                         now_fn=Clock(datetime(2020, 1, 6, tzinfo=UTC)))
    await sc.run_once()
    p2 = await sc.run_once()
    with tarfile.open(p2) as t:
        assert all("/" not in n and not n.startswith("analytics-") for n in t.getnames())


# ---------------------------------------------------------------- lifespan
def _app(tmp_path, bg, **kw):
    from app.serverclient import ServerClient
    return create_app(S(tmp_path, **kw), start_background=bg, client=ServerClient("http://fake"))


def test_lifespan_starts_and_cancels_scheduler_cleanly(tmp_path, monkeypatch):
    ran = []

    async def fake_run(self):
        ran.append("start")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            ran.append("cancelled")
            raise

    monkeypatch.setattr(BackupScheduler, "run", fake_run)
    app = _app(tmp_path, True)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
    assert ran == ["start", "cancelled"]


def test_lifespan_real_scheduler_disabled_exits_without_error(tmp_path):
    with TestClient(_app(tmp_path, True, backup_enabled=False)) as c:
        assert c.get("/api/health").status_code == 200


def test_lifespan_no_scheduler_without_background(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(BackupScheduler, "run", lambda self: called.append(1))
    with TestClient(_app(tmp_path, False)):
        pass
    assert called == []


# ---------------------------------------------------------------- fix round 1
async def test_loop_survives_now_fn_failure_once(tmp_path, caplog):
    spy = Spy()
    clock = Clock(d(2026, 6, 10, 12), limit=2)
    boom = [True]

    def now():
        if boom[0]:
            boom[0] = False
            raise RuntimeError("clock")
        return clock.now

    sc = BackupScheduler(S(tmp_path), now_fn=now, sleep_fn=clock.sleep, snapshot_fn=spy.snap, compact_fn=spy.compact)
    put_backup(sc.settings, NOW)
    with pytest.raises(Stop):
        await sc.run()
    assert clock.sleeps[0] == 60 and any(r.exc_info for r in caplog.records)
    assert len(spy.calls) == 2  # the backup still happened later


async def test_loop_survives_sleep_fn_failure_once(tmp_path):
    spy = Spy()
    clock = Clock(d(2026, 6, 10, 12), limit=2)
    first = [True]

    async def sleep(secs):
        if first[0]:
            first[0] = False
            raise RuntimeError("sleep")
        await clock.sleep(secs)

    sc = BackupScheduler(S(tmp_path), now_fn=clock, sleep_fn=sleep, snapshot_fn=spy.snap, compact_fn=spy.compact)
    put_backup(sc.settings, clock.now)
    with pytest.raises(Stop):
        await sc.run()
    assert clock.sleeps[0] == 60 and len(spy.calls) == 2


async def test_loop_survives_next_run_failure_once(tmp_path, monkeypatch):
    import app.scheduler as m
    spy = Spy()
    real, first = m.next_run, [True]

    def flaky(now, t):
        if first[0]:
            first[0] = False
            raise RuntimeError("next_run")
        return real(now, t)

    monkeypatch.setattr(m, "next_run", flaky)
    clock = Clock(d(2026, 6, 10, 12), limit=2)
    sc, _ = sched(tmp_path, spy, clock)
    put_backup(sc.settings, clock.now)
    with pytest.raises(Stop):
        await sc.run()
    assert clock.sleeps[0] == 60 and len(spy.calls) == 2


async def test_catch_up_failure_does_not_stop_loop(tmp_path):
    spy = Spy()
    sc, clock = sched(tmp_path, spy, Clock(NOW, limit=1))
    calls = []
    orig = sc._needs_catch_up

    def bad():
        calls.append(1)
        raise RuntimeError("x")

    sc._needs_catch_up = bad
    with pytest.raises(Stop):
        await sc.run()
    assert calls == [1] and clock.sleeps[0] == 60  # catch-up attempted once, not retried every loop


async def test_cancel_inside_error_backoff_propagates(tmp_path):
    started = asyncio.Event()

    async def sleep(_):
        started.set()
        await asyncio.sleep(3600)

    def now():
        raise RuntimeError("x")

    sc = BackupScheduler(S(tmp_path), now_fn=now, sleep_fn=sleep, snapshot_fn=Spy().snap, compact_fn=Spy().compact)
    put_backup(S(tmp_path), NOW)
    t = asyncio.create_task(sc.run())
    await started.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


@pytest.mark.parametrize("mode", ["none", "missing", "empty"])
async def test_unusable_snapshot_result_skips_compaction(tmp_path, mode, caplog):
    spy = Spy()
    empty = tmp_path / "empty.tar"
    empty.write_bytes(b"")
    spy.snap = lambda *a: {"none": None, "missing": tmp_path / "nope.tar", "empty": empty}[mode]
    sc, _ = sched(tmp_path, spy)
    assert await sc.run_once() is None
    assert spy.calls == [] and any(r.levelno == logging.ERROR for r in caplog.records)


async def test_relative_backup_dir_warns_once(tmp_path, caplog):
    sc, _ = sched(tmp_path, Spy(), Clock(NOW, limit=2), backup_dir=Path("rel/bk"))
    put_backup(S(tmp_path, backup_dir=Path("rel/bk")), NOW)  # (cwd-relative; cleaned below)
    try:
        with pytest.raises(Stop):
            await sc.run()
    finally:
        import shutil
        shutil.rmtree("rel", ignore_errors=True)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "BACKUP_DIR" in r.message]
    assert len(warns) == 1 and str(Path("rel/bk").resolve()) in warns[0].message


async def test_absolute_backup_dir_no_warning(tmp_path, caplog):
    sc, _ = sched(tmp_path, Spy(), Clock(NOW, limit=0))
    put_backup(sc.settings, NOW)
    with pytest.raises(Stop):
        await sc.run()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_default_backup_dir_no_warning(tmp_path, caplog):
    sc, _ = sched(tmp_path, Spy(), Clock(NOW, limit=0), backup_dir=None)
    with pytest.raises(Stop):
        await sc.run()
    assert not [r for r in caplog.records if "BACKUP_DIR" in r.message]
