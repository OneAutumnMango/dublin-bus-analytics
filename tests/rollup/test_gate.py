import asyncio
import threading
import time

import httpx

from app.api import routes
from tests.api.test_routes import Env


def test_all_overview_scans_never_overlap(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.app.state.poller.track("15")
    env.add(route="15", delay=10)
    live, peak, lock = 0, 0, threading.Lock()

    def slow(real):
        def wrapped(*a, **k):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.15)
            with lock:
                live -= 1
            return real(*a, **k)
        return wrapped

    for name in ("_route_stats_db", "_heatmap_db", "_forecast_parts_db"):
        monkeypatch.setattr(routes, name, slow(getattr(routes, name)))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t") as c:
            rs = await asyncio.gather(*[c.get(f"/api/all/{e}") for e in ("routes", "heatmap", "forecast")])
            assert [r.status_code for r in rs] == [200, 200, 200]

    asyncio.run(go())
    assert peak == 1
