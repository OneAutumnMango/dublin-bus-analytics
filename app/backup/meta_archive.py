"""Archive finished months of meta.db `arrivals` + `forecast` as meta-YYYY-MM.db.gz (never overwritten)."""
import contextlib
import gzip
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# backup.py imports this module lazily (inside snapshot), so this top-level import is not circular
from app.backup.backup import COMPACT_GRACE_S, _fsync_dir, _integrity, _ro_uri, _sha, _verify_gz

log = logging.getLogger(__name__)

META_ARCHIVE_RE = re.compile(r"^meta-(\d{4})-(\d{2})\.db\.gz$")


def finished_months(meta_db: Path, now: int) -> list[str]:
    with contextlib.closing(sqlite3.connect(_ro_uri(meta_db), uri=True)) as c:
        months = [r[0] for r in c.execute("select distinct substr(service_date, 1, 7) from arrivals order by 1")]
    out = []
    for mo in months:
        if not mo or not re.fullmatch(r"\d{4}-\d{2}", mo):
            continue
        y, m = int(mo[:4]), int(mo[5:])
        end = int(datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp())
        if now >= end + COMPACT_GRACE_S:
            out.append(mo)
    return out


def archive_info(gz: Path) -> dict | None:
    try:
        with tempfile.TemporaryDirectory(prefix="meta-info-") as t:
            db = Path(t) / "i.db"
            with gzip.open(gz, "rb") as src, open(db, "wb") as out:
                shutil.copyfileobj(src, out)
            with contextlib.closing(sqlite3.connect(_ro_uri(db), uri=True)) as c:
                rows = c.execute("select month, arrivals, forecast, max_id from info").fetchall()
        if len(rows) != 1:
            return None
        return dict(zip(("month", "arrivals", "forecast", "max_id"), rows[0]))
    except Exception:
        return None


def _write_db(meta_db: Path, mo: str, tmp_db: Path) -> None:
    like = f"{mo}-%"
    # The read snapshot of `live` starts at its first read after `begin` (the sqlite_master select),
    # so that read must stay first and all later selects share it.
    with contextlib.closing(sqlite3.connect(tmp_db, isolation_level=None)) as c:
        c.execute("attach ? as live", (_ro_uri(meta_db),))
        c.execute("begin")  # one read snapshot of the live db for every select below
        for (sql,) in c.execute("select sql from live.sqlite_master where type='table' and name in ('arrivals','forecast') order by name").fetchall():
            c.execute(sql)
        c.execute("insert into arrivals select * from live.arrivals where service_date like ?", (like,))
        c.execute("insert into forecast select * from live.forecast where arrival_id in (select id from live.arrivals where service_date like ?)", (like,))
        c.execute("create table info(month TEXT, arrivals INT, forecast INT, max_id INT)")
        c.execute("insert into info select ?, (select count(*) from arrivals), (select count(*) from forecast), coalesce((select max(id) from arrivals), 0)", (mo,))
        c.execute("commit")
        c.execute("detach live")
    res = _integrity(tmp_db)
    if res != [("ok",)]:
        raise sqlite3.DatabaseError(f"integrity_check failed for {tmp_db.name}: {res[:3]}")


def _archive_one(meta_db: Path, archive_dir: Path, mo: str, final: Path) -> None:
    tmp_db, tmp_gz = archive_dir / f"meta-{mo}.db.tmp", archive_dir / f"meta-{mo}.db.gz.tmp"
    try:
        _write_db(meta_db, mo, tmp_db)
        with open(tmp_db, "rb") as src, open(tmp_gz, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=3) as out:
                shutil.copyfileobj(src, out)
            raw.flush()  # GzipFile is closed (final block + CRC trailer written) before the fsync
            os.fsync(raw.fileno())
        _verify_gz(tmp_gz, _sha(tmp_db))
        os.replace(tmp_gz, final)
        _fsync_dir(archive_dir)
    finally:
        for p in (tmp_db, tmp_gz):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove temp archive file %s", p)


def archive_meta_months(data_dir: Path, archive_dir: Path, now: int) -> list[Path]:
    """Write meta-YYYY-MM.db.gz for each finished month not yet archived. Errors are logged, never raised."""
    meta_db, archive_dir, written = Path(data_dir) / "meta.db", Path(archive_dir), []
    try:
        months = finished_months(meta_db, now)
    except Exception:
        log.exception("listing finished months of %s failed", meta_db)
        return written
    for mo in months:
        final = archive_dir / f"meta-{mo}.db.gz"
        try:
            if final.exists():
                if archive_info(final) is None:
                    log.warning("meta archive %s exists but is unreadable; left untouched", final)
                continue
            _archive_one(meta_db, archive_dir, mo, final)
            written.append(final)
        except Exception:
            log.exception("archiving meta month %s failed", mo)
    return written


_MONTH_ARR = "select id from arrivals where substr(service_date, 1, 7) = ?"


def _slim_month(c: sqlite3.Connection, mo: str, info: dict) -> None:
    c.execute(f"delete from forecast where arrival_id in ({_MONTH_ARR})", (mo,))
    c.execute("delete from arrivals where substr(service_date, 1, 7) = ?", (mo,))
    c.execute("insert into archived_months values(?, ?, ?)", (mo, info["arrivals"], info["forecast"]))


def _vacuum(c: sqlite3.Connection) -> None:
    c.execute("vacuum")


def slim_meta_copy(copy: Path, archive_dir: Path | None, now: int) -> list[str]:
    """Remove verified-archived finished months from the staged COPY (never the live db); returns them."""
    copy, removed = Path(copy), []
    months = finished_months(copy, now) if archive_dir is not None else []
    with contextlib.closing(sqlite3.connect(copy, isolation_level=None)) as c:
        cache_rows = 0
        if c.execute("select 1 from sqlite_master where type='table' and name='result_cache'").fetchone():
            cache_rows = c.execute("delete from result_cache").rowcount
        for mo in months:
            gz = Path(archive_dir) / f"meta-{mo}.db.gz"
            if not gz.exists():
                log.warning("meta month %s kept in backup: no archive file", mo)
                continue
            info = archive_info(gz)
            if info is None:
                log.warning("meta month %s kept in backup: archive unreadable", mo)
                continue
            if info["month"] != mo:
                log.warning("meta month %s kept in backup: archive records month %s", mo, info["month"])
                continue
            n_arr, max_id = c.execute("select count(*), coalesce(max(id), 0) from arrivals where substr(service_date, 1, 7) = ?", (mo,)).fetchone()
            n_fc = c.execute(f"select count(*) from forecast where arrival_id in ({_MONTH_ARR})", (mo,)).fetchone()[0]
            if (info["arrivals"], info["forecast"]) != (n_arr, n_fc):
                log.warning("meta month %s kept in backup: count mismatch (archive %s/%s, copy %s/%s)",
                            mo, info["arrivals"], info["forecast"], n_arr, n_fc)
                continue
            if info["max_id"] != max_id:
                log.warning("meta month %s kept in backup: max_id mismatch (archive %s, copy %s)", mo, info["max_id"], max_id)
                continue
            c.execute("create table if not exists archived_months(month TEXT PRIMARY KEY, arrivals INT, forecast INT)")
            c.execute("begin")
            try:
                _slim_month(c, mo, info)
                c.execute("commit")
            except BaseException:
                c.execute("rollback")
                raise
            removed.append(mo)
        if removed or cache_rows:
            _vacuum(c)
    return removed
