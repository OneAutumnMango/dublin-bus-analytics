import gc
import logging
import os
import subprocess
import sys
import tempfile
import gzip
import hashlib
import sqlite3
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hset, strategies as st

from app import backup
from app.backup import compact_closed_months, list_backups, snapshot
from app.core.config import Settings
from app.core.db import open_meta, open_samples, open_samples_readonly, samples_path
from app.main import create_app


def ts(y, m, d=1, h=0, mi=0, s=0):
    return int(datetime(y, m, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def mk_month(d, y, m, n=3):
    c = open_samples(d, ts(y, m, 5))
    c.executemany("insert into sample values(?,?,?,?,?,?,?)",
                  [(ts(y, m, 5) + i, "t", "s", i, 3, 100 + i, 1) for i in range(n)])
    c.commit()
    c.close()


def mk_meta(d, n=3):
    c = open_meta(d)
    c.executemany("insert into routes_tracked values(?,?)", [(f"r{i}", i) for i in range(n)])
    c.commit()
    c.close()


def rows(path, sql):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    mk_meta(d)
    mk_month(d, 2026, 9)
    mk_month(d, 2026, 10)
    return d


# ---- snapshot ----

def test_snapshot_restorable_and_consistent_while_writing(data, tmp_path):
    stop = threading.Event()
    errs = []
    committed = [0]

    def writer():
        c = open_samples(data, ts(2026, 10, 5))
        i = 1000
        try:
            while not stop.is_set():
                with c:
                    c.execute("insert into sample values(?,?,?,?,?,?,?)", (i, "w", "s", i, 1, i * 2, 1))
                    c.execute("insert into sample values(?,?,?,?,?,?,?)", (i, "w2", "s", i, 1, i * 2, 1))
                committed[0] += 1
                i += 1
        except Exception as e:  # pragma: no cover
            errs.append(e)
        finally:
            c.close()

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.2)
    try:
        tars, lbs = [], []
        for _ in range(3):
            lbs.append(committed[0])
            tars.append(snapshot(data, tmp_path / "bk", keep=10))
    finally:
        stop.set()
        t.join()
    assert not errs
    out = tmp_path / "restore"
    out.mkdir()
    with tarfile.open(tars[-1]) as tar:
        tar.extractall(out, filter="data")
    f = out / samples_path(out, ts(2026, 10, 5)).name
    assert rows(f, "pragma integrity_check") == [("ok",)]
    r = rows(f, "select count(*) from sample where trip_id='w'")[0][0]
    r2 = rows(f, "select count(*) from sample where trip_id='w2'")[0][0]
    assert r >= lbs[-1] > 0  # everything committed before the snapshot started is in it
    assert r == r2  # both inserts of a txn are present together
    assert rows(f, "select count(*) from sample where trip_id='w' and expected != ts*2")[0][0] == 0


def test_restore_contents_equal(data, tmp_path):
    tar_path = snapshot(data, tmp_path / "bk")
    out = tmp_path / "r"
    out.mkdir()
    with tarfile.open(tar_path) as tar:
        tar.extractall(out, filter="data")
    m = open_meta(out)
    assert m.execute("select * from routes_tracked order by 1").fetchall() == \
        rows(data / "meta.db", "select * from routes_tracked order by 1")
    assert m.execute("pragma integrity_check").fetchall() == [("ok",)]
    m.close()
    for mo in (9, 10):
        s = open_samples(out, ts(2026, mo, 5))
        assert s.execute("select * from sample order by 1").fetchall() == \
            rows(samples_path(data, ts(2026, mo, 5)), "select * from sample order by 1")
        assert s.execute("pragma integrity_check").fetchall() == [("ok",)]
        s.close()


def test_snapshot_includes_gz_as_is_and_safe_names(data, tmp_path):
    compact_closed_months(data, ts(2026, 10, 3))
    gz = data / "samples-2026-09.db.gz"
    assert gz.exists()
    p = snapshot(data, tmp_path / "bk")
    with tarfile.open(p) as tar:
        names = tar.getnames()
        assert sorted(names) == ["meta.db", "samples-2026-09.db.gz", "samples-2026-10.db"]
        for n in names:
            assert not n.startswith("/") and ".." not in Path(n).parts and "/" not in n
        assert tar.extractfile("samples-2026-09.db.gz").read() == gz.read_bytes()


def test_snapshot_retention_keeps_7(data, tmp_path):
    bk = tmp_path / "bk"
    for i in range(10):
        (bk).mkdir(exist_ok=True)
        (bk / f"analytics-202001{i + 1:02d}T000000Z.tar").write_bytes(b"x")
    new = snapshot(data, bk)
    kept = list_backups(bk)
    assert len(kept) == 7 and kept[-1] == new
    assert [p.name for p in kept[:-1]] == [f"analytics-202001{i + 1:02d}T000000Z.tar" for i in range(4, 10)]


def test_retention_keep1_keep0_and_fewer(data, tmp_path):
    bk = tmp_path / "bk"
    with pytest.raises(ValueError):
        snapshot(data, bk, keep=0)
    with pytest.raises(ValueError):
        snapshot(data, bk, keep=-1)
    assert not bk.exists()
    a = snapshot(data, bk, keep=5)
    assert list_backups(bk) == [a]  # fewer than keep: nothing deleted
    b = snapshot(data, bk, keep=1)
    assert list_backups(bk) == [b] and not a.exists()


def test_same_second_snapshots_do_not_overwrite(data, tmp_path, monkeypatch):
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 8, 1, 2, 3, tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", FixedDT)
    ps = [snapshot(data, tmp_path / "bk", keep=20) for _ in range(12)]
    assert len(set(ps)) == 12 and all(p.exists() for p in ps)
    assert list_backups(tmp_path / "bk") == ps  # sorted order == creation order (incl. _010+)


def test_backup_dir_inside_data_dir_excluded(data):
    a = snapshot(data)  # default <data>/backups
    assert a.parent == data / "backups"
    b = snapshot(data)
    with tarfile.open(b) as tar:
        assert all("backups" not in n and not n.startswith("analytics-") for n in tar.getnames())
    assert b.stat().st_size < a.stat().st_size + a.stat().st_size  # not grown by the first tar
    assert abs(b.stat().st_size - a.stat().st_size) < 4096


def test_backup_dir_created(data, tmp_path):
    p = snapshot(data, tmp_path / "a" / "b" / "c")
    assert p.exists()


def test_unwritable_backup_dir_clean_failure(data, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(OSError):
        snapshot(data, blocker / "sub")
    assert blocker.read_text() == "x"


def test_failure_midway_leaves_no_partial(data, tmp_path, monkeypatch):
    bk = tmp_path / "bk"
    monkeypatch.setattr(backup, "_copy_db", lambda s, d: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    with pytest.raises(sqlite3.OperationalError):
        snapshot(data, bk)
    assert list(bk.iterdir()) == []


def test_empty_data_dir_snapshot(tmp_path):
    d = tmp_path / "e"
    d.mkdir()
    p = snapshot(d, tmp_path / "bk")
    with tarfile.open(p) as tar:
        assert tar.getnames() == []


def test_list_backups_missing_dir_and_ignores_others(tmp_path):
    assert list_backups(tmp_path / "nope") == []
    (tmp_path / "junk.tar").write_text("x")
    assert list_backups(tmp_path) == []


# ---- compaction ----

def test_compact_gzips_only_closed_months(data):
    out = compact_closed_months(data, ts(2026, 10, 15))
    assert out == [data / "samples-2026-09.db.gz"]
    assert not (data / "samples-2026-09.db").exists()
    assert not (data / "samples-2026-09.db-wal").exists()
    assert (data / "samples-2026-10.db").exists() and (data / "meta.db").exists()


def test_compact_idempotent(data):
    assert len(compact_closed_months(data, ts(2026, 12, 1))) == 2
    assert compact_closed_months(data, ts(2026, 12, 1)) == []


def test_month_boundary_exact_grace(data):
    g = backup.COMPACT_GRACE_S
    assert g == 2 * 86400
    assert compact_closed_months(data, ts(2026, 10, 1) + g - 1) == []  # Sept not yet eligible
    assert compact_closed_months(data, ts(2026, 10, 1) + g) == [data / "samples-2026-09.db.gz"]
    assert (data / "samples-2026-10.db").exists()
    assert compact_closed_months(data, ts(2026, 11, 1) + g - 1) == []
    assert compact_closed_months(data, ts(2026, 11, 1) + g) == [data / "samples-2026-10.db.gz"]


def test_year_rollover(tmp_path):
    mk_month(tmp_path, 2025, 12)
    mk_month(tmp_path, 2026, 1)
    g = backup.COMPACT_GRACE_S
    assert compact_closed_months(tmp_path, ts(2026, 1, 1) + g - 1) == []
    assert compact_closed_months(tmp_path, ts(2026, 1, 1) + g) == [tmp_path / "samples-2025-12.db.gz"]
    assert (tmp_path / "samples-2026-01.db").exists()
    assert compact_closed_months(tmp_path, ts(2026, 2, 1) + g) == [tmp_path / "samples-2026-01.db.gz"]


def test_future_month_untouched(tmp_path):
    mk_month(tmp_path, 2027, 3)
    assert compact_closed_months(tmp_path, ts(2026, 1, 1)) == []


def test_empty_dir(tmp_path):
    assert compact_closed_months(tmp_path, ts(2026, 1, 1)) == []


def test_ignores_nonmatching_names(tmp_path):
    (tmp_path / "samples-foo.db").write_bytes(b"x")
    assert compact_closed_months(tmp_path, ts(2030, 1, 1)) == []
    assert (tmp_path / "samples-foo.db").exists()


def test_gz_roundtrip_sha256(data):
    c = sqlite3.connect(data / "samples-2026-09.db")
    c.execute("pragma wal_checkpoint(truncate)")
    c.close()
    want = hashlib.sha256((data / "samples-2026-09.db").read_bytes()).hexdigest()
    compact_closed_months(data, ts(2026, 10, 3))
    got = hashlib.sha256(gzip.open(data / "samples-2026-09.db.gz").read()).hexdigest()
    assert got == want


def test_corrupt_original_not_deleted(tmp_path, caplog):
    bad = tmp_path / "samples-2026-01.db"
    bad.write_bytes(b"this is not a database" * 100)
    mk_month(tmp_path, 2026, 2)
    out = compact_closed_months(tmp_path, ts(2026, 6, 1))
    assert out == [tmp_path / "samples-2026-02.db.gz"]  # others still processed
    assert bad.exists()
    assert not (tmp_path / "samples-2026-01.db.gz").exists()
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("verify-*"))
    assert "compaction of samples-2026-01.db failed" in caplog.text


def test_interrupted_compaction_then_rerun(data, monkeypatch):
    orig = backup._verify_gz

    def boom(gz, sha):
        raise RuntimeError("crash")

    monkeypatch.setattr(backup, "_verify_gz", boom)
    assert compact_closed_months(data, ts(2026, 10, 3)) == []
    assert (data / "samples-2026-09.db").exists()
    assert not (data / "samples-2026-09.db.gz").exists()
    assert rows(data / "samples-2026-09.db", "select count(*) from sample") == [(3,)]
    monkeypatch.setattr(backup, "_verify_gz", orig)
    assert compact_closed_months(data, ts(2026, 10, 3)) == [data / "samples-2026-09.db.gz"]


def test_crash_after_gz_before_delete_rerun(data):
    """gz and original both present (crash between rename and unlink): rerun converges."""
    compact_closed_months(data, ts(2026, 10, 3))
    db = data / "samples-2026-09.db"
    db.write_bytes(gzip.open(data / "samples-2026-09.db.gz").read())
    assert compact_closed_months(data, ts(2026, 10, 3)) == [data / "samples-2026-09.db.gz"]
    assert not db.exists()


def test_busy_writer_skipped(data):
    c = sqlite3.connect(data / "samples-2026-09.db", isolation_level=None)
    c.execute("begin")
    c.execute("select count(*) from sample").fetchall()
    # an open read transaction does not block truncate checkpoints; hold a writer lock instead
    c.execute("rollback")
    c.execute("begin immediate")
    c.execute("insert into sample values(1,'x','s',1,1,1,1)")
    try:
        assert compact_closed_months(data, ts(2026, 10, 3)) == []
        assert (data / "samples-2026-09.db").exists()
    finally:
        c.execute("rollback")
        c.close()


def test_lock_serialises_snapshot_and_compaction(data, tmp_path):
    done = []
    backup._lock.acquire()
    try:
        ts_ = [threading.Thread(target=lambda: done.append(snapshot(data, tmp_path / "bk"))),
               threading.Thread(target=lambda: done.append(compact_closed_months(data, ts(2026, 12, 1))))]
        for t in ts_:
            t.start()
        time.sleep(0.3)
        assert done == []
    finally:
        backup._lock.release()
    for t in ts_:
        t.join(10)
    assert len(done) == 2


@given(st.sets(st.tuples(st.integers(2020, 2030), st.integers(1, 12)), max_size=6),
       st.integers(ts(2019, 1), ts(2031, 12, 28)))
@hset(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_compacts_exactly_closed_months(tmp_path_factory, months, now):
    d = tmp_path_factory.mktemp("p")
    for y, m in months:
        mk_month(d, y, m, n=1)
    def end(y, m):
        return int(datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp())

    closed = {(y, m) for y, m in months if now >= end(y, m) + backup.COMPACT_GRACE_S}
    out = compact_closed_months(d, now)
    assert {p.name for p in out} == {f"samples-{y}-{m:02d}.db.gz" for y, m in closed}
    assert {p.name for p in d.glob("samples-*.db")} == {f"samples-{y}-{m:02d}.db" for y, m in months - closed}
    assert compact_closed_months(d, now) == []


# ---- open_samples_readonly / open_samples guards ----

def test_series_reads_gzipped_month(data):
    compact_closed_months(data, ts(2026, 10, 3))
    c = open_samples_readonly(data, ts(2026, 9, 7))
    assert c.execute("select count(*), min(seq), max(seq) from sample").fetchone() == (3, 0, 2)
    c.close()


def test_readonly_plain_month_is_readonly(data):
    c = open_samples_readonly(data, ts(2026, 10, 7))
    assert c.execute("select count(*) from sample").fetchone() == (3,)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("insert into sample values(1,'x','s',1,1,1,1)")
    c.close()


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    d = tmp_path / "tmpdir"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    monkeypatch.setenv("TMPDIR", str(d))
    return d


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    d = tmp_path / "tmpdir"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    monkeypatch.setenv("TMPDIR", str(d))
    return d


def _tmp_left(before):
    import tempfile
    return set(Path(tempfile.gettempdir()).glob("samples-ro-*")) - before


def _tmp_before():
    import tempfile
    return set(Path(tempfile.gettempdir()).glob("samples-ro-*"))


def test_readonly_gz_tempfile_removed_on_close(data, private_tmp):
    compact_closed_months(data, ts(2026, 10, 3))
    before = _tmp_before()
    c = open_samples_readonly(data, ts(2026, 9, 7))
    assert len(_tmp_left(before)) == 1
    c.close()
    c.close()  # idempotent
    assert _tmp_left(before) == set()
    assert not (data / "samples-2026-09.db").exists()  # never created in data_dir


def test_readonly_gz_tempfile_removed_on_gc(data, private_tmp):
    compact_closed_months(data, ts(2026, 10, 3))
    before = _tmp_before()
    c = open_samples_readonly(data, ts(2026, 9, 7))
    del c
    gc.collect()
    assert _tmp_left(before) == set()


def test_readonly_gz_tempfile_removed_on_exception(data, private_tmp):
    compact_closed_months(data, ts(2026, 10, 3))
    before = _tmp_before()
    c = open_samples_readonly(data, ts(2026, 9, 7))
    try:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("select * from nope")
    finally:
        c.close()
    assert _tmp_left(before) == set()


def test_readonly_bad_gz_cleans_up(tmp_path, private_tmp):
    (tmp_path / "samples-2026-01.db.gz").write_bytes(b"not gzip")
    before = _tmp_before()
    with pytest.raises(OSError):
        open_samples_readonly(tmp_path, ts(2026, 1, 5))
    assert _tmp_left(before) == set()


def test_readonly_missing_raises_and_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        open_samples_readonly(tmp_path, ts(2026, 1, 5))
    assert list(tmp_path.iterdir()) == []


def test_open_samples_refuses_gz_only_month(data):
    compact_closed_months(data, ts(2026, 10, 3))
    with pytest.raises(RuntimeError):
        open_samples(data, ts(2026, 9, 7))
    assert not (data / "samples-2026-09.db").exists()


# ---- API ----

@pytest.fixture
def client(data):
    app = create_app(Settings(server_url="http://x", data_dir=data))
    ticks = iter(range(int(time.time()), 2**40, 100))  # each request is 100 s later: the rate limit never trips
    app.state.backup_clock = lambda: next(ticks)
    return TestClient(app)


def test_api_post_and_download(client, data):
    r = client.post("/api/backup")
    assert r.status_code == 200
    j = r.json()
    f = data / "backups" / j["file"]
    assert f.exists() and j["size"] == f.stat().st_size
    d = client.get("/api/backup/download")
    assert d.status_code == 200 and d.content == f.read_bytes()
    assert j["file"] in d.headers["content-disposition"]


def test_api_download_404_without_backups(client):
    assert client.get("/api/backup/download").status_code == 404


def test_api_download_returns_newest(client, data):
    client.post("/api/backup")
    last = client.post("/api/backup").json()["file"]
    d = client.get("/api/backup/download")
    assert d.content == (data / "backups" / last).read_bytes()


# ---- fix round 1 ----

def test_never_clobber_existing_gz(tmp_path, caplog):
    mk_month(tmp_path, 2026, 1, n=1000)
    assert compact_closed_months(tmp_path, ts(2026, 6, 1))
    plain = tmp_path / "samples-2026-01.db"
    c = sqlite3.connect(plain)
    c.execute("create table sample(ts int, trip_id text, stop_id text, seq int, due_min int, expected int, realtime int,"
              " primary key(trip_id, stop_id, seq, ts)) without rowid")
    c.execute("insert into sample values(1,'x','s',1,1,1,1)")
    c.commit()
    c.close()
    before = plain.read_bytes()
    assert compact_closed_months(tmp_path, ts(2026, 6, 1)) == []
    assert plain.read_bytes() == before
    r = open_samples_readonly(tmp_path, ts(2026, 1, 5))
    assert r.execute("select count(*) from sample").fetchone() == (1000,)
    r.close()
    assert "samples-2026-01.db" in caplog.text and "skipped" in caplog.text


def test_identical_plain_and_gz_plain_removed(data):
    compact_closed_months(data, ts(2026, 10, 3))
    plain = data / "samples-2026-09.db"
    plain.write_bytes(gzip.open(data / "samples-2026-09.db.gz").read())
    gz_before = (data / "samples-2026-09.db.gz").read_bytes()
    assert compact_closed_months(data, ts(2026, 10, 3)) == [data / "samples-2026-09.db.gz"]
    assert not plain.exists() and (data / "samples-2026-09.db.gz").read_bytes() == gz_before


def test_open_samples_refuses_when_gz_exists_even_with_plain(data):
    compact_closed_months(data, ts(2026, 10, 3))
    (data / "samples-2026-09.db").write_bytes(b"")
    with pytest.raises(RuntimeError):
        open_samples(data, ts(2026, 9, 7))


def test_snapshot_with_open_write_txn_and_uncheckpointed_wal(data, tmp_path):
    c = sqlite3.connect(data / "samples-2026-10.db", isolation_level=None)
    c.execute("pragma wal_autocheckpoint=0")
    for i in range(50):  # committed, but only in the WAL: a plain file copy would miss these
        c.execute("insert into sample values(?,?,?,?,?,?,?)", (5000 + i, "c", "s", i, 1, i, 1))
    c.execute("begin immediate")
    c.execute("insert into sample values(9999,'uncommitted','s',1,1,1,1)")
    try:
        p = snapshot(data, tmp_path / "bk")
    finally:
        c.execute("commit")
        c.close()
    out = tmp_path / "r"
    out.mkdir()
    with tarfile.open(p) as tar:
        tar.extractall(out, filter="data")
    f = out / "samples-2026-10.db"
    assert rows(f, "pragma integrity_check") == [("ok",)]
    assert rows(f, "select count(*) from sample where trip_id='c'") == [(50,)]
    assert rows(f, "select count(*) from sample where trip_id='uncommitted'") == [(0,)]
    assert rows(f, "select count(*) from sample") == [(53,)]


# ---- item 2 ----

def test_writer_blocked_during_compaction_and_gz_complete(data, monkeypatch):
    orig = backup._verify_gz
    seen = []

    def spy(gz, sha):
        c = sqlite3.connect(data / "samples-2026-09.db", timeout=0.1)
        try:
            c.execute("insert into sample values(77,'late','s',1,1,1,1)")
            c.commit()
            seen.append("wrote")
        except sqlite3.OperationalError as e:
            seen.append(str(e))
        finally:
            c.close()
        orig(gz, sha)

    monkeypatch.setattr(backup, "_verify_gz", spy)
    assert compact_closed_months(data, ts(2026, 10, 3)) == [data / "samples-2026-09.db.gz"]
    assert seen and ("locked" in seen[0] or "busy" in seen[0])
    r = open_samples_readonly(data, ts(2026, 9, 7))
    assert r.execute("select count(*) from sample").fetchone() == (3,)
    r.close()


def test_verify_rejects_truncated_but_valid_gz(data, tmp_path):
    raw = (data / "samples-2026-09.db").read_bytes()
    gz = tmp_path / "t.db.gz"
    gz.write_bytes(gzip.compress(raw[:-4096]))
    with pytest.raises(sqlite3.DatabaseError, match="does not match"):
        backup._verify_gz(gz, hashlib.sha256(raw).hexdigest())


def test_verify_rejects_integrity_not_ok(data, monkeypatch):
    monkeypatch.setattr(backup, "_integrity", lambda p: [("row 1 missing",)])
    assert compact_closed_months(data, ts(2026, 10, 3)) == []
    assert (data / "samples-2026-09.db").exists() and not (data / "samples-2026-09.db.gz").exists()
    assert not list(data.glob("*.tmp")) and not list(data.glob("verify-*"))


def test_source_modified_before_unlink_aborts(data, monkeypatch):
    orig = backup._verify_gz

    def tamper(gz, sha):
        orig(gz, sha)
        with open(data / "samples-2026-09.db", "ab") as f:
            f.write(b"\0" * 16)

    monkeypatch.setattr(backup, "_verify_gz", tamper)
    assert compact_closed_months(data, ts(2026, 10, 3)) == []
    assert (data / "samples-2026-09.db").exists() and not (data / "samples-2026-09.db.gz").exists()


# ---- item 3 ----

@pytest.mark.parametrize("name", ["a#b", "a?b", "a%20b", "a b", "a&b", "a%b", "a;b=c"])
def test_weird_data_dir_names(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    mk_meta(d)
    mk_month(d, 2026, 9)
    p = snapshot(d, tmp_path / "bk")
    out = tmp_path / "r"
    out.mkdir()
    with tarfile.open(p) as tar:
        tar.extractall(out, filter="data")
    assert rows(out / "meta.db", "select count(*) from routes_tracked") == [(3,)]
    assert rows(out / "samples-2026-09.db", "select count(*) from sample") == [(3,)]
    c = open_samples_readonly(d, ts(2026, 9, 7))
    assert c.execute("select count(*) from sample").fetchone() == (3,)
    c.close()
    compact_closed_months(d, ts(2026, 12, 1))
    c = open_samples_readonly(d, ts(2026, 9, 7))
    assert c.execute("select count(*) from sample").fetchone() == (3,)
    c.close()
    assert sorted(x.name for x in tmp_path.iterdir()) == sorted([name, "bk", "r"])  # no stray db next to the dir


# ---- items 5-9 ----

def test_directory_named_like_backup_ignored(data, tmp_path, client):
    bk = tmp_path / "bk"
    (bk / "analytics-20200101T000000Z.tar").mkdir(parents=True)
    assert list_backups(bk) == []
    p = snapshot(data, bk, keep=1)
    assert list_backups(bk) == [p] and (bk / "analytics-20200101T000000Z.tar").is_dir()


def test_api_download_ignores_directory_named_like_backup(client, data):
    (data / "backups" / "analytics-20990101T000000Z.tar").mkdir(parents=True)
    assert client.get("/api/backup/download").status_code == 404
    j = client.post("/api/backup").json()
    assert client.get("/api/backup/download").content == (data / "backups" / j["file"]).read_bytes()


def test_clock_behind_newest_backup_keeps_final(data, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    future = bk / "analytics-20990101T000000Z.tar"
    future.write_bytes(b"x")
    p = snapshot(data, bk, keep=1)
    assert p.exists() and future.exists()  # final kept; future-dated file is neither counted nor deleted
    assert list_backups(bk) == [p, future]


def test_link_unsupported_falls_back(data, tmp_path, monkeypatch):
    def nolink(a, b):
        raise PermissionError("no hard links")

    monkeypatch.setattr(backup.os, "link", nolink)

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 8, 1, 2, 3, tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", FixedDT)
    a = snapshot(data, tmp_path / "bk", keep=5)
    b = snapshot(data, tmp_path / "bk", keep=5)  # name collision under fallback -> next suffix
    assert a != b and a.exists() and b.exists()
    with tarfile.open(b) as tar:
        assert "meta.db" in tar.getnames()
    assert not [p for p in (tmp_path / "bk").iterdir() if p.name.startswith(".snap")]


def test_snapshot_fsyncs_tar_and_dir(data, tmp_path, monkeypatch):
    calls = []
    real = backup.os.fsync
    monkeypatch.setattr(backup.os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    snapshot(data, tmp_path / "bk")
    assert len(calls) >= 2


def _age(p, secs):
    import os
    t = time.time() - secs
    os.utime(p, (t, t))


def test_snapshot_cleans_only_stale_own_leftovers(data, tmp_path):
    bk = tmp_path / "bk"
    (bk / ".snap-old").mkdir(parents=True)
    (bk / ".snap-old" / "x").write_text("x")
    (bk / ".snap-new").mkdir()
    (bk / "unrelated.txt").write_text("u")
    (bk / ".other").write_text("o")
    _age(bk / ".snap-old", 7200)
    _age(bk / "unrelated.txt", 7200)
    _age(bk / ".other", 7200)
    snapshot(data, bk)
    assert not (bk / ".snap-old").exists()
    assert (bk / ".snap-new").exists() and (bk / "unrelated.txt").exists() and (bk / ".other").exists()


def test_compaction_cleans_only_stale_own_leftovers(data):
    for n in ("verify-abcd1234.db", "verify-abcd1234.db-wal", "samples-2020-01.db.gz.tmp", "notes.txt", "verify-new12345.db"):
        (data / n).write_text("x")
    for n in ("verify-abcd1234.db", "verify-abcd1234.db-wal", "samples-2020-01.db.gz.tmp", "notes.txt"):
        _age(data / n, 7200)
    compact_closed_months(data, ts(2026, 10, 3))
    assert sorted(p.name for p in data.glob("verify-*")) == ["verify-new12345.db"]
    assert not (data / "samples-2020-01.db.gz.tmp").exists()
    assert (data / "notes.txt").exists()


# ---- fix round 2 ----

def test_crash_wal_compaction_keeps_all_rows(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    code = (
        "import os,sys\nfrom pathlib import Path\nfrom app.core.db import open_samples\n"
        "c = open_samples(Path(sys.argv[1]), int(sys.argv[2]))\n"
        "c.execute('pragma wal_autocheckpoint=0')\n"
        "c.executemany('insert into sample values(?,?,?,?,?,?,?)', [(i,'t','s',i,1,i,1) for i in range(500)])\n"
        "c.commit()\nos._exit(0)\n")
    root = Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, "-c", code, str(d), str(ts(2026, 1, 5))], check=True, cwd=root,
                   env={**os.environ, "PYTHONPATH": str(root)})
    db = d / "samples-2026-01.db"
    assert (d / "samples-2026-01.db-wal").stat().st_size > 4 * db.stat().st_size  # data only in the WAL
    assert compact_closed_months(d, ts(2026, 6, 1)) == [d / "samples-2026-01.db.gz"]
    assert not db.exists() and not (d / "samples-2026-01.db-wal").exists()
    c = open_samples_readonly(d, ts(2026, 1, 5))
    assert c.execute("select count(*) from sample").fetchone() == (500,)
    c.close()
    raw = tmp_path / "raw.db"
    raw.write_bytes(gzip.open(d / "samples-2026-01.db.gz").read())
    assert rows(raw, "select count(*) from sample") == [(500,)]


def test_non_wal_month_with_reserved_writer_skipped(tmp_path, caplog):
    f = tmp_path / "samples-2026-01.db"
    c = sqlite3.connect(f, isolation_level=None)
    c.execute("create table sample(ts int, trip_id text, stop_id text, seq int, due_min int, expected int, realtime int)")
    c.execute("begin")
    c.executemany("insert into sample values(?,?,?,?,?,?,?)", [(i, "t", "s", i, 1, i, 1) for i in range(50)])
    c.execute("commit")
    c.execute("begin immediate")
    c.execute("insert into sample values(99,'t','s',99,1,1,1)")
    sk = []
    try:
        assert compact_closed_months(tmp_path, ts(2026, 6, 1), skipped=sk) == []
    finally:
        c.execute("commit")
        c.close()
    assert sk == [f] and not (tmp_path / "samples-2026-01.db.gz").exists()
    assert "skipped" in caplog.text
    assert rows(f, "select count(*) from sample") == [(51,)]


def test_stale_cleanup_exact_shapes(data):
    keep = ["foo.tmp", "verify-x.txt", "verify-important.db", "my.db.gz.tmp", "verify-abcd1234.txt"]
    gone = ["verify-abcd1234.db", "verify-abcd1234.db-journal", "samples-2020-01.db.gz.tmp"]
    for n in keep + gone:
        (data / n).write_text("x")
        _age(data / n, 7200)
    compact_closed_months(data, ts(2026, 10, 3))
    assert all((data / n).exists() for n in keep)
    assert not any((data / n).exists() for n in gone)


def test_existing_gz_appearing_midway_is_not_overwritten(data, monkeypatch):
    gz = data / "samples-2026-09.db.gz"
    orig = backup._verify_gz

    def appear(g, sha):
        orig(g, sha)
        gz.write_bytes(b"precious")

    monkeypatch.setattr(backup, "_verify_gz", appear)
    sk = []
    assert compact_closed_months(data, ts(2026, 10, 3), skipped=sk) == []
    assert gz.read_bytes() == b"precious" and sk == [data / "samples-2026-09.db"]
    assert (data / "samples-2026-09.db").exists() and not list(data.glob("*.tmp"))


def test_skipped_differing_plain_and_gz(data):
    compact_closed_months(data, ts(2026, 10, 3))
    other = sqlite3.connect(data / "samples-2026-09.db")
    other.execute("create table z(x)")
    other.commit()
    other.close()
    sk = []
    assert compact_closed_months(data, ts(2026, 10, 3), skipped=sk) == []
    assert sk == [data / "samples-2026-09.db"]  # the not-yet-eligible October is not reported


def test_skipped_verification_failure_and_readonly(data, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(backup, "_integrity", lambda p: [("bad",)])
        sk = []
        assert compact_closed_months(data, ts(2026, 12, 3), skipped=sk) == []
        assert sk == [data / "samples-2026-09.db", data / "samples-2026-10.db"]
    real_open = open

    def ro(file, mode="r", *a, **k):
        if "w" in mode and str(file).endswith(".gz.tmp"):
            raise PermissionError("read-only fs")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", ro)
    sk = []
    assert compact_closed_months(data, ts(2026, 12, 3), skipped=sk) == []
    assert len(sk) == 2


def test_fallback_unlinks_placeholder_when_replace_fails(data, tmp_path, monkeypatch):
    monkeypatch.setattr(backup.os, "link", lambda a, b: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(backup.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("replace boom")))
    bk = tmp_path / "bk"
    with pytest.raises(OSError, match="replace boom"):
        snapshot(data, bk)
    assert list(bk.iterdir()) == []


def test_zero_byte_and_impossible_date_backups_ignored(tmp_path):
    (tmp_path / "analytics-20200101T000000Z.tar").write_bytes(b"")
    (tmp_path / "analytics-20201301T000000Z.tar").write_bytes(b"x")
    (tmp_path / "analytics-20200102T000000Z.tar").write_bytes(b"x")
    assert [p.name for p in list_backups(tmp_path)] == ["analytics-20200102T000000Z.tar"]


def _mk(bk, name, mtime=None, body=b"x"):
    bk.mkdir(exist_ok=True)
    (bk / name).write_bytes(body)
    if mtime:
        os.utime(bk / name, (mtime, mtime))


def test_order_by_name_not_mtime(tmp_path):
    _mk(tmp_path, "analytics-20200101T000000Z.tar", mtime=time.time() + 5000)
    _mk(tmp_path, "analytics-20200102T000000Z.tar", mtime=1)
    assert [p.name[10:18] for p in list_backups(tmp_path)] == ["20200101", "20200102"]
    assert backup.newest_backup(tmp_path).name == "analytics-20200102T000000Z.tar"


def test_newest_backup_none_when_empty(tmp_path):
    assert backup.newest_backup(tmp_path) is None


def test_future_dated_never_default_and_warned(client, data, caplog):
    client.post("/api/backup")
    last = client.post("/api/backup").json()["file"]
    _mk(data / "backups", "analytics-20990101T000000Z.tar", body=b"FUTURE")
    with caplog.at_level(logging.WARNING):
        d = client.get("/api/backup/download")
    assert d.content == (data / "backups" / last).read_bytes()
    assert "future-dated" in caplog.text


def test_only_future_dated_falls_back(client, data):
    _mk(data / "backups", "analytics-20990101T000000Z.tar", body=b"FUTURE")
    assert client.get("/api/backup/download").content == b"FUTURE"


def test_future_dated_not_counted_in_retention(data, tmp_path):
    bk = tmp_path / "bk"
    _mk(bk, "analytics-20200101T000000Z.tar")
    _mk(bk, "analytics-20200102T000000Z.tar")
    _mk(bk, "analytics-20990101T000000Z.tar")
    p = snapshot(data, bk, keep=2)
    names = [x.name for x in list_backups(bk)]
    assert "analytics-20200101T000000Z.tar" not in names  # oldest real one retired
    assert "analytics-20200102T000000Z.tar" in names and p.name in names
    assert "analytics-20990101T000000Z.tar" in names  # untouched


def test_touching_old_tar_changes_nothing(data, tmp_path):
    bk = tmp_path / "bk"
    for i in (1, 2, 3):
        _mk(bk, f"analytics-2020010{i}T000000Z.tar")
    os.utime(bk / "analytics-20200101T000000Z.tar", (time.time() + 99, time.time() + 99))
    p = snapshot(data, bk, keep=2)
    assert [x.name[10:18] for x in list_backups(bk)] == ["20200103", p.name[10:18]]


def test_api_file_param_exact_match(client, data):
    a = client.post("/api/backup").json()["file"]
    _mk(data / "backups", "analytics-20200101T000000Z.tar", body=b"OLD")
    assert client.get("/api/backup/download", params={"file": "analytics-20200101T000000Z.tar"}).content == b"OLD"
    assert client.get("/api/backup/download", params={"file": a}).status_code == 200
    for bad in ("nope.tar", "../meta.db", "../../etc/passwd", "/etc/passwd", a + "x", a[:-1], "", "meta.db"):
        assert client.get("/api/backup/download", params={"file": bad}).status_code == 404, bad


def test_readonly_close_tolerates_missing_tempfile(data, private_tmp):
    compact_closed_months(data, ts(2026, 10, 3))
    before = _tmp_before()
    c = open_samples_readonly(data, ts(2026, 9, 7))
    for f in _tmp_left(before):
        f.unlink()
    c.close()


def test_too_many_same_second_snapshots(data, tmp_path, monkeypatch):
    def exists(a, b):
        raise FileExistsError

    monkeypatch.setattr(backup.os, "link", exists)
    with pytest.raises(RuntimeError):
        snapshot(data, tmp_path / "bk")
    assert [p.name for p in (tmp_path / "bk").iterdir()] == []


def test_verify_rejects_integrity_failure(tmp_path):
    c = sqlite3.connect(tmp_path / "a.db")
    c.execute("create table t(x int primary key)")
    c.execute("insert into t values(1)")
    c.commit()
    c.close()
    raw = bytearray((tmp_path / "a.db").read_bytes())
    raw[4096:8192] = b"\xff" * 4096  # destroy the table root page
    gz = tmp_path / "bad.db.gz"
    gz.write_bytes(gzip.compress(bytes(raw)))
    with pytest.raises(sqlite3.DatabaseError):
        backup._verify_gz(gz, hashlib.sha256(bytes(raw)).hexdigest())
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("verify")] == []





def _frozen_stamp(monkeypatch):
    class _D(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 10, 12, 0, 0, tzinfo=tz)
    monkeypatch.setattr(backup, "datetime", _D)


def test_snapshot_same_second_name_order_is_creation_order(tmp_path, monkeypatch):
    _frozen_stamp(monkeypatch)
    mk_meta(tmp_path)
    made = [snapshot(tmp_path, tmp_path / "bk", keep=3) for _ in range(6)]
    assert len({p.name for p in made}) == 6  # no slot ever reused
    assert [p.name for p in made] == sorted(p.name for p in made)
    assert list_backups(tmp_path / "bk") == made[-3:]


def test_snapshot_retention_after_middle_slot_deleted(tmp_path, monkeypatch):
    _frozen_stamp(monkeypatch)
    mk_meta(tmp_path)
    bk = tmp_path / "bk"
    made = [snapshot(tmp_path, bk, keep=10) for _ in range(4)]
    made[1].unlink()
    made[3].unlink()  # highest slot gone too: the next name still sorts after every survivor
    new = snapshot(tmp_path, bk, keep=2)
    assert new.name > made[2].name
    assert list_backups(bk) == [made[2], new]


def test_snapshot_counter_cap_raises(tmp_path, monkeypatch):
    _frozen_stamp(monkeypatch)
    mk_meta(tmp_path)
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "analytics-20260610T120000Z_999.tar").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="too many"):
        snapshot(tmp_path, bk)


# ---- POST /api/backup guards: same-origin and rate limit ----

def test_api_origin_mismatch_403(client, data):
    r = client.post("/api/backup", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and "detail" in r.json()
    assert not list((data / "backups").glob("*.tar")) if (data / "backups").exists() else True


def test_api_origin_matching_and_absent_ok(client):
    assert client.post("/api/backup", headers={"Origin": "http://testserver"}).status_code == 200


def test_api_rate_limited_with_retry_after(data):
    clock = [time.time()]
    app = create_app(Settings(server_url="http://x", data_dir=data))
    app.state.backup_clock = lambda: clock[0]
    c = TestClient(app)
    assert c.post("/api/backup").status_code == 200
    clock[0] += 30
    r = c.post("/api/backup")
    assert r.status_code == 429 and r.json()["detail"] and 1 <= int(r.headers["retry-after"]) <= 60
    assert c.get("/api/backup/download").status_code == 200
    clock[0] += 100  # snapshot may take a moment; stay clear of the 60 s edge
    assert c.post("/api/backup").status_code == 200


def test_api_future_dated_newest_does_not_block(client, data):
    _mk(data / "backups", "analytics-20990101T000000Z.tar", body=b"FUTURE")
    assert client.post("/api/backup").status_code == 200
