import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.backup import backup as bk
from app.core.config import Settings
from app.core.db import open_meta, open_samples
from app.main import create_app
from app.maintenance import day as D

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


def test_preview_shape(client):
    r = client.get("/api/data/day", params={"date": DAY, "route": "15", "extra": "x"})
    assert r.status_code == 200
    assert r.json() == {"date": DAY, "route": "15", "window": {"start": W0, "end": W1}, "arrivals": 1, "forecasts": 1,
                        "samples": 1, "gaps": 0, "months": ["samples-2026-11.db"], "skipped_months": [], "truncated": False}


def test_remove_then_empty(client):
    r = client.post("/api/data/day/remove", params={"date": DAY, "confirm": DAY})
    assert r.status_code == 200 and (r.json()["arrivals"], r.json()["forecasts"], r.json()["samples"]) == (1, 1, 1)
    assert _n(client, "arrivals") == 0 and _n(client, "forecast") == 0
    assert client.post("/api/data/day/remove", params={"date": DAY, "confirm": DAY}).json()["arrivals"] == 0


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
    assert ok.status_code == 200


def test_busy_lock_409(client):
    assert bk._lock.acquire(blocking=False)
    try:
        r = client.post("/api/data/day/remove", params={"date": DAY, "confirm": DAY})
    finally:
        bk._lock.release()
    assert r.status_code == 409 and "detail" in r.json() and _n(client, "arrivals") == 1
    assert not bk._lock.locked()


def test_locked_db_409_with_partial_and_lock_released(client, monkeypatch):
    def boom(*a, **k):
        raise D.DayBusy("samples-2026-11.db", {"arrivals": 0})
    monkeypatch.setattr(D, "remove", boom)
    r = client.post("/api/data/day/remove", params={"date": DAY, "confirm": DAY})
    assert r.status_code == 409 and "samples-2026-11.db" in r.json()["detail"] and r.json()["done"] == {"arrivals": 0}
    assert not bk._lock.locked()
