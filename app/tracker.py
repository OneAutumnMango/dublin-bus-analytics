"""Tracker: turns per-tick route arrivals into change-only samples and finalised arrivals.

Threading: owns one meta connection and one cached samples connection (current UTC month);
use from a single thread only (see app/db.py). Call close() when done.

Ordering rule: an ingest whose `now` is <= the last accepted `now` for that route is ignored
(returns zeros). Stale/duplicate polls must not rewrite history or cause bogus vanishes.
Within one tick, repeated keys collapse to the last occurrence.

shortcut: in-flight (unfinalised) keys are lost on restart; at most one arrival per live bus
per stop is dropped. The UNIQUE constraint prevents double-counting after restart. Upgrade by
loading live state from the sample table.
shortcut: meta and samples are separate SQLite files, so the tick commit is not atomic across
both; an exception before commit rolls both back and restores in-memory state.
"""
import copy
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .db import open_meta, open_samples, samples_path
from .models import Arrival

DUBLIN = ZoneInfo("Europe/Dublin")
FINALISED_TTL_S = 6 * 3600  # finalised keys are remembered this long; UNIQUE covers older ones
FINALISE_DUE_MAX = 2


@dataclass
class IngestResult:
    samples_written: int
    finalised: int
    dropped: int


@dataclass
class _State:
    route: str
    scheduled: int | None
    last_sample: tuple
    last_due: int
    actual: int | None  # last expected seen with due <= 2
    saw_zero: bool
    ever_realtime: bool
    gone_at: int | None = None  # dropped on vanish; kept (bounded by TTL) so a reappearance stays change-only


class Tracker:
    def __init__(self, data_dir: Path, seq_lookup: Callable[[Arrival], Awaitable[int]]):
        self.data_dir = Path(data_dir)
        self.seq_lookup = seq_lookup
        self.meta = open_meta(self.data_dir)
        self._samples: sqlite3.Connection | None = None
        self._samples_path: Path | None = None
        self._live: dict[tuple, _State] = {}
        self._finalised: dict[tuple, int] = {}  # key -> now at finalisation
        self._seq: dict[tuple, int] = {}
        self._last_now: dict[str, int] = {}

    def close(self) -> None:
        if self._samples is not None:
            self._samples.close()
            self._samples = None
        self.meta.close()  # sqlite close is idempotent

    def _samples_conn(self, now: int) -> sqlite3.Connection:
        path = samples_path(self.data_dir, now)
        if self._samples is None or path != self._samples_path:
            if self._samples is not None:
                self._samples.close()
            self._samples = open_samples(self.data_dir, now)
            self._samples_path = path
        return self._samples

    def _on_finalised(self, arrival_id: int, row: dict) -> None:
        """Hook run inside the tick transaction for each newly inserted arrival (Task 5: forecasts)."""

    async def _seq_of(self, a: Arrival) -> int:
        ck = (a.trip_id, a.stop_id, a.scheduled)
        if ck not in self._seq:
            self._seq[ck] = await self.seq_lookup(a)
        return self._seq[ck]

    def _finalise(self, key: tuple, st: _State, now: int) -> int:
        trip, stop, seq = key
        actual = st.actual
        basis = st.scheduled if st.scheduled is not None else actual
        row = dict(trip_id=trip, stop_id=stop, seq=seq,
                   service_date=datetime.fromtimestamp(basis, DUBLIN).date().isoformat(),
                   scheduled=st.scheduled, actual=actual,
                   delay=None if st.scheduled is None else actual - st.scheduled,
                   confidence=int(st.saw_zero), realtime=int(st.ever_realtime), route=st.route)
        cur = self.meta.execute(
            "insert or ignore into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,realtime,route)"
            " values(:trip_id,:stop_id,:seq,:service_date,:scheduled,:actual,:delay,:confidence,:realtime,:route)", row)
        self._finalised[key] = now
        if cur.rowcount == 0:
            return 0
        self._on_finalised(cur.lastrowid, row)
        return 1

    async def ingest(self, route: str, arrivals: list[Arrival], now: int) -> IngestResult:
        if now <= self._last_now.get(route, -1):
            return IngestResult(0, 0, 0)
        snap = (copy.deepcopy(self._live), dict(self._finalised), dict(self._seq), dict(self._last_now))
        try:
            res = await self._ingest(route, arrivals, now)
            # shortcut: commit order meta then samples; a crash between leaves a finalised row
            # without its last sample, which is harmless for stats.
            self.meta.commit()
            self._samples_conn(now).commit()
            return res
        except BaseException:
            self.meta.rollback()
            if self._samples is not None:
                self._samples.rollback()
            self._live, self._finalised, self._seq, self._last_now = snap
            raise

    async def _ingest(self, route: str, arrivals: list[Arrival], now: int) -> IngestResult:
        self._last_now[route] = now
        for k in [k for k, t in self._finalised.items() if now - t > FINALISED_TTL_S]:
            del self._finalised[k]
        for k in [k for k, s in self._live.items() if s.gone_at is not None and now - s.gone_at > FINALISED_TTL_S]:
            del self._live[k]
        samples = self._samples_conn(now)
        written = finalised = dropped = 0
        seen: set[tuple] = set()
        keyed: dict[tuple, Arrival] = {}
        for a in arrivals:
            keyed[(a.trip_id, a.stop_id, await self._seq_of(a))] = a
        for key, a in keyed.items():
            seen.add(key)
            if key in self._finalised:
                continue
            sample = (a.due_min, a.expected, int(a.realtime))
            st = self._live.get(key)
            if st is None:
                st = self._live[key] = _State(route, a.scheduled, (), a.due_min, None, False, False)
            if sample != st.last_sample:
                samples.execute("insert into sample values(?,?,?,?,?,?,?)", (now, *key[:2], key[2], *sample))
                written += 1
                st.last_sample = sample
            st.gone_at = None
            st.last_due = a.due_min
            st.ever_realtime |= a.realtime
            if a.due_min <= FINALISE_DUE_MAX:
                st.actual = a.expected
            if a.due_min == 0:
                st.saw_zero = True
                finalised += self._finalise(key, st, now)
                del self._live[key]
        for key in [k for k, s in self._live.items() if s.route == route and s.gone_at is None and k not in seen]:
            st = self._live[key]
            if st.last_due <= FINALISE_DUE_MAX:
                del self._live[key]
                finalised += self._finalise(key, st, now)
            else:
                st.gone_at = now
                dropped += 1
        return IngestResult(written, finalised, dropped)
