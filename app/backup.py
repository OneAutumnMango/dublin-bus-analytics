"""Snapshot (consistent tar of all dbs) and monthly compaction (gzip closed months)."""
import contextlib
import gzip
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
_lock = threading.Lock()  # serialises snapshot vs compaction
_MONTH = re.compile(r"^samples-(\d{4})-(\d{2})\.db$")
_TAR = re.compile(r"^analytics-\d{8}T\d{6}Z(_\d{3})?\.tar$")


def list_backups(backup_dir: Path) -> list[Path]:
    """Backup tars, oldest first (names sort chronologically; same-second collisions get _NNN)."""
    d = Path(backup_dir)
    return sorted((p for p in d.iterdir() if _TAR.match(p.name)), key=lambda p: p.name) if d.is_dir() else []


def _copy_db(src: Path, dst: Path) -> None:
    with contextlib.closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as s, \
            contextlib.closing(sqlite3.connect(dst)) as d:
        s.backup(d)


def snapshot(data_dir: Path, backup_dir: Path | None = None, keep: int = 7) -> Path:
    if keep < 1:
        raise ValueError("keep must be >= 1")
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir) if backup_dir is not None else data_dir / "backups"
    with _lock:
        backup_dir.mkdir(parents=True, exist_ok=True)
        # only top-level db files are collected, so a backup_dir inside data_dir is never included
        files = [p for p in sorted(data_dir.iterdir())
                 if p.is_file() and (p.name == "meta.db" or re.fullmatch(r"samples-.*\.db(\.gz)?", p.name))]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with tempfile.TemporaryDirectory(prefix=".snap-", dir=backup_dir) as stage:
            tmp_tar = Path(stage) / "out.tar"
            with tarfile.open(tmp_tar, "w") as tar:
                for p in files:
                    if p.name.endswith(".gz"):
                        tar.add(p, arcname=p.name)
                    else:
                        copy = Path(stage) / p.name
                        _copy_db(p, copy)
                        tar.add(copy, arcname=p.name)
                        copy.unlink()
            final = None
            for n in range(1000):
                cand = backup_dir / f"analytics-{stamp}{'' if n == 0 else f'_{n:03d}'}.tar"
                try:
                    os.link(tmp_tar, cand)  # atomic, fails rather than overwrite
                except FileExistsError:
                    continue
                final = cand
                break
            if final is None:
                raise RuntimeError("too many backups in one second")
        for old in list_backups(backup_dir)[:-keep]:
            old.unlink()
        return final


def _verify_gz(gz: Path) -> None:
    fd, tmp = tempfile.mkstemp(prefix="verify-", suffix=".db", dir=gz.parent)
    try:
        with os.fdopen(fd, "wb") as out, gzip.open(gz, "rb") as src:
            shutil.copyfileobj(src, out)
        with contextlib.closing(sqlite3.connect(tmp)) as c:
            res = c.execute("pragma integrity_check").fetchall()
        if res != [("ok",)]:
            raise sqlite3.DatabaseError(f"integrity_check failed for {gz.name}: {res[:3]}")
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(tmp + suffix).unlink(missing_ok=True)


def _compact_one(db: Path) -> Path | None:
    # Fold WAL into the main file so it is complete on its own. If a writer still holds the
    # db the checkpoint is busy -> skip this month this time (never touch a db with an open writer).
    try:
        with contextlib.closing(sqlite3.connect(db, timeout=1)) as c:
            busy = c.execute("pragma wal_checkpoint(truncate)").fetchone()[0]
    except sqlite3.DatabaseError:
        busy = 0  # unreadable: let verification reject it below, original stays
    if busy:
        log.warning("compaction skipped, %s busy", db.name)
        return None
    gz = db.with_name(db.name + ".gz")
    tmp = db.with_name(db.name + ".gz.tmp")
    try:
        with open(db, "rb") as src, open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
            shutil.copyfileobj(src, out)
            out.flush()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, gz)
        try:
            _verify_gz(gz)
        except Exception:
            gz.unlink(missing_ok=True)
            raise
    finally:
        tmp.unlink(missing_ok=True)
    db.unlink()  # only after the verified gz is in place
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    return gz


def compact_closed_months(data_dir: Path, now: int) -> list[Path]:
    """Gzip months strictly before now's UTC month. A failing month is logged and left as is."""
    cur = datetime.fromtimestamp(now, timezone.utc)
    done = []
    with _lock:
        for p in sorted(Path(data_dir).glob("samples-*.db")):
            m = _MONTH.match(p.name)
            if not m or (int(m[1]), int(m[2])) >= (cur.year, cur.month):
                continue
            try:
                gz = _compact_one(p)
            except Exception:
                log.exception("compaction of %s failed; original kept", p.name)
                continue
            if gz:
                done.append(gz)
    return done
