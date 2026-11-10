import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.backup.backup import _stamp, list_backups, newest_backup, snapshot

router = APIRouter()
MIN_BACKUP_INTERVAL_S = 60


def _dir(request: Request):
    s = request.app.state.settings
    return getattr(s, "backup_dir", None) or s.data_dir / "backups"


@router.post("/api/backup")
def create_backup(request: Request):
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin backup requests are not allowed")
    now = getattr(request.app.state, "backup_clock", time.time)()
    # newest backup by its name stamp; future-dated ones do not count
    ages = [now - _stamp(t) for t in list_backups(_dir(request))]
    ages = [a for a in ages if a >= 0]
    if ages and min(ages) < MIN_BACKUP_INTERVAL_S:
        wait = int(MIN_BACKUP_INTERVAL_S - min(ages)) + 1
        return JSONResponse({"detail": f"A backup was made less than {MIN_BACKUP_INTERVAL_S} s ago; try again in {wait} s."},
                            status_code=429, headers={"Retry-After": str(wait)})
    s = request.app.state.settings
    p = snapshot(s.data_dir, _dir(request), getattr(s, "backup_keep", 7))
    return {"file": p.name, "size": p.stat().st_size}


@router.get("/api/backup/download")
def download_backup(request: Request, file: str | None = None):
    # `file` must exactly equal a listed backup name; it is never used as a path
    if file is None:
        p = newest_backup(_dir(request))
    else:
        p = next((t for t in list_backups(_dir(request)) if t.name == file), None)
    if p is None:
        raise HTTPException(404, "no such backup")
    return FileResponse(p, media_type="application/x-tar", filename=p.name)
