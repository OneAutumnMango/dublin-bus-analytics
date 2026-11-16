import logging
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone

import pytest

from app.backup import backup
from app.backup.meta_archive import restore_archived_months
from app.core.db import open_meta

NOW = int(datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp())  # Aug, Sep, Oct finished; Nov current
MONTHS = ["2026-08", "2026-09", "2026-10", "2026-11"]


def seed(d):
    """Explicit interleaved ids across month boundaries, several routes, forecast rows, some NULL columns."""
    c = open_meta(d)
    n = 0
    for i in range(40):
        mo = MONTHS[i % 4]  # ids interleave across months
        n += 3
        null = i % 5 == 0
        c.execute("insert into arrivals(id, trip_id, stop_id, seq, service_date, scheduled, actual, delay, confidence, realtime, route)"
                  " values(?,?,?,?,?,?,?,?,?,?,?)",
                  (n, f"t{i}", f"s{i % 3}", i, f"{mo}-{i % 28 + 1:02d}", 1000 + i, None if null else 1010 + i,
                   None if null else 10, 3, i % 2, ("46A", "39A", "145")[i % 3]))
        if i % 4 != 1:
            c.executemany("insert into forecast values(?,?,?,?)", [(n, h, 1000 + i, None if null else h) for h in (5, 10, 15)])
    c.commit()
    c.close()


def dump(d):
    c = sqlite3.connect(d / "meta.db")
    try:
        return [c.execute(f"select * from {t} order by 1, 2").fetchall() for t in ("arrivals", "forecast")]
    finally:
        c.close()


def scenario(tmp_path, name):
    d, a = tmp_path / f"{name}-data", tmp_path / f"{name}-arch"
    d.mkdir()
    a.mkdir()
    seed(d)
    backup.snapshot(d, tmp_path / f"{name}-bk", keep=3, archive_dir=a, now=NOW)
    written = {p.name: p.stat().st_mtime_ns for p in a.glob("meta-*.db.gz")}
    newest = backup.snapshot(d, tmp_path / f"{name}-bk", keep=3, archive_dir=a, now=NOW + 60)
    assert {p.name: p.stat().st_mtime_ns for p in a.glob("meta-*.db.gz")} == written  # archived once
    assert sorted(written) == [f"meta-{m}.db.gz" for m in MONTHS[:3]]
    fresh = tmp_path / f"{name}-fresh"
    fresh.mkdir()
    with tarfile.open(newest) as t:
        t.extractall(fresh, filter="data")
    return d, a, fresh


def markers(d):
    c = sqlite3.connect(d / "meta.db")
    try:
        return [r[0] for r in c.execute("select month from archived_months order by 1")]
    finally:
        c.close()


def test_full_roundtrip_preserves_every_row(tmp_path, caplog):
    d, a, fresh = scenario(tmp_path, "ok")
    assert markers(fresh) == MONTHS[:3]
    assert len(dump(fresh)[0]) < len(dump(d)[0])  # tar really was slim
    assert restore_archived_months(fresh, a) == MONTHS[:3]
    assert dump(fresh) == dump(d)
    assert markers(fresh) == []
    assert restore_archived_months(fresh, a) == []
    assert dump(fresh) == dump(d)

    # loud failure: one archive gone
    d2, a2, fresh2 = scenario(tmp_path, "gone")
    gone = a2 / "meta-2026-09.db.gz"
    kept = tmp_path / "kept.gz"
    shutil.move(gone, kept)
    with caplog.at_level(logging.ERROR):
        assert restore_archived_months(fresh2, a2) == ["2026-08", "2026-10"]
    assert markers(fresh2) == ["2026-09"]
    assert any("2026-09" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    c = sqlite3.connect(fresh2 / "meta.db")
    assert c.execute("select count(*) from arrivals where service_date like '2026-09-%'").fetchone() == (0,)
    c.close()
    shutil.move(kept, gone)
    assert restore_archived_months(fresh2, a2) == ["2026-09"]
    assert dump(fresh2) == dump(d2)
    assert markers(fresh2) == []


def test_old_full_tar_restores_as_before(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    seed(d)
    tar_path = backup.snapshot(d, tmp_path / "bk", keep=3, archive_dir=None, now=NOW)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with tarfile.open(tar_path) as t:
        t.extractall(fresh, filter="data")
    assert restore_archived_months(fresh, tmp_path / "none") == []
    assert dump(fresh) == dump(d)
