#!/usr/bin/env python3
"""Write synthetic but plausible demo data into DATA_DIR with the real schema helpers.

  scripts/seed_demo.py DATA_DIR [--routes 15,46A] [--days 14] [--end YYYY-MM-DD] [--seed 1] [--fake-server PORT]

Deterministic for the same arguments (seeded random.Random, `--end` defaults to yesterday: pass it for
reproducible output) and idempotent (INSERT OR IGNORE on the natural keys). Without a bus data server the
route view shows `order_source: none`; `--fake-server PORT` then serves /v1/routes, /v1/routes/{r}/stops and
empty arrivals on 127.0.0.1 so `SERVER_URL=http://127.0.0.1:PORT uvicorn app.main:app` gives the full UI."""
import argparse
import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.db import open_meta, open_samples  # noqa: E402
from app.collect.forecast import HORIZONS  # noqa: E402
from app.collect.poller import ROUTE_RE  # noqa: E402

DUBLIN = ZoneInfo("Europe/Dublin")
N_STOPS = 6
MAX_DAYS = 366


def stops_for(route: str) -> list[str]:
    return [f"{route.replace(' ', '_')}-S{i + 1:02d}" for i in range(N_STOPS)]


def parse_routes(raw: str) -> list[str]:
    routes = [r.strip() for r in raw.split(",")]
    if not raw.strip() or any(not r for r in routes):
        raise ValueError("--routes must be a non-empty comma separated list without empty names")
    for r in routes:
        if not ROUTE_RE.match(r):
            raise ValueError(f"invalid route name {r!r}")
    return list(dict.fromkeys(routes))


def _base_delay(hour: float, weekend: bool) -> float:
    base = 40.0
    if 7 <= hour < 9.5:
        base = 150.0
    elif 16 <= hour < 19:
        base = 200.0
    elif hour >= 20:
        base = 0.0
    return base * (0.4 if weekend else 1.0)


def _samples(rng, sched, actual, realtime, confirmed):
    """Change-only samples (ts, due, expected, realtime), like the tracker writes them."""
    out, last = [], None

    def add(ts, expected, rt):
        nonlocal last
        due = max(0, (expected - ts) // 60)
        if last is None or (expected, rt) != last or (due <= 0 and not any(s[1] <= 0 for s in out)):
            out.append((ts, due, expected, rt))
        last = (expected, rt)

    ts = actual - 35 * 60
    if not realtime:
        add(ts, sched, 0)
        return out
    add(ts, sched, 0)  # scheduled-only until the first live prediction
    ts += rng.randint(60, 180)
    while ts < actual - 60:
        add(ts, actual + round(rng.gauss(0, 15 + (actual - ts) / 60 * 5)), 1)
        ts += rng.randint(90, 240)
    if confirmed:
        add(actual, actual, 1)  # first due == 0 reading
    else:
        add(actual - 90, actual + rng.randint(-20, 20), 1)  # vanished with due <= 2
    return out


def _forecast(samples, actual):
    out = []
    for h in HORIZONS:
        latest = [s for s in samples if s[0] <= actual - h * 60]
        if latest and latest[-1][3] == 1:
            exp = [s for s in latest if s[3] == 1 and s[2] is not None][-1][2]
            out.append((h, exp, actual - exp))
    return out


def seed(data_dir, routes, days, seed_value=1, end=None) -> dict:
    if days < 1 or days > MAX_DAYS:
        raise ValueError(f"--days must be 1..{MAX_DAYS}")
    if end is None:
        end = date.today() - timedelta(days=1)  # whole days in the past: no arrival or forecast lies in the future
    if end >= date.today():
        raise ValueError("--end must be before today (demo rows must lie in the past)")
    rng = random.Random(seed_value)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    meta = open_meta(data_dir)
    sconns, n_arr = {}, 0
    try:
        anchor = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())
        for r in routes:
            meta.execute("insert or ignore into routes_tracked values (?, ?)", (r, anchor - days * 86400))
        for r in routes:
            stops = stops_for(r)
            route_bias = rng.uniform(-20, 40)
            for d in range(days - 1, -1, -1):
                day = end - timedelta(days=d)
                weekend = day.weekday() >= 5
                for k in range(0, 31 if not weekend else 16):
                    minutes = 6 * 60 + 30 + k * (30 if not weekend else 60)
                    start = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=DUBLIN)
                    trip = f"{r}-{day:%Y%m%d}-{minutes // 60:02d}{minutes % 60:02d}"
                    trip_off = rng.gauss(0, 60) - (120 if rng.random() < 0.08 else 0)
                    for i, stop in enumerate(stops):
                        sched = int(start.timestamp()) + i * 240
                        hour = minutes / 60 + i * 4 / 60
                        rt_kind = rng.random()
                        realtime, confirmed = (0, False) if rt_kind < 0.04 else (1, rt_kind > 0.16)
                        delay = round(_base_delay(hour, weekend) + route_bias + i * 25 + trip_off + rng.gauss(0, 30)) if realtime else 0
                        actual = sched + delay
                        samples = _samples(rng, sched, actual, realtime, confirmed)
                        if realtime and not confirmed:
                            actual = samples[-1][2]
                            delay = actual - sched
                        cur = meta.execute(
                            "insert or ignore into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,realtime,route)"
                            " values(?,?,?,?,?,?,?,?,?,?)",
                            (trip, stop, sched, sched_date(sched), sched, actual, delay, int(confirmed), realtime, r))
                        n_arr += cur.rowcount
                        aid = meta.execute("select id from arrivals where trip_id=? and stop_id=? and seq=? and service_date=?",
                                           (trip, stop, sched, sched_date(sched))).fetchone()[0]
                        meta.executemany("insert or ignore into forecast values (?,?,?,?)",
                                         [(aid, h, p, e) for h, p, e in _forecast(samples, actual)] if realtime else [])
                        _write_samples(data_dir, sconns, trip, stop, sched, samples)
        meta.commit()
        for c in sconns.values():
            c.commit()
    finally:
        meta.close()
        for c in sconns.values():
            c.close()
    return {"arrivals_inserted": n_arr}


def sched_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, DUBLIN).date().isoformat()


def _write_samples(data_dir, conns, trip, stop, seq, samples):
    for ts, due, exp, rt in samples:
        key = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")
        if key not in conns:
            conns[key] = open_samples(data_dir, ts)
        conns[key].execute("insert or ignore into sample values (?,?,?,?,?,?,?)", (ts, trip, stop, seq, due, exp, rt))


def fake_server(port: int, routes: list[str]) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            p = [unquote(x) for x in urlparse(self.path).path.split("/")[2:]]
            if p == ["routes"]:
                body = [{"id": f"r-{r}", "shortName": r, "longName": f"Demo route {r}", "agencyId": "demo"} for r in routes]
            elif len(p) == 3 and p[0] == "routes" and p[1] in routes and p[2] == "stops":
                ids = stops_for(p[1])
                st = lambda lst: [{"id": s, "name": f"Demo stop {s[-2:]}"} for s in lst]  # noqa: E731
                body = {"routeId": p[1], "shortName": p[1], "directions": {"Outbound": st(ids), "Inbound": st(ids[::-1])}}
            elif len(p) == 3 and p[0] == "routes" and p[2] == "arrivals":
                body = {}
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data_dir")
    ap.add_argument("--routes", default="15,46A")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="last day (YYYY-MM-DD, before today), default yesterday")
    ap.add_argument("--fake-server", type=int, metavar="PORT")
    a = ap.parse_args(argv)
    srv = None
    try:
        routes = parse_routes(a.routes)
        if a.days < 1 or a.days > MAX_DAYS:
            raise ValueError(f"--days must be 1..{MAX_DAYS}")
        if a.fake_server is not None:  # bind BEFORE seeding so a bad port fails fast
            if not 1 <= a.fake_server <= 65535:
                raise ValueError("--fake-server PORT must be 1..65535")
            try:
                srv = fake_server(a.fake_server, routes)
            except OSError as e:
                raise ValueError(f"cannot listen on 127.0.0.1:{a.fake_server}: {e.strerror or e}") from None
        res = seed(a.data_dir, routes, a.days, a.seed, a.end)
    except ValueError as e:
        if srv:
            srv.server_close()
        ap.error(str(e))
    print(f"seeded {a.data_dir}: {res['arrivals_inserted']} new arrivals for routes {', '.join(routes)}")
    if srv:
        print(f"fake bus server on http://127.0.0.1:{a.fake_server} (Ctrl-C to stop)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
