import sqlite3
import threading

import pytest
from hypothesis import given, strategies as st

from app.core import db as _db
from app.core.db import samples_path


@pytest.fixture(autouse=True)
def _close_all(monkeypatch):
    opened = []

    def wrap(fn):
        def f(*a, **k):
            c = fn(*a, **k)
            opened.append(c)
            return c
        return f

    monkeypatch.setattr(_db, "open_meta", wrap(_db.open_meta))
    monkeypatch.setattr(_db, "open_samples", wrap(_db.open_samples))
    yield
    for c in opened:
        try:
            c.close()
        except sqlite3.Error:
            pass


def open_meta(*a, **k):
    return _db.open_meta(*a, **k)


def open_samples(*a, **k):
    return _db.open_samples(*a, **k)


def test_meta_schema_has_tables(tmp_path):
    c = open_meta(tmp_path)
    names = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    assert {"routes_tracked", "arrivals", "forecast", "gaps", "meta_kv"} <= names


def test_wal_enabled(tmp_path):
    assert open_meta(tmp_path).execute("pragma journal_mode").fetchone()[0] == "wal"


def test_creates_data_dir(tmp_path):
    open_meta(tmp_path / "a" / "b")
    assert (tmp_path / "a" / "b" / "meta.db").exists()


def test_reopen_idempotent_keeps_data(tmp_path):
    c = open_meta(tmp_path)
    c.execute("insert into routes_tracked values ('46A', 1)")
    c.commit()
    c.close()
    c = open_meta(tmp_path)
    assert c.execute("select route, added from routes_tracked").fetchall() == [("46A", 1)]


def _arr(c, trip="t", stop="s", seq=1, date="2026-11-08"):
    c.execute(
        "insert into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,"
        "confidence,realtime,route) values (?,?,?,?,1,2,1,1,1,'46A')",
        (trip, stop, seq, date),
    )


def test_arrival_unique_key_rejects_duplicate(tmp_path):
    c = open_meta(tmp_path)
    _arr(c)
    with pytest.raises(sqlite3.IntegrityError):
        _arr(c)
    # differing in any key column is fine
    _arr(c, trip="u")
    _arr(c, stop="x")
    _arr(c, seq=2)
    _arr(c, date="2026-11-09")
    assert c.execute("select count(*) from arrivals").fetchone()[0] == 5


def test_forecast_pk_and_route_pk(tmp_path):
    c = open_meta(tmp_path)
    c.execute("insert into forecast values (1,30,5,0)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("insert into forecast values (1,30,6,0)")
    c.execute("insert into forecast values (1,20,6,0)")
    c.execute("insert into routes_tracked values ('1',0)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("insert into routes_tracked values ('1',0)")
    c.execute("insert into meta_kv values ('k','v')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("insert into meta_kv values ('k','w')")


def test_concurrent_read_while_writing(tmp_path):
    w = open_meta(tmp_path)
    w.execute("insert into meta_kv values ('a','1')")
    w.commit()
    w.execute("begin")
    w.execute("insert into meta_kv values ('b','2')")  # uncommitted write open
    out = []

    def read():
        r = open_meta(tmp_path)
        out.append(r.execute("select count(*) from meta_kv").fetchone()[0])
        r.close()

    t = threading.Thread(target=read)
    t.start()
    t.join(5)
    assert out == [1]  # reader not blocked, sees only committed data
    w.commit()


def test_samples_file_named_by_month(tmp_path):
    ts = 1794096000  # 2026-11-08 UTC
    assert samples_path(tmp_path, ts) == tmp_path / "samples-2026-11.db"
    open_samples(tmp_path, ts)
    assert (tmp_path / "samples-2026-11.db").exists()


@pytest.mark.parametrize(
    "ts,name",
    [
        (1793491199, "samples-2026-10.db"),  # 2026-10-31 23:59:59
        (1793491200, "samples-2026-11.db"),  # 2026-11-01 00:00:00
        (1796083199, "samples-2026-11.db"),  # 2026-11-30 23:59:59
        (1796083200, "samples-2026-12.db"),  # 2026-12-01 00:00:00
        (1798761599, "samples-2026-12.db"),  # 2026-12-31 23:59:59
        (1798761600, "samples-2027-01.db"),  # 2027-01-01 00:00:00
        (0, "samples-1970-01.db"),
    ],
)
def test_month_boundaries_utc(tmp_path, ts, name):
    assert samples_path(tmp_path, ts).name == name


def test_samples_is_without_rowid_and_wal(tmp_path):
    c = open_samples(tmp_path, 0)
    assert "WITHOUT ROWID" in c.execute("select sql from sqlite_master where name='sample'").fetchone()[0].upper()
    assert c.execute("pragma journal_mode").fetchone()[0] == "wal"


def test_sample_pk_dedup_and_reopen(tmp_path):
    c = open_samples(tmp_path, 0)
    c.execute("insert into sample values (10,'t','s',1,5,100,1)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("insert into sample values (10,'t','s',1,4,99,0)")
    c.execute("insert or ignore into sample values (10,'t','s',1,4,99,0)")
    c.execute("insert into sample values (11,'t','s',1,5,100,1)")  # new ts ok
    c.commit()
    c.close()
    c = open_samples(tmp_path, 0)
    assert c.execute("select count(*) from sample").fetchone()[0] == 2
    assert c.execute("select due_min from sample where ts=10").fetchone()[0] == 5


@given(a=st.integers(0, 4_000_000_000), b=st.integers(0, 4_000_000_000))
def test_samples_path_monotone_and_named(a, b):
    from pathlib import Path
    import re

    pa, pb = samples_path(Path("d"), a), samples_path(Path("d"), b)
    assert re.fullmatch(r"samples-\d{4}-(0[1-9]|1[0-2])\.db", pa.name)
    if a <= b:
        assert pa.name <= pb.name


def test_indexes_exist_and_used(tmp_path):
    c = open_meta(tmp_path)
    names = {r[1] for r in c.execute("pragma index_list('arrivals')")}
    assert {"ix_arr_route_sched", "ix_arr_stop_sched"} <= names
    for col, ix in (("route", "ix_arr_route_sched"), ("stop_id", "ix_arr_stop_sched")):
        plan = " ".join(
            r[3] for r in c.execute(
                f"explain query plan select * from arrivals where {col}=? and scheduled between ? and ?",
                ("x", 1, 2))
        )
        assert ix in plan
    open_meta(tmp_path).close()  # idempotent index creation


def _cols(c, t):
    return [(r[1], r[2].lower()) for r in c.execute(f"pragma table_info({t})")]


def test_column_names_and_types(tmp_path):
    m = open_meta(tmp_path)
    assert _cols(m, "routes_tracked") == [("route", "text"), ("added", "int")]
    assert _cols(m, "arrivals") == [
        ("id", "integer"), ("trip_id", "text"), ("stop_id", "text"), ("seq", "int"),
        ("service_date", "text"), ("scheduled", "int"), ("actual", "int"), ("delay", "int"),
        ("confidence", "int"), ("realtime", "int"), ("route", "text")]
    assert _cols(m, "forecast") == [
        ("arrival_id", "int"), ("horizon_min", "int"), ("predicted", "int"), ("error_s", "int")]
    assert _cols(m, "gaps") == [("start", "int"), ("end", "int")]
    assert _cols(m, "meta_kv") == [("k", "text"), ("v", "text")]
    assert _cols(open_samples(tmp_path, 0), "sample") == [
        ("ts", "int"), ("trip_id", "text"), ("stop_id", "text"), ("seq", "int"),
        ("due_min", "int"), ("expected", "int"), ("realtime", "int")]


@pytest.mark.parametrize("opener", [lambda d: open_meta(d), lambda d: open_samples(d, 0)])
def test_connection_is_thread_bound(tmp_path, opener):
    c = opener(tmp_path)
    errs = []

    def use():
        try:
            c.execute("select 1")
        except sqlite3.ProgrammingError as e:
            errs.append(e)

    t = threading.Thread(target=use)
    t.start()
    t.join(5)
    assert len(errs) == 1


def test_samples_reopen_idempotent_and_concurrent_read(tmp_path):
    w = open_samples(tmp_path, 0)
    w.execute("insert into sample values (1,'t','s',1,5,100,1)")
    w.commit()
    w.close()
    w = open_samples(tmp_path, 0)
    assert w.execute("select count(*) from sample").fetchone()[0] == 1
    w.execute("begin")
    w.execute("insert into sample values (2,'t','s',1,5,100,1)")
    r = open_samples(tmp_path, 0)
    assert r.execute("select count(*) from sample").fetchone()[0] == 1
    w.commit()
    assert r.execute("select count(*) from sample").fetchone()[0] == 2


def test_meta_two_connections_read_while_writing(tmp_path):
    w, r = open_meta(tmp_path), open_meta(tmp_path)
    w.execute("begin")
    w.execute("insert into meta_kv values ('a','1')")
    assert r.execute("select count(*) from meta_kv").fetchone()[0] == 0
    w.commit()
    assert r.execute("select count(*) from meta_kv").fetchone()[0] == 1
