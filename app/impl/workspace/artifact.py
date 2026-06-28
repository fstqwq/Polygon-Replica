from __future__ import annotations
import base64
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.impl.runtime.config import config
from app.main_util import contains_symlink_component
from app.service.platform.process import is_canonical_artifact_id
from app.service.repository.revision import git_commit_count


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
    problem_id = config.workspace_service.known_problem_id(problem_slug)
    if problem_id is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    artifact_path = config.verification_service.artifact_path_for_problem_artifact(problem_id, artifact_id)
    if not artifact_path:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        base = config.fs_manager.cache_artifacts_root.resolve()
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


def verification_artifact_blob(verification_id: str, rel: str) -> tuple[bytes, str] | None:
    safe_verification_id = str(verification_id or "").strip()
    rel_norm = rel.lstrip("/")
    if not safe_verification_id or not rel_norm:
        return None
    parts = Path(rel_norm).parts
    if len(parts) == 3 and parts[0] == "blob":
        encoded = str(parts[1] or "").strip()
        filename = Path(parts[2]).name
        if (not encoded) or (not filename):
            return None
        padding = "=" * ((4 - (len(encoded) % 4)) % 4)
        try:
            token = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
        except Exception:
            return None
        if not token:
            return None
        if not config.verification_service.verification_has_artifact_token(safe_verification_id, token):
            return None
        blob = config.verification_service.resolve_artifact_blob(token)
        if blob is None:
            return None
        return (blob, filename)
    if len(parts) == 3 and parts[0] == "output":
        task_id = str(parts[1] or "").strip()
        filename = Path(parts[2]).name
        if (not task_id) or (not filename):
            return None
        task_output = config.verification_service.verification_task_output_ref(safe_verification_id, task_id)
        if task_output is None:
            return None
        test_name, ref = task_output
        expected_name = f"{Path(test_name).stem}.out" if Path(test_name).stem else "program.out"
        if filename != expected_name:
            return None
        if not ref:
            return None
        blob = config.verification_service.resolve_artifact_blob(ref)
        if blob is None:
            return None
        return (blob, filename)
    filename = Path(rel_norm).name
    if rel_norm == f"tests/{filename}" and filename:
        ref = config.verification_service.verification_artifact_ref(safe_verification_id, filename, "input_ref")
        if not ref:
            return None
        blob = config.verification_service.resolve_artifact_blob(ref)
        if blob is None:
            return None
        return (blob, filename)
    if rel_norm == f"ans/{filename}" and filename:
        stem = Path(filename).stem
        if not stem:
            return None
        test_name = f"{stem}.in"
        ref = config.verification_service.verification_artifact_ref(safe_verification_id, test_name, "answer_ref")
        if not ref:
            return None
        blob = config.verification_service.resolve_artifact_blob(ref)
        if blob is None:
            return None
        return (blob, filename)
    return None


def verification_blob_virtual_rel(token: str, *, filename: str = "") -> str:
    safe_token = str(token or "").strip()
    if not safe_token:
        return ""
    encoded = base64.urlsafe_b64encode(safe_token.encode("utf-8")).decode("ascii").rstrip("=")
    download_name = Path(filename).name or Path(safe_token).name or "artifact.bin"
    return f"blob/{encoded}/{download_name}"


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


def export_download_filename(ctx: dict, verification_id: str, stored_filename: str) -> str | None:
    archive_name = Path(stored_filename).name.strip()
    if not verification_id or not archive_name:
        return None
    source_commit = config.export_service.download_source_commit(
        int(ctx["problem"]["id"]),
        int(ctx["workspace"]["id"]),
        verification_id,
        archive_name,
    )
    if not source_commit:
        return None
    revision = git_commit_count(Path(ctx["workspace"]["path"]), source_commit) if source_commit else None
    revision_display = f"v{revision}" if revision is not None and revision >= 0 else "v?"
    problem_slug = ctx["problem"]["slug"]
    if not problem_slug:
        return None
    return f"{problem_slug}-{revision_display}.zip"


def workspace_verification_id_for_run(ctx: dict, run_id: str) -> str:
    return config.verification_service.workspace_verification_id_for_run(
        int(ctx["problem"]["id"]),
        int(ctx["workspace"]["id"]),
        run_id,
    )


def assert_workspace_artifact_access(ctx: dict, artifact_id: str) -> None:
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    if str(artifact_id or "").startswith("p-"):
        if config.verification_service.workspace_artifact_exists(
            problem_id,
            workspace_id,
            artifact_id,
        ):
            return
        raise HTTPException(status_code=404, detail="artifact not found in workspace")
    verification_row = config.verification_service.verification_record(str(artifact_id or "").strip())
    if (
        verification_row is not None
        and int(verification_row["problem_id"] or 0) == problem_id
        and int(verification_row["workspace_id"] or 0) == workspace_id
    ):
        return
    raise HTTPException(status_code=404, detail="artifact not found in workspace")
