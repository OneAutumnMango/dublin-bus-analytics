"""Poll tracked routes every poll_seconds, feed the Tracker, record server outages as gaps.

Gap policy: a gap row is (start, end) in epoch seconds. The first ServerUnavailable in a tick opens a
row (start=end=now); while failures continue the open row's end is extended; the first tick with no
failure and at least one successful fetch closes it (end=that tick's now). The open gap is in-memory
only: a restarted Poller does not resume an old row, the next outage simply opens a new one (rows
never overlap because the old row's end is <= the new start).
"""
import asyncio
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path

from app.db import open_meta
from app.serverclient import RouteNotFound, ServerUnavailable
from app.tracker import Tracker

log = logging.getLogger(__name__)

ROUTE_RE = re.compile(r"^[A-Za-z0-9 _./-]{1,20}$")


class SeqResolver:
    """Tracker seq_lookup: rank of arrival.scheduled among the trip's calls at that stop.
    Raises (never returns None) when it cannot answer, so the tracker skips the arrival."""

    def __init__(self, client, max_trips: int = 2048):
        self.client = client
        self.max_trips = max_trips
        self._cache: OrderedDict[str, dict[str, list[int]]] = OrderedDict()
        self._lock = asyncio.Lock()  # one trip fetch at a time; avoids duplicate fetches per trip

    async def _ranks(self, trip_id: str) -> dict[str, list[int]]:
        async with self._lock:
            if trip_id in self._cache:
                self._cache.move_to_end(trip_id)
                return self._cache[trip_id]
            by_stop: dict[str, list[int]] = {}
            for c in await self.client.trip_calls(trip_id):
                s = c.get("scheduledEpochSeconds")
                if isinstance(s, int) and not isinstance(s, bool):
                    by_stop.setdefault(str(c.get("stopId")), []).append(s)
            for v in by_stop.values():
                v.sort()
            self._cache[trip_id] = by_stop
            while len(self._cache) > self.max_trips:
                self._cache.popitem(last=False)
            return by_stop

    async def __call__(self, a) -> int:
        if a.scheduled is None:
            raise ValueError("no scheduled time")
        return (await self._ranks(a.trip_id))[a.stop_id].index(a.scheduled)  # KeyError/ValueError if unmatched


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

    def track(self, route: str) -> None:
        if not isinstance(route, str) or not ROUTE_RE.match(route := route.strip()):
            raise ValueError(f"invalid route {route!r}")
        c = self._meta()
        try:
            with c:
                c.execute("insert or ignore into routes_tracked(route, added) values (?, ?)",
                          (route, int(time.time())))
        finally:
            c.close()

    def untrack(self, route: str) -> None:
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
        if down or fetched:
            self._gap(now, down)

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
            if delay < 0:  # overran: skip missed slots rather than overlap or burst
                nxt = self.mono_fn()
                delay = 0
            await self.sleep_fn(delay)
