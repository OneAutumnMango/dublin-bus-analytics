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
import math
import re
import sqlite3
import threading
import time
from array import array
from bisect import bisect_right
from collections import defaultdict, deque
from contextlib import closing
from typing import Annotated
from urllib.parse import unquote

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from app import stats
from app.db import open_meta
from app.poller import ROUTE_RE
from app.serverclient import RouteNotFound, ServerUnavailable



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
            min_confidence: QStr = "1") -> dict:
    p = {"ci": _float("ci", ci), "pct_lo": _float("pct_lo", pct_lo),
         "pct_hi": _float("pct_hi", pct_hi), "window_lo": _int("window_lo", window_lo),
         "window_hi": _int("window_hi", window_hi),
         "since": None if since is None else _int("since", since),
         "until": None if until is None else _int("until", until),
         "weekdays": None if weekdays is None else _int_list("weekdays", weekdays, 0, 6),
         "hours": None if hours is None else _int_list("hours", hours, 0, 23),
         "min_confidence": _int("min_confidence", min_confidence), "truncated": False}
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
    return {**p, "realtime_only": True}


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


MAX_HEAVY_SCANS = 2  # concurrent heavy scans; each can hold ~100+ MB, the threadpool is far larger
HEAVY_ROWS = 50_000  # a request matching more arrivals than this takes a heavy slot
_HEAVY = threading.BoundedSemaphore(MAX_HEAVY_SCANS)
_held = threading.local()


def _heavy_scope(fn):
    """Handler body wrapper: releases the heavy slot `_cap` may have taken on this thread, on any exit.
    Lock order is always slot first, then _DB_LOCK per chunk, so waiting for a slot never holds _DB_LOCK."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        finally:
            if getattr(_held, "slot", False):
                _held.slot = False
                _HEAVY.release()
    return wrapper


def _cap(request, p, where: str, args: list, delay=True) -> dict:
    """Params with `since` raised (and truncated=True) when more than MAX_ROWS rows match. When more than
    HEAVY_ROWS match it also blocks for one of MAX_HEAVY_SCANS heavy slots (released by _heavy_scope).
    The probes ignore NULL-delay rows (delay=True) exactly as the scan does."""
    cond, cargs = _cond(p)
    probe = (f"select scheduled from arrivals where {where} and {cond}"
             f"{' and delay is not null' if delay else ''} order by scheduled desc limit 2 offset ?")
    rows = _fetch(request, probe, args + cargs + [MAX_ROWS - 1])
    if len(rows) == 2:
        p = {**p, "since": rows[0][0], "truncated": True}
    if len(rows) == 2 or _fetch(request, probe, args + cargs + [HEAVY_ROWS]):
        _HEAVY.acquire()
        _held.slot = True
    return p


def _tz(rows, i, p):
    """Weekday/hour (Dublin-local) filter in Python on tuples; i = index of `scheduled`."""
    if p["weekdays"] is None and p["hours"] is None:
        return rows
    kept = stats.filter_rows([{"scheduled": r[i], "_r": r} for r in rows], min_confidence=0,
                             realtime_only=False, weekdays=p["weekdays"], hours=p["hours"])
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

@_heavy_scope
def _summary_db(request, r, p, lists):
    p = _cap(request, p, "route = ?", [r])
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
    return await run_in_threadpool(_summary_db, request, r, p, lists)


def _histogram(delays):
    counts = [0] * (2 + (HIST_HI - HIST_LO) // HIST_W)
    for d in delays:
        counts[min(max((d - HIST_LO) // HIST_W + 1, 0), len(counts) - 1)] += 1
    edges = list(range(HIST_LO, HIST_HI + 1, HIST_W))
    return [{"lo": a, "hi": b, "count": c} for a, b, c in zip([None] + edges, edges + [None], counts)]


@router.get("/stops/{s}/stats")
@_heavy_scope
def stop_stats(request: Request, s: StopP, p: P, route: QStr = None):
    """Stop stats (optionally for one route): n, low_n, delay{summary; ci_low..ci_high is the CI of
    the actual-minus-scheduled delay}, on_time_pct, reliability, histogram (60 s bins from -600 to
    1800 plus open-ended outer bins) and the 20 most recent arrivals."""
    where, args = ("stop_id = ?", [s]) if route is None else ("stop_id = ? and route = ?", [s, route])
    p = _cap(request, p, where, args)
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
@_heavy_scope
def route_heatmap(request: Request, r: RouteP, p: P):
    """cells: [{weekday 0=Mon, hour, n, low_n, mean_delay}] for Dublin-local scheduled time."""
    p = _cap(request, p, "route = ?", [r])
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


@_heavy_scope
def _accumulation_db(request, r, p, lists, direction):
    p = _cap(request, p, "route = ?", [r])
    stop_ix: dict[str, int] = {}
    trip_ix: dict[tuple, int] = {}
    cols = [array("q") for _ in range(4)]  # trip no, stop no, delay, scheduled (flat, no per-row objects)

    def fn(rows):
        for t, sd, sid, d, sch in rows:  # rows arrive ordered by scheduled
            cols[0].append(trip_ix.setdefault((t, sd), len(trip_ix)))
            cols[1].append(stop_ix.setdefault(sid, len(stop_ix)))
            cols[2].append(d)
            cols[3].append(sch)

    _scan_arrivals(request, p, "route = ?", [r], "trip_id, service_date, stop_id, delay", fn)
    del trip_ix
    names = list(stop_ix)
    pos = {name: defaultdict(list) for name in lists}
    for name, ids in lists.items():
        for i, sid in enumerate(ids):
            if sid in stop_ix:
                pos[name][stop_ix[sid]].append(i)
    trip_no, stop_no, dly, sch = (np.frombuffer(c, dtype=np.int64) for c in cols) if cols[0] else ([],) * 4
    order = np.argsort(trip_no, kind="stable")  # stable: keeps scheduled order inside each trip
    cuts = [0, *(np.flatnonzero(np.diff(trip_no[order])) + 1).tolist(), len(order)] if len(order) else [0]
    # per group: [trips, {position: [stop no, n, sum, n_delta, delta sum]}]
    agg = {name: [0, {}] for name in lists} | {None: [0, {}]}  # None (not a str) = fallback group
    for lo, hi in zip(cuts, cuts[1:]):
        ix = order[lo:hi]
        name, matched = _assign(stop_no[ix].tolist(), dly[ix].tolist(), sch[ix].tolist(), pos)
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
        acc = [{"stop_id": names[a[0]], "position": pp, "mean_delay": a[2] / a[1],
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
    groups, p = await run_in_threadpool(_accumulation_db, request, r, p, lists, direction)
    return _clean({"route": r, "filters": _filters(p), "order_source": "server", "groups": groups})


@router.get("/routes/{r}/forecast")
@_heavy_scope
def route_forecast(request: Request, r: RouteP, p: P):
    """horizons: always [30,20,10,5,2] in that order: {horizon_min, n, low_n, mean_error, mae,
    p_low, p_high} (null stats when n=0). error = actual minus predicted seconds."""
    p = _cap(request, p, "route = ?", [r], delay=False)
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
    return _clean({"route": r, "filters": _filters(p), "horizons": hs})
