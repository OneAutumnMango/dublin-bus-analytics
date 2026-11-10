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

