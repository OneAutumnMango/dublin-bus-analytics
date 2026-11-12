import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.backup import backup as bk
from app.core.config import Settings
from app.core.db import open_meta, open_samples
from app.main import create_app
from app.maintenance import day as D
from app.maintenance import jobs as J

DAY = "2026-11-05"
W0, W1 = D.window(D.date(2026, 11, 5))


@pytest.fixture
def client(tmp_path):
    m = open_meta(tmp_path)
    m.execute("insert into arrivals(id,trip_id,stop_id,seq,service_date,scheduled,route) values (1,'t','s',1,?,?,'15')", (DAY, W0 + 5))
    m.execute("insert into forecast values (1,5,1,1)")
    m.commit(); m.close()
    s = open_samples(tmp_path, W0)
    s.execute("insert into sample values (?,?,?,?,?,?,?)", (W0 + 5, "t", "s", 1, 1, 1, 1))
    s.commit(); s.close()
    return TestClient(create_app(Settings("http://fake", tmp_path, 30), start_background=False))


def _n(client, table):
    c = sqlite3.connect(client.app.state.settings.data_dir / "meta.db")
    try:
        return c.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        c.close()


def _post(client, params, **kw):
    return client.post("/api/data/day/remove", params=params, **kw)


def _wait(client, job_id, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = client.get(f"/api/data/day/remove/{job_id}")
        assert r.status_code == 200
        if r.json()["state"] != "running":
            return r.json()
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _remove(client, params):
    r = _post(client, params)
    assert r.status_code == 202, r.text
    return _wait(client, r.json()["job_id"])


def test_preview_shape(client):
    r = client.get("/api/data/day", params={"date": DAY, "route": "15", "extra": "x"})
    assert r.status_code == 200
    assert r.json() == {"date": DAY, "route": "15", "window": {"start": W0, "end": W1}, "arrivals": 1, "forecasts": 1,
                        "samples": 1, "gaps": 0, "months": ["samples-2026-11.db"], "skipped_months": [], "truncated": False,
                        "end": DAY, "days": [{"date": DAY, "arrivals": 1, "forecasts": 1, "samples": 1, "gaps": 0}]}


def test_remove_then_empty(client):
    j = _remove(client, {"date": DAY, "confirm": DAY})
    assert j["state"] == "done" and (j["done"]["arrivals"], j["done"]["forecasts"], j["done"]["samples"]) == (1, 1, 1)
    assert _n(client, "arrivals") == 0 and _n(client, "forecast") == 0
    assert _remove(client, {"date": DAY, "confirm": DAY})["done"]["arrivals"] == 0
    assert not bk._lock.locked()


def test_range_preview_and_remove(client):
    p = client.get("/api/data/day", params={"date": DAY, "end": "2026-11-06"}).json()
    assert p["end"] == "2026-11-06" and [d["date"] for d in p["days"]] == [DAY, "2026-11-06"] and p["arrivals"] == 1
    assert _post(client, {"date": DAY, "end": "2026-11-06", "confirm": DAY}).status_code == 422
    assert _post(client, {"date": DAY, "end": "2026-11-06", "confirm": "2026-11-06"}).status_code == 422
    assert _n(client, "arrivals") == 1
    j = _remove(client, {"date": DAY, "end": "2026-11-06", "confirm": f"{DAY}..2026-11-06"})
    assert j["state"] == "done" and j["day_count"] == 2 and j["done"]["arrivals"] == 1
    assert _n(client, "arrivals") == 0 and not bk._lock.locked()


def test_empty_end_is_single_day(client):
    j = _remove(client, {"date": DAY, "end": "", "confirm": DAY})
    assert j["day_count"] == 1


def test_range_validation_422(client):
    for end in ("2026-11-04", "nope", "2026-11-05x"):
        assert client.get("/api/data/day", params={"date": DAY, "end": end}).status_code == 422
        assert _post(client, {"date": DAY, "end": end, "confirm": f"{DAY}..{end}"}).status_code == 422
    assert client.get("/api/data/day", params={"date": "2026-01-01", "end": "2026-03-31"}).status_code == 422
    assert _n(client, "arrivals") == 1


def test_unknown_job_404(client):
    assert client.get("/api/data/day/remove/nope").status_code == 404


def test_second_post_while_running_409_with_job_id(client, monkeypatch):
    import threading
    gate = threading.Event()
    real = D.remove

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)
    monkeypatch.setattr(D, "remove", slow)
    r = _post(client, {"date": DAY, "confirm": DAY})
    assert r.status_code == 202
    jid = r.json()["job_id"]
    r2 = _post(client, {"date": DAY, "confirm": DAY})
    assert r2.status_code == 409 and r2.json()["job_id"] == jid and "detail" in r2.json()
    gate.set()
    assert _wait(client, jid)["state"] == "done"
    assert not bk._lock.locked()


def test_jobbusy_releases_lock(client, monkeypatch):
    monkeypatch.setattr(J, "start", _raise(J.JobBusy("abc")))
    r = _post(client, {"date": DAY, "confirm": DAY})
    assert r.status_code == 409 and r.json()["job_id"] == "abc" and not bk._lock.locked()


def test_start_failure_releases_lock(client, monkeypatch):
    monkeypatch.setattr(J, "start", _raise(RuntimeError("boom")))
    r = TestClient(client.app, raise_server_exceptions=False).post(
        "/api/data/day/remove", params={"date": DAY, "confirm": DAY})
    assert r.status_code >= 500 and not bk._lock.locked()


@pytest.mark.parametrize("params", [
    {"date": "nope"}, {"date": "2026-02-30"}, {"date": "2099-01-01"}, {"date": "2019-12-31"}, {},
    {"date": DAY, "route": "15\n"}, {"date": DAY, "route": '"15"'}, {"date": DAY, "route": "x" * 21}])
def test_validation_422_no_change(client, params):
    assert client.get("/api/data/day", params=params).status_code == 422
    assert client.post("/api/data/day/remove", params={**params, "confirm": params.get("date", DAY)}).status_code == 422
    assert _n(client, "arrivals") == 1


def test_confirm_mismatch_and_missing(client):
    assert client.post("/api/data/day/remove", params={"date": DAY, "confirm": "2026-11-06"}).status_code == 422
    assert client.post("/api/data/day/remove", params={"date": DAY}).status_code == 422
    assert _n(client, "arrivals") == 1


def test_cross_origin_403(client):
    r = client.post("/api/data/day/remove", params={"date": DAY, "confirm": DAY}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and _n(client, "arrivals") == 1
    ok = client.post("/api/data/day/remove", params={"date": DAY, "confirm": DAY}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 202
    _wait(client, ok.json()["job_id"])


def test_busy_lock_409(client):
    assert bk._lock.acquire(blocking=False)
    try:
        r = client.post("/api/data/day/remove", params={"date": DAY, "confirm": DAY})
    finally:
        bk._lock.release()
    assert r.status_code == 409 and "detail" in r.json() and r.json().get("job_id") is None and _n(client, "arrivals") == 1
    assert not bk._lock.locked()


def test_locked_db_409_with_partial_and_lock_released(client, monkeypatch):
    def boom(*a, **k):
        raise D.DayBusy("samples-2026-11.db", {"arrivals": 0})
    monkeypatch.setattr(D, "remove", boom)
    j = _remove(client, {"date": DAY, "confirm": DAY})
    assert j["state"] == "error" and "samples-2026-11.db" in j["error"] and j["done"]["arrivals"] == 0
    assert not bk._lock.locked()


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


@pytest.mark.parametrize("exc,status", [
    (D.DayBusy("meta.db", {}, "busy"), 409), (D.DayBusy("meta.db", {}, "error"), 503),
    (sqlite3.OperationalError("disk I/O error /secret/path"), 503), (sqlite3.DatabaseError("secret malformed"), 503)])
def test_preview_error_mapping_is_neutral(client, monkeypatch, exc, status):
    monkeypatch.setattr(D, "preview", _raise(exc))
    r = client.get("/api/data/day", params={"date": DAY})
    assert r.status_code == status and "secret" not in r.text and "I/O" not in r.text


@pytest.mark.parametrize("exc,word", [
    (D.DayBusy("meta.db", {"samples": 3}, "busy"), "stayed locked"),
    (D.DayBusy("meta.db", {"samples": 3}, "error"), "failed"),
    (sqlite3.OperationalError("disk I/O error /secret/path"), "failed unexpectedly"),
    (sqlite3.DatabaseError("secret malformed"), "failed unexpectedly")])
def test_remove_error_mapping_is_neutral(client, monkeypatch, exc, word):
    monkeypatch.setattr(D, "remove", _raise(exc))
    j = _remove(client, {"date": DAY, "confirm": DAY})
    assert j["state"] == "error" and word in j["error"] and "secret" not in str(j)
    if "failed" in word:
        assert "locked" not in j["error"]
    assert not bk._lock.locked()


def test_unicode_digit_date_422(client):
    assert client.get("/api/data/day", params={"date": "\u0662\u0660\u0662\u0666-11-05"}).status_code == 422
