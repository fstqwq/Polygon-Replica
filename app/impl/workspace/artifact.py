from __future__ import annotations
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.impl.runtime.config import config
from app.main_util import contains_symlink_component
from app.service.platform.process import is_canonical_artifact_id
from app.service.verification.store import list_verification_rows, verification_run_ids

from .revision import git_commit_count


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
    problem_row = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
    if problem_row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    problem_id = int(problem_row["id"])
    row = None
    if artifact_id.startswith("p-"):
        row = config.db.fetch_one(
            "SELECT artifact_path FROM previews WHERE id=? AND problem_id=?",
            [artifact_id, problem_id],
        )
    else:
        row = config.db.fetch_one(
            """
            SELECT artifact_path FROM (
                SELECT artifact_path
                FROM verifications
                WHERE id=? AND problem_id=?
                UNION ALL
                SELECT artifact_path
                FROM previews
                WHERE id=? AND problem_id=?
            )
            LIMIT 1
            """,
            [artifact_id, problem_id, artifact_id, problem_id],
        )
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    artifact_path = row["artifact_path"]
    if not artifact_path:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        base = config.settings.artifacts_root.resolve()
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
    row = config.db.fetch_one(
        """
        SELECT source_commit
        FROM exports
        WHERE problem_id=? AND workspace_id=? AND verification_id=? AND filename=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [ctx["problem"]["id"], ctx["workspace"]["id"], verification_id, archive_name],
    )
    if row is None:
        return None
    source_commit = row["source_commit"]
    revision = git_commit_count(Path(ctx["workspace"]["path"]), source_commit) if source_commit else None
    revision_display = f"v{revision}" if revision is not None and revision >= 0 else "v?"
    problem_slug = ctx["problem"]["slug"]
    if not problem_slug:
        return None
    return f"{problem_slug}-{revision_display}.zip"


def workspace_verification_id_for_run(ctx: dict, run_id: str) -> str:
    from .context_operation import parse_summary_json

    if not run_id:
        return ""
    verification_rows = list_verification_rows(
        config.db,
        problem_id=int(ctx["problem"]["id"]),
        workspace_id=int(ctx["workspace"]["id"]),
        limit=512,
    )
    verification_id = ""
    for row in verification_rows:
        summary = parse_summary_json(row.get("summary_json"), f"artifact/{run_id}")
        if run_id not in verification_run_ids(summary):
            continue
        verification_id = row.get("id") or ""
        if verification_id:
            break
    return verification_id


def workspace_run_artifact_root(ctx: dict, run_id: str) -> Path:
    verification_id = workspace_verification_id_for_run(ctx, run_id)
    if verification_id:
        try:
            root = config.fs_manager.resolve_verification_run_root(verification_id, run_id).resolve()
        except Exception:
            raise HTTPException(status_code=404, detail="run artifact directory not found")
        if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
            raise HTTPException(status_code=404, detail="run artifact directory not found")
        return root
    raise HTTPException(status_code=404, detail="run not found in workspace")


def safe_run_artifact_path(ctx: dict, run_id: str, rel: str) -> Path:
    root = workspace_run_artifact_root(ctx, run_id)
    norm_rel = rel.lstrip("/")
    candidate = root / norm_rel
    path = candidate.resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail="invalid run artifact path")
    if contains_symlink_component(root, candidate):
        raise HTTPException(status_code=404, detail="run artifact file not found")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="run artifact file not found")
    return path


def assert_workspace_verification_access(ctx: dict, verification_id: str) -> None:
    row = config.db.fetch_one(
        "SELECT id FROM verifications WHERE id=? AND problem_id=? AND workspace_id=? AND kind='verification'",
        [verification_id, ctx["problem"]["id"], ctx["workspace"]["id"]],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="verification not found in workspace")


def assert_workspace_artifact_access(ctx: dict, artifact_id: str) -> None:
    row = None
    if artifact_id.startswith("p-"):
        row = config.db.fetch_one(
            "SELECT id FROM previews WHERE id=? AND problem_id=? AND workspace_id=?",
            [artifact_id, ctx["problem"]["id"], ctx["workspace"]["id"]],
        )
    else:
        row = config.db.fetch_one(
            """
            SELECT id FROM (
                SELECT id
                FROM verifications
                WHERE id=? AND problem_id=? AND workspace_id=?
                UNION ALL
                SELECT id
                FROM previews
                WHERE id=? AND problem_id=? AND workspace_id=?
            )
            LIMIT 1
            """,
            [artifact_id, ctx["problem"]["id"], ctx["workspace"]["id"], artifact_id, ctx["problem"]["id"], ctx["workspace"]["id"]],
        )
    if row is not None:
        return
    raise HTTPException(status_code=404, detail="artifact not found in workspace")
