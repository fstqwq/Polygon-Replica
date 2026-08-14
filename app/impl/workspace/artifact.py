from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.impl.runtime.dependency import runtime
from app.service.platform.process import is_canonical_artifact_id
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.platform.workspace_path import contains_symlink_component
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


def artifact_root(problem: str, artifact_id: str) -> Path:
    if not is_canonical_artifact_id(artifact_id):
        raise HTTPException(status_code=404, detail="artifact not found")
    problem_slug = problem
    if not problem_slug:
        raise HTTPException(status_code=404, detail="artifact not found")
    problem_id = runtime().workspace_service.known_problem_id(problem_slug)
    if problem_id is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    artifact_path = runtime().verification_service.artifact_path_for_problem_artifact(problem_id, artifact_id)
    if not artifact_path:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        base = runtime().storage_layout.cache_artifacts_root.resolve()
        root = Path(artifact_path).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="artifact not found")
    if root != base and base not in root.parents:
        raise HTTPException(status_code=404, detail="artifact not found")
    if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
        raise HTTPException(status_code=404, detail="artifact not found")
    return root


def safe_artifact_path(problem: str, verification_id: str, rel: str) -> Path:
    root = artifact_root(problem, verification_id)
    candidate = root / rel
    path = candidate.resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if contains_symlink_component(root, candidate):
        raise HTTPException(status_code=404, detail="artifact file not found")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found")
    return path


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


def browser_file_response(file_path: Path) -> FileResponse:
    headers = {"X-Content-Type-Options": "nosniff"}
    if file_path.suffix.lower() == ".pdf":
        return FileResponse(
            file_path,
            filename=file_path.name,
            media_type="application/pdf",
            content_disposition_type="inline",
            headers=headers,
        )
    text_like_suffixes = {".log", ".txt", ".tex", ".json", ".md", ".csv", ".xml", ".yaml", ".yml", ".in", ".out", ".ans"}
    suffix = file_path.suffix.lower()
    if suffix in text_like_suffixes:
        return FileResponse(file_path, filename=file_path.name, media_type="text/plain; charset=utf-8", headers=headers)
    return FileResponse(file_path, filename=file_path.name, headers=headers)


def workspace_verification_id_for_run(ctx: dict, run_id: str) -> str:
    return runtime().verification_service.workspace_verification_id_for_run(
        int(ctx["problem"]["id"]),
        int(ctx["workspace"]["id"]),
        run_id,
    )


def assert_workspace_artifact_access(ctx: dict, artifact_id: str) -> None:
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    if str(artifact_id or "").startswith("p-"):
        if runtime().verification_service.workspace_artifact_exists(
            problem_id,
            workspace_id,
            artifact_id,
        ):
            return
        raise HTTPException(status_code=404, detail="artifact not found in workspace")
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
