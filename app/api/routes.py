"""JSON API under /api. Handlers that touch only sqlite are plain `def` (threadpool) and open+close
their own connection per request; proxy/poller handlers are `async def` (the client is async and the
poller's connection is bound to the event-loop thread).

Statistics endpoints share these query parameters (all validated, invalid -> 422):
  ci in (0, 0.9999] default 0.95; pct_lo < pct_hi in [0,1] default 0.05/0.95; window_lo <= window_hi seconds,
  the on-time window, default -60/300; since/until epoch ints (inclusive, on `scheduled`);
  weekdays (comma list 0=Mon..6) and hours (comma list 0..23), Dublin-local;
  min_confidence (0 or 1, DEFAULT 1): stats only use rows whose actual was confirmed by a due==0
  sample. Pass min_confidence=0 to include unconfirmed (confidence 0) rows. Non-realtime
  (scheduled-only) rows are ALWAYS excluded. Every response echoes the applied filters in `filters`.
A route/stop with no stored data gives 200 with n=0 and null stats; only the server-proxy endpoints
can 404 (unknown route). `arrivals.seq` is a per-visit key (the scheduled epoch), never a stop position:
stop order comes only from the server's route_stops (`order_source` 'server' | 'none'). low_n is true when n < 10.
"""
import asyncio
import functools
import logging
import math
import os
import re
import sqlite3
import threading
import time
import weakref
import zlib
from array import array
from bisect import bisect_right
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import unquote

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from app.analysis import lab, stats
from app.core.db import open_meta, open_samples_readonly
from app.collect.poller import ROUTE_RE
from app.collect.serverclient import RouteNotFound, ServerUnavailable



class RawPathRoute(APIRoute):
    """Match on the still-encoded path so an encoded slash (route "15/A" as 15%2FA) stays inside
    one path parameter; Starlette would otherwise decode it before routing and 404.
    Depends on the ASGI `raw_path` key and on unpinned FastAPI/Starlette routing internals
    (Route.matches child scope); test_slash_route_encoded is the tripwire if an upgrade breaks it."""

    def matches(self, scope):
        raw = scope.get("raw_path")
        match, child = super().matches({**scope, "path": raw.decode("ascii", "replace")} if raw else scope)
        if match == Match.FULL:
            child["path_params"] = {k: unquote(v) for k, v in child["path_params"].items()}
            child["path"] = scope["path"]
        return match, child


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", route_class=RawPathRoute)

LOW_N = 10
FALLBACK = "__unknown__"  # query value selecting the fallback group (internally keyed by None)
STOPS_TTL, STOPS_CACHE_MAX, STOPS_TIMEOUT = 300, 256, 3.0  # route_stops cache seconds / entries / call timeout
CI_MAX = 0.9999
HORIZONS = (30, 20, 10, 5, 2)
RECENT = 20
HIST_LO, HIST_HI, HIST_W = -600, 1800, 60  # seconds; outer bins are open-ended
_INT = re.compile(r"-?[0-9]{1,12}")  # [0-9], not \d: Arabic-Indic digits must be rejected
_FLOAT = re.compile(r"-?(?:[0-9]+\.?[0-9]*|\.[0-9]+)")

RouteP = Annotated[str, Path(min_length=1, max_length=32)]
StopP = Annotated[str, Path(min_length=1, max_length=64)]
QStr = Annotated[str | None, Query(max_length=64)]


def _bad(msg):
    raise HTTPException(422, msg)


def _int(name, raw):
    if not _INT.fullmatch(raw):
        _bad(f"{name} must be an integer")
    return int(raw)


def _float(name, raw):
    if not _FLOAT.fullmatch(raw):  # also rejects nan/inf/1_0/unicode digits
        _bad(f"{name} must be a number")
    return float(raw)


def _int_list(name, raw, lo, hi):
    out = sorted({_int(name, p.strip()) for p in raw.split(",")})
    if any(not lo <= v <= hi for v in out):
        _bad(f"{name} values must be in {lo}..{hi}")
    return out


def _params(ci: QStr = "0.95", pct_lo: QStr = "0.05", pct_hi: QStr = "0.95",
            window_lo: QStr = "-60", window_hi: QStr = "300", since: QStr = None,
            until: QStr = None, weekdays: QStr = None, hours: QStr = None,
            min_confidence: QStr = "1", minutes_from: QStr = None, minutes_to: QStr = None) -> dict:
    p = {"ci": _float("ci", ci), "pct_lo": _float("pct_lo", pct_lo),
         "pct_hi": _float("pct_hi", pct_hi), "window_lo": _int("window_lo", window_lo),
         "window_hi": _int("window_hi", window_hi),
         "since": None if since is None else _int("since", since),
         "until": None if until is None else _int("until", until),
         "weekdays": None if weekdays is None else _int_list("weekdays", weekdays, 0, 6),
         "hours": None if hours is None else _int_list("hours", hours, 0, 23),
         "min_confidence": _int("min_confidence", min_confidence), "truncated": False, "minutes": None}
    if (minutes_from is None) != (minutes_to is None):
        _bad("minutes_from and minutes_to must be given together")
    if minutes_from is not None:
        lo, hi = _int("minutes_from", minutes_from), _int("minutes_to", minutes_to)
        if not (0 <= lo <= 1439 and 0 <= hi <= 1439):
            _bad("minutes_from and minutes_to must be in 0..1439")
        p["minutes"] = [lo, hi]
    if not 0 < p["ci"] <= CI_MAX:  # beyond ~1-1e-16 the t quantile is inf; 0.9999 is already extreme
        _bad(f"ci must satisfy 0 < ci <= {CI_MAX}")
    if not 0 <= p["pct_lo"] < p["pct_hi"] <= 1:
        _bad("pct_lo and pct_hi must satisfy 0 <= pct_lo < pct_hi <= 1")
    if p["window_lo"] > p["window_hi"]:
        _bad("window_lo must be <= window_hi")
    if p["since"] is not None and p["until"] is not None and p["since"] > p["until"]:
        _bad("since must be <= until")
    if p["min_confidence"] not in (0, 1):
        _bad("min_confidence must be 0 or 1")
    return p


P = Annotated[dict, Depends(_params)]


def _clean(o):
    """Backstop: non-finite floats become null so a response can never fail JSON encoding."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _filters(p):
    out = {**p, "realtime_only": True}
    if out["minutes"] is None:
        del out["minutes"]  # only echoed when a time-of-day window applies
    return out


def _forecast_filters(p):
    # forecast percentiles are fixed at 5/95, so pct_lo/pct_hi are accepted but not echoed
    return {k: v for k, v in _filters(p).items() if k not in ("pct_lo", "pct_hi")}


# Concurrent sqlite readers in the threadpool slow each other 10-1000x on this host, so ALL sqlite
# work in this module runs under one lock. It is taken per statement / per fetched chunk (never
# around the caller's processing), so short requests interleave with a long scan; `with` releases
# it on any error.
_DB_LOCK = threading.Lock()
CHUNK = 50_000
# Hard cap on arrivals examined per request: only the most recent MAX_ROWS rows (by scheduled) are
# used; when older rows were dropped `filters.truncated` is true and `filters.since` is the effective
# lower bound. Keeps any single request to a few hundred MB.
MAX_ROWS = 500_000


def _fetch(request: Request, sql: str, args: list) -> list[tuple]:
    """Small result sets only (cap probe)."""
    with _DB_LOCK, closing(open_meta(request.app.state.settings.data_dir)) as c:
        return c.execute(sql, args).fetchall()


def _scan(request: Request, sql: str, args: list, fn, size: int | None = None) -> None:
    """Run `sql`, calling fn(list_of_row_tuples) for each fetched chunk, outside the DB lock."""
    size = size or CHUNK
    c = None
    try:
        with _DB_LOCK:
            c = open_meta(request.app.state.settings.data_dir)
            cur = c.execute(sql, args)
            rows = cur.fetchmany(size)
        while rows:
            fn(rows)
            with _DB_LOCK:
                rows = cur.fetchmany(size)
    finally:
        if c is not None:
            with _DB_LOCK:
                c.close()


def _cond(p, a=""):
    """SQL fragment + params for the filters SQLite can apply (realtime, confidence, since/until)."""
    sql, args = f"{a}realtime = 1 and {a}confidence >= ? and {a}scheduled is not null", [p["min_confidence"]]
    for k, op in (("since", ">="), ("until", "<=")):
        if p[k] is not None:
            sql += f" and {a}scheduled {op} ?"
            args.append(p[k])
    return sql, args


MAX_HEAVY_SCANS = 2  # concurrent heavy scans; each can hold ~100+ MB
HEAVY_ROWS = 50_000  # a request matching more arrivals than this takes a heavy slot
_gates: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # event loop -> {name: asyncio.Semaphore}


def _gate(name: str) -> asyncio.Semaphore:
    """Per-event-loop semaphore ("heavy": MAX_HEAVY_SCANS permits, "gz": 1). Waiting happens on the loop,
    so a queued request holds no thread; lock order is always gate first, then _DB_LOCK per chunk."""
    d = _gates.setdefault(asyncio.get_running_loop(), {})
    if name not in d:
        d[name] = asyncio.Semaphore(MAX_HEAVY_SCANS if name == "heavy" else 1)
    return d[name]


def _cap(request, p, where: str, args: list, delay=True) -> tuple[dict, bool]:
    """(params, heavy): params with `since` raised (and truncated=True) when more than MAX_ROWS rows match;
    heavy is true when more than HEAVY_ROWS match. The probes ignore NULL-delay rows (delay=True) exactly
    as the scan does."""
    cond, cargs = _cond(p)
    probe = (f"select scheduled from arrivals where {where} and {cond}"
             f"{' and delay is not null' if delay else ''} order by scheduled desc limit 2 offset ?")
    rows = _fetch(request, probe, args + cargs + [MAX_ROWS - 1])
    if len(rows) == 2:
        return {**p, "since": rows[0][0], "truncated": True}, True
    return p, bool(_fetch(request, probe, args + cargs + [HEAVY_ROWS]))


async def _heavy(request, p, where: str, args: list, work, delay=True):
    """Probe in a thread, then (for a heavy request) wait for a slot ON THE LOOP and run work(capped p)
    in a thread. The permit is released on any exit; a request cancelled while waiting never held one."""
    p, heavy = await run_in_threadpool(_cap, request, p, where, args, delay)
    if not heavy:
        return await run_in_threadpool(work, p)
    async with _gate("heavy"):
        return await run_in_threadpool(work, p)


def _tz(rows, i, p):
    """Weekday/hour (Dublin-local) filter in Python on tuples; i = index of `scheduled`."""
    if p["weekdays"] is None and p["hours"] is None and p["minutes"] is None:
        return rows
    kept = stats.filter_rows([{"scheduled": r[i], "_r": r} for r in rows], min_confidence=0,
                             realtime_only=False, weekdays=p["weekdays"], hours=p["hours"], minutes=p["minutes"])
    return [d["_r"] for d in kept]


def _scan_arrivals(request, p, where: str, args: list, cols: str, fn) -> None:
    """fn(chunk) for filtered arrivals as tuples of `cols` plus `scheduled` as the LAST column, ordered
    by scheduled. realtime/confidence/since/until filter in SQL (index ix_arr_route_sched)."""
    cond, cargs = _cond(p)

    def apply(rows):
        rows = _tz(rows, -1, p)
        if rows:
            fn(rows)

    _scan(request, f"select {cols}, scheduled from arrivals where {where} and {cond} "
          "and delay is not null order by scheduled", args + cargs, apply)


def _summ(delays, p) -> dict:
    s = stats.delay_summary(delays, p["ci"], (p["pct_lo"], p["pct_hi"]))
    return {**s, "low_n": s["n"] < LOW_N,
            "on_time_pct": stats.on_time_pct(delays, p["window_lo"], p["window_hi"])["pct"],
            "reliability": stats.reliability_score(delays, p["window_lo"], p["window_hi"])}


async def _proxy(coro):
    try:
        return await coro
    except RouteNotFound:
        raise HTTPException(404, "not found on server") from None
    except ServerUnavailable as e:
        raise HTTPException(502, f"server unavailable: {e}") from None


class _NoOrder(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


def _sid(x):
    return x.get("id", x.get("stopId"))


class _TTLCache:
    """Small thread-safe cache with expiry and a max size (oldest entry evicted). Clock injectable."""

    def __init__(self, ttl=STOPS_TTL, maxsize=STOPS_CACHE_MAX, clock=time.monotonic):
        self.ttl, self.maxsize, self.clock = ttl, maxsize, clock
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, k):
        with self._lock:
            e = self._d.get(k)
            if e is None:
                return None
            if e[0] <= self.clock():
                del self._d[k]
                return None
            return e[1]

    def put(self, k, v):
        with self._lock:
            now = self.clock()
            if k not in self._d and len(self._d) >= self.maxsize:
                for key in [key for key, e in self._d.items() if e[0] <= now]:
                    del self._d[key]
                if len(self._d) >= self.maxsize:
                    del self._d[next(iter(self._d))]
            self._d[k] = (now + self.ttl, v)

    def clear(self):
        with self._lock:
            self._d.clear()


def _cache(request) -> _TTLCache:
    st = request.app.state
    if getattr(st, "stops_cache", None) is None:
        st.stops_cache = _TTLCache()
    return st.stops_cache


async def _directions(request, r) -> dict[str, list[dict]]:
    """Server's {direction: [stop dicts in order]}, validated, cached 5 min per route (failures are
    never cached; the call has a 3 s timeout). Raises _NoOrder (404 unknown route, 502
    unavailable/timeout/malformed shape)."""
    cache = _cache(request)
    hit = cache.get(r)
    if hit is not None:
        return hit
    try:
        raw = await asyncio.wait_for(request.app.state.client.route_stops(r), STOPS_TIMEOUT)
    except RouteNotFound:
        raise _NoOrder(404, "route not found on server") from None
    except (ServerUnavailable, KeyError, TypeError, AttributeError, ValueError, asyncio.TimeoutError) as e:
        raise _NoOrder(502, f"server unavailable or malformed: {e!r}") from None
    if not (isinstance(raw, dict) and all(
            isinstance(k, str) and isinstance(v, list)
            and all(isinstance(x, dict) and isinstance(_sid(x), str) for x in v)
            for k, v in raw.items())):
        raise _NoOrder(502, "malformed stops response from server")
    cache.put(r, raw)
    return raw


async def _order(request, r):
    """-> ({direction: [stop_id...]}, "server") or (None, "none") when the order is unavailable."""
    try:
        d = await _directions(request, r)
    except _NoOrder:
        return None, "none"
    return {k: [_sid(x) for x in v] for k, v in d.items()}, "server"


async def _bounded_map(request, items: list, fn) -> list:
    """await fn(item) for every item, at most server_concurrency in flight; results in input order,
    exceptions returned as values."""
    sem = asyncio.Semaphore(getattr(request.app.state.settings, "server_concurrency", 2))

    async def one(x):
        async with sem:
            return await fn(x)
    return await asyncio.gather(*(one(x) for x in items), return_exceptions=True)


# ---- routes: proxy + tracking ----

@router.get("/routes")
async def search_routes(request: Request, q: QStr = ""):
    return await _proxy(request.app.state.client.search_routes(q or ""))


async def _tracked(request):
    return {"routes": sorted(await run_in_threadpool(request.app.state.poller.tracked))}


@router.get("/routes/tracked")
async def tracked(request: Request):
    return await _tracked(request)


async def _change(request, route, fn_name):
    route = route.strip()
    if not ROUTE_RE.match(route):
        _bad("invalid route name")
    try:  # poller methods open sqlite connections: keep them off the event loop
        await run_in_threadpool(getattr(request.app.state.poller, fn_name), route)
    except ValueError as e:
        _bad(str(e))
    return await _tracked(request)


@router.put("/routes/tracked/{route}")
async def track(request: Request, route: RouteP):
    return await _change(request, route, "track")


@router.delete("/routes/tracked/{route}")
async def untrack(request: Request, route: RouteP):
    return await _change(request, route, "untrack")


@router.get("/routes/{r}/stops")
async def route_stops(request: Request, r: RouteP):
    try:
        return {"route": r, "directions": await _directions(request, r)}
    except _NoOrder as e:
        raise HTTPException(e.status, e.msg) from None


# ---- stats ----

def _summary_db(request, r, p, lists):
    by_stop: dict[str, array] = defaultdict(lambda: array("q"))
    total = [0]

    def fn(rows):
        total[0] += len(rows)
        for sid, d, _ in rows:
            by_stop[sid].append(d)

    _scan_arrivals(request, p, "route = ?", [r], "stop_id, delay", fn)
    pos = {}  # stop -> (first direction index, first position)
    dirs: dict[str, list[str]] = defaultdict(list)  # stop -> every direction listing it
    for di, (name, ids) in enumerate((lists or {}).items()):
        for i, sid in enumerate(ids):
            pos.setdefault(sid, (di, i))
            if name not in dirs[sid]:
                dirs[sid].append(name)
    out = [{"stop_id": sid, "position": pos[sid][1] if sid in pos else None,
            "direction": dirs[sid][0] if len(dirs.get(sid, ())) == 1 else None,
            "directions": dirs.get(sid, []), **_summ(ds, p)}
           for sid, ds in by_stop.items()]
    out.sort(key=lambda s: (s["stop_id"] not in pos, pos.get(s["stop_id"], (0, 0)), s["stop_id"]))
    n = total[0]
    return _clean({"route": r, "filters": _filters(p), "order_source": "none" if lists is None else "server",
                   "n": n, "low_n": n < LOW_N, "stops": out})


@router.get("/routes/{r}/summary")
async def route_summary(request: Request, r: RouteP, p: P):
    """Per-stop delay stats in ROUTE order from the server's route_stops (first direction listing
    the stop, by its index there); stops the server does not list come last by stop_id with
    position null; server down or malformed -> stop_id order and order_source "none".
    A stop listed by exactly one direction has `direction` set; one listed by several has direction
    null and `directions` naming them all (position is then from the first listing).
    Each stop: stop_id, position, direction, directions, n, low_n, mean/min/max/std/median/ci_low/ci_high/
    p_low/p_high, on_time_pct, reliability."""
    lists, _src = await _order(request, r)
    return await _heavy(request, p, "route = ?", [r], lambda p: _summary_db(request, r, p, lists))


def _histogram(delays):
    counts = [0] * (2 + (HIST_HI - HIST_LO) // HIST_W)
    for d in delays:
        counts[min(max((d - HIST_LO) // HIST_W + 1, 0), len(counts) - 1)] += 1
    edges = list(range(HIST_LO, HIST_HI + 1, HIST_W))
    return [{"lo": a, "hi": b, "count": c} for a, b, c in zip([None] + edges, edges + [None], counts)]


@router.get("/stops/{s}/stats")
async def stop_stats(request: Request, s: StopP, p: P, route: QStr = None):
    """Stop stats (optionally for one route): n, low_n, delay{summary; ci_low..ci_high is the CI of
    the actual-minus-scheduled delay}, on_time_pct, reliability, histogram (60 s bins from -600 to
    1800 plus open-ended outer bins) and the 20 most recent arrivals."""
    where, args = ("stop_id = ?", [s]) if route is None else ("stop_id = ? and route = ?", [s, route])
    return await _heavy(request, p, where, args, lambda p: _stop_stats_db(request, s, route, where, args, p))


def _stop_stats_db(request, s, route, where, args, p):
    delays, recent = array("q"), deque(maxlen=RECENT)

    def fn(rows):
        delays.extend(r[5] for r in rows)
        recent.extend(rows[-RECENT:])

    _scan_arrivals(request, p, where, args, "trip_id, route, seq, service_date, actual, delay, confidence", fn)
    d = _summ(delays, p)
    keys = ("trip_id", "route", "seq", "service_date", "actual", "delay", "confidence", "scheduled")
    return _clean({"stop_id": s, "route": route, "filters": _filters(p), "n": d["n"], "low_n": d["low_n"],
                   "delay": {k: d[k] for k in ("mean", "min", "max", "std", "median", "ci_low", "ci_high",
                                              "p_low", "p_high")},
                   "on_time_pct": d["on_time_pct"], "reliability": d["reliability"],
                   "histogram": _histogram(delays), "recent": [dict(zip(keys, r)) for r in reversed(recent)]})


@router.get("/routes/{r}/heatmap")
async def route_heatmap(request: Request, r: RouteP, p: P):
    """cells: [{weekday 0=Mon, hour, n, low_n, mean_delay}] for Dublin-local scheduled time."""
    return await _heavy(request, p, "route = ?", [r], lambda p: _heatmap_db(request, r, p))


def _heatmap_db(request, r, p):
    cnt, tot = defaultdict(int), defaultdict(float)

    def fn(rows):  # per-chunk heatmap merged by (n, sum): no per-row objects survive the chunk
        for k, v in stats.heatmap([{"delay": d, "scheduled": s} for d, s in rows]).items():
            cnt[k] += v["n"]
            tot[k] += v["mean_delay"] * v["n"]

    _scan_arrivals(request, p, "route = ?", [r], "delay", fn)
    cells = [{"weekday": wd, "hour": hr, "n": cnt[k], "low_n": cnt[k] < LOW_N, "mean_delay": tot[k] / cnt[k]}
             for k in sorted(cnt) for wd, hr in [k]]
    return _clean({"route": r, "filters": _filters(p), "n": sum(cnt.values()), "cells": cells})


def _assign(stops, delays, scheds, pos):
    """One trip: parallel lists (stop index, delay, scheduled) sorted by scheduled; pos:
    {direction: {stop index: [positions]}}. Greedy per direction: each row takes the first
    occurrence of its stop after the previous matched position (handles loops); rows with equal
    scheduled are first ordered by their position in that direction. Best match count wins;
    a tie or no match -> (None, [])."""
    best, tied = (0, None, []), False
    for name, occ in pos.items():
        prev, m = -1, []
        for i in sorted(range(len(stops)), key=lambda i: (scheds[i], occ[stops[i]][0] if stops[i] in occ else len(occ))):
            lst = occ.get(stops[i], ())
            j = bisect_right(lst, prev)
            if j < len(lst):
                prev = lst[j]
                m.append((prev, delays[i], stops[i]))
        if len(m) > best[0]:
            best, tied = (len(m), name, m), False
        elif m and len(m) == best[0]:
            tied = True
    return (None, []) if tied or best[0] == 0 else (best[1], best[2])


def _matched_trips(request, r, p, lists):
    """Per trip occurrence: (service_date, direction name or None, matched) with matched as `_assign`
    returns it. Rows are held in flat arrays (no per-row objects)."""
    stop_ix: dict[str, int] = {}
    trip_ix: dict[tuple, int] = {}
    cols = [array("q") for _ in range(4)]  # trip no, stop no, delay, scheduled

    def fn(rows):
        for t, sd, sid, d, sch in rows:  # rows arrive ordered by scheduled
            cols[0].append(trip_ix.setdefault((t, sd), len(trip_ix)))
            cols[1].append(stop_ix.setdefault(sid, len(stop_ix)))
            cols[2].append(d)
            cols[3].append(sch)

    _scan_arrivals(request, p, "route = ?", [r], "trip_id, service_date, stop_id, delay", fn)
    dates = [k[1] for k in trip_ix]  # dict order == trip no
    del trip_ix
    pos = {name: defaultdict(list) for name in lists}
    for name, ids in lists.items():
        for i, sid in enumerate(ids):
            if sid in stop_ix:
                pos[name][stop_ix[sid]].append(i)
    names = list(stop_ix)
    trip_no, stop_no, dly, sch = (np.frombuffer(c, dtype=np.int64) for c in cols) if cols[0] else ([],) * 4
    order = np.argsort(trip_no, kind="stable")  # stable: keeps scheduled order inside each trip
    cuts = [0, *(np.flatnonzero(np.diff(trip_no[order])) + 1).tolist(), len(order)] if len(order) else [0]
    out = []
    for lo, hi in zip(cuts, cuts[1:]):
        ix = order[lo:hi]
        name, matched = _assign(stop_no[ix].tolist(), dly[ix].tolist(), sch[ix].tolist(), pos)
        out.append((dates[trip_no[ix[0]]], name, [(pp, d, names[si]) for pp, d, si in matched]))
    return out


def _accumulation_db(request, r, p, lists, direction):
    # per group: [trips, {position: [stop id, n, sum, n_delta, delta sum]}]
    agg = {name: [0, {}] for name in lists} | {None: [0, {}]}  # None (not a str) = fallback group
    for _, name, matched in _matched_trips(request, r, p, lists):
        g = agg[name]
        g[0] += 1
        prev = None
        for pp, d, si in matched:
            a = g[1].setdefault(pp, [si, 0, 0, 0, 0])
            a[1] += 1
            a[2] += d
            if prev is not None and prev[0] == pp - 1:
                a[3] += 1
                a[4] += d - prev[1]
            prev = (pp, d)
    want = None if direction == FALLBACK and direction not in lists else direction
    out = []
    for name, (trips, by_pos) in agg.items():
        if not trips or (direction is not None and name != want):
            continue
        acc = [{"stop_id": a[0], "position": pp, "mean_delay": a[2] / a[1],
                "segment_delta": a[4] / a[3] if a[3] else None, "n": a[1], "n_delta": a[3],
                "low_n": a[1] < LOW_N, "low_n_delta": a[3] < LOW_N} for pp, a in sorted(by_pos.items())]
        out.append({"direction": name, "fallback": name is None,
                    "n": trips, "low_n": trips < LOW_N,
                    "n_delta": max((a["n_delta"] for a in acc), default=0), "stops": acc})
    return out, p


@router.get("/routes/{r}/accumulation")
async def route_accumulation(request: Request, r: RouteP, p: P, direction: QStr = None):
    """groups: [{direction, n (trips), n_delta (max trips behind any segment delta), low_n,
    stops:[{stop_id, position, mean_delay, segment_delta, n, n_delta, low_n, low_n_delta}]}].
    A trip's direction is the server direction whose stop list its rows (by scheduled time) follow
    in increasing position, best match wins; a tie or no match -> the fallback group
    (`direction` null, `fallback` true, n only, stops []). Select it with direction=__unknown__
    (query value only; internally the fallback is keyed by None, so a real server direction that is
    literally named "__unknown__" is an ordinary group and direction=__unknown__ then selects that one).
    Server unavailable/malformed -> 200 {"order_source": "none", "groups": []}, unless
    `direction` was requested (then 404 unknown route / 502). Unknown `direction` -> 404."""
    try:
        lists = {k: [_sid(x) for x in v] for k, v in (await _directions(request, r)).items()}
    except _NoOrder as e:
        if direction is not None:
            raise HTTPException(e.status, e.msg) from None
        return {"route": r, "filters": _filters(p), "order_source": "none", "groups": []}
    if direction is not None and direction not in (*lists, FALLBACK):
        raise HTTPException(404, "unknown direction")
    groups, p = await _heavy(request, p, "route = ?", [r],
                             lambda p: _accumulation_db(request, r, p, lists, direction))
    return _clean({"route": r, "filters": _filters(p), "order_source": "server", "groups": groups})


@router.get("/routes/{r}/lab/distribution")
async def route_lab_distribution(request: Request, r: RouteP, p: P):
    """Delay distribution in seconds: ecdf [{x, p}], qq [{z, y}] and tiles {median|p95|buffer|skew|kurtosis:
    {value, ci_low, ci_high}}; CIs resample whole service days and are null below 5 days (enough_days)."""
    return await _heavy(request, p, "route = ?", [r], lambda p: _distribution_db(request, r, p))


_TILES = ("median", "p95", "buffer", "skew", "kurtosis")


def _tile_stats(days) -> dict:
    """The five tiles from ONE concatenation and ONE sort (same formulas as lab.shape_stats)."""
    x = np.sort(np.concatenate(days)) if days else np.empty(0)
    n = len(x)
    if n == 0:
        return dict.fromkeys(_TILES)
    med, p95 = np.median(x), np.percentile(x, 95)
    sd = x.std()
    z = (x - x.mean()) / sd if sd > 0 else None
    return {"median": float(med), "p95": float(p95), "buffer": float(p95 - med),
            "skew": float((z**3).mean()) if z is not None and n >= 3 else None,
            "kurtosis": float((z**4).mean() - 3) if z is not None and n >= 4 else None}


def _distribution_db(request, r, p):
    buckets = defaultdict(lambda: array("d"))  # service_date -> delays; "" when service_date is null

    def fn(rows):
        for sd, d, _ in rows:  # scheduled is appended last by _scan_arrivals
            buckets[sd or ""].append(d)

    _scan_arrivals(request, p, "route = ?", [r], "service_date, delay", fn)
    days = [np.frombuffer(b, dtype=float) for b in buckets.values()]
    n = sum(len(d) for d in days)
    allx = np.concatenate(days) if days else np.empty(0)
    vals = _tile_stats(days)
    cis = lab.day_bootstrap(days, _tile_stats, level=p["ci"])
    tiles = {k: {"value": vals[k], "ci_low": (cis[k] or (None, None))[0], "ci_high": (cis[k] or (None, None))[1]}
             for k in _TILES}
    return _clean({"route": r, "filters": _filters(p), "n": n, "days": len(days), "low_n": n < LOW_N,
                   "enough_days": len(days) >= lab.MIN_DAYS,
                   "ecdf": [{"x": x, "p": q} for x, q in lab.ecdf_points(allx)],
                   "qq": [{"z": z, "y": y} for z, y in lab.qq_points(allx)], "tiles": tiles})


@router.get("/routes/{r}/lab/variance")
async def route_lab_variance(request: Request, r: RouteP, p: P):
    """Share of delay variance (adjusted omega-squared, 0..1) explained by each of hour, weekday, stop and
    date, one factor at a time: factors [{factor, omega2, ci_low, ci_high, levels}]. The four overlap and do
    not sum to 100%; CIs resample whole service days and are null below 5 days (enough_days)."""
    return await _heavy(request, p, "route = ?", [r], lambda p: _variance_db(request, r, p))


_FACTORS = ("hour", "weekday", "stop", "date")


def _variance_db(request, r, p):
    stops: dict = {}
    hw: dict = {}  # scheduled // 3600 -> (hour, weekday); Dublin offsets are whole hours so this is exact
    buckets = defaultdict(lambda: (array("d"), array("b"), array("b"), array("i")))  # y, hour, weekday, stop

    def fn(rows):
        for sd, st, d, sched in rows:  # scheduled is appended last by _scan_arrivals
            k = int(sched) // 3600
            if k not in hw:
                t = stats._local(k * 3600)
                hw[k] = (t.hour, t.weekday())
            y, h, w, s = buckets[sd or ""]
            y.append(d)
            h.append(hw[k][0])
            w.append(hw[k][1])
            s.append(stops.setdefault(st, len(stops)))

    _scan_arrivals(request, p, "route = ?", [r], "service_date, stop_id, delay", fn)
    days = [tuple(np.frombuffer(a, dtype=t) for a, t in zip(b, ("d", "b", "b", "i"))) for b in buckets.values()]
    n = sum(len(d[0]) for d in days)

    def omegas(ds) -> dict:
        if not ds:
            return dict.fromkeys(_FACTORS)
        cols = [np.concatenate(c) for c in zip(*ds)]
        date = np.repeat(np.arange(len(ds)), [len(d[0]) for d in ds])  # each resampled copy is its own group
        return {f: lab.omega_squared(c, cols[0]) for f, c in zip(_FACTORS, (cols[1], cols[2], cols[3], date))}

    vals = omegas(days)
    cis = lab.day_bootstrap(days, omegas, level=p["ci"])
    cols = [np.concatenate(c) for c in zip(*days)] if days else [np.empty(0)] * 4
    levels = {"hour": len(np.unique(cols[1])), "weekday": len(np.unique(cols[2])),
              "stop": len(np.unique(cols[3])), "date": len(days)}
    return _clean({"route": r, "filters": _filters(p), "n": n, "days": len(days), "low_n": n < LOW_N,
                   "enough_days": len(days) >= lab.MIN_DAYS,
                   "factors": [{"factor": f, "omega2": vals[f], "ci_low": (cis[f] or (None, None))[0],
                                "ci_high": (cis[f] or (None, None))[1], "levels": levels[f]} for f in _FACTORS]})


@router.get("/routes/{r}/forecast")
async def route_forecast(request: Request, r: RouteP, p: P):
    """horizons: always [30,20,10,5,2] in that order: {horizon_min, n, low_n, mean_error, mae,
    p_low, p_high} (null stats when n=0); p_low/p_high are always the 5th/95th percentile. error = actual minus predicted seconds."""
    return await _heavy(request, p, "route = ?", [r], lambda p: _forecast_db(request, r, p), delay=False)


def _forecast_db(request, r, p):
    cond, cargs = _cond(p, "a.")
    marks = ",".join("?" * len(HORIZONS))
    errs = {h: array("q") for h in HORIZONS}  # compact per-horizon arrays; percentiles need the values

    def fn(rows):
        for h, e, _ in _tz(rows, 2, p):
            errs[h].append(e)

    _scan(request, "select f.horizon_min, f.error_s, a.scheduled from arrivals a "
          "join forecast f on f.arrival_id = a.id "
          f"where a.route = ? and {cond} and f.error_s is not null "
          f"and f.horizon_min in ({marks})", [r] + cargs + list(HORIZONS), fn)
    hs = []
    for h in HORIZONS:
        a = np.asarray(errs[h], dtype=float)
        v = dict.fromkeys(("mean_error", "mae", "p_low", "p_high"))
        if len(a):
            v = {"mean_error": float(a.mean()), "mae": float(np.abs(a).mean()),
                 "p_low": float(np.percentile(a, 5)), "p_high": float(np.percentile(a, 95))}
        hs.append({"horizon_min": h, "n": len(a), **v, "low_n": len(a) < LOW_N})
    return _clean({"route": r, "filters": _forecast_filters(p), "horizons": hs})


# ---- trip series ----

MAX_VISITS, MAX_POINTS, MAX_SERIES_MONTHS = 50, 2000, 2
SERIES_MARGIN_S = 6 * 3600  # samples lie within an hour or so of a visit's scheduled..actual; 6 h slack each side
TS_MAX = 4_102_444_800  # 2100-01-01: epochs are clamped to 1970..2100 before any calendar arithmetic
_MONTH_FILE = re.compile(r"samples-(19[7-9][0-9]|20[0-9][0-9])-(0[1-9]|1[0-2])\.db(\.gz)?")
TripP = Annotated[str, Path(min_length=1, max_length=64)]
SeriesStop = Annotated[str, Query(min_length=1, max_length=64)]


def _month_no(ts: int) -> int:
    d = datetime.fromtimestamp(min(max(ts, 0), TS_MAX), timezone.utc)
    return d.year * 12 + d.month - 1


def _disk_months(data_dir) -> dict[int, bool]:
    """{month number: is_gz} for samples-YYYY-MM.db / .db.gz (strict file-name match; no user input in
    paths; a gz, when present, is the authority, as in open_samples_readonly). Symlinks are followed."""
    out: dict[int, bool] = {}
    with os.scandir(data_dir) as it:
        for e in it:
            if m := _MONTH_FILE.fullmatch(e.name):
                k = int(m[1]) * 12 + int(m[2]) - 1
                out[k] = out.get(k, False) or bool(m[3])
    return out


def _read_month(data_dir, month_no: int, sql: str, args: list, fn) -> None:
    """fn(row) for each row of one month; a missing month is skipped, an unreadable (corrupt) one is
    logged and skipped. Decompression of a gz month happens outside _DB_LOCK; the connection (and its
    temp copy) is always closed."""
    ts = int(datetime(month_no // 12, month_no % 12 + 1, 1, tzinfo=timezone.utc).timestamp())
    try:
        c = open_samples_readonly(data_dir, ts)
    except FileNotFoundError:
        return
    except (OSError, EOFError, zlib.error, sqlite3.Error) as e:
        log.warning("series: cannot open samples month %d-%02d: %r", month_no // 12, month_no % 12 + 1, e)
        return
    try:
        with _DB_LOCK:
            cur = c.execute(sql, args)
            rows = cur.fetchmany(CHUNK)
        while rows:
            for r in rows:
                fn(r)
            with _DB_LOCK:
                rows = cur.fetchmany(CHUNK)
    except sqlite3.Error as e:  # corrupt file: keep what was read from the other months
        log.warning("series: unreadable samples month %d-%02d: %r", month_no // 12, month_no % 12 + 1, e)
    finally:
        with _DB_LOCK:
            c.close()


def _series_plan(request, trip_id, stop, want_seq, lo, hi) -> dict:
    """Blocking part 1: pick the visits (the MOST RECENT MAX_VISITS by seq) and the months to read."""
    cond, cargs = "", []
    for op, v in (("=", want_seq), (">=", lo), ("<=", hi)):
        if v is not None:
            cond += f" and seq {op} ?"
            cargs.append(v)
    arr = _fetch(request, "select seq, scheduled, actual, delay, confidence, realtime, service_date from arrivals "
                 f"where trip_id = ? and stop_id = ? and seq is not null{cond} "
                 "order by seq desc, service_date desc limit ?", [trip_id, stop, *cargs, MAX_VISITS + 1])
    st = {"truncated": len(arr) > MAX_VISITS, "visits": {}, "cap": MAX_VISITS,
          "sql": "select seq, ts, due_min, expected, realtime from sample where trip_id = ? and stop_id = ?"
                 f"{cond} order by seq, ts", "args": [trip_id, stop, *cargs]}
    disk = _disk_months(request.app.state.settings.data_dir)
    if arr:
        needs = []
        for sq, sch, act, *_ in arr[:MAX_VISITS]:  # newest first
            ts = [t for t in (sq, sch, act) if t is not None]
            need = {m for m in range(_month_no(min(ts) - SERIES_MARGIN_S), _month_no(max(ts) + SERIES_MARGIN_S) + 1)
                    if m in disk}
            if len(need) > MAX_SERIES_MONTHS:
                need = set(sorted(need)[-MAX_SERIES_MONTHS:])
                st["truncated"] = True
            needs.append(need)
        # unpinned: the newest month on disk may hold a newer, sample-only (dropped bus) visit: read it too
        newest = max(disk) if disk and want_seq is None and lo is None and hi is None else None
        used: set[int] = {newest} if newest is not None and len(needs[0] | {newest}) <= MAX_SERIES_MONTHS else set()
        wanted: set[int] = set()
        for (sq, sch, act, dly, conf, rt, sd), need in zip(arr, needs):
            if len(used | need) > MAX_SERIES_MONTHS:  # older visits would need a month we may not read
                st["truncated"] = True
                break
            used |= need
            wanted |= need
            st["visits"].setdefault(sq, {"seq": sq, "scheduled": sch, "last_seen_ts": None, "points": [],
                                         "arrival": {"actual": act, "delay": dly, "confidence": conf,
                                                     "realtime": rt, "service_date": sd}})
        if newest is not None and newest not in wanted:
            st["truncated"] = True  # a month newer than any kept visit's exists: newer visits may be missing
        months = sorted(used)
    else:  # no arrivals row: months on disk within since/until (an exact seq narrows it), else the latest
        if want_seq is not None:
            lo = hi = want_seq
        a = -math.inf if lo is None else _month_no(lo - SERIES_MARGIN_S)
        b = math.inf if hi is None else _month_no(hi + SERIES_MARGIN_S)
        months = sorted(m for m in disk if a <= m <= b)
        if len(months) > MAX_SERIES_MONTHS:
            st["truncated"], months = True, months[-MAX_SERIES_MONTHS:]
    st["months"] = [(m, disk[m]) for m in months]
    return st


def _series_add(st, row) -> None:
    sq, ts, due, exp, rt = row
    visits = st["visits"]
    v = visits.get(sq)
    if v is None:  # sample-only visit (bus dropped before arrival)
        if len(visits) >= st["cap"]:
            st["truncated"] = True
            oldest = min(visits)
            if sq < oldest:
                return
            del visits[oldest]  # keep the most recent visits
        v = visits[sq] = {"seq": sq, "scheduled": sq, "arrival": None, "last_seen_ts": None, "points": []}
    v["last_seen_ts"] = ts
    if len(v["points"]) >= MAX_POINTS:
        st["truncated"] = True
    else:
        v["points"].append({"ts": ts, "due_min": due, "expected": exp, "realtime": rt})


@router.get("/trips/{trip_id}/series")
async def trip_series(request: Request, trip_id: TripP, stop: SeriesStop, seq: QStr = None, since: QStr = None,
                      until: QStr = None):
    """Prediction series of one trip at one stop: {trip_id, stop_id, truncated, visits: [{seq, scheduled,
    arrival: {actual, delay, confidence, realtime, service_date} | null, last_seen_ts,
    points: [{ts, due_min, expected, realtime}]}]}, visits ascending by seq, points by ts.
    A visit is one call of the trip at the stop; `seq` is its SCHEDULED EPOCH (a loop trip has several
    visits, a trip_id reused on another service date has others). `seq` selects one visit; since/until
    (epoch ints, inclusive; out-of-range values simply match nothing) bound the visit's seq (= scheduled).
    `arrival` is null for a bus that was dropped before arriving. The points are a STEP FUNCTION of the
    feed's predictions (rows are written only on first sight, a change of (expected, realtime) or the first
    due_min <= 0), not one per poll; `due_min` is the feed's value when written. A UI reconstructs due at
    time t as max(0, floor((expected - t) / 60)) and must NOT extrapolate past `last_seen_ts` (the latest
    sample of the visit; a dropped bus has no row after it).
    Limits (any cut sets `truncated`): the MOST RECENT MAX_VISITS visits are returned; at most
    MAX_SERIES_MONTHS month files are read, chosen from the kept visits, and older visits whose months
    would not fit are dropped, so a returned visit never lacks points merely because its month was not
    read; at most MAX_POINTS points per visit (the earliest; `last_seen_ts` is still the true latest).
    Months come from the arrivals rows (scheduled/actual +- 6 h; the tracker accepts predictions up to 24 h
    ahead, so a first sample more than 6 h before scheduled, in an earlier month, can be missing: known
    limitation) plus, when unpinned, the newest month on disk (else `truncated`) or, with no arrivals row, from the months
    on disk within since/until/seq (else the most recent ones), so a sample-only visit is found only in
    those months. Reading a gzipped month decompresses it to a temp file; only one such read runs at a time.
    404 when no visit matched; an arrivals row without samples gives 200 with empty points."""
    want_seq = None if seq is None else _int("seq", seq)
    lo = None if since is None else _int("since", since)
    hi = None if until is None else _int("until", until)
    if lo is not None and hi is not None and lo > hi:
        _bad("since must be <= until")
    st = await run_in_threadpool(_series_plan, request, trip_id, stop, want_seq, lo, hi)
    data_dir = request.app.state.settings.data_dir
    for m, gz in st["months"]:  # ascending: points of a visit stay ordered by ts
        read = functools.partial(run_in_threadpool, _read_month, data_dir, m, st["sql"], st["args"],
                                 lambda r: _series_add(st, r))
        if gz:
            async with _gate("gz"):
                await read()
        else:
            await read()
    visits = st["visits"]
    if not visits:
        raise HTTPException(404, "no such trip/stop visit")
    return _clean({"trip_id": trip_id, "stop_id": stop, "truncated": st["truncated"],
                   "visits": [visits[k] for k in sorted(visits)]})


# ---- map ----

def _route_totals(request, r, p, lists):
    """({stop_id: [n, sum of delays]}, {stop_id: [n_change, sum of changes]}, truncated) for one route under
    the filters. The change metric needs one {stop_id: delay} dict per trip occurrence (trip_id,
    service_date), built during the scan and freed before returning; a duplicate stop within one
    occurrence is stored as None (ambiguous)."""
    acc: dict[str, list] = defaultdict(lambda: [0, 0])
    trips: dict[tuple, dict] = {}

    def fn(rows):
        for tid, sd, sid, d, _ in rows:
            a = acc[sid]
            a[0] += 1
            a[1] += d
            t = trips.setdefault((tid, sd), {})
            t[sid] = None if sid in t else d

    _scan_arrivals(request, p, "route = ?", [r], "trip_id, service_date, stop_id, delay", fn)
    changes = stats.stop_changes(trips, lists)
    trips.clear()
    return dict(acc), changes, p["truncated"]


@router.get("/map")
async def map_data(request: Request, p: P):
    """Every tracked route's stops with position (lat/lon from the server's stop lists) and mean delay under
    the filters (`minutes_from`/`minutes_to` = Dublin-local minute-of-day window, wraps midnight).
    {filters, routes: [{route, directions: {name: [stop_id]}}], unavailable: [route], n,
    stops: [{stop_id, code, name, lat, lon, n, mean_delay, n_change, mean_change,
             routes: [{route, n, mean_delay, n_change, mean_change}]}]}.
    mean_delay is the cumulative delay (actual - scheduled) at the stop. mean_change is the per-stop change:
    delay at the stop minus delay at the previous stop of the same trip occurrence (trip_id, service_date)
    in the server's direction order; the first stop of a direction uses its own delay, but only for trips that
    also have the direction's second stop (so the other direction's trips at a shared terminus are ignored).
    A trip counts at a stop only if it has both adjacent stops (and neither occurs twice in it); n_change counts those trips,
    mean_change is null when 0. At stop level mean_change is the n_change-weighted mean over its routes.
    A stop on several routes/directions appears once; stops without coordinates are skipped;
    routes whose stop list the server cannot give are named in `unavailable`."""
    routes, info, unavailable, per_route, truncated = [], {}, [], {}, False
    tracked = (await _tracked(request))["routes"]
    orders = await _bounded_map(request, tracked, lambda r: _order(request, r))
    for order in orders:
        if isinstance(order, BaseException):
            raise order
    ok = [r for r, o in zip(tracked, orders) if o[0] is not None]
    raws = dict(zip(ok, await _bounded_map(request, ok, lambda r: _directions(request, r))))  # cache hits after _order
    for r, (lists, _src) in zip(tracked, orders):
        if lists is None:
            unavailable.append(r)
            continue
        raw = raws[r]
        if isinstance(raw, BaseException):
            raise raw
        routes.append({"route": r, "directions": lists})
        for stops in raw.values():
            for s in stops:
                if isinstance(s.get("lat"), (int, float)) and isinstance(s.get("lon"), (int, float)):
                    info.setdefault(_sid(s), s)
        totals, chg, trunc = await _heavy(request, p, "route = ?", [r],
                                          lambda p, r=r, lists=lists: _route_totals(request, r, p, lists))
        per_route[r], truncated = (totals, chg), truncated or trunc
    merged: dict[str, dict] = {}
    for r, (totals, chg) in per_route.items():
        for sid in set(totals) | set(chg):
            if sid not in info:
                continue
            n, tot = totals.get(sid, (0, 0))
            nc, sc = chg.get(sid, (0, 0))
            if not n:
                continue
            m = merged.setdefault(sid, {"n": 0, "sum": 0, "nc": 0, "sc": 0, "routes": []})
            m["n"] += n
            m["sum"] += tot
            m["nc"] += nc
            m["sc"] += sc
            m["routes"].append({"route": r, "n": n, "mean_delay": tot / n,
                                "n_change": nc, "mean_change": sc / nc if nc else None})
    stops = []
    for sid, s in info.items():
        m = merged.get(sid)
        stops.append({"stop_id": sid, "code": s.get("code"), "name": s.get("name"), "lat": s["lat"], "lon": s["lon"],
                      "n": m["n"] if m else 0, "mean_delay": m["sum"] / m["n"] if m else None,
                      "n_change": m["nc"] if m else 0, "mean_change": m["sc"] / m["nc"] if m and m["nc"] else None,
                      "routes": m["routes"] if m else []})
    return _clean({"filters": {**_filters(p), "truncated": truncated}, "minutes": p["minutes"], "routes": routes,
                   "unavailable": unavailable,
                   "n": sum(s["n"] for s in stops), "stops": stops})


# ---- map shapes ----

SHAPE_TTL, SHAPE_TIMEOUT, SHAPE_MIN_GAP_M = 3600, 5.0, 4.0


def _pt(p):
    """[lat, lon] -> [lon, lat] rounded to 5 decimals (~1 m), or None for anything malformed."""
    if isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p):
        return [round(p[1], 5), round(p[0], 5)]
    return None


def _thin(points) -> list[list[float]]:
    """Shape points as [[lon, lat]], dropping points within SHAPE_MIN_GAP_M of the last kept one; the final
    point is always kept. Malformed points are skipped."""
    pts = [q for q in map(_pt, points) if q is not None]
    out: list[list[float]] = []
    for q in pts:
        if not out or math.hypot((q[0] - out[-1][0]) * 66_000, (q[1] - out[-1][1]) * 111_000) >= SHAPE_MIN_GAP_M:
            out.append(q)
    if pts and out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


async def _shape(request, r, direction):
    """Thinned [[lon, lat]] for a route direction, cached; None when the server has no shape for it."""
    cache = getattr(request.app.state, "shape_cache", None)
    if cache is None:
        cache = request.app.state.shape_cache = _TTLCache(ttl=SHAPE_TTL, maxsize=512)
    hit = cache.get((r, direction))
    if hit is not None:
        return hit
    try:
        raw = await asyncio.wait_for(request.app.state.client.route_shape(r, direction), SHAPE_TIMEOUT)
    except (RouteNotFound, ServerUnavailable, KeyError, TypeError, AttributeError, ValueError, asyncio.TimeoutError):
        return None
    pts = _thin(raw)
    if len(pts) < 2:
        return None
    cache.put((r, direction), pts)
    return pts


@router.get("/map/shapes")
async def map_shapes(request: Request):
    """{shapes: {route: {direction: [[lon, lat], ...]}}} for tracked routes: the real road geometry from the
    server, thinned and cached for an hour. A direction the server has no shape for is simply absent (the page
    then falls back to a line through that direction's stops)."""
    out: dict[str, dict] = {}
    tracked = (await _tracked(request))["routes"]
    orders = await _bounded_map(request, tracked, lambda r: _order(request, r))
    pairs = []
    for r, order in zip(tracked, orders):
        if isinstance(order, BaseException):
            raise order
        if order[0] is not None:
            out[r] = {}
            pairs += [(r, d) for d in order[0]]
    for (r, d), pts in zip(pairs, await _bounded_map(request, pairs, lambda rd: _shape(request, *rd))):
        if isinstance(pts, BaseException):
            raise pts
        if pts:
            out[r][d] = pts
    return {"shapes": out}


# ---- all routes ----

async def _each_tracked(request, p, worker, delay=True):
    """Run worker(request, route, p) (sync, in a thread under _heavy) for every tracked route, each with its own
    MAX_ROWS cap. Returns ([(route, result)...], routes whose scan was truncated)."""
    routes = sorted(await run_in_threadpool(request.app.state.poller.tracked))
    out, truncated = [], []
    for r in routes:
        res, trunc = await _heavy(request, p, "route = ?", [r],
                                  lambda p2, r=r: (worker(request, r, p2), p2["truncated"]), delay)
        out.append((r, res))
        if trunc:
            truncated.append(r)
    return out, truncated


_ROUTE_FIELDS = ("n", "low_n", "mean", "median", "on_time_pct", "reliability", "p_low", "p_high")


def _route_stats_db(request, r, p):
    delays = array("q")
    _scan_arrivals(request, p, "route = ?", [r], "delay", lambda rows: delays.extend(d for d, _ in rows))
    s = _summ(delays, p)
    return {"route": r, **{k: s[k] for k in _ROUTE_FIELDS}}


@router.get("/all/routes")
async def all_routes(request: Request, p: P):
    """Per-route delay stats for every tracked route: route, n, low_n, mean, median, on_time_pct, reliability,
    p_low, p_high. `filters.truncated_routes` lists routes whose scan hit MAX_ROWS."""
    res, trunc = await _each_tracked(request, p, _route_stats_db)
    return _clean({"filters": {**_filters(p), "truncated_routes": trunc}, "routes": [x for _, x in res]})


@router.get("/all/heatmap")
async def all_heatmap(request: Request, p: P):
    """Heatmap cells pooled by arrival across every tracked route: [{weekday, hour, n, low_n, mean_delay}]."""
    res, trunc = await _each_tracked(request, p, _heatmap_db)
    cnt, tot = defaultdict(int), defaultdict(float)
    for _, h in res:
        for c in h["cells"]:
            k = (c["weekday"], c["hour"])
            cnt[k] += c["n"]
            tot[k] += c["mean_delay"] * c["n"]
    cells = [{"weekday": wd, "hour": hr, "n": cnt[(wd, hr)], "low_n": cnt[(wd, hr)] < LOW_N,
              "mean_delay": tot[(wd, hr)] / cnt[(wd, hr)]} for wd, hr in sorted(cnt)]
    return _clean({"filters": {**_filters(p), "truncated_routes": trunc}, "n": sum(cnt.values()), "cells": cells})


FC_CLAMP = 3600  # forecast errors are histogrammed per second within +/- this; sums stay exact


def _forecast_parts_db(request, r, p):
    """{horizon: (n, sum, abs_sum, counts)}; counts has length 2*FC_CLAMP+1, indexed by clamp(error)+FC_CLAMP."""
    cond, cargs = _cond(p, "a.")
    marks = ",".join("?" * len(HORIZONS))
    size = 2 * FC_CLAMP + 1
    acc = {h: [0, 0, 0, np.zeros(size, dtype=np.int64)] for h in HORIZONS}

    def fn(rows):
        by = defaultdict(list)
        for h, e, _ in _tz(rows, 2, p):
            by[h].append(e)
        for h, es in by.items():
            a = np.asarray(es, dtype=np.int64)
            s = acc[h]
            s[0] += len(a)
            s[1] += int(a.sum())
            s[2] += int(np.abs(a).sum())
            s[3] += np.bincount(np.clip(a, -FC_CLAMP, FC_CLAMP) + FC_CLAMP, minlength=size)

    _scan(request, "select f.horizon_min, f.error_s, a.scheduled from arrivals a "
          "join forecast f on f.arrival_id = a.id "
          f"where a.route = ? and {cond} and f.error_s is not null "
          f"and f.horizon_min in ({marks})", [r] + cargs + list(HORIZONS), fn)
    return {h: (s[0], s[1], s[2], s[3]) for h, s in acc.items()}


def _hist_percentile(counts, n, q):
    """Percentile q (0..1) from a histogram of n values, linear interpolation like np.percentile."""
    rank = q * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    cum = np.cumsum(counts)
    v_lo = int(np.searchsorted(cum, lo + 1)) - FC_CLAMP
    v_hi = int(np.searchsorted(cum, hi + 1)) - FC_CLAMP
    return float(v_lo + (v_hi - v_lo) * (rank - lo))


@router.get("/all/forecast")
async def all_forecast(request: Request, p: P):
    """Forecast error pooled by arrival across every tracked route; same horizon rows as the per-route endpoint.
    Percentiles are exact within +/-FC_CLAMP seconds (errors beyond are clamped); mean and mae are exact."""
    res, trunc = await _each_tracked(request, p, _forecast_parts_db, delay=False)
    hs = []
    for h in HORIZONS:
        n, tot, abs_tot = 0, 0, 0
        counts = np.zeros(2 * FC_CLAMP + 1, dtype=np.int64)
        for _, parts in res:
            pn, ps, pa, pc = parts[h]
            n += pn
            tot += ps
            abs_tot += pa
            counts += pc
        v = dict.fromkeys(("mean_error", "mae", "p_low", "p_high"))
        if n:
            v = {"mean_error": tot / n, "mae": abs_tot / n,
                 "p_low": _hist_percentile(counts, n, 0.05), "p_high": _hist_percentile(counts, n, 0.95)}
        hs.append({"horizon_min": h, "n": n, **v, "low_n": n < LOW_N})
    return _clean({"filters": {**_forecast_filters(p), "truncated_routes": trunc}, "horizons": hs})
