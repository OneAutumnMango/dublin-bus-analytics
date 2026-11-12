import gzip
import threading
import traceback
import time
from datetime import date

import pytest

from app.core.db import open_meta, open_samples
from app.maintenance import day as D
from app.maintenance import jobs as J

D1, D2 = date(2026, 11, 5), date(2026, 11, 6)
KEYS = ("arrivals", "forecasts", "samples", "gaps")


def _seed(tmp_path):
    m = open_meta(tmp_path)
    n = 0
    for d in (D1, D2):
        w0 = D.window(d)[0]
        for k in range(2):
            n += 1
            m.execute("insert into arrivals(id,trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,realtime,route)"
                      " values (?,?,?,?,?,?,?,?,?,?,?)", (n, f"t{n}", f"s{n}", n, d.isoformat(), w0 + 100, w0 + 160, 60, 1, 1, "15"))
            m.execute("insert into forecast values (?,?,?,?)", (n, 0, 1, 1))
        m.execute("insert into gaps values (?,?)", (w0 + 10, w0 + 20))
    m.commit(); m.close()
    s = open_samples(tmp_path, D.window(D1)[0])
    for i, d in enumerate((D1, D2)):
        s.execute("insert into sample values (?,?,?,?,?,?,?)", (D.window(d)[0] + 50, f"t{2 * i + 1}", "s1", 1, 1, 1, 1))
    s.commit(); s.close()


def _wait(jid, pred=lambda r: r["state"] != "running"):
    end = time.time() + 10
    while time.time() < end:
        r = J.get(jid)
        if r and pred(r):
            return r
        time.sleep(0.01)
    raise AssertionError("timeout")


@pytest.fixture(autouse=True)
def _clean():
    with J._mu:
        J._jobs.clear()
    yield
    with J._mu:
        J._jobs.clear()


def _lock():
    lk = threading.Lock()
    assert lk.acquire(blocking=False)
    return lk


def test_full_run_matches_preview(tmp_path):
    _seed(tmp_path)
    exp = D.preview_range(tmp_path, [D1, D2], None)
    lk = _lock()
    jid = J.start(tmp_path, [D1, D2], None, lk)
    r = _wait(jid)
    assert r["state"] == "done" and r["error"] is None
    assert r["day_index"] == 2 and r["day_count"] == 2
    assert r["done"] == {k: exp[k] for k in KEYS} == r["total"]
    assert exp["arrivals"] == 4 and exp["samples"] == 2
    assert J.running_id() is None
    assert not lk.locked()


def test_second_start_busy(tmp_path):
    gate = threading.Event()
    real = D.remove

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    _seed(tmp_path)
    lk = _lock()
    D_remove = D.remove
    D.remove = slow
    try:
        jid = J.start(tmp_path, [D1], None, lk)
        assert J.running_id() == jid
        with pytest.raises(J.JobBusy) as e:
            J.start(tmp_path, [D1], None, threading.Lock())
        assert e.value.job_id == jid
        gate.set()
        assert _wait(jid)["state"] == "done"
    finally:
        D.remove = D_remove
        gate.set()


def test_daybusy_on_day_two(tmp_path, monkeypatch):
    _seed(tmp_path)
    real = D.remove
    calls = []

    def fake(dd, d, route, progress=None):
        calls.append(d)
        if d == D2:
            raise D.DayBusy("meta.db", {"arrivals": 1, "forecasts": 1, "samples": 0, "gaps": 0, "_progress": 1})
        return real(dd, d, route, progress=progress)

    monkeypatch.setattr(D, "remove", fake)
    lk = _lock()
    r = _wait(J.start(tmp_path, [D1, D2], None, lk))
    assert r["state"] == "error"
    assert D2.isoformat() in r["error"] and D1.isoformat() not in r["error"]
    assert r["done"] == {"arrivals": 3, "forecasts": 3, "samples": 1, "gaps": 1}
    assert not lk.locked() and J.running_id() is None


def test_runtime_error_releases_lock(tmp_path, monkeypatch):
    _seed(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(D, "remove", boom)
    lk = _lock()
    r = _wait(J.start(tmp_path, [D1], None, lk))
    assert r["state"] == "error" and "secret detail" not in r["error"]
    assert not lk.locked() and J.running_id() is None


def test_get_unknown():
    assert J.get("nope") is None


def test_skipped_months(tmp_path):
    _seed(tmp_path)
    src = tmp_path / "samples-2026-11.db"
    (tmp_path / "samples-2026-11.db.gz").write_bytes(gzip.compress(src.read_bytes()))
    src.unlink()
    r = _wait(J.start(tmp_path, [D1], None, _lock()))
    assert r["state"] == "done" and r["skipped_months"] == ["samples-2026-11.db.gz"]


def test_progress_monotonic(tmp_path, monkeypatch):
    _seed(tmp_path)
    seen = []
    real = D.remove

    def spy(dd, d, route, progress=None):
        def cb(stage, out):
            progress(stage, out)
            seen.append(J.get(J.running_id())["done"]["arrivals"])
        return real(dd, d, route, progress=cb)

    monkeypatch.setattr(D, "remove", spy)
    r = _wait(J.start(tmp_path, [D1, D2], None, _lock()))
    assert seen == sorted(seen) and r["done"]["arrivals"] == 4


def test_unexpected_error_is_logged_with_traceback(tmp_path, monkeypatch, caplog):
    _seed(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(D, "remove", boom)
    with caplog.at_level("WARNING", logger="app.maintenance.jobs"):
        r = _wait(J.start(tmp_path, [D1], None, _lock()))
    recs = [x for x in caplog.records if x.name == "app.maintenance.jobs" and x.levelname == "ERROR"]
    assert len(recs) == 1
    assert recs[0].exc_info and "secret detail" in "".join(traceback.format_exception(*recs[0].exc_info))
    assert "secret detail" not in r["error"]


def test_daybusy_logs_warning(tmp_path, monkeypatch, caplog):
    _seed(tmp_path)

    def busy(*a, **k):
        raise D.DayBusy("meta.db", {})

    monkeypatch.setattr(D, "remove", busy)
    with caplog.at_level("WARNING", logger="app.maintenance.jobs"):
        _wait(J.start(tmp_path, [D1], None, _lock()))
    recs = [x for x in caplog.records if x.name == "app.maintenance.jobs" and x.levelname == "WARNING"]
    assert len(recs) == 1
    assert "meta.db" in recs[0].getMessage() and D1.isoformat() in recs[0].getMessage()


def test_daybusy_before_first_day_not_none(tmp_path, monkeypatch):
    def busy(*a, **k):
        raise D.DayBusy("meta.db", {})

    monkeypatch.setattr(D, "preview_range", busy)
    r = _wait(J.start(tmp_path, [D1], None, _lock()))
    assert r["state"] == "error"
    assert "None" not in r["error"] and "the first day" in r["error"]


def test_release_failure_still_sets_terminal_state(tmp_path, monkeypatch):
    _seed(tmp_path)

    class BadLock:
        def acquire(self, *a, **k):
            return True

        def release(self):
            raise ValueError("boom")

    r = _wait(J.start(tmp_path, [D1], None, BadLock()))
    assert r["state"] == "done"
    monkeypatch.setattr(D, "remove", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = _wait(J.start(tmp_path, [D1], None, BadLock()))
    assert r["state"] == "error"
