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


# ---- slim copy for the nightly tar ----
import shutil
import tarfile

NOW = ts(2026, 11, 20)  # Sep, Oct finished (+ Aug, Jul); Nov current


def staged(d, tmp_path):
    copy = tmp_path / "copy.db"
    backup._copy_db(d / "meta.db", copy)
    return copy


def rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def months_in(db):
    return [r[0] for r in rows(db, "select distinct substr(service_date,1,7) from arrivals order by 1")]


def test_slim_removes_only_verified_months(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-06", "2026-07", "2026-08", "2026-09", "2026-11"])
    archive_meta_months(d, a, NOW)
    (a / "meta-2026-06.db.gz").unlink()  # finished but unarchived
    copy = staged(d, tmp_path)
    assert meta_archive.slim_meta_copy(copy, a, NOW) == ["2026-07", "2026-08", "2026-09"]
    assert months_in(copy) == ["2026-06", "2026-11"]
    assert rows(copy, "select * from archived_months order by 1") == [("2026-07", 3, 6), ("2026-08", 3, 6), ("2026-09", 3, 6)]
    assert rows(copy, "select count(*) from forecast")[0][0] == 12  # 06 + 11 intact
    assert len(rows(d / "meta.db", "select 1 from arrivals")) == 15


def test_slim_keeps_month_on_count_mismatch(dirs, tmp_path, caplog):
    d, a = dirs
    make_meta(d, ["2026-09"])
    archive_meta_months(d, a, NOW)
    copy = staged(d, tmp_path)
    c = sqlite3.connect(copy)
    c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route)"
              " values(9999,'late','s',0,'2026-09-30',1,2,1,3,1,'r')")
    c.commit()
    c.close()
    with caplog.at_level(logging.WARNING):
        assert meta_archive.slim_meta_copy(copy, a, NOW) == []
    assert months_in(copy) == ["2026-09"] and "2026-09" in caplog.text


def test_slim_keeps_month_on_max_id_mismatch(dirs, tmp_path, caplog):
    d, a = dirs
    make_meta(d, ["2026-09"])
    archive_meta_months(d, a, NOW)
    copy = staged(d, tmp_path)
    c = sqlite3.connect(copy)  # same count, different id
    c.execute("update arrivals set id = id + 1000000 where id = (select max(id) from arrivals)")
    c.execute("update forecast set arrival_id = arrival_id + 1000000 where arrival_id = (select max(arrival_id) from forecast)")
    c.commit()
    c.close()
    with caplog.at_level(logging.WARNING):
        assert meta_archive.slim_meta_copy(copy, a, NOW) == []
    assert months_in(copy) == ["2026-09"] and "max_id" in caplog.text


def test_slim_keeps_month_when_archive_corrupt_or_missing(dirs, tmp_path, caplog):
    d, a = dirs
    make_meta(d, ["2026-08", "2026-09", "2026-10"])
    archive_meta_months(d, a, ts(2026, 12, 5))
    g = a / "meta-2026-08.db.gz"
    g.write_bytes(g.read_bytes()[:50])
    (a / "meta-2026-09.db.gz").unlink()
    copy = staged(d, tmp_path)
    with caplog.at_level(logging.WARNING):
        assert meta_archive.slim_meta_copy(copy, a, ts(2026, 12, 5)) == ["2026-10"]
    assert months_in(copy) == ["2026-08", "2026-09"]
    assert "2026-08" in caplog.text and "2026-09" in caplog.text


def _fill_cache(db):
    c = sqlite3.connect(db)
    cols = [r[1] for r in c.execute("pragma table_info(result_cache)")]
    c.execute(f"insert into result_cache({','.join(cols)}) values ({','.join('?' * len(cols))})", ["k" if i == 0 else 1 for i in range(len(cols))])
    c.commit()
    c.close()


def test_slim_archive_dir_none_changes_only_result_cache(dirs, tmp_path):
    d, _ = dirs
    make_meta(d, ["2026-09"])
    _fill_cache(d / "meta.db")
    copy = staged(d, tmp_path)
    before = rows(copy, "select count(*) from arrivals")
    assert meta_archive.slim_meta_copy(copy, None, NOW) == []
    assert rows(copy, "select count(*) from arrivals") == before
    assert not rows(copy, "select name from sqlite_master where name='archived_months'")
    assert rows(copy, "select count(*) from result_cache")[0][0] == 0


def test_slim_empties_result_cache(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09"])
    _fill_cache(d / "meta.db")
    copy = staged(d, tmp_path)
    assert rows(copy, "select count(*) from result_cache")[0][0] == 1
    meta_archive.slim_meta_copy(copy, None, NOW)
    assert rows(copy, "select count(*) from result_cache")[0][0] == 0
    assert rows(d / "meta.db", "select count(*) from result_cache")[0][0] == 1


def test_slim_handles_copy_without_result_cache_table(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09"])
    archive_meta_months(d, a, NOW)
    copy = staged(d, tmp_path)
    c = sqlite3.connect(copy)
    c.execute("drop table result_cache")
    c.commit()
    c.close()
    assert meta_archive.slim_meta_copy(copy, a, NOW) == ["2026-09"]


def test_slim_is_transactional_per_month(dirs, tmp_path, monkeypatch):
    d, a = dirs
    make_meta(d, ["2026-08", "2026-09"])
    archive_meta_months(d, a, NOW)
    copy = staged(d, tmp_path)
    real = meta_archive._slim_month
    calls = []

    def boom(c, mo, info):
        calls.append(mo)
        if len(calls) == 2:
            c.execute("delete from forecast where arrival_id in (select id from arrivals where substr(service_date,1,7)=?)", (mo,))
            raise RuntimeError("boom")
        real(c, mo, info)

    monkeypatch.setattr(meta_archive, "_slim_month", boom)
    with pytest.raises(RuntimeError):
        meta_archive.slim_meta_copy(copy, a, NOW)
    assert months_in(copy) == ["2026-09"]
    assert rows(copy, "select month from archived_months") == [("2026-08",)]
    assert rows(copy, "select count(*) from forecast")[0][0] == 6


def tar_meta(bk, tmp_path):
    t = sorted(bk.glob("analytics-*.tar.gz"))[-1]
    out = tmp_path / ("x" + t.name)
    out.mkdir()
    with tarfile.open(t) as tf:
        tf.extract("meta.db", out)
    return out / "meta.db"


def test_snapshot_tar_meta_is_slim_and_live_db_untouched(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-08", "2026-09", "2026-11"])
    live = content_hash(d)
    backup.snapshot(d, d / "bk", archive_dir=a, now=NOW)
    m = tar_meta(d / "bk", tmp_path)
    assert months_in(m) == ["2026-11"]
    assert rows(m, "select * from archived_months order by 1") == [("2026-08", 3, 6), ("2026-09", 3, 6)]
    assert content_hash(d) == live


def test_same_snapshot_archives_and_slims_new_finished_month(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-10"])
    backup.snapshot(d, d / "bk", archive_dir=a, now=ts(2026, 11, 2))  # grace not over
    assert months_in(tar_meta(d / "bk", tmp_path)) == ["2026-10"]
    backup.snapshot(d, d / "bk", archive_dir=a, now=ts(2026, 11, 3))
    assert (a / "meta-2026-10.db.gz").exists()
    assert months_in(tar_meta(d / "bk", tmp_path)) == []


def test_second_snapshot_idempotent(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])
    backup.snapshot(d, d / "bk", archive_dir=a, now=NOW)
    g = a / "meta-2026-09.db.gz"
    st = g.stat()
    backup.snapshot(d, d / "bk", archive_dir=a, now=NOW)
    assert (g.stat().st_mtime_ns, g.stat().st_size) == (st.st_mtime_ns, st.st_size)
    assert months_in(tar_meta(d / "bk", tmp_path)) == ["2026-11"]


def test_archive_disabled_snapshot_keeps_full_meta(dirs, tmp_path):
    d, _ = dirs
    make_meta(d, ["2026-09", "2026-11"])
    backup.snapshot(d, d / "bk", archive_dir=None, now=NOW)
    m = tar_meta(d / "bk", tmp_path)
    assert months_in(m) == ["2026-09", "2026-11"]
    assert not rows(m, "select name from sqlite_master where name='archived_months'")


def tar_names(bk):
    with tarfile.open(sorted(bk.glob("analytics-*.tar.gz"))[-1]) as tf:
        return tf.getnames()


def test_slim_failure_falls_back_to_full_copy(dirs, tmp_path, monkeypatch):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])

    def boom(copy, *_):
        c = sqlite3.connect(copy)  # closed, so the damage reaches the main file
        c.execute("drop table forecast")
        c.commit()
        c.close()
        raise RuntimeError("boom")

    monkeypatch.setattr(meta_archive, "slim_meta_copy", boom)
    backup.snapshot(d, d / "bk", archive_dir=a, now=NOW)
    m = tar_meta(d / "bk", tmp_path)
    assert months_in(m) == ["2026-09", "2026-11"]
    assert rows(m, "select count(*) from forecast")[0][0] == 12
    assert sorted(n for n in tar_names(d / "bk") if n.startswith("meta.db")) == ["meta.db"]


def test_slim_vacuum_failure_falls_back_to_full_copy(dirs, tmp_path, monkeypatch):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])

    def bad(c):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(meta_archive, "_vacuum", bad)
    backup.snapshot(d, d / "bk", archive_dir=a, now=NOW)
    m = tar_meta(d / "bk", tmp_path)
    assert months_in(m) == ["2026-09", "2026-11"]
    assert not rows(m, "select name from sqlite_master where name='archived_months'")
    assert rows(m, "select count(*) from forecast")[0][0] == 12


def _tamper(tmp_path, sql):
    """Slim a 3-arrival month after running `sql` on the copy; returns (removed, copy)."""
    d, a = tmp_path / "data", tmp_path / "arch"
    d.mkdir(), a.mkdir()
    make_meta(d, ["2026-09"], with_forecast=False)
    c = sqlite3.connect(d / "meta.db")  # ids 7,14,21; forecast only for 14 and 21 (max id 21 stays)
    c.executemany("insert into forecast values(?,?,?,?)", [(i, h, 1, h) for i in (14, 21) for h in (5, 10)])
    c.commit()
    c.close()
    archive_meta_months(d, a, NOW)
    copy = staged(d, tmp_path)
    c = sqlite3.connect(copy)
    c.executescript(sql)
    c.commit()
    c.close()
    return meta_archive.slim_meta_copy(copy, a, NOW), copy


@pytest.mark.parametrize("sql", [
    "delete from arrivals where id = 7",  # arrivals count only
    "delete from forecast where arrival_id = 14 and horizon_min = 5",  # forecast count only
    "delete from forecast where arrival_id = 14; delete from arrivals where id = 14",  # day-removal shape
])
def test_slim_count_checks_each_keep_month(tmp_path, sql, caplog):
    with caplog.at_level(logging.WARNING):
        removed, copy = _tamper(tmp_path, sql)
    assert removed == [] and "count mismatch" in caplog.text
    assert months_in(copy) == ["2026-09"]


def test_slim_keeps_month_when_archive_records_other_month(dirs, tmp_path, monkeypatch, caplog):
    d, a = dirs
    make_meta(d, ["2026-09"])
    archive_meta_months(d, a, NOW)
    real = meta_archive.archive_info
    monkeypatch.setattr(meta_archive, "archive_info", lambda g: {**real(g), "month": "2026-08"})
    copy = staged(d, tmp_path)
    with caplog.at_level(logging.WARNING):
        assert meta_archive.slim_meta_copy(copy, a, NOW) == []
    assert months_in(copy) == ["2026-09"] and "records month" in caplog.text


def _gz_db(path, setup):
    db = path.with_suffix("")
    c = sqlite3.connect(db)
    c.executescript(setup)
    c.commit()
    c.close()
    path.write_bytes(gzip.compress(db.read_bytes()))


BAD_ARCHIVES = {
    "notsqlite": None,
    "two_rows": "create table info(month TEXT, arrivals INT, forecast INT, max_id INT);"
                "insert into info values('2026-09',3,6,147),('2026-09',3,6,147);",
    "zero_rows": "create table info(month TEXT, arrivals INT, forecast INT, max_id INT);",
    "no_info": "create table other(x);",
}


@pytest.mark.parametrize("kind", BAD_ARCHIVES)
def test_archive_info_none_and_slim_keeps_month(dirs, tmp_path, kind):
    d, a = dirs
    make_meta(d, ["2026-09"])
    g = a / "meta-2026-09.db.gz"
    if BAD_ARCHIVES[kind] is None:
        g.write_bytes(gzip.compress(b"this is not sqlite" * 100))
    else:
        _gz_db(g, BAD_ARCHIVES[kind])
    assert archive_info(g) is None
    copy = staged(d, tmp_path)
    assert meta_archive.slim_meta_copy(copy, a, NOW) == []
    assert months_in(copy) == ["2026-09"]


# ---- restore at startup ----
def restored(tmp_path, d, a, now=NOW, name="fresh"):
    """Snapshot d (archiving to a) and extract the tar into a new data dir, as a restore would."""
    tar_path = backup.snapshot(d, tmp_path / f"bk-{name}", keep=3, archive_dir=a, now=now)
    fresh = tmp_path / name
    fresh.mkdir()
    with tarfile.open(tar_path) as t:
        t.extractall(fresh, filter="data")
    return fresh


def marker_months(d):
    return [r[0] for r in rows(d / "meta.db", "select month from archived_months order by 1")]


def test_restore_noop_on_live_db(dirs):
    d, a = dirs
    make_meta(d, ["2026-09"])
    before = content_hash(d)
    assert meta_archive.restore_archived_months(d, a) == []
    assert content_hash(d) == before


def test_restore_noop_when_meta_db_missing_and_not_created(dirs):
    d, a = dirs
    assert meta_archive.restore_archived_months(d, a) == []
    assert not (d / "meta.db").exists()


def test_restore_imports_month_with_same_ids_and_clears_marker(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-08", "2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    assert marker_months(fresh) == ["2026-08", "2026-09"]
    assert meta_archive.restore_archived_months(fresh, a) == ["2026-08", "2026-09"]
    assert marker_months(fresh) == []
    assert content_hash(fresh) == content_hash(d)
    assert not list(fresh.glob("restore-*"))


def test_restore_twice_is_noop(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    assert meta_archive.restore_archived_months(fresh, a) == ["2026-09"]
    assert meta_archive.restore_archived_months(fresh, a) == []
    assert content_hash(fresh) == content_hash(d)


def test_restore_missing_archive_logs_error_keeps_marker(dirs, tmp_path, caplog):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    gz = a / "meta-2026-09.db.gz"
    hidden = tmp_path / "hidden.gz"
    gz.rename(hidden)
    with caplog.at_level(logging.ERROR):
        assert meta_archive.restore_archived_months(fresh, a) == []
    assert marker_months(fresh) == ["2026-09"]
    assert any("2026-09" in r.getMessage() and "meta-2026-09.db.gz" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    hidden.rename(gz)
    assert meta_archive.restore_archived_months(fresh, a) == ["2026-09"]
    assert content_hash(fresh) == content_hash(d)


def _tamper_info(gz, tmp_path, sql):
    db = tmp_path / "t.db"
    db.write_bytes(gzip.decompress(gz.read_bytes()))
    c = sqlite3.connect(db)
    c.execute(sql)
    c.commit()
    c.close()
    gz.write_bytes(gzip.compress(db.read_bytes()))


def test_restore_rolls_back_on_count_mismatch(dirs, tmp_path, caplog):
    d, a = dirs
    make_meta(d, ["2026-08", "2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    _tamper_info(a / "meta-2026-08.db.gz", tmp_path, "update info set arrivals = arrivals + 1")
    with caplog.at_level(logging.ERROR):
        assert meta_archive.restore_archived_months(fresh, a) == ["2026-09"]
    assert marker_months(fresh) == ["2026-08"]
    assert rows(fresh / "meta.db", "select count(*) from arrivals where service_date like '2026-08-%'") == [(0,)]
    assert rows(fresh / "meta.db", "select count(*) from forecast where arrival_id in (select id from arrivals where service_date like '2026-08-%')") == [(0,)]
    assert any("2026-08" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_restore_rejects_archive_for_other_month(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-08", "2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    shutil.copy(a / "meta-2026-09.db.gz", a / "meta-2026-08.db.gz")
    assert meta_archive.restore_archived_months(fresh, a) == ["2026-09"]
    assert marker_months(fresh) == ["2026-08"]
    assert rows(fresh / "meta.db", "select count(*) from arrivals where service_date like '2026-08-%'") == [(0,)]


def test_restore_finds_archive_in_data_dir_when_not_in_archive_dir(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    shutil.copy(a / "meta-2026-09.db.gz", fresh)
    assert meta_archive.restore_archived_months(fresh, tmp_path / "empty") == ["2026-09"]
    assert meta_archive.restore_archived_months(fresh, None) == []


def test_restore_clears_result_cache(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    c = sqlite3.connect(fresh / "meta.db")
    c.execute("insert into result_cache(key, body, computed_at, last_used, ms) values('k', 'b', 1, 1, 1)")
    c.commit()
    c.close()
    assert meta_archive.restore_archived_months(fresh, a) == ["2026-09"]
    assert rows(fresh / "meta.db", "select count(*) from result_cache where key = 'k'") == [(0,)]


def test_restore_continues_after_one_bad_month(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-07", "2026-08", "2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    (a / "meta-2026-08.db.gz").write_bytes(b"junk")
    assert meta_archive.restore_archived_months(fresh, a) == ["2026-07", "2026-09"]
    assert marker_months(fresh) == ["2026-08"]


def test_restore_unique_conflict_counts_as_mismatch(dirs, tmp_path):
    d, a = dirs
    make_meta(d, ["2026-09", "2026-11"])
    fresh = restored(tmp_path, d, a)
    src = sqlite3.connect(d / "meta.db")
    r = list(src.execute("select * from arrivals where service_date like '2026-09-%' order by id limit 1").fetchone())
    src.close()
    r[0] = 999999  # same trip/stop/seq/service_date, different id: insert or ignore skips the archived row
    c = sqlite3.connect(fresh / "meta.db")
    c.execute("insert into arrivals values(" + ",".join("?" * len(r)) + ")", r)
    c.commit()
    c.close()
    assert meta_archive.restore_archived_months(fresh, a) == []
    assert marker_months(fresh) == ["2026-09"]
    assert rows(fresh / "meta.db", "select count(*) from arrivals where service_date like '2026-09-%'") == [(1,)]
