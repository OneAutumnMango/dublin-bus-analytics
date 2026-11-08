from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
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


ENV = ("SERVER_URL", "DATA_DIR", "POLL_SECONDS", "PORT")


@pytest.fixture
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    s = Settings.from_env()
    assert s == Settings("http://dublin-bus-server:8080", Path("/data"), 30, 8081)


def test_every_override(clean_env):
    clean_env.setenv("SERVER_URL", "http://a:1")
    clean_env.setenv("DATA_DIR", "/tmp/z")
    clean_env.setenv("POLL_SECONDS", "7")
    clean_env.setenv("PORT", "1234")
    assert Settings.from_env() == Settings("http://a:1", Path("/tmp/z"), 7, 1234)


def test_boundary_one_is_valid(clean_env):
    clean_env.setenv("POLL_SECONDS", "1")
    clean_env.setenv("PORT", "1")
    s = Settings.from_env()
    assert (s.poll_seconds, s.port) == (1, 1)


@pytest.mark.parametrize("name", ["POLL_SECONDS", "PORT"])
@pytest.mark.parametrize("bad", ["abc", "1.5", "", "0", "-3"])
def test_invalid_int_raises(clean_env, name, bad):
    clean_env.setenv(name, bad)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()
