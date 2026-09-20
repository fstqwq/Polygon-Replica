import re

from app.impl.runtime.dependency import runtime
from app.service.verification.types import WorkspaceVerificationRow


_IDENTITY_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")


def _normalize_identity_token(raw: str | None) -> str:
    if raw is None:
        return ""
    token = raw.strip()
    if not token or _IDENTITY_TOKEN_RE.fullmatch(token) is None:
        return ""
    return token


def normalize_run_id_token(raw: str | None) -> str:
    return _normalize_identity_token(raw)


def normalize_program_id_token(raw: str | None) -> str:
    return _normalize_identity_token(raw)


def latest_workspace_verification(
    problem_id: int,
    workspace_id: int,
    *,
    ok_only: bool = False,
) -> WorkspaceVerificationRow | None:
    return runtime().verification_service.latest_workspace_verification(
        int(problem_id),
        int(workspace_id),
        ok_only=bool(ok_only),
    )
