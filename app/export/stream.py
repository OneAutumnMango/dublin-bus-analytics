"""Streamed JSON export. iter_export yields text chunks whose concatenation is one JSON document.
Connections are opened with check_same_thread=False because a streaming response may pull
successive chunks from different worker threads."""
import json
import re
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from app.core.db import open_samples_readonly

BATCH = 1000
_ARRIVAL_COLS = "id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route"


def _dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"))


def _rows(cur: sqlite3.Cursor) -> Iterator[list[dict]]:
    names = [d[0] for d in cur.description]
    while True:
        batch = cur.fetchmany(BATCH)
        if not batch:
            return
        yield [dict(zip(names, r)) for r in batch]


def _array(cur: sqlite3.Cursor, enrich=None) -> Iterator[str]:
    """Yield '[' ... ']' as chunks of up to BATCH comma-joined rows."""
    yield "["
    first = True
    for rows in _rows(cur):
        if enrich:
            enrich(rows)
        yield ("" if first else ",") + ",".join(_dumps(r) for r in rows)
        first = False
    yield "]"


_MONTH_RE = re.compile(r"^samples-(\d{4})-(\d{2})\.db(\.gz)?$")


def sample_months(data_dir: Path) -> list[int]:
    """One UTC timestamp (first second of the month) per distinct samples month, ascending."""
    out = set()
    for p in Path(data_dir).iterdir():
        m = _MONTH_RE.match(p.name)
        if m and 1 <= int(m[2]) <= 12:
            out.add(int(datetime(int(m[1]), int(m[2]), 1, tzinfo=timezone.utc).timestamp()))
    return sorted(out)


def _next_month(ts: int) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())


_SAMPLE_COLS = "ts, trip_id, stop_id, seq, due_min, expected, realtime"


def _iter_samples(data_dir: Path, route, since, until) -> Iterator[str]:
    """Yield the samples array chunks, one month file (one read transaction) at a time."""
    meta = Path(data_dir) / "meta.db"
    yield "["
    first = True
    if route is None or meta.exists():
        for start in sample_months(data_dir):
            if until is not None and start > until:
                break
            if since is not None and _next_month(start) - 1 < since:
                continue
            try:
                conn = open_samples_readonly(data_dir, start, check_same_thread=False)
            except FileNotFoundError:
                continue
            try:
                where, args = [], []
                if route is not None:
                    conn.execute("attach database ? as m", (f"{meta.resolve().as_uri()}?mode=ro",))
                    where.append("exists (select 1 from m.arrivals a where a.route=? and a.trip_id=s.trip_id"
                                 " and a.stop_id=s.stop_id and a.seq=s.seq)")
                    args.append(route)
                if since is not None:
                    where.append("s.ts>=?"); args.append(since)
                if until is not None:
                    where.append("s.ts<=?"); args.append(until)
                sql = f"select {_SAMPLE_COLS} from sample s"
                if where:
                    sql += " where " + " and ".join(where)
                sql += " order by trip_id, stop_id, seq, ts"
                conn.execute("begin")
                for rows in _rows(conn.execute(sql, args)):
                    yield ("" if first else ",") + ",".join(_dumps(r) for r in rows)
                    first = False
            finally:
                conn.close()
    yield "]"


def iter_export(data_dir: Path, *, route: str | None = None, since: int | None = None,
                until: int | None = None, exported_at: int | None = None) -> Iterator[str]:
    if exported_at is None:
        exported_at = int(time.time())
    path = Path(data_dir) / "meta.db"
    conn = None
    if path.exists():
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                               check_same_thread=False, isolation_level=None)
    try:
        if conn:
            conn.execute("begin")  # one read transaction: a consistent snapshot throughout
        head = {"version": 1, "exported_at": exported_at,
                "filters": {"route": route, "since": since, "until": until}}
        yield "{" + ",".join(f"{_dumps(k)}:{_dumps(v)}" for k, v in head.items()) + ","
        for key, sql in (("routes_tracked", "select route, added from routes_tracked order by route"),
                         ("gaps", "select start, end from gaps order by start")):
            yield f'"{key}":'
            if conn:
                yield from _array(conn.execute(sql))
            else:
                yield "[]"
            yield ","
        yield '"arrivals":'
        if conn:
            where, args = [], []
            if route is not None:
                where.append("route=?"); args.append(route)
            if since is not None:
                where.append("scheduled>=?"); args.append(since)
            if until is not None:
                where.append("scheduled<=?"); args.append(until)
            sql = f"select {_ARRIVAL_COLS} from arrivals"
            if where:
                sql += " where " + " and ".join(where)
            sql += " order by id"

            def add_forecast(rows):
                grouped = {}
                for aid, h, p, e in conn.execute(  # batch <= BATCH ids, under SQLite's variable limit
                        "select arrival_id, horizon_min, predicted, error_s from forecast where arrival_id in ("
                        + ",".join("?" * len(rows)) + ") order by arrival_id, horizon_min",
                        [r["id"] for r in rows]):
                    grouped.setdefault(aid, []).append({"horizon_min": h, "predicted": p, "error_s": e})
                for r in rows:
                    r["forecast"] = grouped.get(r["id"], [])

            yield from _array(conn.execute(sql, args), add_forecast)
        else:
            yield "[]"
        yield ',"samples":'
        yield from _iter_samples(data_dir, route, since, until)
        yield ',"complete":true}'
    finally:
        if conn:
            conn.close()


def gzip_chunks(chunks: Iterable[str]) -> Iterator[bytes]:
    """Compress text chunks into one gzip stream, sync-flushed per chunk so output is not held back."""
    comp = zlib.compressobj(wbits=31)
    try:
        for chunk in chunks:
            data = comp.compress(chunk.encode("utf-8")) + comp.flush(zlib.Z_SYNC_FLUSH)
            if data:
                yield data
        yield comp.flush(zlib.Z_FINISH)
    finally:
        # release the source (sqlite connections, temp gz copy) deterministically on early close
        close = getattr(chunks, "close", None)
        if close is not None:
            close()
