from __future__ import annotations
from app.impl.auth.session import require_session_user

import json
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, TypedDict, cast

from fastapi import Form, Request, Depends
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.artifact import git_commit_count
from app.impl.workspace.context_job import start_export_job
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_operation import audit
from app.impl.run_export.query import (
    _verification_runtime_progress,
    _count_label,
    _verification_href,
)


class ExportAuditDetails(TypedDict):
    status: str
    export_type: str
    source_commit: str
    verification_id: str
    filename: str
    error: str
    export_task_id: str


def _export_type_display(export_type: str) -> str:
    if export_type == "icpc":
        return "ICPC"
    if export_type == "native":
        return "Native"
    return export_type or "-"


def _parse_export_audit_details(raw: str | None) -> ExportAuditDetails:
    details: dict[str, object] = {}
    if raw is not None:
        text = raw.strip()
        if text:
            try:
                details = cast(dict[str, object], json.loads(text))
            except Exception:
                details = {}
    status = cast(str | None, details.get("status"))
    export_type = cast(str | None, details.get("export_type"))
    source_commit = cast(str | None, details.get("source_commit"))
    verification_id = cast(str | None, details.get("verification_id"))
    filename = cast(str | None, details.get("filename"))
    error = cast(str | None, details.get("error"))
    export_task_id = cast(str | None, details.get("export_task_id"))
    return {
        "status": "unknown" if status is None else status,
        "export_type": "icpc" if export_type is None else export_type,
        "source_commit": "" if source_commit is None else source_commit,
        "verification_id": "" if verification_id is None else verification_id,
        "filename": "" if filename is None else filename.strip(),
        "error": "" if error is None else error.strip(),
        "export_task_id": "" if export_task_id is None else export_task_id.strip(),
    }


def _parse_step_status(raw: object) -> str:
    status = cast(str | None, raw)
    if status is None:
        return ""
    return status


def _export_recent_events(
    problem_id: int,
    actor_user_id: int,
    *,
    problem_slug: str,
    username: str,
    limit: int = 20,
) -> list[dict[str, object]]:
    cap = max(1, min(100, int(limit)))
    rows = config.export_service.export_audit_rows(int(problem_id), int(actor_user_id), limit=cap)
    result: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        details = _parse_export_audit_details(cast(str | None, item.get("details_json")))
        status = details["status"]
        source_commit = details["source_commit"]
        verification_id = details["verification_id"]
        filename = details["filename"]
        error_text = details["error"]
        detail = filename if filename else (error_text if error_text else "-")
        runtime_progress = _verification_runtime_progress(
            problem_id=int(problem_id),
            problem_slug=problem_slug,
            username=username,
            verification_id=verification_id,
            event_status=status,
        )
        runtime_detail = runtime_progress["detail"]
        log_href = runtime_progress["log_href"]
        if runtime_detail:
            detail = runtime_detail
        verification_href = _verification_href(
            problem_id=int(problem_id),
            problem_slug=problem_slug,
            username=username,
            verification_id=verification_id,
        )
        result.append(
            {
                "created_at": item.get("created_at"),
                "status": status,
                "export_type": details["export_type"],
                "source_commit": source_commit,
                "source_display": "working tree" if details["export_type"] == "native" and not source_commit else (source_commit[:8] if source_commit else "-"),
                "verification_id": verification_id or "-",
                "export_task_id": details["export_task_id"],
                "filename": filename,
                "detail": detail,
                "running": status == "running",
                "verification_href": verification_href,
                "log_href": log_href,
            }
        )
    return result

def _build_validation_status(verification_row: dict[str, object] | None) -> str:
    if verification_row is None:
        return "validation unknown"
    status = cast(str | None, verification_row.get("status"))
    if status is None:
        status = ""
    details = dict(cast(dict[str, object], verification_row.get("details") or {}))
    sanity_status = _parse_step_status(details.get("sanity_status"))
    if sanity_status == "passed":
        return "validation passed"
    if sanity_status == "failed":
        return "validation failed"
    if sanity_status == "unknown":
        return "validation unknown"
    validation_status = _parse_step_status(details.get("validation_status"))
    if validation_status == "passed":
        return "validation passed"
    if validation_status == "failed":
        return "validation failed"
    if validation_status == "unknown":
        return "validation unknown"
    failed_step = _parse_step_status(details.get("failed_step"))
    if failed_step in {"validate", "sanity"}:
        return "validation failed"
    if status == "ok":
        return "validation passed"
    return "validation unknown"


def _resolve_export_verification_id(
    *,
    problem_id: int,
    workspace_id: int,
    verification_id: str,
    source_commit: str,
) -> str:
    safe_verification_id = str(verification_id or "")
    if safe_verification_id:
        return safe_verification_id
    _ = problem_id
    _ = workspace_id
    _ = source_commit
    return ""

def _export_archive_summary(problem: str, export_id: str, filename: str) -> dict[str, object]:
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
    archive_path = _resolve_export_archive_path(problem, export_id, archive_name)
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
    has_pdf = any(
        name.startswith(f"{package_root}/statement/problem.") and name.endswith(".pdf")
        for name in names
    )
    solutions_total = 0
    solutions_correct = 0
    tests_total = 0
    for name in names:
        if not name.startswith(prefix):
            continue
        if name.startswith(f"{package_root}/submissions/"):
            solutions_total += 1
            if name.startswith(f"{package_root}/submissions/accepted/"):
                solutions_correct += 1
        if name.startswith(f"{package_root}/data/secret/") and name.endswith(".in"):
            tests_total += 1
    result["available"] = True
    result["has_pdf"] = bool(has_pdf)
    result["solutions_total"] = int(solutions_total)
    result["solutions_correct"] = int(solutions_correct)
    result["tests_total"] = int(tests_total)
    return result

def _resolve_export_archive_path(problem: str, export_id: str, filename: str) -> Path | None:
    archive_name = Path(filename.strip()).name
    if (not export_id) or (not archive_name):
        return None
    problem_id = config.workspace_service.known_problem_id(problem)
    if problem_id is None:
        return None
    owner = problem.split("/", 1)[0]
    workspace_ctx = config.workspace_service.workspace_context(problem, owner, include_recent=False)
    workspace_id = int(workspace_ctx["workspace"]["id"])
    return config.export_service.export_archive_path(
        int(problem_id),
        int(workspace_id),
        export_id,
        problem,
        archive_name,
    )

def export_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(problem, user)
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
    exports_rows = config.export_service.workspace_exports(int(ctx['problem']['id']), int(workspace_id), limit=40)
    revision_cache: dict[str, int | None] = {}
    verification_meta_cache: dict[str, dict[str, object] | None] = {}
    archive_summary_cache: dict[tuple[str, str], dict[str, object]] = {}
    exports: list[dict[str, object]] = []
    for row in exports_rows:
        item = dict(row)
        source_commit = cast(str | None, item.get("source_commit"))
        if source_commit is None:
            source_commit = ""
        revision = None
        if source_commit:
            if source_commit in revision_cache:
                revision = revision_cache[source_commit]
            else:
                revision = git_commit_count(workspace, source_commit)
                revision_cache[source_commit] = revision
        item['revision'] = revision
        if cast(str, item["export_type"]) == "native" and not source_commit:
            item['source_display'] = 'working tree'
        else:
            item['source_display'] = f'v{revision}' if revision is not None and revision >= 0 else 'v?'
        stored_filename = cast(str | None, item.get("filename"))
        if stored_filename is None:
            stored_filename = ""
        else:
            stored_filename = Path(stored_filename.strip()).name
        problem_slug = cast(str, ctx["problem"]["slug"])
        fallback_stem = Path(problem_slug).name
        if not fallback_stem:
            fallback_stem = "problem"
        native_export = cast(str, item["export_type"]) == "native"
        if native_export and item["source_display"] == "working tree":
            item['display_filename'] = stored_filename or f"{fallback_stem}.zip"
        elif native_export:
            item['display_filename'] = stored_filename or f"{fallback_stem}-native-{item['source_display']}.zip"
        else:
            item['display_filename'] = stored_filename or f"{fallback_stem}-{item['source_display']}.zip"
        verification_id = _resolve_export_verification_id(
            problem_id=problem_id,
            workspace_id=int(workspace_id),
            verification_id=cast(str | None, item.get("verification_id")) or "",
            source_commit=source_commit,
        )
        verification_meta = verification_meta_cache.get(verification_id)
        if verification_id and (verification_meta is None) and (verification_id not in verification_meta_cache):
            verification_meta = config.verification_service.workspace_verification_detail(
                int(problem_id),
                int(workspace_id),
                verification_id,
            )
            verification_meta_cache[verification_id] = verification_meta
        validation_status = _build_validation_status(verification_meta)
        summary_bits: list[str] = [validation_status]
        summary_key = (cast(str, item["id"]), stored_filename)
        archive_summary = archive_summary_cache.get(summary_key)
        if archive_summary is None:
            archive_summary = _export_archive_summary(problem, summary_key[0], summary_key[1])
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
        item["summary_display"] = f"{item['source_display']} ({', '.join(summary_bits)})" if summary_bits else item["source_display"]
        item["activity_detail"] = f"{item['display_filename']} ({', '.join(summary_bits)})" if summary_bits else item["display_filename"]
        exports.append(item)
    export_events = _export_recent_events(
        problem_id,
        actor_user_id,
        problem_slug=ctx["problem"]["slug"],
        username=ctx["user"]["username"],
        limit=20,
    )
    export_row_index: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in exports:
        export_row_index[(cast(str, item["export_type"]), cast(str, item["source_commit"]), cast(str, item["filename"]))] = item
    activity_rows: list[dict[str, object]] = []
    seen_task_ids: set[str] = set()
    for e in export_events:
        task_id = cast(str, e["export_task_id"])
        if task_id:
            if task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)
        status = cast(str, e["status"])
        export_row = export_row_index.get(
            (
                cast(str, e["export_type"]),
                cast(str, e["source_commit"]),
                cast(str, e["filename"]),
            )
        )
        if status == "ok" and export_row is not None:
            activity_rows.append(
                {
                    "created_at": export_row["created_at"],
                    "type_display": _export_type_display(cast(str, export_row["export_type"])),
                    "source_display": export_row["source_display"],
                    "status": "ok",
                    "detail": export_row["activity_detail"],
                    "open_href": f"/problems/{ctx['problem']['slug']}/exports/{export_row['id']}/{export_row['filename']}",
                    "open_label": "zip",
                }
            )
            continue
        activity_rows.append(
            {
                "created_at": e["created_at"],
                "type_display": _export_type_display(cast(str, e["export_type"])),
                "source_display": e["source_display"],
                "status": status,
                "detail": "running" if status == "running" else e["detail"],
                "open_href": e["verification_href"],
                "open_label": "open",
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
    requested_export_type = export_type.lower()
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    head_commit = cast(str | None, ctx["workspace"].get("head_commit"))
    if head_commit is None:
        head_commit = ""
    if not requested_export_type:
        requested_export_type = 'icpc'
    source_commit = head_commit
    export_task_id = f"exp-{uuid.uuid4().hex[:12]}"
    initial_details: dict[str, object] = {
        'status': 'running',
        'verification_id': verification_id,
        'export_type': requested_export_type,
        'source_commit': source_commit,
        'filename': '',
        'error': '',
        'export_task_id': export_task_id,
    }
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
            requested_verification_id=verification_id,
            requested_export_type=requested_export_type,
            export_task_id=export_task_id,
            initial_details=initial_details,
        )
        msg = 'package generation queued' if started else 'package generation already running for this source'
    except ValueError as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
        msg = str(exc)
    except Exception as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
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
