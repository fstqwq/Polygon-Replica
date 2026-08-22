from fastapi import HTTPException

from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_model import ProblemPageContext
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.verification.artifact import artifact_virtual_path


def artifact_version_number(artifact_id: str | None) -> int | None:
    raw = "" if artifact_id is None else artifact_id
    if not raw:
        return None
    tail = raw.rsplit("-", 1)[-1]
    if tail.isdigit():
        try:
            return int(tail)
        except Exception:
            return None
    return None


def verification_artifact_file(verification_id: str, rel: str) -> tuple[PayloadFile, str] | None:
    safe_verification_id = str(verification_id or "").strip()
    rel_norm = rel.lstrip("/")
    if not safe_verification_id or not rel_norm:
        return None
    artifact = runtime().verification_service.verification_artifact(
        safe_verification_id,
        rel_norm,
    )
    if artifact is None:
        return None
    return artifact.payload, artifact.filename


def verification_blob_virtual_rel(token: str, *, filename: str = "") -> str:
    safe_token = str(token or "").strip()
    if not safe_token:
        return ""
    return artifact_virtual_path(safe_token)


def workspace_verification_id_for_run(
    ctx: ProblemPageContext,
    run_id: str,
) -> str:
    return runtime().verification_service.workspace_verification_id_for_run(
        int(ctx["problem"]["id"]),
        int(ctx["workspace"]["id"]),
        run_id,
    )


def assert_workspace_artifact_access(
    ctx: ProblemPageContext,
    artifact_id: str,
) -> None:
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    verification_row = runtime().verification_service.verification_record(str(artifact_id or "").strip())
    if verification_row is None:
        raise HTTPException(status_code=404, detail="artifact not found in workspace")
    access = runtime().access_query.verification_context(
        actor_user_id=int(ctx["user"]["id"]),
        actor_workspace_id=workspace_id,
        expected_problem_id=problem_id,
        verification=verification_row,
    )
    if access["can_view"]:
        return
    raise HTTPException(status_code=404, detail="artifact not found in workspace")
