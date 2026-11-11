import gzip
import json
import threading
import zlib

from app.core.db import open_meta
from app.export.stream import gzip_chunks, iter_export

KEYS = ["version", "exported_at", "filters", "routes_tracked", "gaps", "arrivals", "samples", "complete"]


def _arr(i, route="15", scheduled=1000):
    return (i, f"t{i}", f"s{i}", i, "2026-11-01", scheduled, scheduled + 60, 60, 0.9, 1, route)


def _build(tmp_path, arrivals=(), forecasts=(), routes=(), gaps=()):
    c = open_meta(tmp_path)
    c.executemany("insert into routes_tracked(route, added) values (?,?)", routes)
    c.executemany("insert into gaps(start, end) values (?,?)", gaps)
    c.executemany(
        "insert into arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route)"
        " values (?,?,?,?,?,?,?,?,?,?,?)", arrivals)
    c.executemany("insert into forecast(arrival_id, horizon_min, predicted, error_s) values (?,?,?,?)", forecasts)
    c.commit()
    c.close()


def _doc(tmp_path, **kw):
    return json.loads("".join(iter_export(tmp_path, **kw)))


def test_document_shape_and_key_order(tmp_path):
    _build(tmp_path)
    d = _doc(tmp_path, exported_at=123)
    assert list(d) == KEYS
    assert d["version"] == 1 and d["exported_at"] == 123 and d["complete"] is True
    assert d["samples"] == []


def test_arrivals_with_nested_forecast(tmp_path):
    _build(tmp_path, arrivals=[_arr(1), _arr(2)], forecasts=[(1, 10, 100, 5), (1, 5, 90, 3)],
           routes=[("15", 1)], gaps=[(1, 2)])
    d = _doc(tmp_path)
    a1, a2 = d["arrivals"]
    assert a1["id"] == 1 and a1["route"] == "15" and a1["scheduled"] == 1000 and a1["delay"] == 60
    assert a1["forecast"] == [{"horizon_min": 5, "predicted": 90, "error_s": 3},
                              {"horizon_min": 10, "predicted": 100, "error_s": 5}]
    assert a2["forecast"] == []
    assert d["routes_tracked"] == [{"route": "15", "added": 1}]
    assert d["gaps"] == [{"start": 1, "end": 2}]


def test_filters_route_since_until(tmp_path):
    _build(tmp_path, arrivals=[_arr(1, "15", 100), _arr(2, "7", 200), _arr(3, "15", 300), _arr(4, "7", 400)])

    def ids(**kw):
        return [a["id"] for a in _doc(tmp_path, **kw)["arrivals"]]

    assert ids(route="15") == [1, 3]
    assert ids(since=200) == [2, 3, 4]
    assert ids(until=300) == [1, 2, 3]
    assert ids(route="7", since=200, until=400) == [2, 4]
    assert ids(route="15", since=200, until=300) == [3]
    assert _doc(tmp_path, route="15", since=1, until=2)["filters"] == {"route": "15", "since": 1, "until": 2}


def test_missing_meta_db_gives_empty_valid_document(tmp_path):
    d = _doc(tmp_path)
    assert d["routes_tracked"] == d["gaps"] == d["arrivals"] == d["samples"] == []
    assert d["complete"] is True


def test_chunks_are_bounded(tmp_path):
    _build(tmp_path, arrivals=[_arr(i) for i in range(1, 5001)])
    chunks = list(iter_export(tmp_path))
    assert len(chunks) > 1
    assert all(len(c.encode()) < 256 * 1024 for c in chunks)
    assert len(json.loads("".join(chunks))["arrivals"]) == 5000


def test_connections_usable_across_threads(tmp_path):
    _build(tmp_path, arrivals=[_arr(i) for i in range(1, 3001)], forecasts=[(1, 5, 1, 1)])
    gen = iter_export(tmp_path)
    out, errs = [], []

    def step():
        try:
            out.append(next(gen))
        except StopIteration:
            out.append(None)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    while not out or out[-1] is not None:
        t = threading.Thread(target=step)
        t.start()
        t.join()
        assert not errs, errs
    assert json.loads("".join(c for c in out if c))["complete"] is True


# ---- samples across months ----
import gzip
import os
import shutil
import tempfile

from app.core.db import SAMPLES_SCHEMA, samples_path
from app.export import stream
from app.export.stream import sample_months

JAN = 1767225600  # 2026-01-01 UTC
FEB = 1769904000  # 2026-02-01 UTC
MAR = 1772323200  # 2026-03-01 UTC


def _month(tmp_path, ts, rows, gz=False):
    import sqlite3
    p = samples_path(tmp_path, ts)
    c = sqlite3.connect(p)
    c.executescript(SAMPLES_SCHEMA)
    c.executemany("insert into sample values (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.execute("pragma journal_mode=delete")
    c.close()
    if gz:
        with open(p, "rb") as f, gzip.open(str(p) + ".gz", "wb") as g:
            shutil.copyfileobj(f, g)
        p.unlink()


def _s(ts, trip, stop="s", seq=1):
    return (ts, trip, stop, seq, 3, ts + 180, 1)


def _tmp_listing():
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("samples-ro-")}


def test_samples_span_months_in_order(tmp_path):
    _build(tmp_path)
    _month(tmp_path, FEB, [_s(FEB + 5, "b"), _s(FEB + 1, "a")])
    _month(tmp_path, JAN, [_s(JAN + 9, "z"), _s(JAN + 2, "y")])
    d = _doc(tmp_path)
    assert [(x["trip_id"]) for x in d["samples"]] == ["y", "z", "a", "b"]
    assert list(d["samples"][0]) == ["ts", "trip_id", "stop_id", "seq", "due_min", "expected", "realtime"]
    assert list(d) == KEYS
    assert sample_months(tmp_path) == [JAN, FEB]


def test_gz_month_is_included_and_temp_removed(tmp_path):
    _build(tmp_path)
    _month(tmp_path, JAN, [_s(JAN + 1, "g1"), _s(JAN + 2, "g2")], gz=True)
    _month(tmp_path, FEB, [_s(FEB + 1, "p1")])
    before = _tmp_listing()
    d = _doc(tmp_path)
    assert [x["trip_id"] for x in d["samples"]] == ["g1", "g2", "p1"]
    assert _tmp_listing() == before
    assert sample_months(tmp_path) == [JAN, FEB]


def test_samples_filtered_by_ts(tmp_path, monkeypatch):
    _build(tmp_path)
    _month(tmp_path, JAN, [_s(JAN + 1, "j")])
    _month(tmp_path, FEB, [_s(FEB + 10, "f1"), _s(FEB + 20, "f2"), _s(FEB + 30, "f3")])
    _month(tmp_path, MAR, [_s(MAR + 1, "m")])
    opened = []
    real = stream.open_samples_readonly
    monkeypatch.setattr(stream, "open_samples_readonly",
                        lambda d, ts, check_same_thread=True: (opened.append(ts), real(d, ts, check_same_thread))[1])
    d = _doc(tmp_path, since=FEB + 20, until=FEB + 30)
    assert [x["trip_id"] for x in d["samples"]] == ["f2", "f3"]
    assert opened == [FEB]


def test_route_filter_joins_through_arrivals(tmp_path):
    _build(tmp_path, arrivals=[(1, "t15", "s", 1, "2026-01-01", 10, 20, 10, 0.9, 1, "15"),
                               (2, "t7", "s", 1, "2026-01-01", 10, 20, 10, 0.9, 1, "7")])
    _month(tmp_path, JAN, [_s(JAN + 1, "t15"), _s(JAN + 2, "t7"), _s(JAN + 3, "t15", seq=9)])
    _month(tmp_path, FEB, [_s(FEB + 1, "t15"), _s(FEB + 2, "t7")], gz=True)
    d = _doc(tmp_path, route="15")
    assert [(x["trip_id"], x["ts"]) for x in d["samples"]] == [("t15", JAN + 1), ("t15", FEB + 1)]


def test_route_filter_without_meta_emits_no_samples(tmp_path):
    _month(tmp_path, JAN, [_s(JAN + 1, "t15")])
    d = _doc(tmp_path, route="15")
    assert d["samples"] == [] and d["complete"] is True


def test_no_sample_files_gives_empty_samples(tmp_path):
    _build(tmp_path)
    assert _doc(tmp_path)["samples"] == []
    assert sample_months(tmp_path) == []


# ---- review fixes ----
def test_many_rows_and_empty_month_give_valid_json(tmp_path):
    _build(tmp_path)
    _month(tmp_path, JAN, [_s(JAN + i, f"a{i:05d}") for i in range(2500)], gz=True)
    _month(tmp_path, FEB, [])
    _month(tmp_path, MAR, [_s(MAR + i, f"b{i:05d}") for i in range(1500)])
    d = _doc(tmp_path)
    assert len(d["samples"]) == 4000
    assert d["complete"] is True


def _spy_open(monkeypatch):
    seen = []
    real = stream.open_samples_readonly

    def spy(d, ts, check_same_thread=True):
        seen.append((ts, _tmp_listing()))
        return real(d, ts, check_same_thread)
    monkeypatch.setattr(stream, "open_samples_readonly", spy)
    return seen


def test_previous_temp_copy_removed_before_next_month_opens(tmp_path, monkeypatch):
    _build(tmp_path)
    _month(tmp_path, JAN, [_s(JAN + 1, "a")], gz=True)
    _month(tmp_path, FEB, [_s(FEB + 1, "b")], gz=True)
    before = _tmp_listing()
    seen = _spy_open(monkeypatch)
    _doc(tmp_path)
    assert [ts for ts, _ in seen] == [JAN, FEB]
    assert all(listing == before for _, listing in seen)
    assert _tmp_listing() == before


def test_temp_copy_removed_when_generator_closed_early(tmp_path):
    _build(tmp_path)
    _month(tmp_path, JAN, [_s(JAN + i, f"a{i:05d}") for i in range(2500)], gz=True)
    before = _tmp_listing()
    gen = iter_export(tmp_path)
    for _ in gen:
        if _tmp_listing() != before:
            break
    assert _tmp_listing() != before
    gen.close()
    assert _tmp_listing() == before


def test_temp_copy_removed_when_error_mid_month(tmp_path, monkeypatch):
    _build(tmp_path)
    _month(tmp_path, JAN, [_s(JAN + 1, "a")], gz=True)
    before = _tmp_listing()
    real = stream._dumps

    def boom(o):
        if isinstance(o, dict) and "trip_id" in o:
            raise RuntimeError("boom")
        return real(o)
    monkeypatch.setattr(stream, "_dumps", boom)
    import pytest
    with pytest.raises(RuntimeError):
        _doc(tmp_path)
    assert _tmp_listing() == before


def test_gzip_round_trip(tmp_path):
    _build(tmp_path, arrivals=[_arr(i) for i in range(1, 50)], routes=[("15", 1)])
    plain = "".join(iter_export(tmp_path, exported_at=5))
    gz = b"".join(gzip_chunks(iter_export(tmp_path, exported_at=5)))
    assert gz[:2] == b"\x1f\x8b"
    assert gzip.decompress(gz).decode() == plain


def test_gzip_empty_input_is_valid_stream():
    assert gzip.decompress(b"".join(gzip_chunks(iter([])))) == b""
    assert gzip.decompress(b"".join(gzip_chunks(iter(["", ""])))) == b""


def test_gzip_flushes_per_chunk_and_is_lazy():
    pulled = []

    def src():
        for c in ("aaa", "bbb", "ccc"):
            pulled.append(c)
            yield c

    g = gzip_chunks(src())
    dec = zlib.decompressobj(wbits=31)
    out = b""
    out += dec.decompress(next(g))
    assert pulled == ["aaa"] and out == b"aaa"
    out += dec.decompress(next(g))
    assert pulled == ["aaa", "bbb"] and out == b"aaabbb"
    out += b"".join(dec.decompress(p) for p in g)
    assert out == b"aaabbbccc" and dec.eof
