from __future__ import annotations

import secrets
import time
from pathlib import Path

from app.db import now_iso
from app.runtime_value import RuntimeValues
from app.service.judgehost.shared import _HOSTNAME_RE, _RUN_ID_RE, task_status_counts

from .state import JudgehostState


class JudgehostCore:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    def __init__(self, state: JudgehostState) -> None:
        self._s = state

    def apply_runtime_values(self, constants: RuntimeValues) -> None:
        with self._s.lock:
            self._s.constants = constants
            self._s.enabled = bool(constants.JUDGEHOST_ENABLE)
            self._s.api_token = str(constants.JUDGEHOST_API_TOKEN or "").strip()
            self._s.api_username = str(getattr(constants, "JUDGEHOST_API_USERNAME", "judgehost") or "judgehost").strip()
            self._s.fetch_batch_size = max(1, min(128, int(constants.JUDGEHOST_FETCH_BATCH_SIZE)))
            self._s.lease_sec = max(5, min(86400, int(constants.JUDGEHOST_LEASE_SEC)))
            self._s.wait_timeout_sec = max(5, min(86400, int(constants.JUDGEHOST_WAIT_TIMEOUT_SEC)))
            self._s.wait_poll_sec = max(0.05, min(30.0, float(constants.JUDGEHOST_WAIT_POLL_SEC)))
            self._s.online_window_sec = max(5, min(86400, int(constants.JUDGEHOST_ONLINE_WINDOW_SEC)))
            self._s.max_source_bytes = max(1024, min(16 * 1024 * 1024, int(constants.JUDGEHOST_MAX_INLINE_SOURCE_BYTES)))
            self._s.max_tests_per_task = max(1, min(10000, int(constants.JUDGEHOST_MAX_TESTS_PER_TASK)))
            self._s.include_build_payload = bool(constants.JUDGEHOST_INCLUDE_BUILD_PAYLOAD)
            self._s.max_binary_payload_bytes = max(
                1024, min(128 * 1024 * 1024, int(constants.JUDGEHOST_MAX_BINARY_PAYLOAD_BYTES))
            )

    def enabled(self) -> bool:
        return bool(self._s.enabled)

    def auth_token_configured(self) -> bool:
        return bool(self._s.api_token)

    def check_api_token(self, token: str) -> bool:
        expected = str(self._s.api_token or "").strip()
        provided = str(token or "").strip()
        if not expected or not provided:
            return False
        return secrets.compare_digest(expected, provided)

    def api_username(self) -> str:
        token = str(self._s.api_username or "").strip()
        return token or "judgehost"

    def check_api_basic(self, username: str, password: str) -> bool:
        expected_user = self.api_username()
        provided_user = str(username or "").strip()
        provided_pass = str(password or "").strip()
        if not provided_user or not provided_pass:
            return False
        if provided_user != expected_user:
            return False
        return self.check_api_token(provided_pass)

    def normalize_run_id(self, run_id: str) -> str:
        token = str(run_id or "").strip()
        if not _RUN_ID_RE.fullmatch(token):
            raise RuntimeError("invalid run id for judgehost task")
        return token

    def normalize_hostname(self, hostname: str) -> str:
        token = str(hostname or "").strip()
        if not _HOSTNAME_RE.fullmatch(token):
            return "judgehost"
        return token

    def task_status_counts(self) -> dict[str, int]:
        with self._s.state_lock:
            return task_status_counts(
                self._s.tasks_by_id,
                queued=self.STATUS_QUEUED,
                leased=self.STATUS_LEASED,
                completed=self.STATUS_COMPLETED,
                failed=self.STATUS_FAILED,
            )

    def task_by_id(self, task_id: str) -> dict[str, object] | None:
        with self._s.state_lock:
            row = self._s.tasks_by_id.get(task_id.strip())
            if row is None:
                return None
            return dict(row)

    def task_payload(self, task_id: str) -> dict[str, object]:
        row = self.task_by_id(task_id)
        if row is None:
            return {}
        return dict(row["payload"])

    def record_host_judging(self, hostname: str, *, label: str = "-", updated_at: str | None = None) -> None:
        safe_host = self.normalize_hostname(hostname)
        ts = time.time()
        now_text = now_iso() if updated_at is None else updated_at
        with self._s.state_lock:
            events = self._s.host_judged_case_events.get(safe_host)
            if events is None:
                events = []
                self._s.host_judged_case_events[safe_host] = events
            events.append(ts)
            cutoff = ts - (5 * 3600.0)
            while events and events[0] < cutoff:
                events.pop(0)
            self._s.host_last_judging[safe_host] = {"label": label, "updated_at": now_text}

    def bind_request_peer_hostname(self, peer_addr: str, hostname: str) -> None:
        safe_peer = str(peer_addr or "").strip()
        safe_host = self.normalize_hostname(hostname)
        if (not safe_peer) or (not safe_host):
            return
        with self._s.state_lock:
            self._s.peer_hostname_by_client_addr[safe_peer] = safe_host

    def hostname_for_request_peer(self, peer_addr: str) -> str:
        safe_peer = str(peer_addr or "").strip()
        if not safe_peer:
            return ""
        with self._s.state_lock:
            return str(self._s.peer_hostname_by_client_addr.get(safe_peer) or "")

    def safe_workspace_source(self, workspace_root: Path, submission_path: str) -> Path:
        workspace_resolved = workspace_root.resolve()
        rel = str(submission_path or "").strip().replace("\\", "/")
        if not rel:
            raise RuntimeError("submission source path is required")
        candidate = (workspace_resolved / rel).resolve()
        if candidate == workspace_resolved or workspace_resolved not in candidate.parents:
            raise RuntimeError("submission source escapes workspace")
        if candidate.is_symlink() or not candidate.exists() or (not candidate.is_file()):
            raise RuntimeError("submission source does not exist")
        return candidate

    def safe_read_bytes(self, path: Path, *, max_bytes: int, label: str) -> bytes:
        size = int(path.stat().st_size)
        if size > max_bytes:
            raise RuntimeError(f"{label} exceeds payload limit: {path.name} ({size} bytes)")
        return path.read_bytes()
