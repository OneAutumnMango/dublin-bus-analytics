"""The overview endpoints must answer exactly the same with rollups as with raw scans."""
import random

import pytest

from app import rollup
from app.core.db import open_meta
from tests.api.test_routes import Env, T0

DAY = 86400
FILTERS = [
    {}, {"min_confidence": "0"}, {"weekdays": "0,2,5"}, {"hours": "6,7,8,17"}, {"weekdays": "1", "hours": "9,10"},
    {"window_lo": "-120", "window_hi": "600"}, {"pct_lo": "0.1", "pct_hi": "0.8"},
    {"minutes_from": "480", "minutes_to": "600"},
]


def _populate(env, seed):
    rng = random.Random(seed)
    c = open_meta(env.s.data_dir)
    for route in ("15", "46A"):
        env.app.state.poller.track(route)
        for day in range(6):
            for i in range(120):
                sched = T0 + day * DAY + rng.randrange(DAY)
                delay = None if rng.random() < .04 else int(rng.gauss(40, 400))
                cur = c.execute(
                    "insert into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,realtime,route)"
                    " values(?,?,?,?,?,?,?,?,?,?)",
                    (f"{route}-{day}-{i}", "s", sched, f"2026-01-0{day + 1}", sched,
                     None if delay is None else sched + delay, delay, rng.choice([0, 1, 1]), int(rng.random() > .1), route))
                for h in (30, 20, 10, 5, 2):
                    if rng.random() < .7:
                        c.execute("insert into forecast values(?,?,?,?)", (cur.lastrowid, h, 0, int(rng.gauss(0, 150))))
    c.commit()
    c.close()


def _norm(o):
    if isinstance(o, dict):
        return {k: _norm(v) for k, v in o.items() if k not in ("since",)}
    if isinstance(o, list):
        return [_norm(v) for v in o]
    return o


def _same(a, b, path=""):
    if isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for k in a:
            _same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert a == pytest.approx(b, rel=1e-9, abs=1e-9), path
    else:
        assert a == b, path


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("q", FILTERS, ids=lambda q: ",".join(q) or "default")
@pytest.mark.parametrize("since_mid_day", [False, True])
def test_overview_rollup_equals_raw(tmp_path, monkeypatch, seed, q, since_mid_day):
    env = Env(tmp_path)
    _populate(env, seed)
    q = dict(q)
    if since_mid_day:  # like the UI: a rolling since lands in the middle of a day, making that day partial
        q["since"] = str(T0 + 2 * DAY + 40_000)
        q["until"] = str(T0 + 4 * DAY + 20_000)
    for ep in ("routes", "heatmap", "forecast"):
        monkeypatch.setattr(rollup, "closed_before", lambda today=None: "2000-01-01")  # nothing closed: raw
        raw = env.get(f"/api/all/{ep}", **q).json()
        monkeypatch.undo()
        monkeypatch.setattr(rollup, "closed_before", lambda today=None: "2026-01-05")  # days 1-4 rolled, 5-6 raw
        got = env.get(f"/api/all/{ep}", **q).json()
        monkeypatch.undo()
        _same(_norm(got), _norm(raw), ep)
    c = open_meta(tmp_path)
    assert (c.execute("select count(*) from ru_done").fetchone()[0] > 0) == ("minutes_from" not in q)  # ran unless ineligible
    c.close()


def test_rolled_days_are_not_truncated_by_max_rows(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.app.state.poller.track("15")
    for i in range(6):
        env.add(route="15", delay=i, scheduled=T0 + i)  # default date 2026-01-01: closed
    monkeypatch.setattr("app.api.routes.MAX_ROWS", 2)
    j = env.get("/api/all/routes").json()
    assert j["filters"]["truncated_routes"] == [] and j["routes"][0]["n"] == 6


def test_stale_rollup_after_remove_day_is_gone(tmp_path):
    env = Env(tmp_path)
    env.app.state.poller.track("15")
    env.add(route="15", delay=10)
    assert env.get("/api/all/routes").json()["routes"][0]["n"] == 1
    from app.core.db import open_meta
    c = open_meta(tmp_path)
    rollup.drop_days(c, ["2026-01-01"])
    c.execute("delete from arrivals"); c.commit(); c.close()
    assert env.get("/api/all/routes").json()["routes"][0]["n"] == 0
