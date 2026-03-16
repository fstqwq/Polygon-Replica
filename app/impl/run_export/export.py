from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import TypedDict, cast

from fastapi import Form, Request

from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.artifact import git_commit_count
from app.impl.workspace.context_job import page_ctx, start_export_job
from app.impl.workspace.context_operation import audit
from app.impl.workspace.context_verification import latest_workspace_committed_stage_verification
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
    return {
        "status": "unknown" if status is None else status,
        "export_type": "icpc" if export_type is None else export_type,
        "source_commit": "" if source_commit is None else source_commit,
        "verification_id": "" if verification_id is None else verification_id,
        "filename": "" if filename is None else filename.strip(),
        "error": "" if error is None else error.strip(),
    }


def _parse_verification_summary(raw: str | None) -> dict[str, object]:
    if raw is None:
        return {}
    text = raw.strip()
    if not text:
        return {}
    try:
        return cast(dict[str, object], json.loads(text))
    except Exception:
        return {}


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
    rows = config.db.fetch_all(
        """
        SELECT created_at,details_json
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='export.create'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(actor_user_id), cap],
    )
    result: list[dict[str, object]] = []
    resolved_commit_keys: set[tuple[str, str]] = set()
    for row in rows:
        item = dict(row)
        details = _parse_export_audit_details(cast(str | None, item.get("details_json")))
        status = details["status"]
        export_type = details["export_type"]
        source_commit = details["source_commit"]
        commit_key = (export_type, source_commit) if source_commit else ("", "")
        if status == "running" and source_commit and commit_key in resolved_commit_keys:
            continue
        verification_id = details["verification_id"]
        if (not verification_id) and status == "running" and source_commit:
            verification_row = config.db.fetch_one(
                """
                SELECT id
                FROM verifications
                WHERE problem_id=? AND source_commit=? AND kind='verification'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [int(problem_id), source_commit],
            )
            if verification_row is not None:
                verification_id = verification_row["id"]
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
                "status_upper": status.upper(),
                "source_commit": source_commit,
                "source_commit_short": source_commit[:8] if source_commit else "-",
                "verification_id": verification_id or "-",
                "detail": detail,
                "running": status == "running",
                "verification_href": verification_href,
                "log_href": log_href,
            }
        )
        if status in {"ok", "failed"} and source_commit:
            resolved_commit_keys.add(commit_key)
    return result

def _build_validation_status(verification_row: dict[str, object] | None) -> str:
    if verification_row is None:
        return "validation unknown"
    status = cast(str | None, verification_row.get("status"))
    if status is None:
        status = ""
    summary = _parse_verification_summary(cast(str | None, verification_row.get("summary_json")))
    steps = cast(list[dict[str, object]] | None, summary.get("steps"))
    if steps is not None:
        for raw in steps:
            step_name = _parse_step_status(raw.get("step"))
            if step_name != "validate":
                continue
            step_status = _parse_step_status(raw.get("status"))
            if step_status == "ok":
                return "validation passed"
            if step_status in {"error", "failed"}:
                return "validation failed"
            break
    failed_step = _parse_step_status(summary.get("failed_step"))
    if failed_step == "validate":
        return "validation failed"
    if status == "ok":
        return "validation passed"
    return "validation unknown"

def _export_archive_summary(problem: str, verification_id: str, filename: str) -> dict[str, object]:
    result: dict[str, object] = {
        "available": False,
        "has_pdf": False,
        "solutions_total": None,
        "solutions_correct": None,
        "tests_total": None,
    }
    archive_name = Path(filename.strip()).name
    if not verification_id or not archive_name:
        return result
    archive_path = _resolve_export_archive_path(problem, verification_id, archive_name)
    if archive_path is None:
        return result
    if not archive_path.exists() or not archive_path.is_file() or archive_path.is_symlink():
        return result
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = [name for name in zf.namelist() if name and not name.endswith("/")]
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
    has_pdf = f"{package_root}/statement/problem.en.pdf" in names
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

def _resolve_export_archive_path(problem: str, verification_id: str, filename: str) -> Path | None:
    archive_name = Path(filename.strip()).name
    if (not verification_id) or (not archive_name):
        return None
    row = config.db.fetch_one("SELECT artifact_path FROM verifications WHERE id=?", [verification_id])
    if row is None:
        return None
    artifact_path = row["artifact_path"]
    if not artifact_path:
        return None
    try:
        root = Path(artifact_path).resolve()
        base = config.settings.artifacts_root.resolve()
    except Exception:
        return None
    if root != base and base not in root.parents:
        return None
    export_dir = (root / "export").resolve()
    if root != export_dir and root not in export_dir.parents:
        return None
    if (not export_dir.exists()) or (not export_dir.is_dir()) or export_dir.is_symlink():
        return None
    candidate = (export_dir / archive_name).resolve()
    if export_dir != candidate and export_dir not in candidate.parents:
        return None
    if (not candidate.exists()) or (not candidate.is_file()) or candidate.is_symlink():
        return None
    return candidate

def export_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx['workspace']['id']
    problem_id = int(ctx['problem']['id'])
    actor_user_id = int(ctx["user"]["id"])
    head_commit = ctx["workspace"].get("head_commit")
    if head_commit is None:
        head_commit = ""
    workspace = Path(ctx['workspace']['path'])
    generate_revision: int | None = git_commit_count(workspace, head_commit) if head_commit else None
    generate_revision_display = f'v{generate_revision}' if generate_revision is not None and generate_revision >= 0 else 'missing'
    active_build = latest_workspace_committed_stage_verification(problem_id, int(workspace_id), head_commit, ok_only=True)
    if not head_commit:
        build_note = 'no committed revision yet; commit changes before generating package'
    elif active_build is None:
        build_note = 'no committed verification for this revision; Generate will build from committed revision'
    else:
        build_note = 'committed revision artifacts are ready for export'
    exports_rows = config.db.fetch_all('\n        SELECT id,verification_id,export_type,filename,sha256,size_bytes,source_commit,created_at\n        FROM exports\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT 40\n        ', [ctx['problem']['id'], workspace_id])
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
        item['revision_display'] = f'v{revision}' if revision is not None and revision >= 0 else 'v?'
        stored_filename = cast(str | None, item.get("filename"))
        if stored_filename is None:
            stored_filename = ""
        else:
            stored_filename = Path(stored_filename.strip()).name
        problem_slug = cast(str, ctx["problem"]["slug"])
        fallback_stem = Path(problem_slug).name
        if not fallback_stem:
            fallback_stem = "problem"
        item['display_filename'] = stored_filename or f"{fallback_stem}-{item['revision_display']}.zip"
        verification_id = cast(str | None, item.get("verification_id"))
        if verification_id is None:
            verification_id = ""
        verification_meta = verification_meta_cache.get(verification_id)
        if verification_id and (verification_meta is None) and (verification_id not in verification_meta_cache):
            row_meta = config.db.fetch_one(
                "SELECT id,status,summary_json FROM verifications WHERE id=? AND problem_id=? AND workspace_id=? AND kind='verification'",
                [verification_id, problem_id, workspace_id],
            )
            verification_meta = dict(row_meta) if row_meta is not None else None
            verification_meta_cache[verification_id] = verification_meta
        validation_status = _build_validation_status(verification_meta)
        summary_bits: list[str] = [validation_status]
        summary_key = (verification_id, stored_filename)
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
        item["summary_display"] = f"{item['revision_display']} ({', '.join(summary_bits)})" if summary_bits else item["revision_display"]
        exports.append(item)
    export_events = _export_recent_events(
        problem_id,
        actor_user_id,
        problem_slug=ctx["problem"]["slug"],
        username=ctx["user"]["username"],
        limit=20,
    )
    return template_response(
        request,
        'export.html',
        {
            'ctx': ctx,
            'build_note': build_note,
            'generate_revision_display': generate_revision_display,
            'exports': exports,
            'export_events': export_events,
        },
    )

def export_create(problem: str, user: str, verification_id: str=Form(''), export_type: str=Form('icpc')):
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
    initial_details: dict[str, object] = {
        'status': 'running',
        'verification_id': verification_id,
        'export_type': requested_export_type,
        'source_commit': head_commit,
        'filename': '',
        'error': '',
    }
    try:
        if requested_export_type != 'icpc':
            raise ValueError('unsupported package type (ICPC only)')
        if not head_commit:
            raise ValueError('no committed revision; commit changes first')
        started = start_export_job(
            problem,
            user,
            actor_user_id=int(ctx['user']['id']),
            problem_id=problem_id,
            workspace_id=workspace_id,
            head_commit=head_commit,
            requested_verification_id=verification_id,
            requested_export_type=requested_export_type,
            initial_details=initial_details,
        )
        msg = 'package generation queued' if started else 'package generation already running for this revision'
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
    return redirect_response(f'/problems/{problem}/{user}/export', status_code=303, message=msg)
