import gc
import gzip
import hashlib
import sqlite3
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hset, strategies as st

from app import backup
from app.backup import compact_closed_months, list_backups, snapshot
from app.config import Settings
from app.db import open_meta, open_samples, open_samples_readonly, samples_path
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

    def writer():
        c = open_samples(data, ts(2026, 10, 5))
        i = 1000
        try:
            while not stop.is_set():
                with c:
                    c.execute("insert into sample values(?,?,?,?,?,?,?)", (i, "w", "s", i, 1, i * 2, 1))
                    c.execute("insert into sample values(?,?,?,?,?,?,?)", (i, "w2", "s", i, 1, i * 2, 1))
                i += 1
        except Exception as e:  # pragma: no cover
            errs.append(e)
        finally:
            c.close()

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.2)
    try:
        tars = [snapshot(data, tmp_path / "bk", keep=10) for _ in range(3)]
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
    assert r > 0
    assert abs(r - r2) <= 0 or r == r2  # both inserts of a txn are present together
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
    compact_closed_months(data, ts(2026, 10, 1))
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
        (bk / f"analytics-2020010{i}T000000Z.tar").write_bytes(b"x")
    new = snapshot(data, bk)
    kept = list_backups(bk)
    assert len(kept) == 7 and kept[-1] == new
    assert [p.name for p in kept[:-1]] == [f"analytics-2020010{i}T000000Z.tar" for i in range(4, 10)]


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


def test_month_boundary(data):
    assert compact_closed_months(data, ts(2026, 10, 31, 23, 59, 59)) == [data / "samples-2026-09.db.gz"]
    assert (data / "samples-2026-10.db").exists()
    assert compact_closed_months(data, ts(2026, 11, 1)) == [data / "samples-2026-10.db.gz"]


def test_year_rollover(tmp_path):
    mk_month(tmp_path, 2025, 12)
    mk_month(tmp_path, 2026, 1)
    assert compact_closed_months(tmp_path, ts(2026, 1, 1)) == [tmp_path / "samples-2025-12.db.gz"]
    assert (tmp_path / "samples-2026-01.db").exists()


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
    compact_closed_months(data, ts(2026, 10, 1))
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

    def boom(gz):
        raise RuntimeError("crash")

    monkeypatch.setattr(backup, "_verify_gz", boom)
    assert compact_closed_months(data, ts(2026, 10, 1)) == []
    assert (data / "samples-2026-09.db").exists()
    assert not (data / "samples-2026-09.db.gz").exists()
    assert rows(data / "samples-2026-09.db", "select count(*) from sample") == [(3,)]
    monkeypatch.setattr(backup, "_verify_gz", orig)
    assert compact_closed_months(data, ts(2026, 10, 1)) == [data / "samples-2026-09.db.gz"]


def test_crash_after_gz_before_delete_rerun(data):
    """gz and original both present (crash between rename and unlink): rerun converges."""
    compact_closed_months(data, ts(2026, 10, 1))
    db = data / "samples-2026-09.db"
    db.write_bytes(gzip.open(data / "samples-2026-09.db.gz").read())
    assert compact_closed_months(data, ts(2026, 10, 1)) == [data / "samples-2026-09.db.gz"]
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
        assert compact_closed_months(data, ts(2026, 10, 1)) == []
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
    cur = datetime.fromtimestamp(now, timezone.utc)
    closed = {(y, m) for y, m in months if (y, m) < (cur.year, cur.month)}
    out = compact_closed_months(d, now)
    assert {p.name for p in out} == {f"samples-{y}-{m:02d}.db.gz" for y, m in closed}
    assert {p.name for p in d.glob("samples-*.db")} == {f"samples-{y}-{m:02d}.db" for y, m in months - closed}
    assert compact_closed_months(d, now) == []


# ---- open_samples_readonly / open_samples guards ----

def test_series_reads_gzipped_month(data):
    compact_closed_months(data, ts(2026, 10, 1))
    c = open_samples_readonly(data, ts(2026, 9, 7))
    assert c.execute("select count(*), min(seq), max(seq) from sample").fetchone() == (3, 0, 2)
    c.close()


def test_readonly_plain_month_is_readonly(data):
    c = open_samples_readonly(data, ts(2026, 10, 7))
    assert c.execute("select count(*) from sample").fetchone() == (3,)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("insert into sample values(1,'x','s',1,1,1,1)")
    c.close()


def _tmp_left(before):
    import tempfile
    return set(Path(tempfile.gettempdir()).glob("samples-ro-*")) - before


def _tmp_before():
    import tempfile
    return set(Path(tempfile.gettempdir()).glob("samples-ro-*"))


def test_readonly_gz_tempfile_removed_on_close(data):
    compact_closed_months(data, ts(2026, 10, 1))
    before = _tmp_before()
    c = open_samples_readonly(data, ts(2026, 9, 7))
    assert len(_tmp_left(before)) == 1
    c.close()
    c.close()  # idempotent
    assert _tmp_left(before) == set()
    assert not (data / "samples-2026-09.db").exists()  # never created in data_dir


def test_readonly_gz_tempfile_removed_on_gc(data):
    compact_closed_months(data, ts(2026, 10, 1))
    before = _tmp_before()
    c = open_samples_readonly(data, ts(2026, 9, 7))
    del c
    gc.collect()
    assert _tmp_left(before) == set()


def test_readonly_gz_tempfile_removed_on_exception(data):
    compact_closed_months(data, ts(2026, 10, 1))
    before = _tmp_before()
    c = open_samples_readonly(data, ts(2026, 9, 7))
    try:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("select * from nope")
    finally:
        c.close()
    assert _tmp_left(before) == set()


def test_readonly_bad_gz_cleans_up(tmp_path):
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
    compact_closed_months(data, ts(2026, 10, 1))
    with pytest.raises(RuntimeError):
        open_samples(data, ts(2026, 9, 7))
    assert not (data / "samples-2026-09.db").exists()


# ---- API ----

@pytest.fixture
def client(data):
    return TestClient(create_app(Settings(server_url="http://x", data_dir=data)))


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


def test_api_download_returns_newest_and_no_filename_param(client, data):
    client.post("/api/backup")
    last = client.post("/api/backup").json()["file"]
    d = client.get("/api/backup/download", params={"file": "../../meta.db"})
    assert d.content == (data / "backups" / last).read_bytes()
    assert client.get("/api/backup/download/../../meta.db").status_code in (404, 405)


def test_readonly_close_tolerates_missing_tempfile(data):
    compact_closed_months(data, ts(2026, 10, 1))
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
        backup._verify_gz(gz)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("verify")] == []
