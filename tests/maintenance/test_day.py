import gzip
import sqlite3
import threading
from datetime import date, datetime, timezone

import pytest

from app.core.db import open_meta, open_samples
from app.maintenance import day as D

DAY = date(2026, 11, 5)  # winter: local == UTC
W0, W1 = D.window(DAY)


def _arr(c, i, route, sd="2026-11-05", forecasts=2):
    c.execute("insert into arrivals(id,trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,realtime,route)"
              " values (?,?,?,?,?,?,?,?,?,?,?)", (i, f"t{i}", f"s{i}", i, sd, W0 + 100, W0 + 160, 60, 1, 1, route))
    for h in range(forecasts):
        c.execute("insert into forecast values (?,?,?,?)", (i, h, 1, 1))


def _sample(c, ts, n, trip=None):
    c.execute("insert into sample values (?,?,?,?,?,?,?)", (ts, trip or f"t{n}", f"s{n}", n, 1, ts, 1))


def _count(path, table):
    c = sqlite3.connect(path)
    try:
        return c.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        c.close()


@pytest.fixture
def data(tmp_path):
    m = open_meta(tmp_path)
    _arr(m, 1, "15"); _arr(m, 2, "15"); _arr(m, 3, "7"); _arr(m, 4, "15", sd="2026-11-04"); _arr(m, 5, "15", sd="2026-11-06")
    m.executemany("insert into gaps values (?,?)", [(W0 + 10, W0 + 20), (W0 - 100, W0 - 50), (W1 + 5, W1 + 9)])
    m.execute("insert into routes_tracked values ('15', 1)"); m.execute("insert into meta_kv values ('k','v')")
    m.commit(); m.close()
    s = open_samples(tmp_path, W0)
    for n in (1, 2, 3):
        _sample(s, W0 + 50 + n, n)
    _sample(s, W0 + 70, 99)          # orphan: no arrivals row
    _sample(s, W0 - 5, 4)             # previous day
    _sample(s, W1 + 5, 5)             # next day
    s.commit(); s.close()
    return tmp_path


def test_window_dst():
    assert D.window(date(2026, 3, 29))[1] - D.window(date(2026, 3, 29))[0] == 23 * 3600
    assert D.window(date(2026, 10, 25))[1] - D.window(date(2026, 10, 25))[0] == 25 * 3600
    assert D.window(DAY)[1] - D.window(DAY)[0] == 86400


@pytest.mark.parametrize("route,exp", [(None, (3, 6, 1, 4)), ("15", (2, 4, 0, 2)), ("7", (1, 2, 0, 1))])
def test_preview_equals_remove(data, route, exp):
    p = D.preview(data, DAY, route)
    assert (p["arrivals"], p["forecasts"], p["gaps"], p["samples"]) == exp
    assert p["months"] == ["samples-2026-11.db"] and p["skipped_months"] == [] and p["truncated"] is False
    assert _count(data / "meta.db", "arrivals") == 5  # preview is read-only
    r = D.remove(data, DAY, route)
    for k in ("arrivals", "forecasts", "gaps", "samples", "months", "skipped_months"):
        assert r[k] == p[k]
    assert _count(data / "meta.db", "arrivals") == 5 - exp[0]
    assert _count(data / "meta.db", "forecast") == 10 - exp[1]
    assert _count(data / "samples-2026-11.db", "sample") == 6 - exp[3]


def test_all_routes_leaves_other_days_and_kv(data):
    D.remove(data, DAY, None)
    m = sqlite3.connect(data / "meta.db")
    assert sorted(r[0] for r in m.execute("select id from arrivals")) == [4, 5]
    assert sorted(r[0] for r in m.execute("select distinct arrival_id from forecast")) == [4, 5]
    assert m.execute("select count(*) from gaps").fetchone()[0] == 2
    assert m.execute("select count(*) from routes_tracked").fetchone()[0] == 1
    assert m.execute("select count(*) from meta_kv").fetchone()[0] == 1
    s = sqlite3.connect(data / "samples-2026-11.db")
    assert sorted(r[0] for r in s.execute("select seq from sample")) == [4, 5]


def test_route_mode_keeps_orphans_gaps_and_other_routes(data):
    D.remove(data, DAY, "15")
    s = sqlite3.connect(data / "samples-2026-11.db")
    assert sorted(r[0] for r in s.execute("select seq from sample")) == [3, 4, 5, 99]
    assert _count(data / "meta.db", "gaps") == 3


def test_window_spanning_two_months(tmp_path):
    d = date(2026, 8, 1)  # local midnight is 2026-07-31T23:00Z
    w0, w1 = D.window(d)
    assert datetime.fromtimestamp(w0, timezone.utc).month == 7
    for ts in (w0 - 1, w0, w1 - 1, w1):
        s = open_samples(tmp_path, ts); _sample(s, ts, ts % 1000); s.commit(); s.close()
    p = D.preview(tmp_path, d, None)
    assert p["months"] == ["samples-2026-07.db", "samples-2026-08.db"] and p["samples"] == 2
    assert D.remove(tmp_path, d, None)["samples"] == 2
    assert _count(tmp_path / "samples-2026-07.db", "sample") == 1
    assert _count(tmp_path / "samples-2026-08.db", "sample") == 1


def test_gz_month_skipped_and_untouched(tmp_path):
    d = date(2026, 8, 1)
    w0, w1 = D.window(d)
    s = open_samples(tmp_path, w1 - 1); _sample(s, w1 - 1, 1); s.commit(); s.close()
    src = tmp_path / "samples-2026-07.db"
    s = sqlite3.connect(src); s.execute("create table sample(ts)"); s.commit(); s.close()
    gz = tmp_path / "samples-2026-07.db.gz"
    gz.write_bytes(gzip.compress(src.read_bytes())); src.unlink()
    before = gz.read_bytes()
    p = D.preview(tmp_path, d, None)
    r = D.remove(tmp_path, d, None)
    assert p["skipped_months"] == r["skipped_months"] == ["samples-2026-07.db.gz"]
    assert r["months"] == ["samples-2026-08.db"] and r["samples"] == 1
    assert gz.read_bytes() == before and not src.exists()


def test_empty_and_missing(tmp_path, data):
    D.remove(data, DAY, None)
    r = D.remove(data, DAY, None)
    assert (r["arrivals"], r["forecasts"], r["samples"], r["gaps"]) == (0, 0, 0, 0)
    empty = tmp_path / "nothing"; empty.mkdir()
    r = D.remove(empty, DAY, "15")
    assert (r["arrivals"], r["samples"]) == (0, 0) and r["months"] == []
    assert D.preview(empty, DAY, None)["arrivals"] == 0
    assert not (empty / "meta.db").exists()


def test_parse_day_validation():
    today = date(2026, 11, 11)
    assert D.parse_day("2026-11-12", today) == date(2026, 11, 12)
    for bad in ("", "2026-13-01", "2026-02-30", "20261105", "2026-11-05\n", "2026-11-13", "2019-12-31", "x"):
        with pytest.raises(D.DayError):
            D.parse_day(bad, today)


def test_batching_commits_per_chunk(data, monkeypatch):
    m = open_meta(data)
    for i in range(10, 40):
        _arr(m, i, "15", forecasts=1)
    s = open_samples(data, W0)
    for i in range(10, 40):
        _sample(s, W0 + 200 + i, i)
    m.commit(); m.close(); s.commit(); s.close()
    monkeypatch.setattr(D, "CHUNK", 7)
    calls = []
    monkeypatch.setattr(D, "_between_chunks", lambda: calls.append(1))
    r = D.remove(data, DAY, None)
    assert r["arrivals"] == 33 and r["samples"] == 34 and r["forecasts"] == 33 + 3
    assert len(calls) >= 5 + 5  # 34 samples / 7, 33 arrivals / 7, gaps
    assert _count(data / "meta.db", "arrivals") == 2
    assert _count(data / "samples-2026-11.db", "sample") == 2


def test_concurrent_writer_not_blocked_and_rows_survive(data, monkeypatch):
    monkeypatch.setattr(D, "CHUNK", 1)
    path = data / "samples-2026-11.db"
    inserted = []

    def hook():
        if len(inserted) < 3:
            w = sqlite3.connect(path, timeout=5)  # the tracker's writer between our chunks
            w.execute("insert into sample values (?,?,?,?,?,?,?)", (W1 + 1000 + len(inserted), "new", "n", 1, 1, 1, 1))
            w.commit(); w.close()
            inserted.append(1)
    monkeypatch.setattr(D, "_between_chunks", hook)
    D.remove(data, DAY, None)
    s = sqlite3.connect(path)
    assert s.execute("select count(*) from sample where trip_id = 'new'").fetchone()[0] == 3


def test_locked_month_reports_partial(data, monkeypatch):
    real = D._open

    def fast(p):
        c = real(p)
        c.execute("pragma busy_timeout=50")
        return c
    monkeypatch.setattr(D, "_open", fast)
    blocker = sqlite3.connect(data / "samples-2026-11.db", isolation_level=None)
    blocker.execute("begin immediate")
    try:
        with pytest.raises(D.DayBusy) as e:
            D.remove(data, DAY, None)
    finally:
        blocker.execute("rollback"); blocker.close()
    assert e.value.part == "samples-2026-11.db"
    assert _count(data / "meta.db", "arrivals") == 5  # meta untouched, retry possible
    assert D.remove(data, DAY, None)["arrivals"] == 3
