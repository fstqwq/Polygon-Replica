from __future__ import annotations

import re

from app.impl.runtime.config import config
from app.service.verification.types import Kind, WorkspaceVerificationRow


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
    return config.verification_service.latest_workspace_verification(
        int(problem_id),
        int(workspace_id),
        ok_only=bool(ok_only),
    )


def latest_workspace_signature_verification(
    problem_id: int,
    workspace_id: int,
    signature: str,
    *,
    ok_only: bool = False,
) -> WorkspaceVerificationRow | None:
    if not signature:
        return None
    rows = config.verification_service.workspace_verification_rows(
        int(problem_id),
        int(workspace_id),
        limit=40,
        kinds=(Kind.ALL.value, Kind.CUSTOM.value),
    )
    return next(
        (
            row
            for row in rows
            if row["signature"] == signature
            and (not ok_only or row["status"] == "ok")
        ),
        None,
    )


def latest_workspace_source_commit_verification(
    problem_id: int,
    workspace_id: int,
    source_commit: str,
    *,
    ok_only: bool = False,
) -> WorkspaceVerificationRow | None:
    if not source_commit:
        return None
    return config.verification_service.workspace_source_commit_verification(
        int(problem_id),
        int(workspace_id),
        source_commit,
        kinds=(Kind.ALL.value, Kind.CUSTOM.value),
        ok_only=bool(ok_only),
    )
