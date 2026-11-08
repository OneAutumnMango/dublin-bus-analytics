"""Poll tracked routes every poll_seconds, feed the Tracker, record server outages as gaps.

Gap policy: a gap row is (start, end) in epoch seconds and means the service as a whole is down: a
tick with at least one ServerUnavailable and zero successful fetches. It opens a row (start=end=now);
while such ticks continue the open row's end is extended; the first tick with any successful fetch
closes it (end=that tick's now). Per-route failures while other routes succeed are only logged. The open gap is in-memory
only: a restarted Poller does not resume an old row, the next outage simply opens a new one (rows
never overlap because the old row's end is <= the new start).

Visit number: the tracker's 'seq' (arrivals.seq) is the arrival's scheduled epoch, not a stop-sequence
rank. The server's /v1/trips stopCalls come from the live feed only (empty before a bus starts,
partial once live), so they cannot number visits. A loop trip's two visits of one stop, and a recycled
trip id on another day, have different scheduled epochs; a bus going from scheduled-only to live keeps
its scheduled epoch, hence the same key. Arrivals with no scheduled time are skipped by the tracker.
"""
import asyncio
import logging
import re
import time
from pathlib import Path

from app.db import open_meta
from app.serverclient import RouteNotFound, ServerUnavailable
from app.tracker import Tracker

log = logging.getLogger(__name__)

ROUTE_RE = re.compile(r"^[A-Za-z0-9 _./-]{1,20}$")


async def seq_lookup(a) -> int:
    """Tracker visit discriminator. The seq column holds the call's scheduled epoch."""
    if a.scheduled is None:
        raise ValueError("no scheduled time")  # tracker skips (and counts) the arrival
    return a.scheduled


class LazyTracker:
    """Defers Tracker construction (which opens the DB) to first use, so create_app does no I/O."""

    def __init__(self, data_dir: Path, seq_lookup):
        self._args = (data_dir, seq_lookup)
        self._t: Tracker | None = None

    async def ingest(self, route, arrivals, now):
        if self._t is None:
            self._t = Tracker(*self._args)
        return await self._t.ingest(route, arrivals, now)

    def close(self) -> None:
        if self._t is not None:
            self._t.close()
            self._t = None


class Poller:
    def __init__(self, settings, client, tracker, *, now_fn=None, mono_fn=time.monotonic,
                 sleep_fn=asyncio.sleep):
        self.settings = settings
        self.client = client
        self.tracker = tracker
        self.now_fn = now_fn or (lambda: int(time.time()))
        self.mono_fn = mono_fn
        self.sleep_fn = sleep_fn
        self._gap_open: tuple[int, int] | None = None  # (rowid, start)
        self._last_now = 0

    # -- tracked routes (short-lived connection per call: safe from any thread) --
    def _meta(self):
        return open_meta(self.settings.data_dir)

    @staticmethod
    def _clean(route: str) -> str:
        if not isinstance(route, str) or not ROUTE_RE.match(route := route.strip()):
            raise ValueError(f"invalid route {route!r}")
        return route

    def track(self, route: str) -> None:
        route = self._clean(route)
        c = self._meta()
        try:
            with c:
                c.execute("insert or ignore into routes_tracked(route, added) values (?, ?)",
                          (route, int(time.time())))
        finally:
            c.close()

    def untrack(self, route: str) -> None:
        route = self._clean(route)
        c = self._meta()
        try:
            with c:
                c.execute("delete from routes_tracked where route=?", (route,))
        finally:
            c.close()

    def tracked(self) -> list[str]:
        c = self._meta()
        try:
            return [r for (r,) in c.execute("select route from routes_tracked order by route")]
        finally:
            c.close()

    # -- gaps --
    def _gap(self, now: int, down: bool) -> None:
        if not down and self._gap_open is None:
            return
        c = self._meta()
        try:
            with c:
                if self._gap_open is None:
                    cur = c.execute("insert into gaps(start, end) values (?, ?)", (now, now))
                    self._gap_open = (cur.lastrowid, now)
                else:
                    c.execute("update gaps set end=? where rowid=?", (max(now, self._gap_open[1]), self._gap_open[0]))
        finally:
            c.close()
        if not down:
            self._gap_open = None

    async def _one(self, route: str, now: int):
        arrivals = await self.client.route_arrivals(route)  # may raise; handled in tick
        try:
            res = await self.tracker.ingest(route, arrivals, now)
        except Exception:
            log.exception("ingest failed for route %s", route)
            return None
        if res.skipped or res.stale:
            log.warning("route %s: skipped=%d stale=%s", route, res.skipped, res.stale)
        return res

    async def tick(self, now: int) -> None:
        routes = self.tracked()
        results = await asyncio.gather(*(self._one(r, now) for r in routes), return_exceptions=True)
        down = fetched = False
        for route, res in zip(routes, results):
            if isinstance(res, ServerUnavailable):
                down = True
                log.warning("server unavailable for route %s: %s", route, res)
            elif isinstance(res, RouteNotFound):
                log.warning("route %s not found on server", route)
            elif isinstance(res, BaseException):
                log.error("poll failed for route %s", route, exc_info=res)
            else:
                fetched = True
        if fetched:  # per-route failures alongside a success are not a service outage
            self._gap(now, False)
        elif down:
            self._gap(now, True)

    async def run(self) -> None:
        interval = self.settings.poll_seconds
        nxt = self.mono_fn()
        while True:
            now = self._last_now = max(self._last_now, int(self.now_fn()))
            try:
                await self.tick(now)
            except Exception:
                log.exception("poll tick failed")
            nxt += interval
            delay = nxt - self.mono_fn()
            if delay < 0:  # overran: realign to now and run the next tick at once; missed slots are dropped, never burst
                nxt = self.mono_fn()
                delay = 0
            await self.sleep_fn(delay)
