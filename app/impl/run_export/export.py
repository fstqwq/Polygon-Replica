from __future__ import annotations
from app.impl.auth.session import require_session_user

import json
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, cast

from fastapi import Form, Request, Depends
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.service.repository.revision import git_commit_count, verification_source_display
from app.impl.workspace.context_job import start_export_job
from app.impl.workspace.context_job_helper import allocate_verification_id
from app.impl.workspace.context_ui import page_ctx
from app.impl.run_export.query import (
    _verification_runtime_progress,
    _count_label,
    _verification_href,
)
from app.service.verification.validation_status import build_validation_status


def _export_type_display(export_type: str) -> str:
    if export_type == "icpc":
        return "ICPC"
    if export_type == "native":
        return "Native"
    return export_type or "-"


def _source_revision_display(workspace: Path, source_commit: str, revision_cache: dict[str, int | None]) -> str:
    return verification_source_display(workspace, source_commit, revision_cache)


def _export_archive_summary(
    problem: str,
    problem_id: int,
    workspace_id: int,
    export_id: str,
    filename: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "available": False,
        "has_pdf": False,
        "solutions_total": None,
        "solutions_correct": None,
        "tests_total": None,
    }
    archive_name = Path(filename.strip()).name
    if not export_id or not archive_name:
        return result
    archive_path = _resolve_export_archive_path(
        problem,
        problem_id,
        workspace_id,
        export_id,
        archive_name,
    )
    if archive_path is None:
        return result
    if not archive_path.exists() or not archive_path.is_file() or archive_path.is_symlink():
        return result
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = [name for name in zf.namelist() if name and not name.endswith("/")]
            native_anchor = next((name for name in names if name.endswith("/config/problem.json")), "")
            if native_anchor:
                package_root = native_anchor[: -len("config/problem.json")]
                solutions_total = sum(
                    1
                    for name in names
                    if name.startswith(f"{package_root}solutions/") and Path(name).suffix.lower() in {".cpp", ".cc", ".cxx", ".c", ".py", ".java", ".kt", ".go", ".rs", ".pas"}
                )
                tests_total = 0
                tests_spec_name = f"{package_root}tests/spec.json"
                if tests_spec_name in names:
                    try:
                        tests_payload = json.loads(zf.read(tests_spec_name).decode("utf-8", errors="replace"))
                        tests_total = len(cast(list[object], tests_payload.get("tests") or [])) if isinstance(tests_payload, dict) else 0
                    except Exception:
                        tests_total = 0
                result["available"] = True
                result["has_pdf"] = False
                result["solutions_total"] = int(solutions_total)
                result["solutions_correct"] = None
                result["tests_total"] = int(tests_total)
                return result
    except Exception:
        return result
    if not names:
        return result
    if "problem.yaml" in names:
        prefix = ""
    else:
        package_root = ""
        for name in names:
            if name.endswith("/problem.yaml"):
                package_root = name.split("/", 1)[0]
                break
        if not package_root:
            package_root = names[0].split("/", 1)[0]
        if not package_root:
            return result
        prefix = f"{package_root}/"
    if prefix and (not any(name.startswith(prefix) for name in names)):
        return result
    has_pdf = any(
        name.startswith(f"{prefix}problem_statement/problem.") and name.endswith(".pdf")
        for name in names
    )
    solutions_total = 0
    solutions_correct = 0
    tests_total = 0
    for name in names:
        if prefix and not name.startswith(prefix):
            continue
        if name.startswith(f"{prefix}submissions/"):
            solutions_total += 1
            if name.startswith(f"{prefix}submissions/accepted/"):
                solutions_correct += 1
        if (
            name.startswith(f"{prefix}data/secret/")
            or name.startswith(f"{prefix}data/sample/")
        ) and name.endswith(".in"):
            tests_total += 1
    result["available"] = True
    result["has_pdf"] = bool(has_pdf)
    result["solutions_total"] = int(solutions_total)
    result["solutions_correct"] = int(solutions_correct)
    result["tests_total"] = int(tests_total)
    return result

def _resolve_export_archive_path(
    problem: str,
    problem_id: int,
    workspace_id: int,
    export_id: str,
    filename: str,
) -> Path | None:
    archive_name = Path(filename.strip()).name
    if (not export_id) or (not archive_name):
        return None
    return config.export_service.export_archive_path(
        problem_id,
        workspace_id,
        export_id,
        problem,
        archive_name,
    )

def export_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace_id = ctx['workspace']['id']
    problem_id = int(ctx['problem']['id'])
    actor_user_id = int(ctx["user"]["id"])
    head_commit = ctx["workspace"].get("head_commit")
    if head_commit is None:
        head_commit = ""
    workspace = Path(ctx['workspace']['path'])
    generate_revision: int | None = git_commit_count(workspace, head_commit) if head_commit else None
    icpc_revision_display = f'v{generate_revision}' if generate_revision is not None and generate_revision >= 0 else 'missing'
    icpc_option_label = (
        f'ICPC (committed revision {icpc_revision_display})'
        if head_commit
        else 'ICPC (requires committed revision)'
    )
    native_option_label = (
        f'Native (committed revision {icpc_revision_display})'
        if head_commit
        else 'Native (requires committed revision)'
    )
    job_rows = config.export_service.workspace_export_jobs(
        problem_id,
        int(workspace_id),
        actor_user_id,
        limit=40,
    )
    revision_cache: dict[str, int | None] = {}
    verification_meta_cache: dict[str, dict[str, object] | None] = {}
    archive_summary_cache: dict[tuple[str, str], dict[str, object]] = {}
    activity_rows: list[dict[str, object]] = []
    for row in job_rows:
        item = dict(row)
        source_commit = cast(str, item["source_commit"])
        source_display = _source_revision_display(workspace, source_commit, revision_cache)
        stored_filename = Path(cast(str, item["filename"])).name if item["filename"] else ""
        export_id = cast(str, item["export_id"])
        status = cast(str, item["status"])
        error_text = cast(str, item["error"])
        verification_id = cast(str, item["verification_id"])
        problem_slug = cast(str, ctx["problem"]["slug"])
        fallback_stem = Path(problem_slug).name
        if not fallback_stem:
            fallback_stem = "problem"
        native_export = cast(str, item["export_type"]) == "native"
        if native_export and source_display == "Workspace":
            display_filename = stored_filename or f"{fallback_stem}.zip"
        elif native_export:
            display_filename = stored_filename or f"{fallback_stem}-native-{source_display}.zip"
        else:
            display_filename = stored_filename or f"{fallback_stem}-{source_display}.zip"
        verification_meta = verification_meta_cache.get(verification_id)
        if verification_id and (verification_meta is None) and (verification_id not in verification_meta_cache):
            verification_meta = config.verification_service.workspace_verification_detail(
                int(problem_id),
                int(workspace_id),
                verification_id,
            )
            verification_meta_cache[verification_id] = verification_meta
        runtime_progress = _verification_runtime_progress(
            problem_id=problem_id,
            problem_slug=problem_slug,
            username=cast(str, ctx["user"]["username"]),
            verification_id=verification_id,
            event_status=status,
        )
        verification_href = _verification_href(
            problem_id=problem_id,
            problem_slug=problem_slug,
            username=cast(str, ctx["user"]["username"]),
            verification_id=verification_id,
        )
        detail = error_text or status
        open_href = verification_href
        open_label = "open"
        if status == "succeeded" and export_id and stored_filename:
            validation_status = build_validation_status(verification_meta)
            summary_bits: list[str] = [validation_status]
            summary_key = (export_id, stored_filename)
            archive_summary = archive_summary_cache.get(summary_key)
            if archive_summary is None:
                archive_summary = _export_archive_summary(
                    problem,
                    problem_id,
                    int(workspace_id),
                    export_id,
                    stored_filename,
                )
                archive_summary_cache[summary_key] = archive_summary
            tests_total = archive_summary.get("tests_total")
            if bool(archive_summary.get("available")):
                summary_bits.insert(0, "has pdf" if bool(archive_summary.get("has_pdf")) else "no pdf")
                solutions_total = archive_summary.get("solutions_total")
                solutions_correct = archive_summary.get("solutions_correct")
                if solutions_total is not None and solutions_correct is not None:
                    summary_bits.append(f"{_count_label(solutions_total, 'solution')} ({solutions_correct} correct)")
            if tests_total is not None:
                summary_bits.append(_count_label(tests_total, "test"))
            detail = f"{display_filename} ({', '.join(summary_bits)})" if summary_bits else display_filename
            open_href = f"/problems/{problem_slug}/exports/{export_id}/{stored_filename}"
            open_label = "zip"
        elif status == "succeeded":
            detail = "artifact unavailable"
        elif runtime_progress["detail"] and (
            status == "running"
            or not error_text
            or error_text.startswith("verification failed:")
        ):
            detail = runtime_progress["detail"]
        activity_rows.append(
            {
                "created_at": item["created_at"],
                "type_display": _export_type_display(cast(str, item["export_type"])),
                "source_display": source_display,
                "status": status,
                "detail": detail,
                "open_href": open_href,
                "open_label": open_label,
            }
        )
    return template_response(
        request,
        'export.html',
        {
            'ctx': ctx,
            'icpc_option_label': icpc_option_label,
            'native_option_label': native_option_label,
            'activity_rows': activity_rows,
        },
    )

def export_create(problem: str, user: Annotated[str, Depends(require_session_user)], verification_id: str=Form(''), export_type: str=Form('icpc')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    require_write_access(ctx)
    _ = verification_id
    requested_export_type = export_type.lower()
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    head_commit = cast(str | None, ctx["workspace"].get("head_commit"))
    if head_commit is None:
        head_commit = ""
    if not requested_export_type:
        requested_export_type = 'icpc'
    source_commit = head_commit
    export_verification_id = allocate_verification_id() if requested_export_type == "icpc" else ""
    export_job_id = f"exp-{uuid.uuid4().hex[:12]}"
    try:
        if requested_export_type not in {'icpc', 'native'}:
            raise ValueError('unsupported package type')
        if not head_commit:
            raise ValueError('no committed revision; commit changes first')
        started = start_export_job(
            problem,
            user,
            actor_user_id=int(ctx['user']['id']),
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=source_commit,
            requested_verification_id=export_verification_id,
            requested_export_type=requested_export_type,
            export_job_id=export_job_id,
        )
        msg = 'package generation queued' if started else 'package generation already running for this source'
    except ValueError as exc:
        msg = str(exc)
    except Exception as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/export', status_code=303, message=msg)


def export_snapshot(problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    require_write_access(ctx)
    archive = config.export_service.create_workspace_snapshot(
        problem,
        workspace_id=int(ctx["workspace"]["id"]),
    )
    archive_root = archive.parent
    return FileResponse(
        archive,
        filename=archive.name,
        media_type="application/zip",
        background=BackgroundTask(lambda: shutil.rmtree(archive_root, ignore_errors=True)),
    )
