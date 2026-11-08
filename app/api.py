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
can 404 (unknown route). low_n is true when n < 10.
"""
import inspect
import math
import re
import sqlite3
from contextlib import closing
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from fastapi.routing import APIRoute
from starlette.routing import Match

from app import stats
from app.db import open_meta
from app.serverclient import RouteNotFound, ServerUnavailable



class RawPathRoute(APIRoute):
    """Match on the still-encoded path so an encoded slash (route "15/A" as 15%2FA) stays inside
    one path parameter; Starlette would otherwise decode it before routing and 404."""

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
TRACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,31}$")
_INT = re.compile(r"^-?\d{1,12}$")

RouteP = Annotated[str, Path(min_length=1, max_length=32)]
StopP = Annotated[str, Path(min_length=1, max_length=64)]
QStr = Annotated[str | None, Query(max_length=64)]


def _bad(msg):
    raise HTTPException(422, msg)


def _int(name, raw):
    if not _INT.match(raw):
        _bad(f"{name} must be an integer")
    return int(raw)


def _float(name, raw):
    try:
        v = float(raw)
    except ValueError:
        _bad(f"{name} must be a number")
    if not math.isfinite(v):
        _bad(f"{name} must be finite")
    return v


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


def _rows(request: Request, sql: str, args: tuple) -> list[dict]:
    with closing(open_meta(request.app.state.settings.data_dir)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args).fetchall()]


_COLS = "trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route"


def _arrivals(request, p, where: str, args: tuple) -> list[dict]:
    # shortcut: time filters applied in Python, not SQL; push into SQL if tables get huge
    rows = _rows(request, f"select {_COLS} from arrivals where {where} and delay is not null "
                 "order by scheduled, id", args)
    return stats.filter_rows(rows, min_confidence=p["min_confidence"], since=p["since"],
                             until=p["until"], weekdays=p["weekdays"], hours=p["hours"])


def _delays(rows):
    return [r["delay"] for r in rows]


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


# ---- routes: proxy + tracking ----

@router.get("/routes")
async def search_routes(request: Request, q: QStr = ""):
    return await _proxy(request.app.state.client.search_routes(q or ""))


@router.get("/routes/tracked")
async def tracked(request: Request):
    return {"routes": sorted(request.app.state.poller.tracked())}


async def _call(x):
    return await x if inspect.isawaitable(x) else x


def _track_name(route):
    if not TRACK_RE.match(route):
        _bad("invalid route name")
    return route


@router.put("/routes/tracked/{route}")
async def track(request: Request, route: RouteP):
    await _call(request.app.state.poller.track(_track_name(route)))
    return {"routes": sorted(request.app.state.poller.tracked())}


@router.delete("/routes/tracked/{route}")
async def untrack(request: Request, route: RouteP):
    await _call(request.app.state.poller.untrack(_track_name(route)))
    return {"routes": sorted(request.app.state.poller.tracked())}


@router.get("/routes/{r}/stops")
async def route_stops(request: Request, r: RouteP):
    return {"route": r, "directions": await _proxy(request.app.state.client.route_stops(r))}


# ---- stats ----

@router.get("/routes/{r}/summary")
def route_summary(request: Request, r: RouteP, p: P):
    """Per-stop delay stats in route order (median seq of the stop's rows, ties by stop_id).
    Each stop: stop_id, seq, n, low_n, mean/min/max/std/median/ci_low/ci_high/p_low/p_high,
    on_time_pct, reliability."""
    rows = _arrivals(request, p, "route = ?", (r,))
    by_stop: dict[str, list[dict]] = {}
    for x in rows:
        by_stop.setdefault(x["stop_id"], []).append(x)
    out = []
    for sid, rs in by_stop.items():
        seqs = sorted(x["seq"] for x in rs if x["seq"] is not None)
        seq = seqs[len(seqs) // 2] if seqs else None
        out.append({"stop_id": sid, "seq": seq, **_summ(_delays(rs), p)})
    out.sort(key=lambda s: (s["seq"] is None, s["seq"], s["stop_id"]))
    return {"route": r, "filters": _filters(p), "n": len(rows), "low_n": len(rows) < LOW_N,
            "stops": out}


def _histogram(delays):
    counts = [0] * (2 + (HIST_HI - HIST_LO) // HIST_W)
    for d in delays:
        counts[min(max((d - HIST_LO) // HIST_W + 1, 0), len(counts) - 1)] += 1
    edges = list(range(HIST_LO, HIST_HI + 1, HIST_W))
    his = edges + [None]
    los = [None] + edges
    return [{"lo": a, "hi": b, "count": c} for a, b, c in zip(los, his, counts)]


@router.get("/stops/{s}/stats")
def stop_stats(request: Request, s: StopP, p: P, route: QStr = None):
    """Stop stats (optionally for one route): n, low_n, delay{summary; ci_low..ci_high is the CI of
    the actual-minus-scheduled delay}, on_time_pct, reliability, histogram (60 s bins from -600 to
    1800 plus open-ended outer bins) and the 20 most recent arrivals."""
    where, args = ("stop_id = ?", (s,)) if route is None else ("stop_id = ? and route = ?", (s, route))
    rows = _arrivals(request, p, where, args)
    d = _summ(_delays(rows), p)
    recent = [{k: x[k] for k in ("trip_id", "route", "seq", "service_date", "scheduled", "actual",
                                "delay", "confidence")} for x in reversed(rows[-RECENT:])]
    return {"stop_id": s, "route": route, "filters": _filters(p), "n": d["n"], "low_n": d["low_n"],
            "delay": {k: d[k] for k in ("mean", "min", "max", "std", "median", "ci_low", "ci_high",
                                       "p_low", "p_high")},
            "on_time_pct": d["on_time_pct"], "reliability": d["reliability"],
            "histogram": _histogram(_delays(rows)), "recent": recent}


@router.get("/routes/{r}/heatmap")
def route_heatmap(request: Request, r: RouteP, p: P):
    """cells: [{weekday 0=Mon, hour, n, low_n, mean_delay}] for Dublin-local scheduled time."""
    rows = _arrivals(request, p, "route = ?", (r,))
    cells = [{**c, "low_n": c["n"] < LOW_N} for c in stats.heatmap_to_json(stats.heatmap(rows))]
    return {"route": r, "filters": _filters(p), "n": len(rows), "cells": cells}


@router.get("/routes/{r}/accumulation")
async def route_accumulation(request: Request, r: RouteP, p: P, direction: QStr = None):
    """directions: [{direction, n_trips, stops:[{stop_id, seq, mean_delay, segment_delta, n, n_delta,
    low_n, low_n_delta}]}]. Direction is inferred per trip from the server's stop lists
    (`direction_source: "server"`); if the server is unreachable all trips fall in one group
    named "all" (`direction_source: "none"`), unless `direction` was requested (then 502)."""
    dirs = None
    try:
        dirs = await request.app.state.client.route_stops(r)
    except (RouteNotFound, ServerUnavailable) as e:
        if direction is not None:
            raise HTTPException(404 if isinstance(e, RouteNotFound) else 502,
                                f"cannot resolve direction: {e}") from None
    if direction is not None and direction not in dirs:
        raise HTTPException(404, "unknown direction")
    sets = {n: {x.get("id", x.get("stopId")) for x in stops} for n, stops in (dirs or {}).items()}
    rows = _arrivals(request, p, "route = ?", (r,))
    trips: dict[tuple, list[dict]] = {}
    for x in rows:
        trips.setdefault((x["trip_id"], x["service_date"]), []).append(x)
    groups: dict[str, list[list[dict]]] = {}
    for t in trips.values():
        ids = [x["stop_id"] for x in t]
        name = "all" if dirs is None else (
            max(sorted(sets), key=lambda n: sum(i in sets[n] for i in ids))
            if sets and any(i in sv for i in ids for sv in sets.values()) else "unknown")
        groups.setdefault(name, []).append(t)
    out = []
    for name in sorted(groups):
        if direction is not None and name != direction:
            continue
        acc = [{**a, "low_n": a["n"] < LOW_N, "low_n_delta": a["n_delta"] < LOW_N}
               for a in stats.accumulation(groups[name])]
        out.append({"direction": name, "n_trips": len(groups[name]), "stops": acc})
    return {"route": r, "filters": _filters(p), "direction_source": "none" if dirs is None else "server",
            "directions": out}


@router.get("/routes/{r}/forecast")
def route_forecast(request: Request, r: RouteP, p: P):
    """horizons: always [30,20,10,5,2] in that order: {horizon_min, n, low_n, mean_error, mae,
    p_low, p_high} (null stats when n=0). error = actual minus predicted seconds."""
    rows = _rows(request, "select f.horizon_min, f.error_s, a.confidence, a.realtime, a.scheduled "
                 "from forecast f join arrivals a on a.id = f.arrival_id "
                 "where a.route = ? and f.error_s is not null", (r,))
    rows = stats.filter_rows(rows, min_confidence=p["min_confidence"], since=p["since"],
                             until=p["until"], weekdays=p["weekdays"], hours=p["hours"])
    acc = stats.forecast_accuracy(rows)
    empty = dict.fromkeys(("mean_error", "mae", "p_low", "p_high"))
    hs = [{"horizon_min": h, **{"n": 0, **empty, **acc.get(h, {})}} for h in HORIZONS]
    for h in hs:
        h["low_n"] = h["n"] < LOW_N
    return {"route": r, "filters": _filters(p), "horizons": hs}
