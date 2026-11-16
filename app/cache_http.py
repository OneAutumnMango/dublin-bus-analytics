"""HTTP middleware serving/storing allowed GET responses via app.cache. Any cache error computes live."""
import asyncio
import json
import logging
import time

from fastapi import FastAPI, Request, Response

from app import cache
from app.core.db import open_meta

log = logging.getLogger(__name__)
_FAIL = object()


def _degraded(body: bytes) -> bool:
    d = json.loads(body)
    return isinstance(d, dict) and bool(d.get("unavailable") or d.get("order_source") == "none")


def install(app: FastAPI) -> None:
    if not app.state.settings.result_cache:
        return
    app.state.result_cache_locks = locks = {}

    async def db(fn):
        """Run fn(conn) on a fresh meta connection in a thread; _FAIL (logged) on any error."""
        def run():
            c = open_meta(app.state.settings.data_dir)
            try:
                return fn(c)
            finally:
                c.close()
        try:
            return await asyncio.to_thread(run)
        except Exception:
            log.exception("result cache unavailable; computing live")
            return _FAIL

    def hit_response(body, at):
        return Response(body, media_type="application/json", headers={"X-Cache": "hit", "X-Computed-At": str(at)})

    @app.middleware("http")
    async def result_cache(request: Request, call_next):
        key = cache.make_key(request.url.path, request.query_params.multi_items()) if request.method == "GET" else None
        if key is None:
            return await call_next(request)
        fresh = "fresh" in request.query_params
        if not fresh:
            hit = await db(lambda c: cache.lookup(c, key))
            if hit not in (None, _FAIL):
                return hit_response(*hit)
        lk = (asyncio.get_running_loop(), key)  # an asyncio.Lock is bound to one loop (TestClient makes one per request)
        lock = locks.setdefault(lk, asyncio.Lock())
        try:
            async with lock:
                if not fresh:  # a concurrent request may have stored it while we waited
                    hit = await db(lambda c: cache.lookup(c, key))
                    if hit not in (None, _FAIL):
                        return hit_response(*hit)
                gen = await db(cache.generation)  # before computing: a clear meanwhile makes store refuse
                t0 = time.monotonic()
                resp = await call_next(request)
                body = b"".join([chunk async for chunk in resp.body_iterator])
                ms = int((time.monotonic() - t0) * 1000)
                out = Response(body, resp.status_code)
                out.raw_headers = [(k, v) for k, v in resp.raw_headers if k != b"content-length"]
                out.headers["content-length"] = str(len(body))
                out.headers["X-Cache"] = "bypass" if fresh else "miss"
                out.headers["X-Computed-At"] = str(int(time.time()))
                if (gen is not _FAIL and resp.status_code == 200
                        and resp.headers.get("content-type", "").startswith("application/json")):
                    try:
                        ok = not await asyncio.to_thread(_degraded, body)
                    except ValueError:
                        ok = False
                    if ok:
                        await db(lambda c: cache.store(c, key, body, ms, gen))
                return out
        finally:
            if not lock.locked():
                locks.pop(lk, None)  # shortcut: a woken waiter may race a new lock; worst case one duplicate compute
