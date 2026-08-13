"""Process-local admission boundary shared by requests and runtime work."""

import threading
from contextlib import contextmanager
from typing import Iterator, Literal


AdmissionState = Literal["open", "draining", "closed"]


class MaintenanceAdmissionGate:
    """Serialize maintenance admission with request and task registration."""

    _EXEMPT_PREFIXES = ("/api/v4/",)
    _EXEMPT_PATHS = frozenset(
        {
            "/api/v4",
            "/maintenance",
            "/admin/maintenance/artifacts/cleanup",
            "/admin/maintenance/source-backup",
            "/admin/maintenance/admission",
            "/admin/maintenance/restart",
        }
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: AdmissionState = "open"
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
        return self._state == "open"

    def allows_runtime_work_locked(self) -> bool:
        """Allow work already admitted before a drain to finish."""

        return self._state != "closed"

    def is_draining_locked(self) -> bool:
        return self._state == "draining"

    def state_locked(self) -> AdmissionState:
        return self._state

    def close_locked(self) -> None:
        self._state = "closed"

    def drain_locked(self) -> None:
        if self._state == "closed":
            raise RuntimeError("maintenance operation is already running")
        self._state = "draining"

    def open_locked(self) -> None:
        self._state = "open"

    def restore_locked(self, state: AdmissionState) -> None:
        self._state = state

    def is_open(self) -> bool:
        with self._lock:
            return self._state == "open"

    def state(self) -> AdmissionState:
        with self._lock:
            return self._state

    def is_exempt(self, path: str) -> bool:
        return path in self._EXEMPT_PATHS or any(
            path.startswith(prefix) for prefix in self._EXEMPT_PREFIXES
        )

    def is_drain_control(self, path: str, method: str) -> bool:
        if method not in {"GET", "HEAD"}:
            return False
        return (
            path == "/admin"
            or path.startswith("/admin/")
            or path == "/favicon.ico"
            or path.startswith("/static/")
        )

    def enter_request(self) -> bool:
        with self._lock:
            if self._state != "open":
                return False
            self._active_requests += 1
            return True

    def enter_control_request(self) -> tuple[bool, bool]:
        """Admit Admin reads while draining and retain exclusivity accounting."""

        with self._lock:
            if self._state == "closed":
                return False, False
            self._active_requests += 1
            return True, True

    def leave_request(self) -> None:
        with self._lock:
            if self._active_requests <= 0:
                raise RuntimeError("maintenance request counter underflow")
            self._active_requests -= 1

    def active_requests_locked(self) -> int:
        return self._active_requests
