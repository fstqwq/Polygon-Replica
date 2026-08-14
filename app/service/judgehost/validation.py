import re
from pathlib import Path

from app.service.execution.identity import canonical_run_id

_SCHEDULING_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class InvalidJudgehostHostname(RuntimeError):
    """Raised when a wire hostname cannot be used as a lease identity."""


def normalize_judgehost_hostname(hostname: str) -> str:
    token = str(hostname or "").strip()
    if not _HOSTNAME_RE.fullmatch(token):
        raise InvalidJudgehostHostname("invalid judgehost hostname")
    return token


def normalize_run_id(run_id: str) -> str:
    try:
        return canonical_run_id(run_id)
    except ValueError as exc:
        raise RuntimeError("invalid run id for judgehost task") from exc


def normalize_verification_program_id(verification_program_id: str) -> str:
    token = str(verification_program_id or "").strip()
    if not _SCHEDULING_TOKEN_RE.fullmatch(token):
        raise RuntimeError("invalid verification program id")
    return token


def safe_workspace_source(workspace_root: Path, submission_path: str) -> Path:
    workspace_resolved = workspace_root.resolve()
    rel = str(submission_path or "").strip().replace("\\", "/")
    if not rel:
        raise RuntimeError("submission source path is required")
    candidate = (workspace_resolved / rel).resolve()
    if candidate == workspace_resolved or workspace_resolved not in candidate.parents:
        raise RuntimeError("submission source escapes workspace")
    if candidate.is_symlink() or not candidate.exists() or not candidate.is_file():
        raise RuntimeError("submission source does not exist")
    return candidate


def read_bounded_file(path: Path, *, max_bytes: int, label: str) -> bytes:
    size = int(path.stat().st_size)
    if size > max_bytes:
        raise RuntimeError(f"{label} exceeds payload limit: {path.name} ({size} bytes)")
    return path.read_bytes()
