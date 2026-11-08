"""Tracker: turns per-tick route arrivals into change-only samples and finalised arrivals.

Seq lookups (possibly slow I/O) are resolved before the lock is taken, concurrently (max 8 in
flight, each bounded by lookup_timeout); the lock then covers only state mutation and DB writes.
A trip whose lookup fails is skipped for the rest of the tick and not retried for NEG_TTL_S.
TTL rule: cached state is evicted when its age is strictly greater than the TTL.

Threading: owns one meta connection and one cached samples connection (current UTC month);
safe for concurrent calls from the single event-loop thread (ingest is serialised by a lock, so
routes may be ingested with asyncio.gather); never use from other threads (see app/db.py).
Call close() when done.

Ordering rule: an ingest whose `now` is <= the last accepted `now` for that route is ignored
(returns zeros with stale=True). Stale/duplicate polls must not rewrite history or cause bogus vanishes.
Within one tick, repeated keys collapse to the last occurrence.

shortcut: in-flight (unfinalised) keys are lost on restart; at most one arrival per live bus
per stop is dropped. The UNIQUE constraint prevents double-counting after restart. Upgrade by
loading live state from the sample table.
shortcut: meta and samples are separate SQLite files, so the tick commit is not atomic across
both; an exception before commit rolls both back and restores in-memory state.
"""
import asyncio
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
_MISSING = object()
NEG_TTL_S = 60  # failed trip lookups are not retried for this long (in ingest `now` time)
INT64 = 2**63 - 1
LOOKUP_CONCURRENCY = 8
EPOCH_WINDOW_S = 86400  # expected/scheduled must be within this of the ingest `now`
FALLBACK_SCHED_S = 600


@dataclass
class IngestResult:
    samples_written: int
    finalised: int
    dropped: int
    skipped: int = 0  # invalid arrivals / failed seq lookups
    stale: bool = False  # ingest ignored because now <= last now for the route


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
    def __init__(self, data_dir: Path, seq_lookup: Callable[[Arrival], Awaitable[int]],
                 lookup_timeout: float = 15.0):
        self.data_dir = Path(data_dir)
        self.seq_lookup = seq_lookup
        self.lookup_timeout = lookup_timeout
        self._neg: dict[str, int] = {}  # trip -> now of last failed lookup
        self._sem: asyncio.Semaphore | None = None
        self.meta = open_meta(self.data_dir)
        self._samples: sqlite3.Connection | None = None
        self._samples_path: Path | None = None
        self._live: dict[tuple, _State] = {}
        self._finalised: dict[tuple, int] = {}  # key -> now at finalisation
        self._seq: dict[tuple, tuple[int, int]] = {}  # (trip, stop, scheduled) -> (seq, last used)
        self._undo: dict[str, dict] = {}
        self._lock: asyncio.Lock | None = None
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
                old, self._samples, self._samples_path = self._samples, None, None
                old.close()  # state is consistent even if opening the new month fails
            self._samples = open_samples(self.data_dir, now)
            self._samples_path = path
        return self._samples

    def _on_finalised(self, arrival_id: int, row: dict) -> None:
        """Hook run inside the tick transaction for each newly inserted arrival (Task 5: forecasts)."""

    def _mark(self, store: str, key) -> None:
        """Undo log: remember the pre-tick value of a key the first time this tick touches it."""
        d = self._undo[store]
        if key not in d:
            src = getattr(self, store)
            d[key] = src[key] if key in src else _MISSING  # states are replaced, never mutated in place

    @staticmethod
    def _row(key: tuple, st: _State) -> dict:
        """Pure: the arrivals row for a finalising state (may raise; callers guard)."""
        trip, stop, seq = key
        actual = st.actual
        basis = st.scheduled if st.scheduled is not None else actual
        return dict(trip_id=trip, stop_id=stop, seq=seq,
                    service_date=datetime.fromtimestamp(basis, DUBLIN).date().isoformat(),
                    scheduled=st.scheduled, actual=actual,
                    delay=None if st.scheduled is None else actual - st.scheduled,
                    confidence=int(st.saw_zero), realtime=int(st.ever_realtime), route=st.route)

    def _insert(self, key: tuple, row: dict, now: int) -> int:
        cur = self.meta.execute(
            "insert or ignore into arrivals(trip_id,stop_id,seq,service_date,scheduled,actual,delay,confidence,realtime,route)"
            " values(:trip_id,:stop_id,:seq,:service_date,:scheduled,:actual,:delay,:confidence,:realtime,:route)", row)
        self._mark("_finalised", key)
        self._finalised[key] = now
        if cur.rowcount == 0:
            return 0
        self._on_finalised(cur.lastrowid, row)
        return 1

    @staticmethod
    def _valid(a: Arrival, now: int) -> bool:
        def num(x, lo=1):
            return type(x) is int and lo <= x <= INT64
        def near(x):  # plausible epoch seconds; also keeps datetime.fromtimestamp safe
            return num(x) and abs(x - now) <= EPOCH_WINDOW_S
        return (isinstance(a.trip_id, str) and bool(a.trip_id) and isinstance(a.stop_id, str) and bool(a.stop_id)
                and num(a.due_min, -INT64) and near(a.expected) and (a.scheduled is None or near(a.scheduled))
                and type(a.realtime) is bool)

    async def _resolve(self, valid: list[Arrival], now: int) -> tuple[dict, set]:
        """Look up uncached seqs outside the lock. Returns (ck -> seq, newly failed trips). Mutates nothing."""
        by_trip: dict[str, dict[tuple, Arrival]] = {}
        for a in valid:
            ck = (a.trip_id, a.stop_id, a.scheduled)
            if ck not in self._seq:
                by_trip.setdefault(a.trip_id, {})[ck] = a
        if self._sem is None:
            self._sem = asyncio.Semaphore(LOOKUP_CONCURRENCY)
        resolved: dict[tuple, int] = {}
        fresh: set[str] = set()

        async def call(a: Arrival) -> int:
            async with self._sem:
                seq = await self.seq_lookup(a)
            if type(seq) is not int or seq < 0:
                raise ValueError("bad seq")
            return seq

        async def one_trip(trip: str, cks: dict[tuple, Arrival]) -> None:
            if now - self._neg.get(trip, -10**18) < NEG_TTL_S:
                return
            for ck, a in cks.items():
                try:  # the timeout budget includes waiting for a semaphore slot
                    resolved[ck] = await asyncio.wait_for(call(a), self.lookup_timeout)
                except Exception:
                    fresh.add(trip)
                    return

        await asyncio.gather(*(one_trip(t, c) for t, c in by_trip.items()))
        return resolved, fresh

    async def ingest(self, route: str, arrivals: list[Arrival], now: int) -> IngestResult:
        if now <= self._last_now.get(route, -1):
            return IngestResult(0, 0, 0, stale=True)
        valid: list[Arrival] = []
        invalid: list[Arrival] = []
        for a in arrivals:
            (valid if self._valid(a, now) else invalid).append(a)
        resolved, fresh = await self._resolve(valid, now)
        async with self._lock_():
            if now <= self._last_now.get(route, -1):
                return IngestResult(0, 0, 0, stale=True)
            self._undo = {"_live": {}, "_finalised": {}, "_seq": {}, "_last_now": {}}
            try:
                res = await self._ingest(route, valid, invalid, now, resolved)
                # shortcut: commit order meta then samples; a crash between leaves a finalised row
                # without its last sample, so sample-to-arrival joins may miss the last tick.
                self.meta.commit()
                self._samples_conn(now).commit()
            except BaseException:
                self._undo_all()  # first: in-memory state must be right whatever the rollbacks do
                for conn in (self.meta, self._samples):
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                raise  # the original exception
            for trip in fresh:
                self._neg[trip] = now
            return res

    def _undo_all(self) -> None:
        for store, d in self._undo.items():
            cur = getattr(self, store)
            for k, v in d.items():
                if v is _MISSING:
                    cur.pop(k, None)
                else:
                    cur[k] = v

    def _lock_(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _sweep(self, now: int) -> None:
        for store, ok in (("_finalised", lambda t: t),
                          ("_live", lambda s: s.gone_at),
                          ("_seq", lambda v: v[1])):
            d = getattr(self, store)
            for k in [k for k, v in d.items() if (t := ok(v)) is not None and now - t > FINALISED_TTL_S]:
                self._mark(store, k)
                del d[k]
        for k in [k for k, t in self._neg.items() if now - t >= NEG_TTL_S]:
            del self._neg[k]

    def _live_fallback(self, route: str, a: Arrival, claimed: set) -> int | None:
        """seq of an unclaimed live key of this route/(trip, stop) with a scheduled time within
        FALLBACK_SCHED_S of the arrival's, for when the seq lookup failed (never mis-assigns loop visits)."""
        if a.scheduled is None:
            return None
        cands = [k for k, s in self._live.items()
                 if k[:2] == (a.trip_id, a.stop_id) and s.route == route and s.gone_at is None
                 and k not in claimed and s.scheduled is not None
                 and abs(a.scheduled - s.scheduled) <= FALLBACK_SCHED_S]
        if not cands:
            return None
        return min(cands, key=lambda k: abs(self._live[k].last_due - a.due_min))[2]

    @staticmethod
    def _shielded(key: tuple, st: _State, unkeyed: set) -> bool:
        return any(u[:2] == key[:2] and (u[2] is None or st.scheduled is None
                                         or abs(u[2] - st.scheduled) <= FALLBACK_SCHED_S) for u in unkeyed)

    async def _ingest(self, route: str, valid: list[Arrival], invalid: list[Arrival], now: int,
                      resolved: dict) -> IngestResult:
        self._mark("_last_now", route)
        self._last_now[route] = now
        self._sweep(now)
        samples = self._samples_conn(now)
        written = finalised = dropped = 0
        skipped = len(invalid)
        seen: set[tuple] = set()
        # (trip, stop) of skipped arrivals: never treated as vanished
        # scheduled None = wildcard (untrusted arrival); else only live keys scheduled within FALLBACK_SCHED_S
        unkeyed = {(a.trip_id, a.stop_id, None) for a in invalid
                   if isinstance(a.trip_id, str) and isinstance(a.stop_id, str)}
        keyed: dict[tuple, Arrival] = {}
        failed: list[Arrival] = []
        for a in valid:
            ck = (a.trip_id, a.stop_id, a.scheduled)
            hit = self._seq.get(ck)
            seq = hit[0] if hit else resolved.get(ck)
            if seq is None:
                failed.append(a)
                continue
            self._mark("_seq", ck)
            self._seq[ck] = (seq, now)  # also refreshes last-used
            keyed[(a.trip_id, a.stop_id, seq)] = a
        for a in failed:  # second pass: only now do we know which live keys are claimed
            seq = self._live_fallback(route, a, set(keyed))
            if seq is None:
                skipped += 1
                unkeyed.add((a.trip_id, a.stop_id, a.scheduled))
            else:
                keyed[(a.trip_id, a.stop_id, seq)] = a
        plan = []
        for key, a in keyed.items():
            seen.add(key)
            if key in self._finalised:
                continue
            try:  # pure phase: one odd arrival must not abort the tick
                old = self._live.get(key)
                st = copy.copy(old) if old else _State(route, a.scheduled, (), a.due_min, None, False, False)
                sample = (a.due_min, a.expected, int(a.realtime))
                write = sample != st.last_sample
                st.last_sample = sample
                st.gone_at = None
                st.last_due = a.due_min
                st.ever_realtime |= a.realtime
                if a.due_min <= FINALISE_DUE_MAX:
                    st.actual = a.expected
                if a.due_min <= 0:
                    st.saw_zero = True
                row = self._row(key, st) if a.due_min <= 0 else None
            except Exception:
                skipped += 1
                unkeyed.add((*key[:2], None))
                continue
            plan.append((key, st, sample if write else None, row))
        for key, st, sample, row in plan:  # write phase: errors here are tick-level and roll back
            self._mark("_live", key)
            if sample is not None:
                samples.execute("insert into sample values(?,?,?,?,?,?,?)", (now, *key[:2], key[2], *sample))
                written += 1
            if row is None:
                self._live[key] = st
            else:
                self._live.pop(key, None)
                finalised += self._insert(key, row, now)
        for key in [k for k, s in self._live.items()
                    if s.route == route and s.gone_at is None and k not in seen and not self._shielded(k, s, unkeyed)]:
            self._mark("_live", key)
            st = copy.copy(self._live[key])
            if st.last_due <= FINALISE_DUE_MAX:
                del self._live[key]
                finalised += self._insert(key, self._row(key, st), now)
            else:
                st.gone_at = now
                self._live[key] = st
                dropped += 1
        return IngestResult(written, finalised, dropped, skipped)
