import asyncio
import sqlite3
import time

import httpx

from app import cache
from app.api import routes
from app.core.db import open_meta
from tests.api.test_routes import Env

URL = "/api/routes/15/summary"


def _env(tmp_path, monkeypatch, **kw):
    env = Env(tmp_path, **kw)
    env.keep_cache = True
    env.fake.set_route_stops("15", {"out": [{"id": "s1"}]})
    env.add(route="15", delay=10)
    n = [0]
    real = routes._summary_db

    def counting(*a, **k):
        n[0] += 1
        return real(*a, **k)
    monkeypatch.setattr(routes, "_summary_db", counting)
    return env, n


def _rows(env):
    c = open_meta(env.s.data_dir)
    try:
        return c.execute("select count(*) from result_cache where key not like 'srv:%'").fetchone()[0]
    finally:
        c.close()


def test_second_identical_request_is_hit_and_skips_handler(tmp_path, monkeypatch):
    env, n = _env(tmp_path, monkeypatch)
    a, b = env.c.get(URL), env.c.get(URL)
    assert a.headers["x-cache"] == "miss" and b.headers["x-cache"] == "hit"
    assert a.json() == b.json() and n[0] == 1
    int(b.headers["x-computed-at"])


def test_fresh_recomputes_and_overwrites(tmp_path, monkeypatch):
    env, n = _env(tmp_path, monkeypatch)
    env.c.get(URL)
    old = int(env.c.get(URL).headers["x-computed-at"])
    time.sleep(1.1)
    assert env.c.get(URL, params={"fresh": 1}).headers["x-cache"] == "bypass" and n[0] == 2
    r = env.c.get(URL)
    assert r.headers["x-cache"] == "hit" and int(r.headers["x-computed-at"]) > old


def test_since_within_same_hour_shares_entry(tmp_path, monkeypatch):
    env, n = _env(tmp_path, monkeypatch)
    env.c.get(URL, params={"since": 3600 * 100 + 5})
    r = env.c.get(URL, params={"since": 3600 * 100 + 900})
    assert r.headers["x-cache"] == "hit" and n[0] == 1


def test_uncached_paths_untouched(tmp_path, monkeypatch):
    env, _ = _env(tmp_path, monkeypatch)
    assert "x-cache" not in env.c.get("/api/routes/tracked").headers


def test_degraded_responses_not_stored(tmp_path, monkeypatch):
    env, n = _env(tmp_path, monkeypatch)
    env.app.state.poller.track("15")

    async def no_dirs(request, r):
        raise routes._NoOrder(502, "down")
    monkeypatch.setattr(routes, "_directions", no_dirs)
    for _ in range(2):
        r = env.c.get("/api/map")
        assert r.status_code == 200 and r.json()["unavailable"] == ["15"]
        assert r.headers["x-cache"] == "miss"
    assert _rows(env) == 0
    for _ in range(2):  # summary degrades to order_source "none"
        assert env.c.get(URL).json()["order_source"] == "none"
    assert n[0] == 2 and _rows(env) == 0


def test_error_status_not_stored(tmp_path, monkeypatch):
    env, _ = _env(tmp_path, monkeypatch)
    for _ in range(2):
        assert env.c.get("/api/routes/15/lab/distribution", params={"ci": "abc"}).status_code == 422
    assert _rows(env) == 0


def test_clear_during_compute_refuses_store(tmp_path, monkeypatch):
    env, n = _env(tmp_path, monkeypatch)
    real = routes._summary_db

    def clearing(*a, **k):
        c = open_meta(env.s.data_dir)
        cache.clear(c)
        c.close()
        return real(*a, **k)
    monkeypatch.setattr(routes, "_summary_db", clearing)
    assert env.c.get(URL).status_code == 200
    assert _rows(env) == 0


def test_concurrent_misses_compute_once(tmp_path, monkeypatch):
    env, n = _env(tmp_path, monkeypatch)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t") as c:
            return await asyncio.gather(*[c.get(URL) for _ in range(5)])
    rs = asyncio.run(go())
    assert {r.status_code for r in rs} == {200} and n[0] == 1


def test_cache_db_failure_falls_back_live(tmp_path, monkeypatch):
    env, n = _env(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(cache, "lookup", boom)
    r = env.c.get(URL)
    assert r.status_code == 200 and r.json()["route"] == "15"


def test_disabled_by_setting(tmp_path, monkeypatch):
    env, _ = _env(tmp_path, monkeypatch, result_cache=False)
    assert "x-cache" not in env.c.get(URL).headers and _rows(env) == 0


def test_disallowed_host_never_served_from_cache(tmp_path, monkeypatch):
    env, _ = _env(tmp_path, monkeypatch, allowed_hosts=("example.test",))
    ok = {"host": "example.test"}
    assert env.c.get(URL, headers=ok).status_code == 200
    assert env.c.get(URL, headers=ok).headers["x-cache"] == "hit"
    r = env.c.get(URL, headers={"host": "evil.test"})
    assert r.status_code == 400 and "x-cache" not in r.headers
    r = env.c.get(URL, headers={"host": "127.0.0.1"})  # loopback stays allowed
    assert r.status_code == 200 and r.headers["x-cache"] == "hit"
