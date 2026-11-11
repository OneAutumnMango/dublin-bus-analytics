"""One-export-at-a-time gate with a lease so a leaked hold self-heals. Per process."""
import threading
import time
from typing import Callable


class ExportGate:
    def __init__(self, lease_s: float = 3600, clock: Callable[[], float] = time.monotonic):
        self._lease_s = lease_s
        self._clock = clock
        self._lock = threading.Lock()
        self._token: object | None = None
        self._expires = 0.0

    def try_acquire(self) -> object | None:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._expires:
                return None
            self._token = object()
            self._expires = now + self._lease_s
            return self._token

    def release(self, token: object) -> None:
        with self._lock:
            if token is self._token:
                self._token = None
