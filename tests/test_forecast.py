import sqlite3
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings, strategies as st

from app.core.db import open_samples
from app.forecast import HORIZONS, extract_forecasts, open_previous_month

ACT = 1_000_000
SCHEMA = ("create table sample(ts int, trip_id text, stop_id text, seq int, due_min int, "
          "expected int, realtime int, primary key(trip_id, stop_id, seq, ts)) without rowid")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(SCHEMA)
    yield c
    c.close()


def add(c, ts, expected, trip="T", stop="S", seq=1, rt=1):
    c.execute("insert into sample values(?,?,?,?,?,?,?)", (ts, trip, stop, seq, 0, expected, rt))


def fc(conns, actual=ACT, trip="T", stop="S", seq=1):
    return extract_forecasts(conns, trip, stop, seq, actual)


def test_picks_latest_sample_before_horizon(conn):
    add(conn, ACT - 3000, 111)
    add(conn, ACT - 2000, 222)  # latest <= ACT-1800
    add(conn, ACT - 1000, 333)  # after the 30 min horizon
    assert fc([conn])[0] == (30, 222, ACT - 222)


def test_missing_horizon_omitted(conn):
    add(conn, ACT - 700, 5)  # qualifies for 10, 5, 2 only
    assert [h for h, _, _ in fc([conn])] == [10, 5, 2]


def test_error_sign_positive_when_late(conn):
    add(conn, ACT - 4000, ACT - 60)  # predicted earlier than actual: bus late
    assert fc([conn])[0][2] == 60


def test_error_sign_negative_when_early(conn):
    add(conn, ACT - 4000, ACT + 90)
    assert fc([conn])[0][2] == -90


def test_horizon_order_and_values(conn):
    for i, h in enumerate(HORIZONS):
        add(conn, ACT - h * 60 - 1, 10 + i)
    assert fc([conn]) == [(h, 10 + i, ACT - 10 - i) for i, h in enumerate(HORIZONS)]


def test_exact_boundary_included_one_second_later_excluded(conn):
    add(conn, ACT - 120, 7)
    assert fc([conn]) == [(2, 7, ACT - 7)]
    c = sqlite3.connect(":memory:")
    c.execute(SCHEMA)
    add(c, ACT - 119, 7)
    assert fc([c]) == []
    c.close()


def test_change_only_old_sample_still_forecast(conn):
    add(conn, ACT - 86400, 42)
    assert [h for h, _, _ in fc([conn])] == list(HORIZONS)
    assert all(p == 42 for _, p, _ in fc([conn]))


def test_latest_scheduled_sample_omits_horizon_no_fallback(conn):
    add(conn, ACT - 5000, 1, rt=1)
    add(conn, ACT - 1000, 2, rt=0)  # latest for h10/h5/h2 (and h20 at -1200? no: -1000 > -1200)
    assert [(h, p) for h, p, _ in fc([conn])] == [(30, 1), (20, 1)]


def test_latest_scheduled_in_current_does_not_fall_through_to_previous():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    for c in (a, b):
        c.execute(SCHEMA)
    add(a, ACT - 5000, 1, rt=0)
    add(b, ACT - 6000, 2, rt=1)
    assert fc([a, b]) == []
    a.close(); b.close()


def test_only_scheduled_samples_gives_nothing(conn):
    add(conn, ACT - 5000, 1, rt=0)
    assert fc([conn]) == []


def test_other_keys_ignored(conn):
    add(conn, ACT - 5000, 1, trip="X")
    add(conn, ACT - 5000, 2, stop="X")
    add(conn, ACT - 5000, 3, seq=2)
    add(conn, ACT - 5000, 4)
    assert fc([conn])[0][1] == 4
    assert fc([conn], seq=3) == []


def test_loop_trip_seq_separation(conn):
    add(conn, ACT - 5000, 100, seq=1)
    add(conn, ACT - 3000, 200, seq=5)
    assert fc([conn], seq=1)[0][1] == 100
    assert fc([conn], seq=5)[0][1] == 200


def test_no_samples_and_no_conns(conn):
    assert fc([conn]) == []
    assert fc([]) == []


def test_current_conn_preferred_over_previous():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    for c in (a, b):
        c.execute(SCHEMA)
    add(a, ACT - 5000, 1)
    add(b, ACT - 6000, 2)
    assert fc([a, b])[0][1] == 1
    a.close(); b.close()


def test_previous_used_when_current_has_none_or_too_late():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    for c in (a, b):
        c.execute(SCHEMA)
    add(b, ACT - 6000, 2)
    assert fc([a, b])[0] == (30, 2, ACT - 2)
    add(a, ACT - 100, 9)  # only qualifies for horizon 0 < 2 min: not at all
    r = fc([a, b])
    assert [x[1] for x in r] == [2] * 5
    a.close(); b.close()


def ts_of(y, m, d=15):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def test_reads_previous_month_file_at_boundary(tmp_path):
    jan = ts_of(2026, 1, 1) + 60
    dec = ts_of(2025, 12, 31) + 80000
    p = open_samples(tmp_path, dec)
    p.execute("insert into sample values(?,?,?,?,?,?,?)", (dec, "T", "S", 1, 5, dec + 300, 1))
    p.commit(); p.close()
    cur = open_samples(tmp_path, jan)
    prev = open_previous_month(tmp_path, jan)
    assert prev is not None
    actual = jan + 600
    r = extract_forecasts([cur, prev], "T", "S", 1, actual)
    assert r and r[0] == (30, dec + 300, actual - dec - 300)
    cur.close(); prev.close()


def test_open_previous_month_none_and_creates_nothing(tmp_path):
    before = sorted(tmp_path.iterdir())
    assert open_previous_month(tmp_path, ts_of(2026, 3)) is None
    assert sorted(tmp_path.iterdir()) == before == []


def test_open_previous_month_year_rollover_and_normal(tmp_path):
    open_samples(tmp_path, ts_of(2025, 12)).close()
    open_samples(tmp_path, ts_of(2026, 2)).close()
    assert open_previous_month(tmp_path, ts_of(2026, 1, 31)) is not None
    assert open_previous_month(tmp_path, ts_of(2026, 3)) is not None  # Feb exists
    assert open_previous_month(tmp_path, ts_of(2026, 2)) is None  # Jan missing
    assert open_previous_month(tmp_path, ts_of(2026, 4)) is None


def test_previous_month_connection_is_read_only(tmp_path):
    open_samples(tmp_path, ts_of(2026, 2)).close()
    prev = open_previous_month(tmp_path, ts_of(2026, 3))
    with pytest.raises(sqlite3.OperationalError):
        prev.execute("insert into sample values(1,'a','b',1,1,1,1)")
    prev.close()


def oracle(conns, trip, stop, seq, actual):
    out = []
    for h in (30, 20, 10, 5, 2):
        cutoff = actual - h * 60
        for rows in conns:
            ok = [r for r in rows if r[1:4] == (trip, stop, seq) and r[0] <= cutoff]
            if ok:
                if max(ok, key=lambda r: r[0])[6] != 1:
                    break
                ok = [r for r in ok if r[6] == 1 and r[5] is not None]
                if ok:
                    best = max(ok, key=lambda r: r[0])
                    out.append((h, best[5], actual - best[5]))
                    break
    return out


row = st.tuples(st.integers(0, 4000), st.sampled_from("AB"), st.sampled_from("XY"),
                st.integers(1, 2), st.just(0), st.integers(0, 6000), st.integers(0, 1))


@settings(max_examples=200, deadline=None)
@given(a=st.lists(row, max_size=15), b=st.lists(row, max_size=15), actual=st.integers(0, 5000),
       key=st.tuples(st.sampled_from("AB"), st.sampled_from("XY"), st.integers(1, 2)))
def test_matches_oracle(a, b, actual, key):
    conns = []
    for rows in (a, b):
        c = sqlite3.connect(":memory:")
        c.execute(SCHEMA)
        c.executemany("insert or replace into sample values(?,?,?,?,?,?,?)", rows)
        conns.append(c)
    dedup = [list({(r[1], r[2], r[3], r[0]): r for r in rows}.values()) for rows in (a, b)]
    got = extract_forecasts(conns, *key, actual)
    assert got == oracle(dedup, *key, actual)
    hs = [g[0] for g in got]
    assert hs == [h for h in HORIZONS if h in hs]
    assert all(e == actual - p for _, p, e in got)
    for c in conns:
        c.close()


def test_null_expected_skipped_falls_back(conn):
    add(conn, ACT - 5000, 11)
    add(conn, ACT - 4000, None)  # latest qualifying but NULL
    assert fc([conn])[0] == (30, 11, ACT - 11)


def test_null_expected_only_omitted(conn):
    add(conn, ACT - 5000, None)
    assert fc([conn]) == []


def test_previous_month_leaves_no_files_behind(tmp_path):
    t = ts_of(2026, 2)
    c = open_samples(tmp_path, t)
    c.execute("insert into sample values(?,?,?,?,?,?,?)", (t, "T", "S", 1, 5, t + 300, 1))
    c.commit(); c.close()
    before = sorted(p.name for p in tmp_path.iterdir())
    prev = open_previous_month(tmp_path, ts_of(2026, 3))
    assert prev.execute("select expected from sample").fetchall() == [(t + 300,)]
    prev.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not any(n.endswith(("-wal", "-shm")) for n in before)


@pytest.mark.parametrize("ts,prev_ts", [
    (int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), ts_of(2025, 12)),
    (int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()), ts_of(2026, 2)),
])
def test_open_previous_month_exact_month_start(tmp_path, ts, prev_ts):
    open_samples(tmp_path, prev_ts).close()
    prev = open_previous_month(tmp_path, ts)
    assert prev is not None
    prev.close()


def test_wal_honoured_when_present(tmp_path):
    t = ts_of(2026, 2)
    c = open_samples(tmp_path, t)
    c.execute("pragma wal_autocheckpoint=0")
    c.execute("insert into sample values(?,?,?,?,?,?,?)", (t, "T", "S", 1, 5, t + 300, 1))
    c.commit()  # stays in the WAL; connection (and WAL) left open
    assert (tmp_path / "samples-2026-02.db-wal").stat().st_size > 0
    prev = open_previous_month(tmp_path, ts_of(2026, 3))
    assert prev.execute("select expected from sample").fetchall() == [(t + 300,)]
    prev.close()
    c.close()


def test_empty_wal_still_uses_immutable(tmp_path):
    t = ts_of(2026, 2)
    open_samples(tmp_path, t).close()
    (tmp_path / "samples-2026-02.db-wal").write_bytes(b"")
    before = sorted(p.name for p in tmp_path.iterdir())
    prev = open_previous_month(tmp_path, ts_of(2026, 3))
    prev.execute("select count(*) from sample").fetchall()
    prev.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
