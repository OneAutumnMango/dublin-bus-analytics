from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.backup import list_backups, snapshot

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
def download_backup(request: Request):
    tars = list_backups(_dir(request))
    if not tars:
        raise HTTPException(404, "no backups")
    return FileResponse(tars[-1], media_type="application/x-tar", filename=tars[-1].name)
