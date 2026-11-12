import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.backup import backup as _backup
from app.collect.poller import ROUTE_RE
from app.maintenance import day as _day
from app.maintenance import jobs as _jobs

router = APIRouter()


def _args(date: str, end: str | None, route: str | None):
    if route is not None and not ROUTE_RE.fullmatch(route):
        raise HTTPException(422, "invalid route")
    try:
        return _day.parse_range(date, end or None), route or None
    except _day.DayError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/api/data/day")
def preview_day(request: Request, date: str = Query(...), end: str | None = None, route: str | None = None):
    days, route = _args(date, end, route)
    try:
        return _day.preview_range(request.app.state.settings.data_dir, days, route)
    except _day.DayBusy as e:
        return _unavailable(e.kind == "busy")
    except sqlite3.Error:
        return _unavailable(False)


def _unavailable(busy: bool):
    if busy:
        return JSONResponse({"detail": "The data is busy; try again shortly."}, status_code=409)
    return JSONResponse({"detail": "The data could not be read."}, status_code=503)


_BUSY = "A backup or another removal is running; try again shortly."


@router.post("/api/data/day/remove")
def remove_day(request: Request, date: str = Query(...), confirm: str = Query(...), end: str | None = None,
               route: str | None = None):
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin requests are not allowed")
    days, route = _args(date, end, route)
    if confirm != (f"{date}..{end}" if end else date):
        raise HTTPException(422, "confirm must equal date" if not end else "confirm must equal date..end")
    # one remove/backup/compaction at a time; never wait for it
    if not _backup._lock.acquire(blocking=False):
        return JSONResponse({"detail": _BUSY, "job_id": _jobs.running_id()}, status_code=409)
    try:
        job_id = _jobs.start(request.app.state.settings.data_dir, days, route, _backup._lock)
    except _jobs.JobBusy as e:
        _backup._lock.release()
        return JSONResponse({"detail": _BUSY, "job_id": e.job_id}, status_code=409)
    except BaseException:
        _backup._lock.release()
        raise
    return JSONResponse({"job_id": job_id}, status_code=202)


@router.get("/api/data/day/remove/{job_id}")
def remove_status(job_id: str):
    rec = _jobs.get(job_id)
    if rec is None:
        raise HTTPException(404, "unknown job")
    return rec
