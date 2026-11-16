import asyncio

from fastapi.testclient import TestClient
import time

from app import cache
from app.api import routes
from app.cache_warm import CacheWarmer
from app.core.config import Settings
from app.core.db import open_meta
from app.main import create_app
from tests.api.test_routes import Env

U1, U2, U3 = "/api/routes/15/summary?", "/api/routes/16/summary?", "/api/routes/17/summary?"
NOW = int(time.time())


def _env(tmp_path, **kw):
    env = Env(tmp_path, **kw)
    env.keep_cache = True
    env.fake.set_route_stops("15", {"out": [{"id": "s1"}]})
    env.add(route="15", delay=10)
    return env


def _seed(env, key, computed, used):
    c = open_meta(env.s.data_dir)
    try:
        cache.store(c, key, b'{"old":1}', 1, None, now=computed)
        c.execute("update result_cache set last_used=? where key=?", (used, key))
        c.commit()
    finally:
        c.close()


def _row(env, key):
    c = open_meta(env.s.data_dir)
    try:
        return c.execute("select body, computed_at from result_cache where key=?", (key,)).fetchone()
    finally:
        c.close()


def _run(env):
    return asyncio.run(CacheWarmer(env.app, now_fn=lambda: NOW).run_once())


def test_run_once_refreshes_only_old_recently_used_entries(tmp_path):
    env = _env(tmp_path)
    _seed(env, U1, NOW - 4000, NOW - 100)
    _seed(env, U2, NOW - 100, NOW - 100)
    _seed(env, U3, NOW - 3 * 86400, NOW - 3 * 86400)
    assert _run(env) == 1
    assert _row(env, U1)[1] > NOW - 4000
    assert _row(env, U2)[1] == NOW - 100


def test_failed_recompute_keeps_old_body(tmp_path, monkeypatch):
    env = _env(tmp_path)
    _seed(env, U1, NOW - 5000, NOW - 100)
    _seed(env, "/api/routes/15/heatmap?", NOW - 4000, NOW - 100)
    real = routes._summary_db

    def boom(request, route, *a, **k):
        if route == "15":
            raise RuntimeError("x")
        return real(request, route, *a, **k)
    monkeypatch.setattr(routes, "_summary_db", boom)
    assert _run(env) == 1
    assert _row(env, U1)[1] == NOW - 5000
    assert _row(env, "/api/routes/15/heatmap?")[1] > NOW - 4000


def test_warm_request_passes_allowed_hosts(tmp_path):
    env = _env(tmp_path, allowed_hosts=("example.test",))
    _seed(env, U1, NOW - 4000, NOW - 100)
    assert _run(env) == 1


def test_warm_keeps_last_used_and_idle_entry_stops_being_warmed(tmp_path):
    env = _env(tmp_path)
    _seed(env, U1, NOW - 4000, NOW - 100)
    assert _run(env) == 1
    c = open_meta(env.s.data_dir)
    assert c.execute("select last_used from result_cache where key=?", (U1,)).fetchone()[0] == NOW - 100
    c.close()
    assert asyncio.run(CacheWarmer(env.app, now_fn=lambda: NOW - 100 + 3 * 86400).run_once()) == 0


def test_run_evicts_expired(tmp_path):
    env = _env(tmp_path)
    _seed(env, U1, NOW - 15 * 86400, NOW - 15 * 86400)
    _run(env)
    assert _row(env, U1) is None


def test_lifespan_starts_warmer_only_when_enabled(tmp_path):
    kw = dict(server_url="http://fake", data_dir=tmp_path)
    assert create_app(Settings(result_cache=False, **kw)).state.warmer is None
    assert create_app(Settings(**kw), start_background=False).state.warmer is None
    app = create_app(Settings(**kw))
    assert app.state.warmer is not None

    class Idle:
        async def run(self):
            await asyncio.sleep(3600)
    app.state.poller = app.state.scheduler = Idle()
    seen = []
    real = app.state.warmer.run

    async def run():
        try:
            await real()
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise
    app.state.warmer.run = run
    with TestClient(app):
        pass
    assert seen == ["cancelled"]
