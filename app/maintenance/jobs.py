"""In-process background runner for multi-day removal. One job at a time; records live 600 s."""
import logging
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from app.maintenance import day

KEYS = ("arrivals", "forecasts", "samples", "gaps")
KEEP_S = 600

_mu = threading.Lock()
_jobs: dict[str, dict] = {}


class JobBusy(Exception):
    def __init__(self, job_id: str | None):
        super().__init__("a removal job is already running")
        self.job_id = job_id


def _zero() -> dict:
    return {k: 0 for k in KEYS}


def _prune() -> None:  # caller holds _mu
    now = time.monotonic()
    for k in [k for k, r in _jobs.items() if r["state"] != "running" and now - r["_end"] > KEEP_S]:
        del _jobs[k]


def running_id() -> str | None:
    with _mu:
        return next((k for k, r in _jobs.items() if r["state"] == "running"), None)


def get(job_id: str) -> dict | None:
    with _mu:
        _prune()
        r = _jobs.get(job_id)
        if r is None:
            return None
        return {**{k: v for k, v in r.items() if not k.startswith("_")},
                "done": dict(r["done"]), "total": dict(r["total"]), "skipped_months": list(r["skipped_months"])}


def start(data_dir, days: "list[date]", route: str | None, lock) -> str:
    with _mu:
        _prune()
        for k, r in _jobs.items():
            if r["state"] == "running":
                raise JobBusy(k)
        jid = uuid.uuid4().hex
        _jobs[jid] = {"job_id": jid, "state": "running", "stage": "starting", "day_index": 0,
                      "day_count": len(days), "current_day": None, "done": _zero(), "total": _zero(),
                      "skipped_months": [], "error": None, "_end": 0.0}
    try:
        threading.Thread(target=_run, args=(jid, Path(data_dir), list(days), route, lock), daemon=True).start()
    except BaseException:
        with _mu:
            del _jobs[jid]
        raise
    return jid


def _set(jid: str, **kw) -> None:
    with _mu:
        _jobs[jid].update(kw)


def _run(jid, data_dir, days, route, lock) -> None:
    base = _zero()  # sum of finished days
    final: dict = {"state": "error", "error": "Removal failed unexpectedly."}
    try:
        p = day.preview_range(data_dir, days, route)
        _set(jid, total={k: int(p[k]) for k in KEYS}, skipped_months=list(p["skipped_months"]))
        for i, d in enumerate(days):
            _set(jid, day_index=i, current_day=d.isoformat(), stage="starting")

            def cb(stage, out, base=base):
                live = {k: int(out[k]) for k in KEYS}
                _set(jid, stage=stage, done={k: base[k] + live[k] for k in KEYS})

            res = day.remove(data_dir, d, route, progress=cb)
            for k in KEYS:
                base[k] += int(res[k])
            _set(jid, done=dict(base), day_index=i + 1)
        final = {"state": "done", "stage": "done", "current_day": None, "done": dict(base)}
    except day.DayBusy as e:
        with _mu:
            cur = _jobs[jid]["current_day"]
        label = cur or "the first day"
        logging.getLogger(__name__).warning("day removal job %s stopped: %s %s on %s", jid, e.part, e.kind, label)
        what = "stayed locked" if e.kind == "busy" else "failed"
        final = {"state": "error", "error": f"Removal of {label} stopped: {e.part} {what}.",
                 "done": {k: base[k] + int(e.done.get(k, 0)) for k in KEYS}}
    except Exception:
        logging.getLogger(__name__).exception("day removal job %s failed", jid)
    finally:
        try:
            lock.release()
        except Exception:
            pass
        finally:
            with _mu:  # terminal state last, so a finished job never still holds the lock
                _jobs[jid].update(final, _end=time.monotonic())
