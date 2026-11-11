from pathlib import Path

import pytest

from app.core.config import Settings

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


BENV = ("BACKUP_ENABLED", "BACKUP_TIME", "BACKUP_KEEP", "BACKUP_DIR")


@pytest.fixture
def benv(clean_env):
    for k in BENV:
        clean_env.delenv(k, raising=False)
    return clean_env


def test_backup_defaults(benv):
    s = Settings.from_env()
    assert (s.backup_enabled, s.backup_time, s.backup_keep, s.backup_dir) == (True, "03:00", 7, None)
    assert s.backup_path == Path("/data/backups")


def test_env_roundtrip_all_backup_vars(benv):
    benv.setenv("BACKUP_ENABLED", "No")
    benv.setenv("BACKUP_TIME", "23:59")
    benv.setenv("BACKUP_KEEP", "1")
    benv.setenv("BACKUP_DIR", "rel/b")
    s = Settings.from_env()
    assert (s.backup_enabled, s.backup_time, s.backup_keep, s.backup_dir) == (False, "23:59", 1, Path("rel/b"))
    assert s.backup_path == Path("rel/b")


@pytest.mark.parametrize("v,exp", [("true", True), ("TRUE", True), ("1", True), ("yes", True),
                                   ("false", False), ("False", False), ("0", False), ("no", False)])
def test_backup_enabled_parsing(benv, v, exp):
    benv.setenv("BACKUP_ENABLED", v)
    assert Settings.from_env().backup_enabled is exp


@pytest.mark.parametrize("name,bad", [
    ("BACKUP_ENABLED", "maybe"), ("BACKUP_ENABLED", ""), ("BACKUP_ENABLED", "2"),
    ("BACKUP_TIME", "25:00"), ("BACKUP_TIME", "3:00"), ("BACKUP_TIME", "03:60"), ("BACKUP_TIME", ""),
    ("BACKUP_TIME", "abc"), ("BACKUP_TIME", "03:00\n"), ("BACKUP_TIME", "24:00"),
    ("BACKUP_KEEP", "0"), ("BACKUP_KEEP", "-1"), ("BACKUP_KEEP", "x"), ("BACKUP_KEEP", "1.5"), ("BACKUP_KEEP", ""),
    ("BACKUP_DIR", ""), ("BACKUP_DIR", "   "),
])
def test_invalid_backup_values_rejected(benv, name, bad):
    benv.setenv(name, bad)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("t", ["00:00", "03:00", "19:59", "23:59"])
def test_valid_backup_time_boundaries(benv, t):
    benv.setenv("BACKUP_TIME", t)
    assert Settings.from_env().backup_time == t


SENV = ("SERVER_TIMEOUT_SECONDS", "SERVER_CONCURRENCY")


@pytest.fixture
def senv(clean_env):
    for k in SENV:
        clean_env.delenv(k, raising=False)
    return clean_env


def test_server_tuning_defaults(senv):
    s = Settings.from_env()
    assert (s.server_timeout_seconds, s.server_concurrency) == (45, 2)
    assert Settings("http://fake", Path("/x"), 30).server_timeout_seconds == 45


def test_server_tuning_env_parses(senv):
    senv.setenv("SERVER_TIMEOUT_SECONDS", "90")
    senv.setenv("SERVER_CONCURRENCY", "4")
    s = Settings.from_env()
    assert (s.server_timeout_seconds, s.server_concurrency) == (90, 4)


@pytest.mark.parametrize("name,bad", [("SERVER_CONCURRENCY", "0"), ("SERVER_TIMEOUT_SECONDS", "abc")])
def test_server_tuning_invalid_rejected(senv, name, bad):
    senv.setenv(name, bad)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()
