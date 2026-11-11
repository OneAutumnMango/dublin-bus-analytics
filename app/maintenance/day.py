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
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class DayError(Exception):
    """Bad input (message safe to show)."""


class DayBusy(Exception):
    """A database stayed locked; `done` holds the counts removed before the abort."""
    def __init__(self, part: str, done: dict):
        super().__init__(part)
        self.part, self.done = part, done


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


def _open(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.execute("pragma busy_timeout=5000")
    c.execute("pragma journal_mode=wal")
    c.execute("pragma synchronous=normal")
    return c


def _checkpoint(c: sqlite3.Connection) -> None:
    try:
        c.execute("pragma wal_checkpoint(passive)").fetchall()
    except sqlite3.Error:
        pass


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


def _scan_months(data_dir: Path, d, route, w, apply: bool, state: dict) -> tuple[list[str], list[str]]:
    """Samples of every editable month: count (preview) or delete in chunks. Returns (months, skipped)."""
    edit, skipped = _classify(data_dir, w)
    state["months"], state["skipped_months"] = [], skipped
    keys = []
    if route:
        meta = data_dir / "meta.db"
        if meta.exists():
            where, args = _arrival_where(d, route)
            with closing(_open(meta)) as m:
                keys = m.execute(f"select trip_id, stop_id, seq from arrivals where {where}", args).fetchall()
    for name in edit:
        try:
            with closing(_open(data_dir / name)) as c:
                n = _samples_one(c, w, keys if route else None, apply)
                if apply:
                    _checkpoint(c)
        except sqlite3.OperationalError:
            raise DayBusy(name, dict(state)) from None
        state["samples"] += n
        state["months"].append(name)
    return edit, skipped


_PK = "(trip_id, stop_id, seq, ts)"


def _samples_one(c: sqlite3.Connection, w, keys, apply: bool) -> int:
    tbl = c.execute("select 1 from sqlite_master where name = 'sample'").fetchone()
    if not tbl:
        return 0
    cond, args = "ts >= ? and ts < ?", [w[0], w[1]]
    if keys is not None:
        c.execute("create temp table if not exists dkeys(trip_id, stop_id, seq, primary key(trip_id, stop_id, seq))")
        c.execute("delete from dkeys")
        c.executemany("insert or ignore into dkeys values (?,?,?)", keys)
        cond += " and exists (select 1 from dkeys k where k.trip_id = sample.trip_id and k.stop_id = sample.stop_id and k.seq = sample.seq)"
    if not apply:
        return c.execute(f"select count(*) from sample where {cond}", args).fetchone()[0]
    total, last = 0, None
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
        total += len(rows)
        last = rows[-1]
        _between_chunks()
        if len(rows) < CHUNK:
            break
    return total


def _meta_counts(c, d, route, w) -> tuple[int, int, int]:
    where, args = _arrival_where(d, route)
    a = c.execute(f"select count(*) from arrivals where {where}", args).fetchone()[0]
    f = c.execute(f"select count(*) from forecast where arrival_id in (select id from arrivals where {where})", args).fetchone()[0]
    g = 0 if route else c.execute("select count(*) from gaps where start >= ? and start < ?", w).fetchone()[0]
    return a, f, g


def preview(data_dir: Path, d: date, route: str | None) -> dict:
    data_dir, w = Path(data_dir), window(d)
    out = _zero(d, route, w)
    meta = data_dir / "meta.db"
    if meta.exists():
        with closing(_open(meta)) as c:
            out["arrivals"], out["forecasts"], out["gaps"] = _meta_counts(c, d, route, w)
    _scan_months(data_dir, d, route, w, False, out)
    out["truncated"] = False
    return out


def remove(data_dir: Path, d: date, route: str | None) -> dict:
    data_dir, w = Path(data_dir), window(d)
    out = _zero(d, route, w)
    _scan_months(data_dir, d, route, w, True, out)
    meta = data_dir / "meta.db"
    if not meta.exists():
        return out
    try:
        with closing(_open(meta)) as c:
            where, args = _arrival_where(d, route)
            ids = [r[0] for r in c.execute(f"select id from arrivals where {where}", args)]
            for i in range(0, len(ids), CHUNK):
                part = ids[i:i + CHUNK]
                ph = ",".join("?" * len(part))
                with _DB_LOCK:  # forecasts and their arrivals in ONE transaction: no orphan forecast
                    out["forecasts"] += c.execute(f"delete from forecast where arrival_id in ({ph})", part).rowcount
                    out["arrivals"] += c.execute(f"delete from arrivals where id in ({ph})", part).rowcount
                    c.commit()
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
    except sqlite3.OperationalError:
        raise DayBusy("meta.db", dict(out)) from None
    return out
