"""Remove (or preview removing) all recorded data of one Dublin-local day.

What goes: `arrivals` of that `service_date` (optionally one route) with their `forecast` rows, raw
`sample` rows and `gaps`. Never touched: `routes_tracked`, `meta_kv`, other days.

Samples have no route column. Without a route every sample whose `ts` lies in the day's window
[local midnight, next local midnight) goes. With a route only samples whose (trip_id, stop_id, seq)
belong to that route's arrivals of the day (and whose ts is in the window) go; samples without an
arrivals row (a bus dropped before arriving) and gaps cannot be attributed and are left alone.
A gzipped month (`samples-YYYY-MM.db.gz`) is immutable: it is skipped and reported.

Mechanics: sample files go first (the route keys come from the arrivals, which must still exist),
then meta: forecasts and their arrivals in one transaction per chunk (a forecast never outlives its
arrival), then gaps. Every chunk is its own short transaction (CHUNK rows) taken under the API's
`_DB_LOCK` only for that chunk, so neither the tracker's writer nor read endpoints wait long. A
failure (busy file) aborts with a report of what was already removed; running it again finishes.
Callers hold `app.backup.backup._lock` for the whole operation. No vacuum, no undo: back up first.

Tracker interplay: the tracker's in-memory `_finalised` keys keep deleted arrivals from being
re-inserted for about 6 h, but a bus held at "due now" may still finalise after the delete and
create a new row for that day; repeat the removal if that matters.
"""
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.api.routes import _DB_LOCK

DUBLIN = ZoneInfo("Europe/Dublin")
CHUNK = 2000
MIN_DATE = date(2020, 1, 1)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class DayError(Exception):
    """Bad input (message safe to show)."""


class DayBusy(Exception):
    """A database stayed locked (kind "busy") or failed otherwise (kind "error"); `done` holds the
    counts removed before the abort."""
    def __init__(self, part: str, done: dict, kind: str = "busy"):
        super().__init__(part)
        self.part, self.done, self.kind = part, done, kind


def _between_chunks() -> None:
    """Hook called after every committed chunk (tests patch it)."""


def parse_day(text: str, today: date | None = None) -> date:
    if not _DATE_RE.fullmatch(text or ""):
        raise DayError("date must be YYYY-MM-DD")
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise DayError("date is not a valid calendar date") from None
    today = today or datetime.now(DUBLIN).date()
    if d < MIN_DATE:
        raise DayError("date is before 2020-01-01")
    if d > today + timedelta(days=1):
        raise DayError("date is too far in the future")
    return d


def window(d: date) -> tuple[int, int]:
    s = datetime.combine(d, time.min, tzinfo=DUBLIN)
    e = datetime.combine(d + timedelta(days=1), time.min, tzinfo=DUBLIN)
    return int(s.timestamp()), int(e.timestamp())


def _months(start: int, end: int) -> list[str]:
    a = datetime.fromtimestamp(start, timezone.utc)
    b = datetime.fromtimestamp(end - 1, timezone.utc)
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"samples-{y:04d}-{m:02d}.db")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _open(path: Path, rw: bool) -> sqlite3.Connection | None:
    """Open an EXISTING db (never creates one): read-only for previews, read-write for removal.
    Returns None when the file vanished (e.g. compaction gzipped it meanwhile)."""
    c = None
    try:
        c = sqlite3.connect(path.resolve().as_uri() + ("?mode=rw" if rw else "?mode=ro"), uri=True)
        c.execute("pragma busy_timeout=5000")
        if rw:
            c.execute("pragma synchronous=normal")
        c.execute("select count(*) from sqlite_master").fetchall()
    except sqlite3.OperationalError:
        if c is not None:
            c.close()
        if not path.exists():
            return None
        raise
    return c


def _checkpoint(c: sqlite3.Connection) -> None:
    try:
        c.execute("pragma wal_checkpoint(passive)").fetchall()
    except sqlite3.Error:
        pass


def _fail(part: str, done: dict, e: sqlite3.Error) -> DayBusy:
    busy = isinstance(e, sqlite3.OperationalError) and any(w in str(e).lower() for w in ("locked", "busy"))
    return DayBusy(part, dict(done), "busy" if busy else "error")


def _zero(d, route, w):
    return {"date": d.isoformat(), "route": route, "window": {"start": w[0], "end": w[1]},
            "arrivals": 0, "forecasts": 0, "samples": 0, "gaps": 0, "months": [], "skipped_months": []}


def _classify(data_dir: Path, w) -> tuple[list[str], list[str]]:
    edit, skipped = [], []
    for name in _months(*w):
        if (data_dir / (name + ".gz")).exists():
            skipped.append(name + ".gz")
        elif (data_dir / name).exists():
            edit.append(name)
    return edit, skipped


def _arrival_where(d: date, route: str | None):
    return ("service_date = ?" + (" and route = ?" if route else ""),
            [d.isoformat()] + ([route] if route else []))


def _scan_months(data_dir: Path, d, route, w, apply: bool, state: dict) -> None:
    """Samples of every editable month: count (preview) or delete in chunks, updating `state`."""
    edit, skipped = _classify(data_dir, w)
    state["months"], state["skipped_months"] = [], skipped
    keys = []
    if route:
        try:
            m = _open(data_dir / "meta.db", False) if (data_dir / "meta.db").exists() else None
            if m is not None:
                with closing(m):
                    where, args = _arrival_where(d, route)
                    keys = m.execute(f"select trip_id, stop_id, seq from arrivals where {where}", args).fetchall()
        except sqlite3.Error as e:
            raise _fail("meta.db", state, e) from None
    for name in edit:
        try:
            c = _open(data_dir / name, apply)
            if c is None:  # vanished since listing: gzipped by compaction (or gone); never recreate it
                if (data_dir / (name + ".gz")).exists():
                    state["skipped_months"] = skipped = skipped + [name + ".gz"]
                continue
            with closing(c):
                state["months"].append(name)
                _samples_one(c, w, keys if route else None, apply, state)
                if apply:
                    _checkpoint(c)
        except sqlite3.Error as e:
            raise _fail(name, state, e) from None


_PK = "(trip_id, stop_id, seq, ts)"


def _load_keys(c: sqlite3.Connection, keys) -> None:
    c.execute("create temp table if not exists dkeys(trip_id, stop_id, seq, primary key(trip_id, stop_id, seq))")
    c.execute("delete from dkeys")
    c.executemany("insert or ignore into dkeys values (?,?,?)", keys)
    c.commit()  # no implicit transaction may stay open: its later read would pin a WAL snapshot


def _samples_one(c: sqlite3.Connection, w, keys, apply: bool, state: dict) -> None:
    if not c.execute("select 1 from sqlite_master where name = 'sample'").fetchall():
        return
    cond, args = "ts >= ? and ts < ?", [w[0], w[1]]
    if keys is not None:
        _load_keys(c, keys)
        cond += " and exists (select 1 from dkeys k where k.trip_id = sample.trip_id and k.stop_id = sample.stop_id and k.seq = sample.seq)"
    if not apply:
        state["samples"] += c.execute(f"select count(*) from sample where {cond}", args).fetchall()[0][0]
        return
    last = None
    while True:
        # keyset pagination over the primary key: linear overall, no rescanning of earlier rows
        after = f" and {_PK} > (?,?,?,?)" if last else ""
        with _DB_LOCK:
            rows = c.execute(f"select trip_id, stop_id, seq, ts from sample where {cond}{after} order by trip_id, stop_id, seq, ts limit ?",
                             args + (list(last) if last else []) + [CHUNK]).fetchall()
            if not rows:
                break
            c.executemany(f"delete from sample where {_PK} = (?,?,?,?)", rows)
            c.commit()
        state["samples"] += len(rows)  # per committed chunk: exact even if a later chunk fails
        last = rows[-1]
        _between_chunks()
        if len(rows) < CHUNK:
            break


def _meta_counts(c, d, route, w) -> tuple[int, int, int]:
    where, args = _arrival_where(d, route)
    a = c.execute(f"select count(*) from arrivals where {where}", args).fetchall()[0][0]
    f = c.execute(f"select count(*) from forecast where arrival_id in (select id from arrivals where {where})", args).fetchall()[0][0]
    g = 0 if route else c.execute("select count(*) from gaps where start >= ? and start < ?", w).fetchall()[0][0]
    return a, f, g


def preview(data_dir: Path, d: date, route: str | None) -> dict:
    data_dir, w = Path(data_dir), window(d)
    out = _zero(d, route, w)
    meta = data_dir / "meta.db"
    try:
        c = _open(meta, False) if meta.exists() else None
        if c is not None:
            with closing(c):
                out["arrivals"], out["forecasts"], out["gaps"] = _meta_counts(c, d, route, w)
    except sqlite3.Error as e:
        raise _fail("meta.db", out, e) from None
    _scan_months(data_dir, d, route, w, False, out)
    out["truncated"] = False
    return out


def remove(data_dir: Path, d: date, route: str | None) -> dict:
    data_dir, w = Path(data_dir), window(d)
    out = _zero(d, route, w)
    _scan_months(data_dir, d, route, w, True, out)
    meta = data_dir / "meta.db"
    try:
        c = _open(meta, True) if meta.exists() else None
        if c is None:
            return out
        with closing(c):
            where, args = _arrival_where(d, route)
            ids = [r[0] for r in c.execute(f"select id from arrivals where {where}", args).fetchall()]
            for i in range(0, len(ids), CHUNK):
                part = ids[i:i + CHUNK]
                ph = ",".join("?" * len(part))
                with _DB_LOCK:  # forecasts and their arrivals in ONE transaction: no orphan forecast
                    nf = c.execute(f"delete from forecast where arrival_id in ({ph})", part).rowcount
                    na = c.execute(f"delete from arrivals where id in ({ph})", part).rowcount
                    c.commit()
                out["forecasts"] += nf
                out["arrivals"] += na
                _between_chunks()
            if not route:
                while True:
                    with _DB_LOCK:
                        n = c.execute("delete from gaps where rowid in (select rowid from gaps where start >= ? and start < ? limit ?)",
                                      (w[0], w[1], CHUNK)).rowcount
                        c.commit()
                    out["gaps"] += n
                    _between_chunks()
                    if n < CHUNK:
                        break
            _checkpoint(c)
    except sqlite3.Error as e:
        raise _fail("meta.db", out, e) from None
    return out
