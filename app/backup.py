"""Snapshot (tar of all dbs) and monthly compaction (gzip closed months).

Snapshot: meta.db and each samples file are individually point-in-time consistent (sqlite backup API)
but not mutually atomic (a writer may commit to meta.db between the copies).

Compaction precondition: closed months have no writers. The poller clock is monotone and the tracker
closes its old-month connection on rollover, so a month that ended COMPACT_GRACE_S ago is idle; as a
second line of defence each month is held under an exclusive sqlite lock (skipped if unobtainable)."""
import contextlib
import gzip
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
_lock = threading.Lock()  # serialises snapshot vs compaction
_MONTH = re.compile(r"^samples-(\d{4})-(\d{2})\.db$")
_TAR = re.compile(r"^analytics-\d{8}T\d{6}Z(_\d{3})?\.tar$")
COMPACT_GRACE_S = 2 * 86400
STALE_S = 3600


def _ro_uri(p: Path) -> str:
    return f"{Path(p).resolve().as_uri()}?mode=ro"


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _clean_stale(d: Path, patterns: tuple[str, ...]) -> None:
    """Remove our own leftovers (crashed runs) older than STALE_S; nothing else."""
    cutoff = time.time() - STALE_S
    for pat in patterns:
        for p in d.glob(pat):
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:  # pragma: no cover - raced with another cleaner
                pass


def list_backups(backup_dir: Path) -> list[Path]:
    """Backup tars (regular files with the exact name pattern), oldest first by mtime then name."""
    d = Path(backup_dir)
    if not d.is_dir():
        return []
    found = []
    for p in d.iterdir():
        if _TAR.match(p.name) and p.is_file():
            try:
                found.append((p.stat().st_mtime_ns, p.name, p))
            except FileNotFoundError:  # pragma: no cover
                pass
    return [p for _, _, p in sorted(found)]


def _copy_db(src: Path, dst: Path) -> None:
    with contextlib.closing(sqlite3.connect(_ro_uri(src), uri=True)) as s, \
            contextlib.closing(sqlite3.connect(dst)) as d:
        s.backup(d)


def _reserve(tmp_tar: Path, backup_dir: Path, stamp: str) -> Path:
    for n in range(1000):
        cand = backup_dir / f"analytics-{stamp}{'' if n == 0 else f'_{n:03d}'}.tar"
        try:
            try:
                os.link(tmp_tar, cand)  # atomic, fails rather than overwrite
            except FileExistsError:
                raise
            except OSError:  # filesystem without hard links (some bind mounts)
                os.close(os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                os.replace(tmp_tar, cand)
        except FileExistsError:
            continue
        return cand
    raise RuntimeError("too many backups in one second")


def snapshot(data_dir: Path, backup_dir: Path | None = None, keep: int = 7) -> Path:
    if keep < 1:
        raise ValueError("keep must be >= 1")
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir) if backup_dir is not None else data_dir / "backups"
    with _lock:
        backup_dir.mkdir(parents=True, exist_ok=True)
        _clean_stale(backup_dir, (".snap-*",))
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
            fd = os.open(tmp_tar, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            final = _reserve(tmp_tar, backup_dir, stamp)
        _fsync_dir(backup_dir)
        # never delete the tar just made, even if the wall clock is behind older backups
        others = [p for p in list_backups(backup_dir) if p != final]
        for old in others[:max(0, len(others) - (keep - 1))]:
            old.unlink()
        return final


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gz_sha(gz: Path) -> str:
    h = hashlib.sha256()
    with gzip.open(gz, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity(path: Path) -> list:
    with contextlib.closing(sqlite3.connect(path)) as c:
        return c.execute("pragma integrity_check").fetchall()


def _verify_gz(gz: Path, expected_sha: str) -> None:
    """The gz must decompress to exactly the source bytes AND be a sound sqlite db."""
    fd, tmp = tempfile.mkstemp(prefix="verify-", suffix=".db", dir=gz.parent)
    try:
        with os.fdopen(fd, "wb") as out, gzip.open(gz, "rb") as src:
            shutil.copyfileobj(src, out)
        if _sha(Path(tmp)) != expected_sha:
            raise sqlite3.DatabaseError(f"{gz.name} does not match its source")
        res = _integrity(Path(tmp))
        if res != [("ok",)]:
            raise sqlite3.DatabaseError(f"integrity_check failed for {gz.name}: {res[:3]}")
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(tmp + suffix).unlink(missing_ok=True)


@contextlib.contextmanager
def _exclusive(db: Path):
    """Hold the db exclusively and fold its WAL into the main file. Raises sqlite3.OperationalError if busy."""
    c = sqlite3.connect(db, timeout=1, isolation_level=None)
    try:
        c.execute("pragma locking_mode=exclusive")
        c.execute("select count(*) from sqlite_master").fetchall()
        if c.execute("pragma wal_checkpoint(truncate)").fetchone()[0]:  # pragma: no cover - defensive; unreachable under the exclusive lock
            raise sqlite3.OperationalError("checkpoint busy")
        yield
    finally:
        c.close()


def _remove_original(db: Path) -> None:
    db.unlink()
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)


def _compact_one(db: Path) -> Path | None:
    """Returns the gz path if this call produced/confirmed it and removed the plain file; None if skipped."""
    gz = db.with_name(db.name + ".gz")
    tmp = db.with_name(db.name + ".gz.tmp")
    try:
        with _exclusive(db):
            src_sha = _sha(db)
            if gz.exists():
                # never overwrite an existing gz: drop the plain file only if it is identical
                if _gz_sha(gz) != src_sha:
                    raise RuntimeError(f"{gz.name} exists and differs from {db.name}; both left untouched")
                _remove_original(db)
                return gz
            with open(db, "rb") as src, open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
                shutil.copyfileobj(src, out)
                out.flush()
                raw.flush()
                os.fsync(raw.fileno())
            _verify_gz(tmp, src_sha)
            os.rename(tmp, gz)  # gz did not exist (checked above, lock held, one compactor at a time)
            _fsync_dir(gz.parent)
            if _sha(db) != src_sha:
                gz.unlink()
                raise RuntimeError(f"{db.name} changed during compaction; original kept")
            _remove_original(db)
            return gz
    finally:
        tmp.unlink(missing_ok=True)


def compact_closed_months(data_dir: Path, now: int) -> list[Path]:
    """Gzip months that ended at least COMPACT_GRACE_S before `now` (UTC). A failing month is logged
    and left as is; returns the gz paths produced."""
    data_dir = Path(data_dir)
    done, skipped = [], []
    with _lock:
        _clean_stale(data_dir, ("verify-*.db*", "*.gz.tmp"))
        for p in sorted(data_dir.glob("samples-*.db")):
            m = _MONTH.match(p.name)
            if not m:
                continue
            y, mo = int(m[1]), int(m[2])
            end = int(datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=timezone.utc).timestamp())
            if now < end + COMPACT_GRACE_S:
                continue
            try:
                gz = _compact_one(p)
            except sqlite3.OperationalError as e:
                log.warning("compaction of %s skipped: %s", p.name, e)
                skipped.append(p.name)
                continue
            except Exception:
                log.exception("compaction of %s failed; original kept", p.name)
                skipped.append(p.name)
                continue
            done.append(gz)
        if skipped:
            log.error("compaction skipped months: %s", ", ".join(skipped))
    return done
