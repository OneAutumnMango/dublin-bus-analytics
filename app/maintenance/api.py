from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.backup import backup as _backup
from app.collect.poller import ROUTE_RE
from app.maintenance import day as _day

router = APIRouter()


def _args(date: str, route: str | None):
    if route is not None and not ROUTE_RE.fullmatch(route):
        raise HTTPException(422, "invalid route")
    try:
        return _day.parse_day(date), route or None
    except _day.DayError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/api/data/day")
def preview_day(request: Request, date: str = Query(...), route: str | None = None):
    d, route = _args(date, route)
    return _day.preview(request.app.state.settings.data_dir, d, route)


@router.post("/api/data/day/remove")
def remove_day(request: Request, date: str = Query(...), confirm: str = Query(...), route: str | None = None):
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin requests are not allowed")
    d, route = _args(date, route)
    if confirm != date:
        raise HTTPException(422, "confirm must equal date")
    # one remove/backup/compaction at a time; never wait for it
    if not _backup._lock.acquire(blocking=False):
        return JSONResponse({"detail": "A backup or another removal is running; try again shortly."}, status_code=409)
    try:
        return _day.remove(request.app.state.settings.data_dir, d, route)
    except _day.DayBusy as e:
        return JSONResponse({"detail": f"{e.part} stayed locked; removal stopped part-way. Run it again to finish.",
                             "done": e.done}, status_code=409)
    finally:
        _backup._lock.release()
