from pathlib import Path

from app.core.config import Settings
from app.core.db import open_meta


def test_open_meta_creates_result_cache(tmp_path):
    c = open_meta(tmp_path)
    cols = [r[1] for r in c.execute("pragma table_info(result_cache)")]
    assert cols == ["key", "body", "computed_at", "last_used", "ms"]


def test_settings_defaults_and_env(monkeypatch):
    d = Settings("http://x", Path("/x"))
    assert d.result_cache is True and d.result_cache_refresh_seconds == 3600
    monkeypatch.setenv("RESULT_CACHE", "false"); monkeypatch.setenv("RESULT_CACHE_REFRESH_SECONDS", "600")
    s = Settings.from_env()
    assert s.result_cache is False and s.result_cache_refresh_seconds == 600
