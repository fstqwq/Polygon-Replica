"""Process-local admission boundary shared by requests and runtime work."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class MaintenanceAdmissionGate:
    """Serialize maintenance admission with request and task registration."""

    _EXEMPT_PREFIXES = ("/api/v4/",)
    _EXEMPT_PATHS = frozenset(
        {
            "/api/v4",
            "/maintenance",
            "/admin/maintenance/artifacts/cleanup",
            "/admin/maintenance/source-backup",
        }
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open = True
        self._active_requests = 0

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

    def is_exempt(self, path: str) -> bool:
        return path in self._EXEMPT_PATHS or any(
            path.startswith(prefix) for prefix in self._EXEMPT_PREFIXES
        )

    def enter_request(self) -> bool:
        with self._lock:
            if not self._open:
                return False
            self._active_requests += 1
            return True

    def leave_request(self) -> None:
        with self._lock:
            if self._active_requests <= 0:
                raise RuntimeError("maintenance request counter underflow")
            self._active_requests -= 1

    def active_requests_locked(self) -> int:
        return self._active_requests
