import gzip
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_meta
from app.main import create_app


def _arr(i, route, scheduled=1000):
    return (i, f"t{i}", f"s{i}", i, "2026-11-01", scheduled, scheduled + 60, 60, 0.9, 1, route)


@pytest.fixture
def client(tmp_path):
    c = open_meta(tmp_path)
    c.executemany(
        "insert into arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route)"
        " values (?,?,?,?,?,?,?,?,?,?,?)", [_arr(1, "15/A"), _arr(2, "7")])
    c.commit()
    c.close()
    return TestClient(create_app(Settings("http://fake", tmp_path, 30), start_background=False))


def test_export_returns_valid_json_with_attachment_header(client):
    r = client.get("/api/export.json")
    assert r.status_code == 200
    assert r.json()["complete"] is True
    assert r.headers["content-type"].startswith("application/json")
    assert re.fullmatch(r'attachment; filename="analytics-export-\d{8}\.json"', r.headers["content-disposition"])
    assert r.headers["cache-control"] == "no-store"


def test_export_route_filter_and_filename(client):
    r = client.get("/api/export.json", params={"route": "15/A"})
    assert re.fullmatch(r'attachment; filename="analytics-export-\d{8}-15_A\.json"', r.headers["content-disposition"])
    assert [a["route"] for a in r.json()["arrivals"]] == ["15/A"]


def test_export_gzip(client):
    r = client.get("/api/export.json?gzip=1")
    assert r.headers["content-type"].startswith("application/gzip")
    assert r.headers["content-disposition"].endswith('.json.gz"')
    assert json.loads(gzip.decompress(r.content))["complete"] is True


@pytest.mark.parametrize("params", [{"route": "bad;route"}, {"route": "x" * 21}, {"route": ""},
                                    {"since": "abc"}, {"until": "x"}, {"since": 2, "until": 1},
                                    {"since": 2**63}, {"until": -1}, {"since": -1}, {"until": 2**62 + 1}])
def test_export_invalid_params_422(client, params):
    assert client.get("/api/export.json", params=params).status_code == 422


def test_export_busy_returns_429(client):
    gate = client.app.state.export_gate
    tok = gate.try_acquire()
    r = client.get("/api/export.json")
    assert r.status_code == 429 and r.headers["retry-after"] == "30" and "detail" in r.json()
    gate.release(tok)
    assert client.get("/api/export.json").status_code == 200


def test_gate_released_after_stream_consumed(client):
    assert client.get("/api/export.json").status_code == 200
    gate = client.app.state.export_gate
    tok = gate.try_acquire()
    assert tok is not None
    gate.release(tok)


@pytest.mark.parametrize("route", ['a"b', "a\nb", "a\r\nb", "15\n", "a;b", "..\\x", "é", "a b"])
def test_header_injection_safe(client, route):
    r = client.get("/api/export.json", params={"route": route})
    assert r.status_code == 422
    assert "content-disposition" not in r.headers


@pytest.mark.parametrize("route", ["..", "../..", "a/../b", "a b", "./."])
def test_filename_sanitised(client, route):
    r = client.get("/api/export.json", params={"route": route})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    name = re.fullmatch(r'attachment; filename="([^"]*)"', cd).group(1)
    assert re.fullmatch(r"analytics-export-\d{8}-[A-Za-z0-9_-]+\.json", name)


def test_validation_failures_do_not_hold_gate(client):
    gate = client.app.state.export_gate
    for p in ({"route": "a;b"}, {"since": "abc"}, {"since": 2, "until": 1},
              {"since": 2**63}, {"until": -1}):
        assert client.get("/api/export.json", params=p).status_code == 422
        tok = gate.try_acquire()
        assert tok is not None
        gate.release(tok)


def test_abandoned_response_releases_gate(client):
    from app.export.api import export_json

    class Req:
        app = client.app

    resp = export_json(Req(), route=None, since=None, until=None, gzip=False)
    gate = client.app.state.export_gate
    assert gate.try_acquire() is None  # held while streaming
    import asyncio

    async def consume_one_then_abandon():
        it = resp.body_iterator
        await it.__anext__()
        await it.aclose()  # client disconnect closes the stream mid-way

    asyncio.run(consume_one_then_abandon())
    tok = gate.try_acquire()
    assert tok is not None
    gate.release(tok)


def test_gate_lease_expires_if_never_streamed(client):
    from app.export.gate import ExportGate
    t = [0.0]
    g = ExportGate(lease_s=3600, clock=lambda: t[0])
    assert g.try_acquire() is not None and g.try_acquire() is None
    t[0] = 3601
    assert g.try_acquire() is not None


def test_export_accepts_upper_bound_2_pow_62(client):
    r = client.get("/api/export.json", params={"until": 2**62})
    assert r.status_code == 200
    assert json.loads(r.content)["complete"] is True
