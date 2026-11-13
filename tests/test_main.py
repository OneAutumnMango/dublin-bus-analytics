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
