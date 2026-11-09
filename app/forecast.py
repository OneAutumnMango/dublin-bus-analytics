"""Forecast extraction: what the feed predicted for an arrival at fixed horizons before it happened.

Limits: a gzipped (closed, archived) previous month yields None from open_previous_month, so
forecasts for the first ~30 min of a month are skipped in that case. extract_forecasts can raise
sqlite3.DatabaseError on a corrupt samples file; the caller (tracker) must handle it."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.db import samples_path

HORIZONS = (30, 20, 10, 5, 2)

_LATEST = "select realtime from sample where trip_id=? and stop_id=? and seq=? and ts<=? order by ts desc limit 1"
_Q = ("select expected from sample where trip_id=? and stop_id=? and seq=? and realtime=1 "
      "and expected is not null and ts<=? order by ts desc limit 1")


def extract_forecasts(samples_conns: Sequence[sqlite3.Connection], trip_id: str, stop_id: str,
                      seq: int, actual: int) -> list[tuple[int, int, int]]:
    """(horizon_min, predicted_epoch, error_s) per horizon; conns searched newest month first.

    A forecast is what the system displayed then: the latest sample at or before the horizon decides.
    If it is scheduled-only (realtime=0) the horizon is omitted (no fallback to older realtime samples);
    a realtime row with NULL expected falls back to the next-latest non-NULL realtime sample."""
    out = []
    for h in HORIZONS:
        for c in samples_conns:
            args = (trip_id, stop_id, seq, actual - h * 60)
            latest = c.execute(_LATEST, args).fetchone()
            if latest is None:
                continue
            if latest[0] != 1:
                break
            row = c.execute(_Q, args).fetchone()
            if row:
                out.append((h, row[0], actual - row[0]))
                break
    return out


def open_previous_month(data_dir: Path, ts: int) -> sqlite3.Connection | None:
    """Read-only connection to the previous UTC month's samples file, or None; never creates files.

    Normally opened with immutable=1 so SQLite never creates -wal/-shm files; that mode ignores any WAL
    content. If a non-empty <file>-wal exists it is opened with plain mode=ro instead so the WAL is
    honoured; a read-only connection to a WAL database may then create a -shm sidecar (acceptable)."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    first = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    path = samples_path(data_dir, int(first.timestamp()) - 1)
    if not path.exists():
        return None
    wal = path.with_name(path.name + "-wal")
    # an uncheckpointed WAL (crash, or writer still holding the file) must be honoured: no immutable
    mode = "mode=ro" if wal.exists() and wal.stat().st_size > 0 else "mode=ro&immutable=1"
    return sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
