import sqlite3
import threading

import pytest
from hypothesis import given, strategies as st

from app.db import open_meta, open_samples, samples_path


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
