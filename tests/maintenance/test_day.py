import gzip
import sqlite3
import threading
import time
from functools import partial
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
    m.execute("insert into routes_tracked values ('15', 1)")
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


def test_all_routes_leaves_other_days_and_tracked_routes(data):
    D.remove(data, DAY, None)
    m = sqlite3.connect(data / "meta.db")
    assert sorted(r[0] for r in m.execute("select id from arrivals")) == [4, 5]
    assert sorted(r[0] for r in m.execute("select distinct arrival_id from forecast")) == [4, 5]
    assert m.execute("select count(*) from gaps").fetchone()[0] == 2
    assert m.execute("select count(*) from routes_tracked").fetchone()[0] == 1
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


def _fast_busy(monkeypatch):
    real = D._open

    def fast(p, rw):
        c = real(p, rw)
        c.execute("pragma busy_timeout=50")
        return c
    monkeypatch.setattr(D, "_open", fast)


def test_locked_month_reports_partial(data, monkeypatch):
    _fast_busy(monkeypatch)
    blocker = sqlite3.connect(data / "samples-2026-11.db", isolation_level=None)
    blocker.execute("begin immediate")
    try:
        with pytest.raises(D.DayBusy) as e:
            D.remove(data, DAY, None)
    finally:
        blocker.execute("rollback"); blocker.close()
    assert e.value.part == "samples-2026-11.db" and e.value.kind == "busy"
    assert e.value.done["samples"] == 0 and e.value.done["arrivals"] == 0
    assert _count(data / "meta.db", "arrivals") == 5  # meta untouched, retry possible
    assert D.remove(data, DAY, None)["arrivals"] == 3


def test_locked_mid_month_done_is_exact(data, monkeypatch):
    _fast_busy(monkeypatch)
    monkeypatch.setattr(D, "CHUNK", 2)
    blk, calls = [], []

    def hook():
        calls.append(1)
        if len(calls) == 1:  # after the first committed chunk another writer grabs the file
            b = sqlite3.connect(data / "samples-2026-11.db", isolation_level=None)
            b.execute("begin immediate")
            blk.append(b)
    monkeypatch.setattr(D, "_between_chunks", hook)
    try:
        with pytest.raises(D.DayBusy) as e:
            D.remove(data, DAY, None)
    finally:
        for b in blk:
            b.execute("rollback"); b.close()
    assert e.value.done["samples"] == 2 and e.value.done["arrivals"] == 0
    assert _count(data / "samples-2026-11.db", "sample") == 6 - 2


def test_vanished_month_not_recreated(data, monkeypatch):
    real = D._classify

    def racing(dd, w):
        out = real(dd, w)  # compaction gzips and unlinks the plain month right after we listed it
        (dd / "samples-2026-11.db").unlink()
        for suf in ("-wal", "-shm"):
            (dd / ("samples-2026-11.db" + suf)).unlink(missing_ok=True)
        (dd / "samples-2026-11.db.gz").write_bytes(b"gz")
        return out
    monkeypatch.setattr(D, "_classify", racing)
    p = D.preview(data, DAY, None)
    assert p["months"] == [] and p["skipped_months"] == ["samples-2026-11.db.gz"] and p["samples"] == 0
    assert not (data / "samples-2026-11.db").exists()
    assert sorted(f.name for f in data.glob("samples-*")) == ["samples-2026-11.db.gz"]


def test_vanished_month_not_recreated_on_remove(data, monkeypatch):
    real = D._classify

    def racing(dd, w):
        out = real(dd, w)
        for f in dd.glob("samples-2026-11.db*"):
            f.unlink()
        return out
    monkeypatch.setattr(D, "_classify", racing)
    r = D.remove(data, DAY, None)
    assert r["months"] == [] and r["arrivals"] == 3
    assert list(data.glob("samples-*")) == []


def test_preview_does_not_modify_files(data):
    before = {f.name: f.read_bytes() for f in data.glob("*.db")}
    D.preview(data, DAY, "15")
    assert {f.name: f.read_bytes() for f in data.glob("*.db")} == before


def test_route_first_chunk_survives_concurrent_commit(data, monkeypatch):
    orig = sqlite3.connect
    path = data / "samples-2026-11.db"
    fired = []

    class C(sqlite3.Connection):
        def executemany(self, sql, rows):
            if sql.startswith("delete from sample") and not fired:
                fired.append(1)  # the tracker commits between our SELECT and our first DELETE
                w = orig(path, timeout=5)
                w.execute("insert into sample values (?,?,?,?,?,?,?)", (W1 + 500, "new", "n", 1, 1, 1, 1))
                w.commit(); w.close()
            return super().executemany(sql, rows)
    monkeypatch.setattr(D.sqlite3, "connect", partial(orig, factory=C))
    r = D.remove(data, DAY, "15")
    assert fired and r["samples"] == 2
    assert orig(path).execute("select count(*) from sample where trip_id='new'").fetchone()[0] == 1


# --- test strength -------------------------------------------------------------------------------
def test_route_mode_keyed_sample_outside_window_survives(data):
    s = open_samples(data, W0)
    _sample(s, W0 - 3600, 1)   # same trip/stop/seq as deleted arrival 1, previous day
    _sample(s, W1 + 3600, 1)   # and next day
    s.commit(); s.close()
    p = D.preview(data, DAY, "15")
    r = D.remove(data, DAY, "15")
    assert p["samples"] == r["samples"] == 2
    s = sqlite3.connect(data / "samples-2026-11.db")
    assert sorted(x for x, in s.execute("select ts from sample where seq = 1")) == [W0 - 3600, W1 + 3600]


@pytest.mark.parametrize("day,start_utc,end_utc,month", [
    (date(2026, 3, 29), (2026, 3, 29, 0), (2026, 3, 29, 23), "2026-03"),
    (date(2026, 10, 25), (2026, 10, 24, 23), (2026, 10, 26, 0), "2026-10")])
def test_dst_windows_and_boundaries(tmp_path, day, start_utc, end_utc, month):
    a = int(datetime(*start_utc, tzinfo=timezone.utc).timestamp())
    b = int(datetime(*end_utc, tzinfo=timezone.utc).timestamp())
    assert D.window(day) == (a, b)
    s = open_samples(tmp_path, a)
    for ts in (a - 1, a, b - 1, b):
        _sample(s, ts, ts % 1000)
    s.commit(); s.close()
    assert D.preview(tmp_path, day, None)["samples"] == 2
    assert D.remove(tmp_path, day, None)["samples"] == 2
    c = sqlite3.connect(tmp_path / f"samples-{month}.db")
    assert sorted(x for x, in c.execute("select ts from sample")) == [a - 1, b]


def test_plain_and_gz_both_present_plain_untouched(tmp_path):
    d = date(2026, 8, 1)
    w0, w1 = D.window(d)
    s = open_samples(tmp_path, w0 - 1); _sample(s, w0 + 0, 1); s.commit(); s.close()
    plain = tmp_path / "samples-2026-07.db"
    (tmp_path / "samples-2026-07.db.gz").write_bytes(gzip.compress(b"x"))
    before = plain.read_bytes()
    p = D.preview(tmp_path, d, None)
    r = D.remove(tmp_path, d, None)
    assert p["skipped_months"] == r["skipped_months"] == ["samples-2026-07.db.gz"]
    assert r["months"] == [] and r["samples"] == 0  # 2026-08 does not exist: simply omitted
    assert plain.read_bytes() == before


def test_keyset_pagination_same_key_many_ts(tmp_path, monkeypatch):
    m = open_meta(tmp_path)
    _arr(m, 1, "15"); _arr(m, 2, "7")
    m.commit(); m.close()
    s = open_samples(tmp_path, W0)
    for k in range(10):
        _sample(s, W0 + 10 + k, 1)          # key of route 15, 10 timestamps
        _sample(s, W0 + 10 + k, 2)          # key of route 7
    for ts in (W0 - 1, W1):
        _sample(s, ts, 1)
    s.commit(); s.close()
    monkeypatch.setattr(D, "CHUNK", 3)
    assert D.remove(tmp_path, DAY, "15")["samples"] == 10
    c = sqlite3.connect(tmp_path / "samples-2026-11.db")
    assert c.execute("select count(*) from sample where seq = 1").fetchone()[0] == 2
    assert c.execute("select count(*) from sample where seq = 2").fetchone()[0] == 10


def test_forecast_chunks_precise_and_no_orphans_on_failure(data, monkeypatch):
    monkeypatch.setattr(D, "CHUNK", 1)
    m = sqlite3.connect(data / "meta.db")
    # the second arrival chunk fails mid-transaction, after its forecast delete ran
    m.execute("create trigger boom before delete on arrivals when old.id = 2 begin select raise(abort, 'secret'); end")
    m.commit(); m.close()
    with pytest.raises(D.DayBusy) as e:
        D.remove(data, DAY, None)
    assert e.value.kind == "error" and e.value.done["arrivals"] == 1 and e.value.done["forecasts"] == 2
    c = sqlite3.connect(data / "meta.db")
    assert c.execute("select count(*) from forecast where arrival_id not in (select id from arrivals)").fetchone()[0] == 0
    assert sorted(r for r, in c.execute("select distinct arrival_id from forecast")) == [2, 3, 4, 5]
    assert sorted(r for r, in c.execute("select id from arrivals")) == [2, 3, 4, 5]


def test_concurrent_writer_thread_during_multichunk_remove(data, monkeypatch):
    m = open_meta(data)
    for i in range(10, 30):
        _arr(m, i, "15", forecasts=1)
    s = open_samples(data, W0)
    for i in range(10, 30):
        _sample(s, W0 + 300 + i, i)
    m.commit(); m.close(); s.commit(); s.close()
    monkeypatch.setattr(D, "CHUNK", 3)
    monkeypatch.setattr(D, "_between_chunks", lambda: time.sleep(0.01))
    path = data / "samples-2026-11.db"
    lat, errs = [], []

    def writer():
        try:
            for i in range(25):
                t = time.monotonic()
                w = sqlite3.connect(path, timeout=5)
                w.execute("insert into sample values (?,?,?,?,?,?,?)", (W1 + 1000 + i, "new", "n", 1, 1, 1, 1))
                w.commit(); w.close()
                lat.append(time.monotonic() - t)
                time.sleep(0.005)
        except Exception as e:  # pragma: no cover
            errs.append(e)
    th = threading.Thread(target=writer); th.start()
    r = D.remove(data, DAY, None)
    th.join()
    assert not errs and r["arrivals"] == 23 and max(lat) < 5
    assert sqlite3.connect(path).execute("select count(*) from sample where trip_id='new'").fetchone()[0] == 25


TODAY = date(2026, 11, 12)


def test_parse_range_single_and_multi():
    assert D.parse_range("2026-11-05", None, TODAY) == [date(2026, 11, 5)]
    r = D.parse_range("2026-11-05", "2026-11-07", TODAY)
    assert r == [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 7)]
    assert D.MAX_DAYS == 62


def test_parse_range_limits_and_errors():
    assert len(D.parse_range("2026-09-01", "2026-11-01", TODAY)) == 62
    for a, b in [("2026-11-05", "2026-11-04"), ("2026-09-01", "2026-11-02"),
                 ("2026-11-05", "nope"), ("2026-11-05", "2026-13-40"), ("bad", None),
                 ("2026-11-05", "2026-12-30")]:
        with pytest.raises(D.DayError):
            D.parse_range(a, b, TODAY)


def test_preview_range_sums_days(data):
    d2 = date(2026, 11, 6)
    p = D.preview_range(data, [DAY, d2], None)
    p1, p2 = D.preview(data, DAY, None), D.preview(data, d2, None)
    assert p["date"] == "2026-11-05" and p["end"] == "2026-11-06"
    for k in ("arrivals", "forecasts", "samples", "gaps"):
        assert p[k] == p1[k] + p2[k]
    assert [x["date"] for x in p["days"]] == ["2026-11-05", "2026-11-06"]
    assert p["days"][0] == {"date": "2026-11-05", "arrivals": 3, "forecasts": 6, "samples": 4, "gaps": 1}
    assert p["months"] == ["samples-2026-11.db"] and p["skipped_months"] == []


def test_remove_progress_callback(data, monkeypatch):
    monkeypatch.setattr(D, "CHUNK", 1)
    calls = []
    D.remove(data, DAY, None, progress=lambda stage, out: calls.append((stage, dict(out))))
    stages = [s for s, _ in calls]
    assert stages[0] == "samples" and "arrivals" in stages
    assert stages.index("arrivals") > max(i for i, s in enumerate(stages) if s == "samples")
    assert set(stages) <= {"samples", "arrivals", "gaps"}
    for k in ("samples", "arrivals", "forecasts", "gaps"):
        vals = [o[k] for _, o in calls]
        assert vals == sorted(vals)
    assert calls[-1][1]["samples"] == 4 and calls[-1][1]["arrivals"] == 3
