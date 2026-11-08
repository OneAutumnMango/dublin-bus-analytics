"""Forecast extraction: what the feed predicted for an arrival at fixed horizons before it happened."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.db import samples_path

HORIZONS = (30, 20, 10, 5, 2)

_Q = ("select expected from sample where trip_id=? and stop_id=? and seq=? and realtime=1 "
      "and ts<=? order by ts desc limit 1")


def extract_forecasts(samples_conns: Sequence[sqlite3.Connection], trip_id: str, stop_id: str,
                      seq: int, actual: int) -> list[tuple[int, int, int]]:
    """(horizon_min, predicted_epoch, error_s) per horizon; conns searched newest month first."""
    out = []
    for h in HORIZONS:
        for c in samples_conns:
            row = c.execute(_Q, (trip_id, stop_id, seq, actual - h * 60)).fetchone()
            if row:
                out.append((h, row[0], actual - row[0]))
                break
    return out


def open_previous_month(data_dir: Path, ts: int) -> sqlite3.Connection | None:
    """Read-only connection to the previous UTC month's samples file, or None; never creates files."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    first = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    path = samples_path(data_dir, int(first.timestamp()) - 1)
    if not path.exists():
        return None
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
