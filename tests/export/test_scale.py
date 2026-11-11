import time
import tracemalloc
from datetime import datetime, timezone

from app.core.db import SAMPLES_SCHEMA, open_meta, samples_path
from app.export.stream import iter_export

N_ARRIVALS = 20_000
PER_ARRIVAL = 10  # 200k samples
MONTH = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())


def _build(tmp_path):
    """One month with 200k samples and 20k arrivals (half on route 15), inserted in batches."""
    import sqlite3
    m = open_meta(tmp_path)
    for lo in range(0, N_ARRIVALS, 5000):
        m.executemany(
            "insert into arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence,"
            " realtime, route) values (?,?,?,?,?,?,?,?,?,?,?)",
            [(i, f"t{i}", f"s{i}", i, "2026-10-01", MONTH + i, MONTH + i + 30, 30, 0.9, 1, "15" if i % 2 else "7")
             for i in range(lo, min(lo + 5000, N_ARRIVALS))])
    m.commit()
    m.close()
    s = sqlite3.connect(samples_path(tmp_path, MONTH))
    s.executescript(SAMPLES_SCHEMA)
    for lo in range(0, N_ARRIVALS, 1000):
        s.executemany(
            "insert into sample(ts, trip_id, stop_id, seq, due_min, expected, realtime) values (?,?,?,?,?,?,?)",
            [(MONTH + i + k * 60, f"t{i}", f"s{i}", i, 10 - k, MONTH + i + 600, 1)
             for i in range(lo, min(lo + 1000, N_ARRIVALS)) for k in range(PER_ARRIVAL)])
    s.commit()
    s.close()


def test_export_memory_is_bounded(tmp_path):
    _build(tmp_path)
    n = 0
    tracemalloc.start()
    try:
        for chunk in iter_export(tmp_path, exported_at=1):
            n += len(chunk)  # discard the chunk
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert n > 10_000_000
    assert peak < 25 * 1024 * 1024, f"peak {peak / 1048576:.1f} MiB"


def test_route_filtered_samples_are_fast_enough(tmp_path):
    _build(tmp_path)
    t0 = time.perf_counter()
    n = sum(len(c) for c in iter_export(tmp_path, route="15", exported_at=1))
    elapsed = time.perf_counter() - t0
    assert n > 1_000_000
    assert elapsed < 5, f"{elapsed:.2f}s"
