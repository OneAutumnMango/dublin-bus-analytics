import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.collect.poller import ROUTE_RE
from app.export.stream import gzip_chunks, iter_export

router = APIRouter()


def _guarded(chunks, gate, token):
    # finally also runs when the client disconnects (the generator is closed)
    try:
        yield from chunks
    finally:
        gate.release(token)


@router.get("/api/export.json")
def export_json(request: Request, route: str | None = None, since: int | None = None,
                until: int | None = None, gzip: bool = Query(False)):
    # validate everything before taking the gate; fullmatch so a trailing newline cannot slip past `$`
    if route is not None and not ROUTE_RE.fullmatch(route):
        raise HTTPException(422, "invalid route")
    if since is not None and until is not None and until < since:
        raise HTTPException(422, "until must not be before since")
    gate = request.app.state.export_gate
    token = gate.try_acquire()
    if token is None:
        return JSONResponse({"detail": "Another export is running; try again shortly."},
                            status_code=429, headers={"Retry-After": "30"})
    try:
        now = datetime.now(timezone.utc)
        name = f"analytics-export-{now:%Y%m%d}"
        if route:
            name += "-" + re.sub(r"[^A-Za-z0-9_-]", "_", route)
        chunks = iter_export(request.app.state.settings.data_dir, route=route, since=since,
                             until=until, exported_at=int(now.timestamp()))
        if gzip:
            body, media, name = gzip_chunks(chunks), "application/gzip", name + ".json.gz"
        else:
            body, media, name = chunks, "application/json", name + ".json"
        return StreamingResponse(
            _guarded(body, gate, token), media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
            background=BackgroundTask(gate.release, token))
    except BaseException:
        gate.release(token)
        raise
