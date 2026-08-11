from __future__ import annotations

import secrets
from pathlib import Path

from app.service.judgehost.shared import _HOSTNAME_RE, _RUN_ID_RE

from app.service.judgehost.state import JudgehostState


class InvalidJudgehostHostname(RuntimeError):
    """Raised when a wire hostname cannot be used as a lease identity."""


def normalize_judgehost_hostname(hostname: str) -> str:
    token = str(hostname or "").strip()
    if not _HOSTNAME_RE.fullmatch(token):
        raise InvalidJudgehostHostname("invalid judgehost hostname")
    return token


class JudgehostCore:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    def __init__(self, state: JudgehostState) -> None:
        self._s = state

    def enabled(self) -> bool:
        return self._s.config_policy().enabled

    def auth_token_configured(self) -> bool:
        return bool(self._s.config_policy().api_token)

    def check_api_token(self, token: str) -> bool:
        expected = self._s.config_policy().api_token
        provided = str(token or "").strip()
        if not expected or not provided:
            return False
        return secrets.compare_digest(expected, provided)

    def api_username(self) -> str:
        return self._s.config_policy().api_username or "judgehost"

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
        return normalize_judgehost_hostname(hostname)

    def task_status_counts(self) -> dict[str, int]:
        return self._s.task_registry.status_counts()

    def task_by_id(self, task_id: str) -> dict[str, object] | None:
        return self._s.task_registry.get(task_id.strip())

    def task_payload(self, task_id: str) -> dict[str, object]:
        row = self.task_by_id(task_id)
        if row is None:
            return {}
        return dict(row["payload"])

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
