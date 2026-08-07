"""Shared synchronization boundary for maintenance and new work admission."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class MaintenanceAdmissionGate:
    """Serialize maintenance admission with request and task registration."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open = True

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            yield

    @contextmanager
    def try_locked(self) -> Iterator[bool]:
        """Try the admission boundary without delaying judgehost polling."""

        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    def is_open_locked(self) -> bool:
        return self._open

    def close_locked(self) -> None:
        self._open = False

    def open_locked(self) -> None:
        self._open = True

    def is_open(self) -> bool:
        with self._lock:
            return self._open
