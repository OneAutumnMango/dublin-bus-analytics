import pytest

from tests.api.test_routes import T0, env  # noqa: F401  (env fixture)

D = "2026-01-01"


def seed(env, trip, calls, t0=T0, date=D, route="4"):
    """calls = [(stop, delay)]; scheduled every 60 s from t0."""
    env.trip(trip, calls, route=route, t0=t0)
    # Env.trip uses the default service_date; point these rows at `date`
    from app.core.db import open_meta
    c = open_meta(env.s.data_dir)
    with c:
        c.execute("update arrivals set service_date=? where trip_id=?", (date, trip))
    c.close()


def test_runs_groups_by_trip_with_direction_and_position(env):
    env.fake.set_route_stops("4", {"out": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "in": [{"id": "d"}, {"id": "c"}, {"id": "b"}]})
    seed(env, "t1", [("a", 0), ("b", 60), ("c", 120)])
    seed(env, "t2", [("d", 30), ("c", 30), ("b", 90)], t0=T0 + 3600)
    d = env.get("/api/routes/4/runs", date=D).json()
    assert d["order_source"] == "server" and d["n"] == 6 and d["truncated"] is False
    assert [r["trip_id"] for r in d["runs"]] == ["t1", "t2"]  # by first scheduled time
    t1, t2 = d["runs"]
    assert (t1["direction"], [v["pos"] for v in t1["visits"]]) == ("out", [0, 1, 2])
    assert (t2["direction"], [v["pos"] for v in t2["visits"]]) == ("in", [0, 1, 2])
    v = t1["visits"][1]
    assert (v["stop_id"], v["scheduled"], v["actual"], v["delay"]) == ("b", T0 + 60, T0 + 120, 60)
    assert {"confidence", "realtime"} <= set(v)
    assert d["directions"] == {"out": ["a", "b", "c"], "in": ["d", "c", "b"]}


def test_runs_only_that_day_and_route_and_keeps_unarrived_rows(env):
    env.fake.set_route_stops("4", {"out": [{"id": "a"}]})
    seed(env, "t1", [("a", 0)])
    seed(env, "other-day", [("a", 0)], date="2026-01-02")
    seed(env, "other-route", [("a", 0)], route="15")
    env.add(route="4", stop="a", delay=None, scheduled=T0 + 900, trip="t9", date=D, realtime=0)
    d = env.get("/api/routes/4/runs", date=D).json()
    assert [r["trip_id"] for r in d["runs"]] == ["t1", "t9"]
    v = d["runs"][1]["visits"][0]
    assert (v["actual"], v["delay"], v["realtime"]) == (None, None, 0)


def test_runs_without_server_order(env):
    seed(env, "t1", [("a", 0), ("b", 0)])  # fake server knows no stops for route 4
    d = env.get("/api/routes/4/runs", date=D).json()
    assert d["order_source"] == "none" and d["directions"] == {}
    assert d["runs"][0]["direction"] is None and [v["pos"] for v in d["runs"][0]["visits"]] == [None, None]


def test_runs_loop_stop_takes_the_next_position(env):
    env.fake.set_route_stops("4", {"out": [{"id": "a"}, {"id": "b"}, {"id": "a"}]})
    seed(env, "t1", [("a", 0), ("b", 0), ("a", 0)])
    d = env.get("/api/routes/4/runs", date=D).json()
    assert [v["pos"] for v in d["runs"][0]["visits"]] == [0, 1, 2]


def test_runs_empty_day_is_200(env):
    d = env.get("/api/routes/4/runs", date=D).json()
    assert d["runs"] == [] and d["n"] == 0


@pytest.mark.parametrize("bad", ["", "2026-1-1", "2026-13-01", "yesterday", "2019-12-31", "2999-01-01"])
def test_runs_bad_date_422(env, bad):
    assert env.get("/api/routes/4/runs", date=bad).status_code == 422


def test_runs_missing_date_422(env):
    assert env.get("/api/routes/4/runs").status_code == 422


def test_runs_row_cap_marks_truncated(env, monkeypatch):
    from app.api import routes
    monkeypatch.setattr(routes, "RUNS_MAX_ROWS", 3)
    seed(env, "t1", [("a", 0), ("b", 0), ("c", 0), ("d", 0), ("e", 0)])
    d = env.get("/api/routes/4/runs", date=D).json()
    assert d["truncated"] is True and d["n"] == 3
