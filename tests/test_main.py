import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_health_returns_ok():
    r = TestClient(create_app(Settings("http://x", Path("/tmp")))).get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_create_app_with_explicit_settings():
    s = Settings("http://x", Path("/d"), 5, 9000)
    assert create_app(s).state.settings is s


def test_create_app_default_settings(monkeypatch):
    monkeypatch.setenv("SERVER_URL", "http://env")
    assert create_app().state.settings.server_url == "http://env"



def test_create_app_uses_configured_timeout(tmp_path):
    app = create_app(Settings("http://fake", tmp_path, 30, server_timeout_seconds=9), start_background=False)
    assert app.state.client._client.timeout.read == 9


def _status(tmp_path, hosts, host):
    app = create_app(Settings("http://x", tmp_path, allowed_hosts=hosts), start_background=False)
    return TestClient(app).get("/api/health", headers={"host": host}).status_code


def test_no_allowed_hosts_accepts_any_host(tmp_path):
    assert _status(tmp_path, (), "evil.example") == 200


@pytest.mark.parametrize("host,code", [
    ("tunnel.example.com", 200), ("TUNNEL.example.com:443", 200), ("localhost:8081", 200),
    ("127.0.0.1", 200), ("[::1]:8081", 200), ("evil.example", 400)])
def test_allowed_hosts_enforced(tmp_path, host, code):
    assert _status(tmp_path, ("tunnel.example.com",), host) == code


def test_allowed_hosts_wildcard(tmp_path):
    assert _status(tmp_path, ("*.example.com",), "a.example.com") == 200


def _restorable(tmp_path):
    import gzip, sqlite3
    from app.core.db import open_meta
    d, a = tmp_path / "d", tmp_path / "a"
    a.mkdir()
    c = open_meta(d)
    c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route)"
              " values(5,'t','s',1,'2026-09-10',1,2,1,3,1,'r')")
    c.commit()
    src = tmp_path / "m.db"
    arch = sqlite3.connect(src)
    for (sql,) in c.execute("select sql from sqlite_master where type='table' and name in ('arrivals','forecast')"):
        arch.execute(sql)
    arch.execute("insert into arrivals values(5,'t','s',1,'2026-09-10',1,2,1,3,1,'r')")
    arch.execute("create table info(month TEXT, arrivals INT, forecast INT, max_id INT)")
    arch.execute("insert into info values('2026-09',1,0,5)")
    arch.commit()
    arch.close()
    (a / "meta-2026-09.db.gz").write_bytes(gzip.compress(src.read_bytes()))
    c.execute("delete from arrivals")
    c.execute("create table archived_months(month TEXT PRIMARY KEY, arrivals INT, forecast INT)")
    c.execute("insert into archived_months values('2026-09',1,0)")
    c.commit()
    c.close()
    return d, a


def test_lifespan_runs_restore(tmp_path):
    import sqlite3
    d, a = _restorable(tmp_path)
    app = create_app(Settings("http://x", d, backup_archive_dir=a), start_background=False)
    with TestClient(app):
        pass
    c = sqlite3.connect(d / "meta.db")
    assert c.execute("select count(*) from arrivals").fetchone() == (1,)
    assert c.execute("select count(*) from archived_months").fetchone() == (0,)
    c.close()


def test_lifespan_survives_restore_exception(tmp_path, monkeypatch):
    import app.main as m

    def boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "restore_archived_months", boom)
    with TestClient(create_app(Settings("http://x", tmp_path), start_background=False)) as c:
        assert c.get("/api/health").status_code == 200
