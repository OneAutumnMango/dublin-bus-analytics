"""Streamed JSON export. iter_export yields text chunks whose concatenation is one JSON document.
Connections are opened with check_same_thread=False because a streaming response may pull
successive chunks from different worker threads."""
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterator

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
                for r in rows:
                    r["forecast"] = [
                        {"horizon_min": h, "predicted": p, "error_s": e}
                        for h, p, e in conn.execute(
                            "select horizon_min, predicted, error_s from forecast"
                            " where arrival_id=? order by horizon_min", (r["id"],))]

            yield from _array(conn.execute(sql, args), add_forecast)
        else:
            yield "[]"
        yield ',"samples":[],"complete":true}'
    finally:
        if conn:
            conn.close()
