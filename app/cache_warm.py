"""Hourly background refresh of stored result-cache entries, by calling the app in-process with fresh=1."""
import asyncio
import logging
import time

import httpx

from app import cache
from app.core.db import open_meta

log = logging.getLogger(__name__)


class CacheWarmer:
    def __init__(self, app, *, now_fn=time.time, sleep_fn=asyncio.sleep):
        self.app, self.now_fn, self.sleep_fn = app, now_fn, sleep_fn

    def _db(self, fn, *args):
        c = open_meta(self.app.state.settings.data_dir)
        try:
            return fn(c, *args)
        finally:
            c.close()

    async def run_once(self) -> int:
        now, n = int(self.now_fn()), 0
        try:
            await asyncio.to_thread(self._db, cache.evict, now)
            keys = await asyncio.to_thread(self._db, cache.stale_keys, now)
        except Exception:
            log.exception("cache warm: evict/list failed")
            return 0
        # loopback Host so ALLOWED_HOSTS (TrustedHostMiddleware) accepts the request
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url="http://127.0.0.1", timeout=300) as client:
            for key in keys:
                try:
                    r = await client.get(key + ("fresh=1" if key.endswith("?") else "&fresh=1"),
                                       headers={"X-Cache-Warm": "1"})
                    if r.status_code == 200:
                        n += 1
                    else:
                        log.warning("cache warm %s: HTTP %s", key, r.status_code)
                except Exception:
                    log.exception("cache warm %s failed", key)
        return n

    async def run(self) -> None:
        await self.sleep_fn(60)
        while True:
            await self.run_once()
            await self.sleep_fn(self.app.state.settings.result_cache_refresh_seconds)
