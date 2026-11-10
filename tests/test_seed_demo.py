import importlib.util
import json
import sqlite3
import threading
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import open_meta
from app.forecast import extract_forecasts
from app.main import create_app

_spec = importlib.util.spec_from_file_location("seed_demo", Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

DUBLIN = ZoneInfo("Europe/Dublin")
END = date(2026, 3, 31)  # window includes the spring-forward Sunday 2026-03-29


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    d = tmp_path_factory.mktemp("seed")
    sd.seed(d, ["15", "46A"], 5, 1, END)
    return d


def dump(d):
    out = {}
    m = sqlite3.connect(d / "meta.db")
    for t in ("routes_tracked", "arrivals", "forecast"):
        out[t] = sorted(m.execute(f"select * from {t}").fetchall(), key=repr)
    m.close()
    for f in sorted(d.glob("samples-*.db")):
        c = sqlite3.connect(f)
        out[f.name] = c.execute("select * from sample order by trip_id, stop_id, seq, ts").fetchall()
        c.close()
    return out


def test_rows_readable_through_the_real_api(seeded):
    c = TestClient(create_app(Settings("http://127.0.0.1:1", seeded), start_background=False))
    assert c.get("/api/routes/tracked").json() == {"routes": ["15", "46A"]}
    s = c.get("/api/routes/15/summary", params={"since": 0}).json()
    assert s["n"] > 500 and len(s["stops"]) == sd.N_STOPS
    assert all(x["n"] > 0 for x in s["stops"])
    assert c.get("/api/routes/15/heatmap").json()["cells"]
    f = c.get("/api/routes/15/forecast").json()["horizons"]
    assert [h["horizon_min"] for h in f] == [30, 20, 10, 5, 2] and all(h["n"] > 0 for h in f)
    st = c.get(f"/api/stops/{sd.stops_for('15')[2]}/stats", params={"route": "15"}).json()
    assert st["n"] > 0 and st["recent"]
    r = st["recent"][0]
    ser = c.get(f"/api/trips/{r['trip_id']}/series", params={"stop": st["stop_id"], "seq": r["seq"]}).json()
    assert ser["visits"][0]["points"] and ser["visits"][0]["arrival"]["delay"] == r["delay"]


def test_deterministic_and_idempotent(tmp_path, seeded):
    a = tmp_path / "a"
    sd.seed(a, ["15", "46A"], 5, 1, END)
    assert dump(a) == dump(seeded)
    before = dump(a)
    assert sd.seed(a, ["15", "46A"], 5, 1, END)["arrivals_inserted"] == 0
    assert dump(a) == before
    b = tmp_path / "b"
    sd.seed(b, ["15", "46A"], 5, 2, END)
    assert dump(b)["arrivals"] != before["arrivals"]


def test_data_dir_created_and_nested(tmp_path):
    d = tmp_path / "x" / "y"
    sd.seed(d, ["15"], 1, 1, END)
    assert (d / "meta.db").exists()


@pytest.mark.parametrize("days", [0, -3, 367])
def test_bad_days_rejected(tmp_path, days):
    with pytest.raises(ValueError):
        sd.seed(tmp_path, ["15"], days, 1, END)


@pytest.mark.parametrize("raw", ["", "  ", "15,,46A", "15;x", "x" * 21, "a'b", ",", "15,<b>"])
def test_bad_routes_rejected(raw):
    with pytest.raises(ValueError):
        sd.parse_routes(raw)


def test_routes_dedupe_and_slash_names():
    assert sd.parse_routes("15, 46A,15,15/A") == ["15", "46A", "15/A"]


def test_main_cli_errors_exit_2(tmp_path, capsys):
    for argv in ([str(tmp_path), "--days", "0"], [str(tmp_path), "--routes", ""], [str(tmp_path), "--end", "nope"]):
        with pytest.raises(SystemExit) as e:
            sd.main(argv)
        assert e.value.code == 2
    assert sd.main([str(tmp_path / "ok"), "--routes", "15/A", "--days", "1", "--end", "2026-01-05"]) == 0
    assert "seeded" in capsys.readouterr().out


def test_samples_follow_change_only_rule_and_match_arrivals(seeded):
    m = sqlite3.connect(seeded / "meta.db")
    arr = {(t, s, q) for t, s, q in m.execute("select trip_id, stop_id, seq from arrivals")}
    m.close()
    per = {}
    for f in seeded.glob("samples-*.db"):
        c = sqlite3.connect(f)
        for ts, t, s, q, due, exp, rt in c.execute("select ts, trip_id, stop_id, seq, due_min, expected, realtime from sample order by ts"):
            per.setdefault((t, s, q), []).append((ts, due, exp, rt))
        c.close()
    assert set(per) == arr
    for key, rows in per.items():
        tss = [r[0] for r in rows]
        assert tss == sorted(set(tss)), key
        for prev, cur in zip(rows, rows[1:]):
            if (prev[2], prev[3]) == (cur[2], cur[3]):
                assert cur[1] <= 0, (key, prev, cur)  # only the final due<=0 row may repeat
            assert prev[1] >= 0 and cur[1] >= 0


def test_forecasts_consistent_with_arrivals_and_real_extractor(seeded):
    m = sqlite3.connect(seeded / "meta.db")
    ids = {i for (i,) in m.execute("select id from arrivals")}
    fc = m.execute("select arrival_id, horizon_min, predicted, error_s from forecast").fetchall()
    assert fc and {r[0] for r in fc} <= ids and {r[1] for r in fc} <= {30, 20, 10, 5, 2}
    for aid, h, pred, err in fc[:200]:
        actual = m.execute("select actual from arrivals where id=?", (aid,)).fetchone()[0]
        assert err == actual - pred
    sample = m.execute("select id, trip_id, stop_id, seq, actual from arrivals where realtime=1 order by id limit 40").fetchall()
    conns = {f.name: sqlite3.connect(f) for f in seeded.glob("samples-*.db")}
    for aid, t, s, q, actual in sample:
        name = datetime.fromtimestamp(actual, ZoneInfo("UTC")).strftime("samples-%Y-%m.db")
        got = sorted(extract_forecasts([conns[name]], t, s, q, actual))
        mine = sorted(m.execute("select horizon_min, predicted, error_s from forecast where arrival_id=?", (aid,)).fetchall())
        assert got == mine, (t, s)
    m.close()


def test_delays_plausible_and_mixed_quality(seeded):
    c = open_meta(seeded)
    rt = [d for (d,) in c.execute("select delay from arrivals where realtime=1 and delay is not null")]
    assert 0 < sum(rt) / len(rt) < 400
    assert min(rt) < 0 < max(rt)  # some early buses
    assert c.execute("select count(*) from arrivals where realtime=0").fetchone()[0] > 0
    assert c.execute("select count(*) from arrivals where realtime=0 and (delay != 0 or confidence != 0)").fetchone()[0] == 0
    assert {r[0] for r in c.execute("select distinct confidence from arrivals")} == {0, 1}
    wd = c.execute("select scheduled, delay from arrivals where realtime=1").fetchall()
    rush = [d for s, d in wd if datetime.fromtimestamp(s, DUBLIN).hour in (8, 17) and datetime.fromtimestamp(s, DUBLIN).weekday() < 5]
    off = [d for s, d in wd if datetime.fromtimestamp(s, DUBLIN).hour in (13, 21) and datetime.fromtimestamp(s, DUBLIN).weekday() < 5]
    assert sum(rush) / len(rush) > sum(off) / len(off)
    c.close()


def test_scheduled_times_are_dublin_local_across_dst(seeded):
    c = open_meta(seeded)
    first_stop = sd.stops_for("15")[0]
    rows = c.execute("select scheduled, service_date, seq from arrivals where stop_id=?", (first_stop,)).fetchall()
    days = set()
    for sched, sdate, seq in rows:
        loc = datetime.fromtimestamp(sched, DUBLIN)
        assert 6 <= loc.hour <= 21 and loc.minute in (0, 30), loc
        assert loc.date().isoformat() == sdate and seq == sched
        days.add(sdate)
    assert {"2026-03-28", "2026-03-29", "2026-03-30"} <= days
    c.close()


def test_fake_server_serves_routes_and_stops():
    srv = sd.fake_server(0, ["15", "15/A"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        get = lambda p: json.load(urllib.request.urlopen(base + p, timeout=5))  # noqa: E731
        assert [r["shortName"] for r in get("/v1/routes")] == ["15", "15/A"]
        d = get("/v1/routes/15%2FA/stops")["directions"]
        assert [s["id"] for s in d["Outbound"]] == sd.stops_for("15/A") and d["Inbound"][0]["id"] == d["Outbound"][-1]["id"]
        assert get("/v1/routes/15/arrivals") == {}
        with pytest.raises(urllib.error.HTTPError):
            get("/v1/routes/zzz/stops")
    finally:
        srv.shutdown()
        srv.server_close()
