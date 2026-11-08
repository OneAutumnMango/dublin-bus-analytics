"""JSON API under /api. Handlers that touch only sqlite are plain `def` (threadpool) and open+close
their own connection per request; proxy/poller handlers are `async def` (the client is async and the
poller's connection is bound to the event-loop thread).

Statistics endpoints share these query parameters (all validated, invalid -> 422):
  ci (0,1) default 0.95; pct_lo < pct_hi in [0,1] default 0.05/0.95; window_lo <= window_hi seconds,
  the on-time window, default -60/300; since/until epoch ints (inclusive, on `scheduled`);
  weekdays (comma list 0=Mon..6) and hours (comma list 0..23), Dublin-local;
  min_confidence (0 or 1, DEFAULT 1): stats only use rows whose actual was confirmed by a due==0
  sample. Pass min_confidence=0 to include unconfirmed (confidence 0) rows. Non-realtime
  (scheduled-only) rows are ALWAYS excluded. Every response echoes the applied filters in `filters`.
A route/stop with no stored data gives 200 with n=0 and null stats; only the server-proxy endpoints
can 404 (unknown route). `arrivals.seq` is a per-visit key (the scheduled epoch), never a stop position:
stop order comes only from the server's route_stops (`order_source` 'server' | 'none'). low_n is true when n < 10.
"""
import re
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from contextlib import closing
from typing import Annotated
from urllib.parse import unquote

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
         "min_confidence": _int("min_confidence", min_confidence)}
    if not 0 < p["ci"] < 1:
        _bad("ci must be strictly between 0 and 1")
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


def _filters(p):
    return {**p, "realtime_only": True}


def _fetch(request: Request, sql: str, args: list) -> list[tuple]:
    with closing(open_meta(request.app.state.settings.data_dir)) as c:
        return c.execute(sql, args).fetchall()


def _cond(p, a=""):
    """SQL fragment + params for the filters SQLite can apply (realtime, confidence, since/until)."""
    sql, args = f"{a}realtime = 1 and {a}confidence >= ? and {a}scheduled is not null", [p["min_confidence"]]
    for k, op in (("since", ">="), ("until", "<=")):
        if p[k] is not None:
            sql += f" and {a}scheduled {op} ?"
            args.append(p[k])
    return sql, args


def _tz(rows, i, p):
    """Weekday/hour (Dublin-local) filter in Python on tuples; i = index of `scheduled`."""
    if p["weekdays"] is None and p["hours"] is None:
        return rows
    kept = stats.filter_rows([{"scheduled": r[i], "_r": r} for r in rows], min_confidence=0,
                             realtime_only=False, weekdays=p["weekdays"], hours=p["hours"])
    return [d["_r"] for d in kept]


def _arrivals(request, p, where: str, args: list, cols: str) -> list[tuple]:
    """Filtered arrivals as tuples of `cols` plus `scheduled` appended as the LAST column,
    ordered by scheduled. realtime/confidence/since/until filter in SQL (index ix_arr_route_sched)."""
    cond, cargs = _cond(p)
    rows = _fetch(request, f"select {cols}, scheduled from arrivals where {where} and {cond} "
                  "and delay is not null order by scheduled", args + cargs)
    return _tz(rows, -1, p)


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


async def _directions(request, r) -> dict[str, list[dict]]:
    """Server's {direction: [stop dicts in order]}, validated. Raises _NoOrder (404 unknown
    route, 502 unavailable or malformed shape)."""
    try:
        raw = await request.app.state.client.route_stops(r)
    except RouteNotFound:
        raise _NoOrder(404, "route not found on server") from None
    except (ServerUnavailable, KeyError, TypeError, AttributeError, ValueError) as e:
        raise _NoOrder(502, f"server unavailable or malformed: {e}") from None
    if not (isinstance(raw, dict) and all(
            isinstance(k, str) and isinstance(v, list)
            and all(isinstance(x, dict) and isinstance(_sid(x), str) for x in v)
            for k, v in raw.items())):
        raise _NoOrder(502, "malformed stops response from server")
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

def _summary_db(request, r, p, lists):
    rows = _arrivals(request, p, "route = ?", [r], "stop_id, delay")
    by_stop: dict[str, list[int]] = defaultdict(list)
    for sid, d, _ in rows:
        by_stop[sid].append(d)
    pos = {}  # stop -> (direction index, position, direction); first occurrence wins
    for di, (name, ids) in enumerate((lists or {}).items()):
        for i, sid in enumerate(ids):
            pos.setdefault(sid, (di, i, name))
    out = [{"stop_id": sid, "position": pos[sid][1] if sid in pos else None,
            "direction": pos[sid][2] if sid in pos else None, **_summ(ds, p)}
           for sid, ds in by_stop.items()]
    out.sort(key=lambda s: (s["stop_id"] not in pos, pos.get(s["stop_id"], (0, 0))[:2], s["stop_id"]))
    return {"route": r, "filters": _filters(p), "order_source": "none" if lists is None else "server",
            "n": len(rows), "low_n": len(rows) < LOW_N, "stops": out}


@router.get("/routes/{r}/summary")
async def route_summary(request: Request, r: RouteP, p: P):
    """Per-stop delay stats in ROUTE order from the server's route_stops (first direction listing
    the stop, by its index there); stops the server does not list come last by stop_id with
    position null; server down or malformed -> stop_id order and order_source "none".
    Each stop: stop_id, position, direction, n, low_n, mean/min/max/std/median/ci_low/ci_high/
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
def stop_stats(request: Request, s: StopP, p: P, route: QStr = None):
    """Stop stats (optionally for one route): n, low_n, delay{summary; ci_low..ci_high is the CI of
    the actual-minus-scheduled delay}, on_time_pct, reliability, histogram (60 s bins from -600 to
    1800 plus open-ended outer bins) and the 20 most recent arrivals."""
    where, args = ("stop_id = ?", [s]) if route is None else ("stop_id = ? and route = ?", [s, route])
    rows = _arrivals(request, p, where, args,
                     "trip_id, route, seq, service_date, actual, delay, confidence")
    delays = [r[5] for r in rows]
    d = _summ(delays, p)
    keys = ("trip_id", "route", "seq", "service_date", "actual", "delay", "confidence", "scheduled")
    recent = [dict(zip(keys, r)) for r in reversed(rows[-RECENT:])]
    return {"stop_id": s, "route": route, "filters": _filters(p), "n": d["n"], "low_n": d["low_n"],
            "delay": {k: d[k] for k in ("mean", "min", "max", "std", "median", "ci_low", "ci_high",
                                       "p_low", "p_high")},
            "on_time_pct": d["on_time_pct"], "reliability": d["reliability"],
            "histogram": _histogram(delays), "recent": recent}


@router.get("/routes/{r}/heatmap")
def route_heatmap(request: Request, r: RouteP, p: P):
    """cells: [{weekday 0=Mon, hour, n, low_n, mean_delay}] for Dublin-local scheduled time."""
    rows = _arrivals(request, p, "route = ?", [r], "delay")
    h = stats.heatmap([{"delay": d, "scheduled": s} for d, s in rows])
    cells = [{**c, "low_n": c["n"] < LOW_N} for c in stats.heatmap_to_json(h)]
    return {"route": r, "filters": _filters(p), "n": len(rows), "cells": cells}


def _assign(trip, pos):
    """trip: [(stop_id, delay)] sorted by scheduled; pos: {direction: {stop_id: [positions]}}.
    Greedy per direction: each row takes the first occurrence of its stop after the previous
    matched position (handles loops). Best match count wins; a tie or no match -> (None, [])."""
    scored = []
    for name, occ in pos.items():
        prev, m = -1, []
        for sid, d in trip:
            lst = occ.get(sid, ())
            j = bisect_right(lst, prev)
            if j < len(lst):
                prev = lst[j]
                m.append({"stop_id": sid, "seq": prev, "delay": d})
        scored.append((len(m), name, m))
    top = max((x[0] for x in scored), default=0)
    win = [x for x in scored if x[0] == top]
    return (win[0][1], win[0][2]) if top > 0 and len(win) == 1 else (None, [])


def _accumulation_db(request, r, p, lists, direction):
    rows = _arrivals(request, p, "route = ?", [r], "trip_id, service_date, stop_id, delay")
    trips: dict[tuple, list] = defaultdict(list)
    for t, sd, sid, d, _ in rows:  # rows come ordered by scheduled
        trips[(t, sd)].append((sid, d))
    pos = {}
    for name, ids in lists.items():
        pos[name] = defaultdict(list)
        for i, sid in enumerate(ids):
            pos[name][sid].append(i)
    groups: dict[str, list] = {name: [] for name in lists} | {"unknown": []}
    for trip in trips.values():
        name, matched = _assign(trip, pos)
        groups[name or "unknown"].append(matched)
    out = []
    for name, ts in groups.items():
        if not ts or (direction is not None and name != direction):
            continue
        # unknown trips have no positions, so no per-stop numbers are fabricated for them
        acc = [] if name == "unknown" else [
            {"stop_id": a["stop_id"], "position": a["seq"], "mean_delay": a["mean_delay"],
             "segment_delta": a["segment_delta"], "n": a["n"], "n_delta": a["n_delta"],
             "low_n": a["n"] < LOW_N, "low_n_delta": a["n_delta"] < LOW_N}
            for a in stats.accumulation(ts)]
        out.append({"direction": name, "n": len(ts), "low_n": len(ts) < LOW_N,
                    "n_delta": max((a["n_delta"] for a in acc), default=0), "stops": acc})
    return out


@router.get("/routes/{r}/accumulation")
async def route_accumulation(request: Request, r: RouteP, p: P, direction: QStr = None):
    """groups: [{direction, n (trips), n_delta (max trips behind any segment delta), low_n,
    stops:[{stop_id, position, mean_delay, segment_delta, n, n_delta, low_n, low_n_delta}]}].
    A trip's direction is the server direction whose stop list its rows (by scheduled time) follow
    in increasing position, best match wins; a tie or no match -> group "unknown" (n only, stops []).
    Server unavailable/malformed -> 200 {"order_source": "none", "groups": []}, unless
    `direction` was requested (then 404 unknown route / 502). Unknown `direction` -> 404."""
    try:
        lists = {k: [_sid(x) for x in v] for k, v in (await _directions(request, r)).items()}
    except _NoOrder as e:
        if direction is not None:
            raise HTTPException(e.status, e.msg) from None
        return {"route": r, "filters": _filters(p), "order_source": "none", "groups": []}
    if direction is not None and direction not in (*lists, "unknown"):
        raise HTTPException(404, "unknown direction")
    groups = await run_in_threadpool(_accumulation_db, request, r, p, lists, direction)
    return {"route": r, "filters": _filters(p), "order_source": "server", "groups": groups}


@router.get("/routes/{r}/forecast")
def route_forecast(request: Request, r: RouteP, p: P):
    """horizons: always [30,20,10,5,2] in that order: {horizon_min, n, low_n, mean_error, mae,
    p_low, p_high} (null stats when n=0). error = actual minus predicted seconds."""
    cond, cargs = _cond(p, "a.")
    marks = ",".join("?" * len(HORIZONS))
    rows = _tz(_fetch(request, "select f.horizon_min, f.error_s, a.scheduled from arrivals a "
                      "join forecast f on f.arrival_id = a.id "
                      f"where a.route = ? and {cond} and f.error_s is not null "
                      f"and f.horizon_min in ({marks})", [r] + cargs + list(HORIZONS)), 2, p)
    acc = stats.forecast_accuracy([{"horizon_min": h, "error_s": e} for h, e, _ in rows])
    empty = dict.fromkeys(("mean_error", "mae", "p_low", "p_high"))
    hs = [{"horizon_min": h, **{"n": 0, **empty, **acc.get(h, {})}} for h in HORIZONS]
    for h in hs:
        h["low_n"] = h["n"] < LOW_N
    return {"route": r, "filters": _filters(p), "horizons": hs}
