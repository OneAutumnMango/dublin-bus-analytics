"""Result cache storage (table result_cache in meta.db). Pure keying/storage; no HTTP.
Rows keyed `srv:*` are static server data: never cleared or evicted. The generation counter
lives in the row `srv:rc_gen` (meta_kv was removed from the schema)."""
import gzip
import re
import time
from urllib.parse import urlencode

_R = r"[^/]+"
ALLOWED = re.compile(
    r"^/api/(?:all/(?:routes|heatmap|forecast|lab/(?:distribution|variance|persistence))|map|map/shapes"
    rf"|routes/{_R}/(?:summary|heatmap|forecast|accumulation|lab/(?:distribution|variance|persistence))"
    rf"|stops/{_R}/stats|trips/{_R}/series)$")
_GEN = "srv:rc_gen"
_NOSRV = "key not like 'srv:%'"


def make_key(path, query):
    if not ALLOWED.fullmatch(path):
        return None
    pairs = []
    for k, v in query:
        if k == "fresh":
            continue
        if k in ("since", "until"):
            try:
                v = str(int(v) // 3600 * 3600)
            except ValueError:
                pass
        pairs.append((k, v))
    return path + "?" + urlencode(sorted(pairs))


def generation(c):
    r = c.execute("select body from result_cache where key=?", (_GEN,)).fetchone()
    return int(r[0]) if r else 0


def lookup(c, key, now=None, with_ms=False):
    now = int(time.time()) if now is None else now
    r = c.execute("select body, computed_at, last_used, ms from result_cache where key=?", (key,)).fetchone()
    if r is None:
        return None
    if now - r[2] > 600:
        c.execute("update result_cache set last_used=? where key=?", (now, key))
        c.commit()
    out = gzip.decompress(r[0]), r[1]
    return out + (r[3],) if with_ms else out


def store(c, key, body, ms, gen, now=None, keep_last_used=False):
    """Store a gzipped body. A pending caller transaction is committed first. keep_last_used (warmer):
    an existing row keeps its last_used, so a background refresh does not count as use."""
    if c.in_transaction:
        c.commit()
    now = int(time.time()) if now is None else now
    c.execute("begin immediate")  # lock first so the generation check and insert are atomic
    try:
        if gen is not None and gen != generation(c):
            c.rollback()
            return False
        r = c.execute("select last_used from result_cache where key=?", (key,)).fetchone() if keep_last_used else None
        c.execute("insert or replace into result_cache(key, body, computed_at, last_used, ms) values(?,?,?,?,?)",
                  (key, gzip.compress(body), now, r[0] if r else now, ms))
        c.commit()
        return True
    except BaseException:
        c.rollback()
        raise


def clear(c):
    """Drop cached rows and bump the generation. A pending caller transaction is committed first."""
    if c.in_transaction:
        c.commit()
    c.execute("begin immediate")
    try:
        g = generation(c) + 1
        c.execute(f"delete from result_cache where {_NOSRV}")
        c.execute("insert or replace into result_cache(key, body, computed_at, last_used, ms) values(?,?,0,0,0)",
                  (_GEN, str(g).encode()))
        c.commit()
    except BaseException:
        c.rollback()
        raise


def evict(c, now, max_age=14 * 86400, max_entries=500, max_bytes=100 * 2**20):
    n = c.execute(f"delete from result_cache where {_NOSRV} and last_used < ?", (now - max_age,)).rowcount
    rows = c.execute(f"select key, length(body) from result_cache where {_NOSRV} order by last_used desc").fetchall()
    keep, total = 0, 0
    for _, size in rows:  # keep the most recently used prefix that fits both caps
        if keep >= max_entries or total + size > max_bytes:
            break
        keep, total = keep + 1, total + size
    drop = [k for k, _ in rows[keep:]]
    c.executemany("delete from result_cache where key=?", [(k,) for k in drop])
    c.commit()
    return n + len(drop)


def stale_keys(c, now, used_within=2 * 86400, min_age=3000):
    return [r[0] for r in c.execute(
        f"select key from result_cache where {_NOSRV} and last_used >= ? and computed_at <= ? order by computed_at",
        (now - used_within, now - min_age))]
