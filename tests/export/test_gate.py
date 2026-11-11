import threading

from app.export.gate import ExportGate


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_gate_allows_one_holder():
    g = ExportGate()
    tok = g.try_acquire()
    assert tok is not None
    assert g.try_acquire() is None
    g.release(object())  # unknown token: ignored
    assert g.try_acquire() is None
    g.release(tok)
    g.release(tok)  # idempotent
    tok2 = g.try_acquire()
    assert tok2 is not None
    g.release(tok)  # stale: does not free tok2
    assert g.try_acquire() is None


def test_gate_lease_expires():
    clk = Clock()
    g = ExportGate(lease_s=100, clock=clk)
    old = g.try_acquire()
    clk.t = 99
    assert g.try_acquire() is None
    clk.t = 100
    new = g.try_acquire()
    assert new is not None and new is not old
    g.release(old)
    assert g.try_acquire() is None
    g.release(new)
    assert g.try_acquire() is not None


def test_gate_thread_contention_exactly_one_wins():
    g = ExportGate()
    n = 32
    barrier = threading.Barrier(n)
    results = []

    def run():
        barrier.wait()
        results.append(g.try_acquire())

    ts = [threading.Thread(target=run) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len([r for r in results if r is not None]) == 1
