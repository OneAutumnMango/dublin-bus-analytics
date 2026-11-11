import json
import threading

from app.core.db import open_meta
from app.export.stream import iter_export

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
