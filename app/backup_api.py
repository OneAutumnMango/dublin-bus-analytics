from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.backup import list_backups, newest_backup, snapshot

router = APIRouter()


def _dir(request: Request):
    s = request.app.state.settings
    return getattr(s, "backup_dir", None) or s.data_dir / "backups"


@router.post("/api/backup")
def create_backup(request: Request):
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
