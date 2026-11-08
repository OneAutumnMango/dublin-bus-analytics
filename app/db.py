"""SQLite storage. One connection per owner: the tracker/poller owns one connection used only
from the event-loop thread; API handlers and backup open their own connection per call
(cheap under WAL) and close it. Connections are thread-bound (check_same_thread default)."""
import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

META_SCHEMA = """
create table if not exists routes_tracked(route text primary key, added int);
create table if not exists arrivals(
  id integer primary key, trip_id text, stop_id text, seq int, service_date text,
  scheduled int, actual int, delay int, confidence int, realtime int, route text,
  unique(trip_id, stop_id, seq, service_date));
create table if not exists forecast(
  arrival_id int, horizon_min int, predicted int, error_s int,
  primary key(arrival_id, horizon_min));
create index if not exists ix_arr_route_sched on arrivals(route, scheduled);
create index if not exists ix_arr_stop_sched on arrivals(stop_id, scheduled);
create table if not exists gaps(start int, end int);
create table if not exists meta_kv(k text primary key, v text);
"""

SAMPLES_SCHEMA = """
create table if not exists sample(
  ts int, trip_id text, stop_id text, seq int, due_min int, expected int, realtime int,
  primary key(trip_id, stop_id, seq, ts)) without rowid;
"""


def _connect(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    # busy_timeout 5s rides out writer contention; synchronous=NORMAL is safe under WAL (may lose last commit on power loss only)
    c.execute("pragma busy_timeout=5000")
    c.execute("pragma journal_mode=wal")
    c.execute("pragma synchronous=normal")
    c.executescript(schema)
    return c


def open_meta(data_dir: Path) -> sqlite3.Connection:
    return _connect(Path(data_dir) / "meta.db", META_SCHEMA)


def samples_path(data_dir: Path, ts: int) -> Path:
    return Path(data_dir) / f"samples-{datetime.fromtimestamp(ts, timezone.utc):%Y-%m}.db"


def open_samples(data_dir: Path, ts: int) -> sqlite3.Connection:
    p = samples_path(data_dir, ts)
    if p.with_name(p.name + ".gz").exists():  # refuse before connecting: no check-then-create race
        raise RuntimeError(f"{p.name} is closed (gzipped) and immutable")
    return _connect(p, SAMPLES_SCHEMA)


class _TempConnection(sqlite3.Connection):
    """Read-only connection over a decompressed temp copy; the copy is removed on close/GC."""
    _tmp: str | None = None

    def close(self):
        try:
            super().close()
        finally:
            self._cleanup()

    def _cleanup(self):
        tmp, self._tmp = self._tmp, None
        if tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def __del__(self):
        self._cleanup()


def open_samples_readonly(data_dir: Path, ts: int) -> sqlite3.Connection:
    """Read-only access to a month, plain or gzipped. Never creates the month; FileNotFoundError if absent."""
    p = samples_path(data_dir, ts)
    gz = p.with_name(p.name + ".gz")
    if not gz.exists():  # a gz, when present, is the authority for a closed month
        if p.exists():
            return sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
        raise FileNotFoundError(str(p))
    fd, tmp = tempfile.mkstemp(prefix="samples-ro-", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as out, gzip.open(gz, "rb") as src:
            shutil.copyfileobj(src, out)
        c = sqlite3.connect(f"{Path(tmp).resolve().as_uri()}?mode=ro&immutable=1", uri=True, factory=_TempConnection)
    except BaseException:
        os.unlink(tmp)
        raise
    c._tmp = tmp
    return c
