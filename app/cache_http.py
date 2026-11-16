"""HTTP middleware serving/storing allowed GET responses via app.cache. Any cache error computes live."""
import asyncio
import json
import logging
import time

from fastapi import FastAPI, Response
from starlette.requests import Request

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
    app.state.result_cache_tasks = tasks = set()  # detached computes; the lifespan cancels them on shutdown

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

    def done(t):
        tasks.discard(t)
        if not t.cancelled():
            t.exception()  # mark retrieved: a cancelled request never awaits it (the request itself saw any error)

    async def run_app(inner, scope):
        """Run the downstream app in-process; its receive never reports a disconnect, so a client that gives up
        cannot cancel the compute. -> (status, raw_headers, body)."""
        msgs = []

        async def send(m):
            msgs.append(m)

        async def receive():
            await asyncio.Event().wait()  # GET only: no body, and never a disconnect
        await inner(scope, receive, send)
        start = next(m for m in msgs if m["type"] == "http.response.start")
        return start["status"], start["headers"], b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body")

    async def compute(inner, scope, request, key, fresh):
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
                status, raw, body = await run_app(inner, scope)
                ms = int((time.monotonic() - t0) * 1000)
                hdr = {k.lower(): v for k, v in raw}
                out = Response(body, status)
                out.raw_headers = [(k, v) for k, v in raw if k.lower() not in (b"content-length", b"x-cache-skip")]
                out.headers["content-length"] = str(len(body))
                out.headers["X-Cache"] = "bypass" if fresh else "miss"
                out.headers["X-Computed-At"] = str(int(time.time()))
                # x-cache-skip: the handler flagged a degraded result: never store, never send
                if (b"x-cache-skip" not in hdr and gen is not _FAIL and status == 200
                        and hdr.get(b"content-type", b"").startswith(b"application/json")):
                    try:
                        ok = not await asyncio.to_thread(_degraded, body)
                    except ValueError:
                        ok = False
                    if ok:
                        await db(lambda c: cache.store(c, key, body, ms, gen,
                                                       keep_last_used=request.headers.get("x-cache-warm") == "1"))
                return out
        finally:
            if not lock.locked():
                locks.pop(lk, None)  # shortcut: a woken waiter may race a new lock; worst case one duplicate compute

    class ResultCache:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            request = Request(scope) if scope["type"] == "http" else None
            key = (cache.make_key(scope["path"], request.query_params.multi_items())
                   if request and scope["method"] == "GET" else None)
            if key is None:
                return await self.inner(scope, receive, send)
            fresh = "fresh" in request.query_params
            if not fresh:
                hit = await db(lambda c: cache.lookup(c, key))
                if hit not in (None, _FAIL):
                    return await hit_response(*hit)(scope, receive, send)
            # detached: a client that gives up (the UI aborts after a while) must not cancel the compute or the
            # store, or a slow cold page would never reach the cache
            task = asyncio.create_task(compute(self.inner, scope, request, key, fresh))
            tasks.add(task)
            task.add_done_callback(done)
            out = await asyncio.shield(task)
            await out(scope, receive, send)

    app.add_middleware(ResultCache)
