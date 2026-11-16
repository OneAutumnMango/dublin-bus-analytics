import gzip
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from app.backup import backup, meta_archive
from app.backup.meta_archive import archive_info, archive_meta_months, finished_months
from app.core.db import open_meta

DAY = 86400


def ts(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def make_meta(path, months, per_month=3, with_forecast=True):
    """Create <path>/meta.db with `per_month` arrivals (explicit ids) for each 'YYYY-MM' in months,
    and 2 forecast rows per arrival when with_forecast."""
    c = open_meta(path)
    n = 0
    for mo in months:
        for i in range(per_month):
            n += 1
            c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route)"
                      " values(?,?,?,?,?,?,?,?,?,?,?)", (n * 7, f"t{n}", "s", i, f"{mo}-{i + 10:02d}", 1000 + n, 1010 + n, 10, 3, 1, "r"))
            if with_forecast:
                c.executemany("insert into forecast values(?,?,?,?)", [(n * 7, h, 1000 + n, h) for h in (5, 10)])
    c.commit()
    c.close()


def content_hash(path):
    c = sqlite3.connect(path / "meta.db")
    try:
        h = hashlib.sha256()
        for t in ("arrivals", "forecast"):
            h.update(repr(c.execute(f"select * from {t} order by 1, 2").fetchall()).encode())
        return h.hexdigest()
    finally:
        c.close()


def gz_rows(gz, tmp_path, sql):
    out = tmp_path / "x.db"
    out.write_bytes(gzip.decompress(gz.read_bytes()))
    c = sqlite3.connect(out)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


@pytest.fixture
def dirs(tmp_path):
    d, a = tmp_path / "data", tmp_path / "arch"
    d.mkdir()
    a.mkdir()
    return d, a


def test_finished_months_respects_grace(dirs):
    d, _ = dirs
    make_meta(d, ["2026-09", "2026-10"])
    m = d / "meta.db"
    assert finished_months(m, ts(2026, 10, 1) + DAY) == []
    assert finished_months(m, ts(2026, 10, 1) + 2 * DAY) == ["2026-09"]
    assert finished_months(m, ts(2026, 11, 1) + 2 * DAY) == ["2026-09", "2026-10"]


def test_finished_months_december(dirs):
    d, _ = dirs
    make_meta(d, ["2025-12"])
    assert finished_months(d / "meta.db", ts(2026, 1, 1) + 2 * DAY - 1) == []
    assert finished_months(d / "meta.db", ts(2026, 1, 1) + 2 * DAY) == ["2025-12"]


def test_archive_written_once_with_same_ids(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-10"])
    now = ts(2026, 10, 1) + 2 * DAY
    assert archive_meta_months(d, a, now) == [a / "meta-2026-09.db.gz"]
    gz = a / "meta-2026-09.db.gz"
    before = gz.read_bytes()
    live = sqlite3.connect(d / "meta.db")
    exp_arr = live.execute("select * from arrivals where service_date like '2026-09-%' order by id").fetchall()
    exp_fc = live.execute("select * from forecast where arrival_id in (select id from arrivals where service_date like '2026-09-%') order by 1,2").fetchall()
    live.close()
    assert gz_rows(gz, tmp_path, "select * from arrivals order by id") == exp_arr
    assert gz_rows(gz, tmp_path, "select * from forecast order by 1,2") == exp_fc
    assert gz_rows(gz, tmp_path, "select month, arrivals, forecast, max_id from info") == [("2026-09", 3, 6, max(r[0] for r in exp_arr))]
    assert archive_info(gz) == {"month": "2026-09", "arrivals": 3, "forecast": 6, "max_id": max(r[0] for r in exp_arr)}
    assert archive_meta_months(d, a, now) == []
    assert gz.read_bytes() == before
    assert sorted(p.name for p in a.iterdir()) == ["meta-2026-09.db.gz"]


def test_archive_info_unreadable(tmp_path):
    bad = tmp_path / "meta-2026-09.db.gz"
    bad.write_bytes(b"junk")
    assert archive_info(bad) is None
    assert archive_info(tmp_path / "nope.db.gz") is None


def test_archive_never_overwrites(dirs, caplog):
    d, a = dirs
    make_meta(d, ["2026-09"])
    gz = a / "meta-2026-09.db.gz"
    gz.write_bytes(b"not a gzip")
    with caplog.at_level(logging.WARNING):
        assert archive_meta_months(d, a, ts(2026, 11, 1)) == []
    assert gz.read_bytes() == b"not a gzip"
    assert any("meta-2026-09.db.gz" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_archive_skips_empty_month_and_failures_do_not_raise(dirs, monkeypatch, caplog):
    d, a = dirs
    make_meta(d, ["2026-09"], per_month=0)
    assert archive_meta_months(d, a, ts(2026, 11, 1)) == []
    assert list(a.iterdir()) == []
    make_meta(d, ["2026-09"])

    def boom(*args, **kw):
        raise OSError("boom")

    monkeypatch.setattr(meta_archive.gzip, "GzipFile", boom)
    with caplog.at_level(logging.ERROR):
        assert archive_meta_months(d, a, ts(2026, 11, 1)) == []
    assert list(a.iterdir()) == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    monkeypatch.undo()
    monkeypatch.setattr(meta_archive.os, "replace", boom)
    assert archive_meta_months(d, a, ts(2026, 11, 1)) == []
    assert list(a.iterdir()) == []


def test_archive_leaves_live_db_unchanged(dirs):
    d, a = dirs
    make_meta(d, ["2026-08", "2026-09", "2026-10"])
    h = content_hash(d)
    c = sqlite3.connect(d / "meta.db")
    counts = [c.execute(f"select count(*) from {t}").fetchone() for t in ("arrivals", "forecast")]
    c.close()
    assert len(archive_meta_months(d, a, ts(2026, 11, 1) + 2 * DAY)) == 3
    c = sqlite3.connect(d / "meta.db")
    assert counts == [c.execute(f"select count(*) from {t}").fetchone() for t in ("arrivals", "forecast")]
    c.close()
    assert content_hash(d) == h


def test_stale_temp_files_cleaned(dirs):
    import os
    d, a = dirs
    make_meta(d, ["2026-09"])
    old = []
    for n in ("meta-2026-01.db.tmp", "meta-2026-01.db.gz.tmp"):
        (a / n).write_bytes(b"x")
        os.utime(a / n, (1, 1))
        old.append(a / n)
    backup.snapshot(d, d / "bk", archive_dir=a, now=ts(2026, 11, 1))
    assert not any(p.exists() for p in old)


def test_snapshot_writes_meta_archive_but_not_when_archive_dir_none(dirs):
    d, a = dirs
    make_meta(d, ["2026-09"])
    backup.snapshot(d, d / "bk", archive_dir=None, now=ts(2026, 11, 1))
    assert list(a.iterdir()) == []
    backup.snapshot(d, d / "bk", archive_dir=a, now=ts(2026, 11, 1))
    assert (a / "meta-2026-09.db.gz").exists()


def test_gz_fully_written_before_its_fsync(dirs, monkeypatch):
    import os
    d, a = dirs
    make_meta(d, ["2026-09"])
    seen = []
    real = os.fsync

    def spy(fd):
        try:
            name = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            name = ""
        if name.endswith(".db.gz.tmp"):
            seen.append(os.fstat(fd).st_size)
        return real(fd)

    monkeypatch.setattr(meta_archive.os, "fsync", spy)
    archive_meta_months(d, a, ts(2026, 11, 1))
    assert seen and seen[-1] == (a / "meta-2026-09.db.gz").stat().st_size


@pytest.mark.parametrize("target,val", [("_verify_gz", OSError("bad gz")), ("_integrity", [("corrupt",)])])
def test_failed_verification_leaves_nothing(dirs, monkeypatch, target, val):
    d, a = dirs
    make_meta(d, ["2026-09"])

    def fake(*args, **kw):
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(meta_archive, target, fake)
    assert archive_meta_months(d, a, ts(2026, 11, 1)) == []
    assert list(a.iterdir()) == []


def test_archive_consistent_while_writer_runs(dirs, tmp_path):
    import threading
    import time
    d, a = dirs
    make_meta(d, ["2026-09"])
    stop = threading.Event()
    errs = []

    def writer():
        c = open_meta(d)
        c.execute("pragma busy_timeout=5000")
        n = 10_000
        try:
            while not stop.is_set():
                n += 1
                c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date) values(?,?,?,?,?)", (n, f"w{n}", "s", 0, "2026-09-20"))
                c.executemany("insert into forecast values(?,?,?,?)", [(n, h, 1, 1) for h in (5, 10)])
                c.commit()
        except Exception as e:  # pragma: no cover
            errs.append(e)
        finally:
            c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(0.2)
        for _ in range(3):
            for p in a.iterdir():
                p.unlink()
            archive_meta_months(d, a, ts(2026, 11, 1))
            gz = a / "meta-2026-09.db.gz"
            assert gz.exists()
            info = archive_info(gz)
            assert gz_rows(gz, tmp_path, "select count(*), max(id) from arrivals") == [(info["arrivals"], info["max_id"])]
            assert gz_rows(gz, tmp_path, "select count(*) from forecast") == [(info["forecast"],)]
            assert gz_rows(gz, tmp_path, "select count(*) from forecast where arrival_id not in (select id from arrivals)") == [(0,)]
    finally:
        stop.set()
        t.join()
    assert not errs


def test_null_and_garbage_service_dates_and_columns(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09"], with_forecast=False)
    c = open_meta(d)
    c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date) values(900, 'n', 's', 1, NULL)")
    c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date) values(901, 'g', 's', 1, 'garbage')")
    c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date) values(902, 'x', NULL, NULL, '2026-09-30')")
    c.commit()
    c.close()
    assert archive_meta_months(d, a, ts(2026, 11, 1)) == [a / "meta-2026-09.db.gz"]
    gz = a / "meta-2026-09.db.gz"
    assert gz_rows(gz, tmp_path, "select id, stop_id, seq, scheduled, route from arrivals where id=902") == [(902, None, None, None, None)]
    assert archive_info(gz)["arrivals"] == 4
    assert gz_rows(gz, tmp_path, "select count(*) from arrivals where id in (900, 901)") == [(0,)]


def test_missing_or_bad_meta_db_returns_empty(dirs, caplog):
    d, a = dirs
    with caplog.at_level(logging.ERROR):
        assert archive_meta_months(d, a, ts(2026, 11, 1)) == []
    (d / "meta.db").write_bytes(b"this is not a database" * 100)
    with caplog.at_level(logging.ERROR):
        assert archive_meta_months(d, a, ts(2026, 11, 1)) == []
    assert list(a.iterdir()) == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)
