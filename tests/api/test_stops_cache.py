import json
import time

from app import cache
from app.core.db import open_meta
from tests.api.test_routes import Env

URL = "/api/routes/15/summary"
KEY = "srv:stops:15"
STOPS = {"out": [{"id": "s1"}, {"id": "s2"}]}


def _env(tmp_path, seed=True, **kw):
    env = Env(tmp_path, **kw)
    env.keep_cache = True
    env.fake.set_route_stops("15", STOPS)
    if seed:
        env.add(route="15", delay=10)
    return env


def _row(env):
    c = open_meta(env.s.data_dir)
    try:
        return cache.lookup(c, KEY)
    finally:
        c.close()


def _put(env, body, computed_at):
    c = open_meta(env.s.data_dir)
    try:
        cache.store(c, KEY, body, 1, None, now=computed_at)
    finally:
        c.close()



def test_stops_survive_restart(tmp_path):
    env = _env(tmp_path)
    assert env.c.get(URL).json()["order_source"] == "server"
    env2 = _env(tmp_path, seed=False)
    env2.fake.set_down(True)
    r = env2.c.get(URL, params={"fresh": 1})
    assert r.status_code == 200 and r.json()["order_source"] == "server"


def test_expired_persisted_stops_refetched(tmp_path):
    env = _env(tmp_path)
    _put(env, json.dumps(STOPS).encode(), int(time.time()) - 86400 - 10)
    assert env.c.get(URL).json()["order_source"] == "server"
    assert env.fake.requests  # server was called
    assert _row(env)[1] >= int(time.time()) - 5  # and the row was refreshed


def test_failed_fetch_not_persisted(tmp_path):
    env = _env(tmp_path)
    env.fake.set_down(True)
    assert env.c.get(URL).json()["order_source"] == "none"
    assert _row(env) is None


def test_malformed_persisted_row_ignored(tmp_path):
    env = _env(tmp_path)
    for bad in (b"not json{", b'{"out": "x"}', b'{"out": [{"id": 5}]}'):
        _put(env, bad, int(time.time()))
        env.fake.requests.clear()
        env.app.state.stops_cache = None
        assert env.c.get(URL, params={"fresh": 1}).json()["order_source"] == "server"
        assert env.fake.requests


def test_disabled_cache_does_not_persist(tmp_path):
    env = _env(tmp_path, result_cache=False)
    assert env.c.get(URL).json()["order_source"] == "server"
    assert _row(env) is None
