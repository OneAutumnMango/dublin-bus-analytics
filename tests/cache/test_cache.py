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


import gzip

from app.cache import clear, evict, generation, lookup, make_key, stale_keys, store

DAY = 86400


def test_key_rounds_since_until_and_ignores_fresh_and_order():
    a = make_key("/api/map", [("since", "7300"), ("a", "1")])
    assert a == make_key("/api/map", [("a", "1"), ("since", "10799"), ("fresh", "1")])
    assert a != make_key("/api/map", [("a", "1"), ("since", "10800")])


def test_key_none_for_uncached_paths():
    for p in ["/api/routes/15/stops", "/api/routes/tracked", "/api/export.json", "/api/health"]:
        assert make_key(p, []) is None
    for p in ["/api/routes/15/lab/persistence", "/api/all/forecast", "/api/stops/s1/stats", "/api/trips/t1/series"]:
        assert make_key(p, []) is not None


def test_store_lookup_roundtrip(tmp_path):
    c = open_meta(tmp_path)
    assert store(c, "k", b'{"a":1}', 5, None, now=1000)
    assert lookup(c, "k", now=1000) == (b'{"a":1}', 1000)
    big = b'{"a":"' + b"x" * 10000 + b'"}'
    store(c, "big", big, 5, None, now=1000)
    raw = c.execute("select body from result_cache where key='big'").fetchone()[0]
    assert len(raw) < len(big) and gzip.decompress(raw) == big
    assert lookup(c, "missing") is None


def test_lookup_with_ms(tmp_path):
    c = open_meta(tmp_path)
    store(c, "k", b"x", 42, None, now=1000)
    assert lookup(c, "k", now=1000, with_ms=True) == (b"x", 1000, 42)
    assert lookup(c, "k", now=1000) == (b"x", 1000)
    assert lookup(c, "missing", with_ms=True) is None


def test_lookup_bumps_last_used_only_when_old(tmp_path):
    c = open_meta(tmp_path)
    store(c, "k", b"x", 1, None, now=1000)
    lu = lambda: c.execute("select last_used from result_cache where key='k'").fetchone()[0]
    lookup(c, "k", now=1100)
    assert lu() == 1000
    lookup(c, "k", now=1700)
    assert lu() == 1700


def test_clear_bumps_generation_and_refuses_stale_store(tmp_path):
    c = open_meta(tmp_path)
    store(c, "srv:x", b"s", 1, None, now=1)
    g = generation(c)
    clear(c)
    assert generation(c) == g + 1
    assert store(c, "k", b"x", 1, g) is False
    assert c.execute("select key from result_cache where key not like 'srv:%'").fetchall() == []
    assert lookup(c, "srv:x", now=2) is not None


def test_evict_by_age_and_count_and_bytes(tmp_path):
    c = open_meta(tmp_path)
    now = 100 * DAY
    store(c, "old", b"x", 1, None, now=now - 15 * DAY)
    store(c, "ok", b"x", 1, None, now=now - 13 * DAY)
    store(c, "srv:old", b"x", 1, None, now=now - 15 * DAY)
    assert evict(c, now) == 1
    keys = lambda: sorted(r[0] for r in c.execute("select key from result_cache where key not like 'srv:rc%'"))
    assert keys() == ["ok", "srv:old"]
    for i in range(4):
        store(c, f"n{i}", b"x", 1, None, now=now - 100 + i)
    assert evict(c, now, max_entries=2) == 3  # ok, n0, n1 go; n2, n3 stay
    assert keys() == ["n2", "n3", "srv:old"]
    import os
    for i in range(3):
        store(c, f"b{i}", os.urandom(300), 1, None, now=now - 10 + i)
    evict(c, now, max_bytes=700)
    tot = c.execute("select sum(length(body)) from result_cache where key not like 'srv:%'").fetchone()[0]
    assert tot <= 700 and c.execute("select count(*) from result_cache where key='srv:old'").fetchone()[0] == 1


def test_make_key_rejects_trailing_newline():
    assert make_key("/api/map\n", []) is None
    assert make_key("/api/map", []) == "/api/map?"


def test_stale_keys_order_and_filters(tmp_path):
    c = open_meta(tmp_path)
    now = 100 * DAY
    store(c, "a", b"x", 1, None, now=now - 5000)
    store(c, "b", b"x", 1, None, now=now - 9000)
    store(c, "fresh", b"x", 1, None, now=now - 100)
    store(c, "unused", b"x", 1, None, now=now - 3 * DAY)
    store(c, "srv:z", b"x", 1, None, now=now - 9000)
    assert stale_keys(c, now) == ["b", "a"]


def test_clear_with_pending_transaction(tmp_path):
    c = open_meta(tmp_path)
    store(c, "k", b"x", 1, None, now=1)
    c.execute("insert into gaps(start, end) values(1, 2)")
    clear(c)
    c2 = open_meta(tmp_path)
    assert c2.execute("select count(*) from gaps").fetchone()[0] == 1
    assert c2.execute("select count(*) from result_cache where key='k'").fetchone()[0] == 0


def test_store_with_pending_transaction(tmp_path):
    c = open_meta(tmp_path)
    c.execute("insert into gaps(start, end) values(1, 2)")
    assert store(c, "k", b"x", 1, None, now=1) is True
    c2 = open_meta(tmp_path)
    assert c2.execute("select count(*) from gaps").fetchone()[0] == 1
    assert c2.execute("select count(*) from result_cache where key='k'").fetchone()[0] == 1


def test_store_keep_last_used(tmp_path):
    c = open_meta(tmp_path)
    lu = lambda: c.execute("select last_used from result_cache where key='k'").fetchone()[0]
    store(c, "k", b"x", 1, None, now=1000, keep_last_used=True)
    assert lu() == 1000  # no row yet: now
    store(c, "k", b"y", 1, None, now=2000, keep_last_used=True)
    assert lu() == 1000 and lookup(c, "k", now=2000)[1] == 2000 and gzip.decompress(
        c.execute("select body from result_cache where key='k'").fetchone()[0]) == b"y"
    store(c, "k", b"z", 1, None, now=3000)
    assert lu() == 3000
