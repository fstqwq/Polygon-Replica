from __future__ import annotations
import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote_plus
from fastapi import File, Form, UploadFile
from fastapi.responses import FileResponse
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from app.db import now_iso
from app.impl.auth import (
    _redirect_response,
    _template_response,
)
from app.impl.config import config
from app.main_utils import (
    _normalize_optional_component_source_path,
    _normalize_optional_component_source_path_safe,
    upload_compile_check_error,
    workspace_source_compile_check_error,
)
from app.services.icpc_package_import_service import ICPCPackageImportService
from app.services.polygon_package_import_service import PolygonPackageImportService
from app.services.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior
from app.services.tests_spec import TESTS_SPEC_REL, load_tests_spec
from app.services.util import is_canonical_artifact_id, run_cmd

from app.impl.workspace import (
    _allocate_invocation_id,
    _allocate_run_id,
    _assert_workspace_artifact_access,
    _assert_workspace_build_access,
    _audit,
    _browser_file_response,
    _dedupe_preserve_order,
    _export_download_filename,
    _git_commit_count,
    _latest_workspace_build,
    _latest_workspace_committed_build,
    _normalize_run_id_token,
    _normalize_run_test_name_token,
    _normalize_problem_mode,
    _parse_run_detail_ids,
    _parse_run_detail_invocation_id,
    _parse_run_test_names,
    _read_problem_config,
    _record_async_run_failure,
    _require_write_access,
    _run_invocation_scope_run_ids,
    _run_list_rows,
    _run_source_labels_from_audit,
    _run_solution_options_context,
    _run_test_options_context,
    _safe_artifact_path,
    _safe_run_artifact_path,
    _start_export_job,
    _start_run_execute_batch,
    _build_run_detail_context,
    page_ctx,
)

_C = config.constants
_POLYGON_IMPORT_SERVICE = PolygonPackageImportService()
_ICPC_IMPORT_SERVICE = ICPCPackageImportService()
_POLYGON_LINUX_PACKAGE_SUFFIX_RE = re.compile(r"-\d+\$linux$", re.IGNORECASE)
_PROBLEM_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{safe_count} {token}"


def _summary_object(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _build_ref_for_build(problem_id: int, build_id: str) -> str:
    safe_build_id = str(build_id or "").strip()
    if (not safe_build_id) or (not is_canonical_artifact_id(safe_build_id)):
        return ""
    row = config.db.fetch_one(
        "SELECT build_ref FROM builds WHERE id=? AND problem_id=?",
        [safe_build_id, int(problem_id)],
    )
    if row is None:
        return ""
    return str(row["build_ref"] or "").strip().lower()


def _build_artifact_root(problem_id: int, build_id: str) -> Path | None:
    build_ref = _build_ref_for_build(problem_id, build_id)
    if not build_ref:
        return None
    try:
        root = config.fs_manager.build_paths(build_ref).root.resolve()
    except Exception:
        return None
    try:
        if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
            return None
    except OSError:
        return None
    return root


def _count_files_with_suffix(directory: Path, suffix: str) -> int:
    count = 0
    safe_suffix = str(suffix or "").lower()
    if not safe_suffix:
        return 0
    try:
        if (not directory.exists()) or (not directory.is_dir()) or directory.is_symlink():
            return 0
    except OSError:
        return 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = str(entry.name or "")
                if not name.lower().endswith(safe_suffix):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                count += 1
    except Exception:
        return 0
    return count


def _build_tests_total_from_artifacts(artifact_root: Path) -> int:
    logs_meta = artifact_root / "logs" / "tests_meta.json"
    try:
        if logs_meta.exists() and logs_meta.is_file() and (not logs_meta.is_symlink()):
            payload = json.loads(logs_meta.read_text(encoding="utf-8", errors="replace"))
            if isinstance(payload, list):
                return max(0, int(len(payload)))
    except Exception:
        pass
    tests_dir = artifact_root / "tests"
    return _count_files_with_suffix(tests_dir, ".in")


def _build_validated_count_from_log(validate_log: Path) -> int:
    try:
        if (not validate_log.exists()) or (not validate_log.is_file()) or validate_log.is_symlink():
            return 0
    except OSError:
        return 0
    seen: set[str] = set()
    try:
        with validate_log.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = str(raw or "").strip()
                if ": " not in line:
                    continue
                test_name, _rest = line.split(": ", 1)
                token = str(test_name or "").strip()
                if not token.lower().endswith(".in"):
                    continue
                seen.add(token)
    except Exception:
        return 0
    return max(0, int(len(seen)))


def _build_runtime_progress(
    *,
    problem_id: int,
    problem_slug: str,
    username: str,
    build_id: str,
    event_status: str,
) -> dict[str, str]:
    result = {
        "detail": "",
        "log_href": "",
    }
    safe_build_id = str(build_id or "").strip()
    if (not safe_build_id) or (not is_canonical_artifact_id(safe_build_id)):
        return result
    build_row = config.db.fetch_one(
        "SELECT status,summary_json FROM builds WHERE id=? AND problem_id=?",
        [safe_build_id, int(problem_id)],
    )
    build_status = str(build_row["status"] or "").strip().lower() if build_row is not None else ""
    build_summary = _summary_object(build_row["summary_json"] if build_row is not None else None)
    artifact_root = _build_artifact_root(int(problem_id), safe_build_id)
    if artifact_root is None:
        if event_status == "running":
            if build_status in {"queued", "pending"}:
                result["detail"] = "build queued"
            elif build_status == "running":
                result["detail"] = "build running"
            elif build_status == "ok":
                result["detail"] = "packaging export bundle"
        return result

    logs_dir = artifact_root / "logs"
    generate_log = logs_dir / "generate.log"
    validate_log = logs_dir / "validate.log"
    solve_log = logs_dir / "solve.log"
    failure_log = logs_dir / "failure.log"
    compile_log = logs_dir / "compile.log"
    tests_total = _build_tests_total_from_artifacts(artifact_root)
    outputs_generated = _count_files_with_suffix(artifact_root / "ans", ".ans")
    validated_count = _build_validated_count_from_log(validate_log)

    def _log_href(name: str) -> str:
        return f"/problems/{problem_slug}/{username}/artifacts/{safe_build_id}/logs/{name}"

    if event_status == "running":
        if build_status in {"queued", "pending"}:
            result["detail"] = "build queued"
            return result
        if build_status == "ok":
            result["detail"] = "packaging export bundle"
            return result
        if solve_log.exists() and solve_log.is_file() and (not solve_log.is_symlink()):
            if tests_total > 0:
                result["detail"] = f"generate outputs {min(outputs_generated, tests_total)}/{tests_total}"
            else:
                result["detail"] = "generate outputs running"
            result["log_href"] = _log_href("solve.log")
            return result
        if validate_log.exists() and validate_log.is_file() and (not validate_log.is_symlink()):
            if tests_total > 0:
                result["detail"] = f"validate inputs {min(validated_count, tests_total)}/{tests_total}"
            else:
                result["detail"] = "validate inputs running"
            result["log_href"] = _log_href("validate.log")
            return result
        if generate_log.exists() and generate_log.is_file() and (not generate_log.is_symlink()):
            if tests_total > 0:
                result["detail"] = f"generate inputs {tests_total} prepared"
            else:
                result["detail"] = "generate inputs running"
            result["log_href"] = _log_href("generate.log")
            return result
        if compile_log.exists() and compile_log.is_file() and (not compile_log.is_symlink()):
            result["detail"] = "compile running"
            result["log_href"] = _log_href("compile.log")
            return result
        result["detail"] = "build running"
        return result

    if event_status == "failed":
        detail = str(build_summary.get("error") or "").strip()
        if not detail:
            failed_step = str(build_summary.get("failed_step") or "").strip()
            failed_test = str(build_summary.get("failed_test") or "").strip()
            if failed_step and failed_test:
                detail = f"{failed_step} failed on {failed_test}"
            elif failed_step:
                detail = f"{failed_step} failed"
        if detail:
            result["detail"] = detail
        if failure_log.exists() and failure_log.is_file() and (not failure_log.is_symlink()):
            result["log_href"] = _log_href("failure.log")
    return result


def _verification_href_for_build(
    *,
    problem_id: int,
    problem_slug: str,
    username: str,
    build_id: str,
) -> str:
    safe_build_id = str(build_id or "").strip()
    if (not safe_build_id) or (not is_canonical_artifact_id(safe_build_id)):
        return ""
    rows = config.db.fetch_all(
        """
        SELECT id,summary_json
        FROM runs
        WHERE problem_id=? AND build_id=?
        ORDER BY created_at DESC
        LIMIT 80
        """,
        [int(problem_id), safe_build_id],
    )
    for row in rows:
        summary = _summary_object(row["summary_json"] if row is not None else None)
        invocation = summary.get("invocation")
        if not isinstance(invocation, dict):
            continue
        source = str(invocation.get("source") or "").strip().lower()
        if source not in {"verification.start", "sidebar"}:
            continue
        invocation_id = _normalize_run_id_token(str(invocation.get("id") or ""))
        if not invocation_id:
            continue
        return f"/problems/{problem_slug}/{username}/run/details?invocation_id={quote_plus(invocation_id)}"
    return ""


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
        details = _summary_object(item.get("details_json"))
        status = str(details.get("status") or "").strip().lower() or "unknown"
        export_type = str(details.get("export_type") or "icpc").strip().lower() or "icpc"
        source_commit = str(details.get("source_commit") or "").strip()
        commit_key = (export_type, source_commit) if source_commit else ("", "")
        if status == "running" and source_commit and commit_key in resolved_commit_keys:
            continue
        build_id = str(details.get("build_id") or "").strip()
        if (not build_id) and status == "running" and source_commit:
            build_row = config.db.fetch_one(
                """
                SELECT id
                FROM builds
                WHERE problem_id=? AND source_commit=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [int(problem_id), source_commit],
            )
            if build_row is not None:
                build_id = str(build_row["id"] or "").strip()
        filename = str(details.get("filename") or "").strip()
        error_text = str(details.get("error") or "").strip()
        detail = filename if filename else (error_text if error_text else "-")
        runtime_progress = _build_runtime_progress(
            problem_id=int(problem_id),
            problem_slug=str(problem_slug or "").strip(),
            username=str(username or "").strip(),
            build_id=build_id,
            event_status=status,
        )
        runtime_detail = str(runtime_progress.get("detail") or "").strip()
        log_href = str(runtime_progress.get("log_href") or "").strip()
        if runtime_detail:
            detail = runtime_detail
        verification_href = _verification_href_for_build(
            problem_id=int(problem_id),
            problem_slug=str(problem_slug or "").strip(),
            username=str(username or "").strip(),
            build_id=build_id,
        )
        result.append(
            {
                "created_at": item.get("created_at"),
                "status": status,
                "status_upper": status.upper(),
                "source_commit": source_commit,
                "source_commit_short": source_commit[:8] if source_commit else "-",
                "build_id": build_id or "-",
                "detail": detail,
                "running": status == "running",
                "verification_href": verification_href,
                "log_href": log_href,
            }
        )
        if status in {"ok", "failed"} and source_commit:
            resolved_commit_keys.add(commit_key)
    return result


def _detail_invocation_id(detail_ctx: dict[str, object]) -> str:
    columns = detail_ctx.get("detail_columns")
    if not isinstance(columns, list):
        return ""
    for col in columns:
        if not isinstance(col, dict):
            continue
        summary = col.get("summary")
        if not isinstance(summary, dict):
            continue
        invocation = summary.get("invocation")
        if not isinstance(invocation, dict):
            continue
        token = _normalize_run_id_token(str(invocation.get("id") or ""))
        if token:
            return token
    return ""


def _rerun_solution_paths_from_invocation(
    *,
    problem_id: int,
    workspace_id: int,
    actor_user_id: int,
    workspace: Path,
    invocation_id: str,
) -> list[str]:
    safe_invocation_id = _normalize_run_id_token(invocation_id)
    if not safe_invocation_id:
        return []
    run_ids = _run_invocation_scope_run_ids(
        problem_id,
        workspace_id,
        safe_invocation_id,
        actor_user_id=actor_user_id,
    )
    run_ids = _dedupe_preserve_order(
        [_normalize_run_id_token(item) for item in run_ids if _normalize_run_id_token(item)]
    )
    if not run_ids:
        return []
    summary_by_run: dict[str, dict[str, object]] = {}
    placeholders = ",".join(("?" for _ in run_ids))
    rows = config.db.fetch_all(
        f"""
        SELECT id,summary_json
        FROM runs
        WHERE problem_id=? AND workspace_id=? AND id IN ({placeholders})
        """,
        [int(problem_id), int(workspace_id), *run_ids],
    )
    for row in rows:
        run_id = _normalize_run_id_token(row["id"] if row is not None else "")
        if not run_id:
            continue
        summary_by_run[run_id] = _summary_object(row["summary_json"] if row is not None else "")
    try:
        audit_sources = _run_source_labels_from_audit(
            int(problem_id),
            int(actor_user_id),
            run_ids,
            limit=max(240, len(run_ids) * 8),
        )
    except Exception:
        audit_sources = {}
    out: list[str] = []
    for run_id in run_ids:
        summary = summary_by_run.get(run_id) or {}
        source_rel = str(summary.get("source") or "").strip()
        if not source_rel:
            source_rel = str(audit_sources.get(run_id) or "").strip()
        safe_solution = _normalize_optional_component_source_path_safe(
            source_rel,
            "solutions",
            "solution path",
        )
        if not safe_solution:
            continue
        candidate = (workspace / safe_solution).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except Exception:
            continue
        if candidate.exists() and candidate.is_file() and (not candidate.is_symlink()):
            out.append(safe_solution)
    return _dedupe_preserve_order(out)


def _run_detail_use_compact_layout(detail_ctx: dict[str, object]) -> bool:
    columns = detail_ctx.get("detail_columns")
    if not isinstance(columns, list):
        return False
    column_count = len(columns)
    if column_count >= 12:
        return True
    if column_count <= 8:
        return False
    max_title_len = 0
    for col in columns:
        if not isinstance(col, dict):
            continue
        title = str(col.get("title") or col.get("source") or "").strip()
        if len(title) > max_title_len:
            max_title_len = len(title)
    return max_title_len >= 28


def _mark_run_cancelled(run_id: str, reason: str) -> None:
    safe_run_id = _normalize_run_id_token(run_id)
    if not safe_run_id:
        return
    row = config.db.fetch_one("SELECT summary_json FROM runs WHERE id=?", [safe_run_id])
    summary = _summary_object(row["summary_json"] if row is not None else None)
    summary["cancelled"] = True
    summary["cancel_reason"] = str(reason or "").strip()
    if not str(summary.get("error") or "").strip():
        summary["error"] = str(reason or "").strip()
    config.db.execute(
        """
        UPDATE runs
        SET status='failed', summary_json=?, finished_at=?
        WHERE id=?
        """,
        [json.dumps(summary), now_iso(), safe_run_id],
    )


def _cancel_judgehost_tasks(run_ids: list[str], reason: str) -> int:
    safe_ids = _dedupe_preserve_order([_normalize_run_id_token(item) for item in run_ids if _normalize_run_id_token(item)])
    service = getattr(config, "judgehost_task_service", None)
    affected = 0
    if not safe_ids:
        return affected
    result_obj = {"error": str(reason or "").strip() or "verification cancelled by user"}
    if service is not None:
        try:
            affected = int(service.cancel_tasks_for_runs(safe_ids, reason=str(result_obj["error"])))
        except Exception:
            affected = 0

    if service is not None:
        try:
            service.cancel_domjudge_jobs_for_runs(safe_ids, final_status="failed")
        except Exception:
            pass

    return affected


def _finalize_cancelled_builds(run_ids: list[str], reason: str) -> int:
    safe_run_ids = _dedupe_preserve_order([_normalize_run_id_token(item) for item in run_ids if _normalize_run_id_token(item)])
    if not safe_run_ids:
        return 0
    placeholders = ",".join(("?" for _ in safe_run_ids))
    build_rows = config.db.fetch_all(
        f"""
        SELECT DISTINCT build_id
        FROM runs
        WHERE id IN ({placeholders})
        """,
        [*safe_run_ids],
    )
    build_ids: list[str] = []
    for row in build_rows:
        if row is None:
            continue
        token = str(row["build_id"] or "").strip()
        if not token:
            continue
        if token == str(_C.RUN_PLACEHOLDER_BUILD_ID):
            continue
        if token not in build_ids:
            build_ids.append(token)
    if not build_ids:
        return 0
    now_text = now_iso()
    cancelled_count = 0
    service = getattr(config, "judgehost_task_service", None)
    cancel_reason = str(reason or "").strip() or "verification cancelled by user"
    for build_id in build_ids:
        active_task_count = 0
        if service is not None:
            try:
                active_task_count = int(service.active_task_count_for_build(build_id))
            except Exception:
                active_task_count = 0
        if active_task_count > 0:
            continue

        def _tx(conn):
            build_row = conn.execute(
                """
                SELECT summary_json
                FROM builds
                WHERE id=? AND status IN ('running','queued','pending')
                """,
                [build_id],
            ).fetchone()
            if build_row is None:
                return 0
            summary = _summary_object(build_row["summary_json"] if build_row is not None else None)
            summary["cancelled"] = True
            summary["cancel_reason"] = cancel_reason
            if not str(summary.get("error") or "").strip():
                summary["error"] = cancel_reason
            cursor = conn.execute(
                """
                UPDATE builds
                SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND status IN ('running','queued','pending')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runs
                      WHERE build_id=?
                        AND status IN ('running','queued','pending')
                  )
                """,
                [json.dumps(summary), now_text, build_id, build_id],
            )
            try:
                return int(cursor.rowcount or 0)
            except Exception:
                return 0

        if int(config.db.write_transaction(_tx)) > 0:
            cancelled_count += 1
    return cancelled_count


def _slugify_problem_id(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > 64:
        token = token[:64].rstrip("-")
    return token


def _normalize_problem_slug_segment_required(raw: str) -> str:
    token = _slugify_problem_id(raw)
    if not token or (not _PROBLEM_SEGMENT_RE.fullmatch(token)):
        raise ValueError(_C.PROBLEM_ID_RULE_MESSAGE)
    return token


def _problem_full_slug(owner: str, slug_segment: str) -> str:
    safe_owner = str(owner or "").strip().lower()
    if not _C.USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(_C.USERNAME_RULE_MESSAGE)
    safe_segment = _normalize_problem_slug_segment_required(slug_segment)
    return f"{safe_owner}/{safe_segment}"


def _import_slug_base_from_package_name(package_name: str) -> str:
    raw_stem = str(Path(str(package_name or "imported-problem.zip")).stem or "").strip()
    normalized_stem = _POLYGON_LINUX_PACKAGE_SUFFIX_RE.sub("", raw_stem).strip()
    if not normalized_stem:
        normalized_stem = raw_stem
    stem = _slugify_problem_id(normalized_stem)
    base = stem or "imported-problem"
    if not _PROBLEM_SEGMENT_RE.fullmatch(base):
        return "imported-problem"
    return base


def _next_available_problem_slug(owner: str, base: str) -> str:
    token = str(base or "").strip()
    if not token:
        token = "imported-problem"
    token = _normalize_problem_slug_segment_required(token)
    candidate = token
    idx = 2
    while config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [_problem_full_slug(owner, candidate)]) is not None:
        suffix = f"-{idx}"
        prefix_len = max(1, 64 - len(suffix))
        prefix = token[:prefix_len].rstrip("-") or "p"
        candidate = f"{prefix}{suffix}"
        idx += 1
    return candidate


def build_import_slug_hint(owner: str, filename: str, requested_slug: str) -> dict[str, object]:
    package_name = str(filename or "").strip()
    requested = str(requested_slug or "").strip()
    base = _import_slug_base_from_package_name(package_name)
    if requested:
        normalized = _slugify_problem_id(requested)
        valid = bool(normalized and _PROBLEM_SEGMENT_RE.fullmatch(normalized))
        if not valid:
            return {
                "ok": True,
                "filename": package_name,
                "requested_slug": requested,
                "valid": False,
                "exists": False,
                "base": base,
                "suggested": _next_available_problem_slug(owner, base),
                "message": _C.PROBLEM_ID_RULE_MESSAGE,
            }
        full_requested = _problem_full_slug(owner, normalized)
        exists = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [full_requested]) is not None
        suggested = _next_available_problem_slug(owner, normalized) if exists else normalized
        message = ""
        if exists:
            message = f"problem already exists: {full_requested}"
        return {
            "ok": True,
            "filename": package_name,
            "requested_slug": normalized,
            "valid": True,
            "exists": bool(exists),
            "base": base,
            "suggested": suggested,
            "message": message,
        }

    suggested = _next_available_problem_slug(owner, base)
    return {
        "ok": True,
        "filename": package_name,
        "requested_slug": "",
        "valid": True,
        "exists": bool(suggested != base),
        "base": base,
        "suggested": suggested,
        "message": "",
    }
def _resolve_import_problem_slug(owner: str, requested_slug: str, package_name: str) -> str:
    requested = str(requested_slug or "").strip()
    if requested:
        normalized = _normalize_problem_slug_segment_required(requested)
        full_requested = _problem_full_slug(owner, normalized)
        exists = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [full_requested])
        if exists is not None:
            suggestion = _next_available_problem_slug(owner, normalized)
            raise ValueError(f"problem already exists: {full_requested} (try: {_problem_full_slug(owner, suggestion)})")
        return full_requested

    base = _import_slug_base_from_package_name(package_name)
    return _problem_full_slug(owner, _next_available_problem_slug(owner, base))


def _is_safe_regular_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and (not path.is_symlink())
    except OSError:
        return False


def _bare_repo_head_commit(bare_repo: Path) -> str:
    try:
        if not bare_repo.exists():
            return ""
        if bare_repo.is_symlink() or (not bare_repo.is_dir()):
            raise ValueError("import target bare repository path is invalid")
    except OSError:
        raise ValueError("import target bare repository path is invalid")
    proc = run_cmd(["git", "-C", str(bare_repo), "rev-parse", "--verify", "HEAD"])
    if proc.returncode != 0:
        return ""
    return str(proc.stdout or "").strip()


def _workspace_problem_mode(workspace: Path) -> str:
    cfg_path = workspace / "config" / "problem.json"
    try:
        if cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()):
            payload = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                mode = str(payload.get("mode") or "").strip().lower()
                if mode:
                    return mode
    except Exception:
        return "pass-fail"
    return "pass-fail"


def _sample_manual_rows_missing_answers(workspace: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    try:
        entries = load_tests_spec(workspace / TESTS_SPEC_REL)
    except Exception as exc:
        raise ValueError(f"invalid tests/spec.json after import: {exc}") from exc

    rows: list[tuple[int, str]] = []
    missing: list[tuple[int, str]] = []
    for index, row in enumerate(entries, start=1):
        if not isinstance(row, dict):
            continue
        if not bool(row.get("sample")):
            continue
        if str(row.get("kind") or "").strip().lower() != "manual":
            continue
        test_id = str(row.get("id") or "").strip()
        if not test_id:
            continue
        rows.append((index, test_id))
        answer_path = workspace / "tests" / "answers" / f"{test_id}.ans"
        if not _is_safe_regular_file(answer_path):
            missing.append((index, test_id))
    return rows, missing


def _materialize_polygon_sample_answers(problem: str, user: str, workspace: Path) -> dict[str, object]:
    sample_rows, missing_rows = _sample_manual_rows_missing_answers(workspace)
    if not sample_rows:
        return {"sample_manual_total": 0, "sample_answers_missing": 0, "sample_answers_materialized": 0, "build_id": ""}
    if not missing_rows:
        return {"sample_manual_total": len(sample_rows), "sample_answers_missing": 0, "sample_answers_materialized": 0, "build_id": ""}
    mode = _workspace_problem_mode(workspace)
    if mode != "pass-fail":
        return {
            "sample_manual_total": len(sample_rows),
            "sample_answers_missing": len(missing_rows),
            "sample_answers_materialized": 0,
            "build_id": "",
            "skipped_mode": mode,
        }

    build_id = config.build_service.run_build(problem, user, prefer_local_solve_backend=True)
    build_row = config.db.fetch_one(
        "SELECT status,summary_json,build_ref FROM builds WHERE id=?",
        [build_id],
    )
    if build_row is None:
        raise ValueError(f"sample answer materialization build missing: {build_id}")
    build_status = str(build_row["status"] or "").strip().lower()
    if build_status != "ok":
        summary = _summary_object(build_row["summary_json"])
        error_text = str(summary.get("error") or "").strip()
        if error_text:
            raise ValueError(f"sample answer materialization build failed ({build_id}): {error_text}")
        raise ValueError(f"sample answer materialization build failed ({build_id})")
    build_ref = str(build_row["build_ref"] or "").strip().lower()
    if not build_ref:
        raise ValueError(f"sample answer materialization build has no build_ref: {build_id}")
    try:
        artifact_root = config.fs_manager.build_paths(build_ref).root.resolve()
    except Exception as exc:
        raise ValueError(f"sample answer materialization build has invalid build_ref: {build_id}") from exc
    ans_dir = artifact_root / "ans"
    if not ans_dir.exists() or not ans_dir.is_dir() or ans_dir.is_symlink():
        raise ValueError(f"sample answer materialization build missing ans directory: {build_id}")

    materialized = 0
    for index, test_id in missing_rows:
        source_name = f"{int(index):03d}.ans"
        source_answer = ans_dir / source_name
        if not _is_safe_regular_file(source_answer):
            raise ValueError(
                f"sample answer missing from build output for test id {test_id} (build case {source_name})"
            )
        target_answer = workspace / "tests" / "answers" / f"{test_id}.ans"
        target_answer.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_answer, target_answer)
        materialized += 1

    _sample_rows_after, still_missing = _sample_manual_rows_missing_answers(workspace)
    if still_missing:
        first_idx, first_id = still_missing[0]
        raise ValueError(f"sample answer still missing after materialization: test id {first_id} (spec row {first_idx})")

    return {
        "sample_manual_total": len(sample_rows),
        "sample_answers_missing": len(missing_rows),
        "sample_answers_materialized": materialized,
        "build_id": build_id,
    }


def _is_package_marker(names: list[str], marker: str) -> bool:
    safe_marker = str(marker or "").replace("\\", "/").strip().strip("/")
    if not safe_marker:
        return False
    suffix = "/" + safe_marker
    for raw in names:
        token = str(raw or "").replace("\\", "/").strip().strip("/")
        if not token:
            continue
        if token == safe_marker or token.endswith(suffix):
            return True
    return False


def _detect_problem_package_format(package_payload: bytes) -> str:
    raw = bytes(package_payload or b"")
    if not raw:
        raise ValueError("package file is empty")
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            names = [str(item or "") for item in zf.namelist()]
    except Exception as exc:
        raise ValueError(f"invalid zip package: {exc}") from exc
    has_problem_xml = _is_package_marker(names, "problem.xml")
    has_problem_yaml = _is_package_marker(names, "problem.yaml")
    if has_problem_xml:
        return "polygon"
    if has_problem_yaml:
        return "icpc"
    raise ValueError("unsupported package format: expected problem.xml (Polygon) or problem.yaml (ICPC)")


def import_package_as_new_problem(
    actor_user_id: int,
    actor_user: str,
    package_name: str,
    package_content: bytes,
    requested_slug: str = "",
    source_problem: str = "",
    normalize_test_data_newlines: bool = False,
) -> dict[str, object]:
    safe_actor_user = str(actor_user or "").strip()
    if not safe_actor_user:
        raise ValueError("actor user is required")
    safe_package_name = str(package_name or "").strip()
    if not safe_package_name:
        raise ValueError("package filename is required")
    payload = bytes(package_content or b"")
    if not payload:
        raise ValueError("package file is empty")

    target_problem = _resolve_import_problem_slug(safe_actor_user, str(requested_slug or "").strip(), safe_package_name)
    package_format = _detect_problem_package_format(payload)
    target_bare = (config.settings.bare_root / f"{target_problem}.git").resolve()
    existing_bare_head = _bare_repo_head_commit(target_bare)
    if existing_bare_head:
        raise ValueError(f"import target already has revision history: {target_problem}")
    target_segment = str(target_problem.split("/", 1)[1] if "/" in target_problem else target_problem).strip()
    config.workspace_service.ensure_problem(target_problem, f"{target_segment.title()} Problem")
    config.workspace_service.grant_repo_access(target_problem, safe_actor_user, "owner")
    target_workspace = Path(config.workspace_service.ensure_workspace(target_problem, safe_actor_user))
    workspace_head = run_cmd(["git", "-C", str(target_workspace), "rev-parse", "--verify", "HEAD"])
    if workspace_head.returncode == 0 and str(workspace_head.stdout or "").strip():
        raise ValueError(f"import target already has revision history: {target_problem}")
    sample_answer_summary: dict[str, object] = {}
    with config.workspace_service.workspace_lock(target_workspace):
        if package_format == "polygon":
            result = _POLYGON_IMPORT_SERVICE.import_package(
                target_workspace,
                safe_package_name,
                payload,
                normalize_test_data_newlines=bool(normalize_test_data_newlines),
            )
        elif package_format == "icpc":
            result = _ICPC_IMPORT_SERVICE.import_package(
                target_workspace,
                safe_package_name,
                payload,
                normalize_test_data_newlines=bool(normalize_test_data_newlines),
            )
        else:
            raise ValueError(f"unsupported package format: {package_format}")
        imported_title = str(result.get("title") or "").strip()
        if imported_title:
            config.workspace_service.set_problem_name(target_problem, imported_title)
    if package_format == "polygon":
        sample_answer_summary = _materialize_polygon_sample_answers(target_problem, safe_actor_user, target_workspace)
        tests_summary = result.get("tests")
        if isinstance(tests_summary, dict):
            tests_summary["sample_answers_materialized"] = int(sample_answer_summary.get("sample_answers_materialized") or 0)
            tests_summary["sample_answers_missing"] = int(sample_answer_summary.get("sample_answers_missing") or 0)
            tests_summary["sample_manual_total"] = int(sample_answer_summary.get("sample_manual_total") or 0)
            current_answers = int(tests_summary.get("answers") or 0)
            tests_summary["answers"] = current_answers + int(sample_answer_summary.get("sample_answers_materialized") or 0)
    if sample_answer_summary:
        result["sample_answers"] = sample_answer_summary
    details = {
        "package": safe_package_name,
        "package_format": package_format,
        "source_problem": str(source_problem or "").strip(),
        "target_problem": target_problem,
        "statement": result.get("statement"),
        "tests": result.get("tests"),
        "components": result.get("components"),
        "solutions": result.get("solutions"),
    }
    target_problem_row = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [target_problem])
    if target_problem_row is not None:
        _audit(actor_user_id, int(target_problem_row["id"]), "export.import", details)
    tests_info = result.get("tests") if isinstance(result.get("tests"), dict) else {}
    total_tests = int(tests_info.get("total") or 0) if isinstance(tests_info, dict) else 0
    return {"target_problem": target_problem, "total_tests": total_tests, "result": result, "package_format": package_format}


def import_statement_language_warning(import_result: dict[str, object] | None) -> str:
    payload = dict(import_result or {})
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    statement = result.get("statement")
    if not isinstance(statement, dict):
        return ""
    return str(statement.get("language_warning") or "").strip()

def run_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = _read_problem_config(workspace)
    execute_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    workspace_id = int(ctx['workspace']['id'])
    requested_invocation_id = _parse_run_detail_invocation_id(request)
    requested_detail_ids = _parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    if requested_invocation_id:
        detail_scope_run_ids = _run_invocation_scope_run_ids(
            int(ctx['problem']['id']),
            workspace_id,
            requested_invocation_id,
            actor_user_id=int(ctx['user']['id']),
        )
    elif requested_detail_ids:
        detail_scope_run_ids = requested_detail_ids
    if requested_invocation_id or requested_detail_ids:
        detail_ctx = _build_run_detail_context(
            ctx,
            detail_scope_run_ids,
            execute_mode,
            requested_invocation_id=requested_invocation_id,
        )
        cancel_invocation_id = requested_invocation_id or _detail_invocation_id(detail_ctx)
        detail_ctx["cancel_invocation_id"] = cancel_invocation_id
        detail_ctx["cancel_available"] = bool(cancel_invocation_id and detail_ctx.get("detail_running"))
        detail_table_compact = _run_detail_use_compact_layout(detail_ctx)
        detail_ctx["detail_table_compact"] = detail_table_compact
        detail_page_ctx = dict(ctx)
        detail_page_ctx['page_wide_content'] = detail_table_compact
        detail_page_ctx['topbar_max_1400'] = detail_table_compact
        return _template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})
    runs = _run_list_rows(int(ctx['problem']['id']), workspace_id, workspace, limit=10, actor_user_id=int(ctx['user']['id']))
    return _template_response(request, 'run.html', {'ctx': ctx, 'runs': runs})

def run_new_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    workspace_id = int(ctx['workspace']['id'])
    active_build = _latest_workspace_build(int(ctx['problem']['id']), workspace_id, ok_only=True)
    solution_options, default_submission_path, solution_options_truncated = _run_solution_options_context(workspace)
    test_options, test_options_truncated, test_options_source = _run_test_options_context(problem, workspace, active_build)
    selected_solution_paths: list[str] = []
    for raw in request.query_params.getlist('solution_paths'):
        normalized = _normalize_optional_component_source_path_safe(raw, 'solutions', 'solution path')
        if normalized:
            selected_solution_paths.append(normalized)
    selected_solution_paths = _dedupe_preserve_order(selected_solution_paths)
    rerun_invocation_id = _normalize_run_id_token(request.query_params.get("rerun_invocation_id"))
    force_recompile = str(request.query_params.get("force_recompile") or "").strip().lower() in {"1", "true", "yes", "on"}
    if (not selected_solution_paths) and rerun_invocation_id:
        selected_solution_paths = _rerun_solution_paths_from_invocation(
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=workspace_id,
            actor_user_id=int(ctx["user"]["id"]),
            workspace=workspace,
            invocation_id=rerun_invocation_id,
        )
    if not selected_solution_paths and default_submission_path:
        selected_solution_paths = [default_submission_path]
    selected_test_names_raw = request.query_params.getlist('test_names')
    selected_test_names = _parse_run_test_names(selected_test_names_raw)
    allowed_test_names = {str(row.get('name') or '') for row in test_options}
    selected_test_names = [name for name in selected_test_names if name in allowed_test_names]
    if not selected_test_names_raw and test_options and (not test_options_truncated):
        selected_test_names = [str(row.get('name') or '') for row in test_options if str(row.get('name') or '').strip()]
    return _template_response(
        request,
        'run_execute.html',
        {
            'ctx': ctx,
            'solution_options': solution_options,
            'solution_options_truncated': solution_options_truncated,
            'selected_solution_paths': selected_solution_paths,
            'test_options': test_options,
            'test_options_truncated': test_options_truncated,
            'test_options_source': test_options_source,
            'selected_test_names': selected_test_names,
            'force_recompile': bool(force_recompile),
        },
    )

def run_details_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = _read_problem_config(workspace)
    execute_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    requested_invocation_id = _parse_run_detail_invocation_id(request)
    requested_detail_ids = _parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    if requested_invocation_id:
        detail_scope_run_ids = _run_invocation_scope_run_ids(
            int(ctx['problem']['id']),
            int(ctx['workspace']['id']),
            requested_invocation_id,
            actor_user_id=int(ctx['user']['id']),
        )
    elif requested_detail_ids:
        detail_scope_run_ids = requested_detail_ids
    detail_ctx = _build_run_detail_context(
        ctx,
        detail_scope_run_ids,
        execute_mode,
        requested_invocation_id=requested_invocation_id,
    )
    cancel_invocation_id = requested_invocation_id or _detail_invocation_id(detail_ctx)
    detail_ctx["cancel_invocation_id"] = cancel_invocation_id
    detail_ctx["cancel_available"] = bool(cancel_invocation_id and detail_ctx.get("detail_running"))
    detail_table_compact = _run_detail_use_compact_layout(detail_ctx)
    detail_ctx["detail_table_compact"] = detail_table_compact
    detail_page_ctx = dict(ctx)
    detail_page_ctx['page_wide_content'] = detail_table_compact
    detail_page_ctx['topbar_max_1400'] = detail_table_compact
    return _template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})


def run_details_test_fragment(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = _read_problem_config(workspace)
    execute_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))

    requested_invocation_id = _parse_run_detail_invocation_id(request)
    requested_detail_ids = _parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    if requested_invocation_id:
        detail_scope_run_ids = _run_invocation_scope_run_ids(
            int(ctx['problem']['id']),
            int(ctx['workspace']['id']),
            requested_invocation_id,
            actor_user_id=int(ctx['user']['id']),
        )
    elif requested_detail_ids:
        detail_scope_run_ids = requested_detail_ids

    test_name = _normalize_run_test_name_token(request.query_params.get('test'))
    if not test_name:
        raise HTTPException(status_code=400, detail='test is required')

    detail_ctx = _build_run_detail_context(
        ctx,
        detail_scope_run_ids,
        execute_mode,
        requested_invocation_id=requested_invocation_id,
        include_row_details=True,
        detail_test_name=test_name,
    )
    detail_rows = detail_ctx.get('detail_rows')
    if not isinstance(detail_rows, list) or not detail_rows:
        raise HTTPException(status_code=404, detail='test detail not found')
    row = detail_rows[0] if isinstance(detail_rows[0], dict) else None
    if row is None:
        raise HTTPException(status_code=404, detail='test detail not found')
    detail_columns = detail_ctx.get('detail_columns')
    if not isinstance(detail_columns, list):
        detail_columns = []
    response = config.templates.TemplateResponse(
        request,
        '_run_test_detail_fragment.html',
        {
            'ctx': ctx,
            'row': row,
            'detail_columns': detail_columns,
        },
    )
    return response


def run_cancel(problem: str, user: str, invocation_id: str = Form("")):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    _require_write_access(ctx)
    safe_invocation_id = _normalize_run_id_token(invocation_id)
    if not safe_invocation_id:
        return _redirect_response(
            f"/problems/{problem}/{user}/run",
            status_code=303,
            message="verification id is required",
        )
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    invocation_run_ids = _run_invocation_scope_run_ids(
        problem_id,
        workspace_id,
        safe_invocation_id,
        actor_user_id=actor_user_id,
    )
    invocation_run_ids = _dedupe_preserve_order(
        [_normalize_run_id_token(item) for item in invocation_run_ids if _normalize_run_id_token(item)]
    )
    details_url = f"/problems/{problem}/{user}/run/details?invocation_id={quote_plus(safe_invocation_id)}"
    if not invocation_run_ids:
        return _redirect_response(details_url, status_code=303, message="verification not found")
    reason = "verification cancelled by user"
    try:
        source_hints = _run_source_labels_from_audit(
            problem_id,
            actor_user_id,
            invocation_run_ids,
            limit=max(240, len(invocation_run_ids) * 8),
        )
    except Exception:
        source_hints = {}

    cancelled_runs = 0
    for run_token in invocation_run_ids:
        row = config.db.fetch_one(
            "SELECT status,build_id,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?",
            [run_token, problem_id, workspace_id],
        )
        status_text = str(row["status"] or "").strip().lower() if row is not None else "missing"
        if status_text in {"ok", "failed"}:
            summary_done = _summary_object(row["summary_json"] if row is not None else None)
            if not bool(summary_done.get("cancelled")):
                continue
        build_id = str(row["build_id"] or "").strip() if row is not None else ""
        if not build_id:
            build_id = str(_C.RUN_PLACEHOLDER_BUILD_ID)
        summary_obj = _summary_object(row["summary_json"] if row is not None else None)
        source_label = str(summary_obj.get("source") or "").strip()
        if not source_label:
            source_label = str(source_hints.get(run_token) or "").strip() or "verification"
        if row is None:
            _record_async_run_failure(
                problem,
                user,
                run_token,
                mode="pass-fail",
                source_label=source_label,
                error=reason,
                build_id=build_id,
                invocation_id=safe_invocation_id,
                invocation_run_ids=invocation_run_ids,
                expected_behavior="unknown",
                invocation_source="run.execute",
                synthesize_failed_tests=False,
                failure_stage="cancel",
                execution_skipped=True,
            )
        _mark_run_cancelled(run_token, reason)
        cancelled_runs += 1

    cancelled_tasks = _cancel_judgehost_tasks(invocation_run_ids, reason)
    cancelled_builds = _finalize_cancelled_builds(invocation_run_ids, reason)
    cancel_details: dict[str, object] = {
        "invocation_id": safe_invocation_id,
        "run_ids": invocation_run_ids,
        "run_count": len(invocation_run_ids),
        "cancelled_runs": cancelled_runs,
        "cancelled_tasks": cancelled_tasks,
        "cancelled_builds": cancelled_builds,
        "reason": reason,
    }
    _audit(actor_user_id, problem_id, "run.cancel", cancel_details)
    if cancelled_runs > 0 or cancelled_tasks > 0:
        msg = f"cancel requested ({cancelled_runs}/{len(invocation_run_ids)} runs)"
    else:
        msg = "verification already finished"
    return _redirect_response(details_url, status_code=303, message=msg)

def run_execute(
    problem: str,
    user: str,
    build_id: str = Form(""),
    solution_paths: list[str] = Form(default=[]),
    test_names: list[str] = Form(default=[]),
    submission_upload: UploadFile | None = File(None),
    force_recompile: str = Form(""),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = _read_problem_config(workspace)
    run_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    solution_options, _, _ = _run_solution_options_context(workspace)
    solution_expected_map: dict[str, str] = {}
    for row in solution_options:
        path = str(row.get('path') or '').strip()
        if not path:
            continue
        solution_expected_map[path] = normalize_expected_behavior(str(row.get('expected_behavior') or 'unknown'))
    upload_content = None
    upload_filename = ''
    uploaded = False
    force_recompile_flag = str(force_recompile or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    try:
        if submission_upload is not None:
            normalized_name = (submission_upload.filename or '').strip()
            if normalized_name:
                upload_filename = normalized_name
                upload_content = submission_upload.file.read()
                uploaded = True
        raw_solution_paths: list[str] = []
        if isinstance(solution_paths, str):
            raw_solution_paths.append(solution_paths)
        elif isinstance(solution_paths, list):
            raw_solution_paths.extend([str(item or '') for item in solution_paths])
        elif solution_paths:
            raw_solution_paths.extend([str(item or '') for item in list(solution_paths)])
        selected_solution_paths: list[str] = []
        for raw in raw_solution_paths:
            token = str(raw or '').strip()
            if not token:
                continue
            selected_solution_paths.append(_normalize_optional_component_source_path(token, 'solutions', 'solution path'))
        selected_solution_paths = _dedupe_preserve_order(selected_solution_paths)
        selected_test_names = _parse_run_test_names(test_names)
        execution_targets: list[tuple[str | None, bool]] = []
        execution_targets.extend(((path, False) for path in selected_solution_paths))
        if uploaded:
            execution_targets.append((None, True))
        if not execution_targets:
            msg = 'select at least one solution or upload source file'
            return _redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        deduped_targets: list[tuple[str | None, bool]] = []
        seen_targets: set[tuple[str, bool]] = set()
        for target_submission_path, target_is_upload in execution_targets:
            key = (str(target_submission_path or ''), bool(target_is_upload))
            if key in seen_targets:
                continue
            seen_targets.add(key)
            deduped_targets.append((target_submission_path, target_is_upload))
        execution_targets = deduped_targets
        if uploaded and isinstance(upload_content, (bytes, bytearray)):
            compile_check_error = upload_compile_check_error(
                workspace,
                upload_filename,
                bytes(upload_content),
                compile_program=config.toolchain_service.compile_program,
                cxxflags=list(config.run_service.SUBMISSION_CPP_CXXFLAGS),
            )
            if compile_check_error:
                msg = f'upload compile check failed: {compile_check_error}'
                return _redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        for target_submission_path, target_is_upload in execution_targets:
            if target_is_upload:
                continue
            solution_path = str(target_submission_path or '').strip()
            if not solution_path:
                continue
            compile_check_error = workspace_source_compile_check_error(
                workspace,
                solution_path,
                compile_program=config.toolchain_service.compile_program,
                cxxflags=list(config.run_service.SUBMISSION_CPP_CXXFLAGS),
            )
            if compile_check_error:
                msg = f'compile check failed: {compile_check_error}'
                return _redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        requested_build_id = str(build_id or '').strip()
        if requested_build_id:
            _assert_workspace_build_access(ctx, requested_build_id)
        run_ids: list[str] = []
        resolved_submission_paths: list[str] = []
        background_targets: list[dict[str, object]] = []
        invocation_id = _allocate_invocation_id()
        for target_submission_path, target_is_upload in execution_targets:
            run_id = _allocate_run_id()
            run_ids.append(run_id)
            source_label = target_submission_path or upload_filename or 'upload'
            expected_behavior = 'unknown'
            if target_submission_path:
                resolved_submission_paths.append(target_submission_path)
                expected_behavior = solution_expected_map.get(target_submission_path, 'unknown')
                if expected_behavior == 'unknown':
                    safe_solution = _normalize_optional_component_source_path_safe(target_submission_path, 'solutions', 'solution path')
                    if safe_solution:
                        expected_behavior = infer_expected_behavior_from_name(safe_solution)
            background_targets.append({'run_id': run_id, 'submission_path': target_submission_path or '', 'upload_content': upload_content if target_is_upload else None, 'upload_filename': upload_filename if target_is_upload else '', 'source_label': source_label, 'expected_behavior': normalize_expected_behavior(expected_behavior)})
        primary_run_id = run_ids[0] if run_ids else ''
        run_execute_details: dict[str, object] = {
            'invocation_id': invocation_id,
            'run_id': primary_run_id,
            'run_ids': run_ids,
            'run_count': len(run_ids),
            'build_id': requested_build_id,
            'submission_paths': resolved_submission_paths,
            'solution_paths': selected_solution_paths,
            'selected_test_names': selected_test_names,
            'uploaded': uploaded,
            'mode': run_mode,
            'implicit_build_generated': not bool(requested_build_id),
            'invocation_backend': config.invocation_backend_service.active_backend_name(),
            'async': True,
            'status': 'queued',
            'force_recompile': bool(force_recompile_flag),
        }
        _audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', run_execute_details)
        try:
            started = _start_run_execute_batch(
                problem,
                user,
                requested_build_id=requested_build_id,
                run_mode=run_mode,
                targets=background_targets,
                invocation_id=invocation_id,
                invocation_run_ids=run_ids,
                selected_test_names=selected_test_names,
                force_recompile=bool(force_recompile_flag),
            )
        except Exception as exc:
            failed_details = dict(run_execute_details)
            failed_details['status'] = 'failed'
            failed_details['error'] = str(exc)
            _audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return _redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=str(exc))
        if not started:
            failed_details = dict(run_execute_details)
            failed_details['status'] = 'failed'
            failed_details['error'] = 'verification queue rejected'
            _audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return _redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message='verification queue rejected')
        message_parts: list[str] = []
        if selected_test_names:
            message_parts.append(f'tests selected ({len(selected_test_names)})')
        message_parts.append(f'verification running ({len(run_ids)} programs)')
        message_text = '; '.join(message_parts)
        if primary_run_id:
            return _redirect_response(
                f'/problems/{problem}/{user}/run/details?invocation_id={quote_plus(invocation_id)}',
                status_code=303,
                message=message_text,
            )
        return _redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=message_text)
    finally:
        if submission_upload is not None:
            submission_upload.file.close()

def _build_validation_status(build_row: dict[str, object] | None) -> str:
    if not isinstance(build_row, dict):
        return "validation unknown"
    status = str(build_row.get("status") or "").strip().lower()
    summary_raw = str(build_row.get("summary_json") or "").strip()
    summary: dict[str, object] = {}
    if summary_raw:
        try:
            parsed = json.loads(summary_raw)
            if isinstance(parsed, dict):
                summary = parsed
        except Exception:
            summary = {}
    steps = summary.get("steps")
    if isinstance(steps, list):
        for raw in steps:
            if not isinstance(raw, dict):
                continue
            step_name = str(raw.get("step") or "").strip().lower()
            if step_name != "validate":
                continue
            step_status = str(raw.get("status") or "").strip().lower()
            if step_status == "ok":
                return "validation passed"
            if step_status in {"error", "failed"}:
                return "validation failed"
            break
    failed_step = str(summary.get("failed_step") or "").strip().lower()
    if failed_step == "validate":
        return "validation failed"
    if status == "ok":
        return "validation passed"
    return "validation unknown"


def _export_archive_summary(problem: str, build_id: str, filename: str) -> dict[str, object]:
    result: dict[str, object] = {
        "available": False,
        "has_pdf": False,
        "solutions_total": None,
        "solutions_correct": None,
        "tests_total": None,
    }
    safe_build = str(build_id or "").strip()
    safe_filename = Path(str(filename or "").strip()).name
    if not safe_build or not safe_filename:
        return result
    archive_path = _resolve_export_archive_path(problem, safe_build, safe_filename)
    if archive_path is None:
        return result
    if not archive_path.exists() or not archive_path.is_file() or archive_path.is_symlink():
        return result
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = [str(name or "") for name in zf.namelist() if str(name or "") and (not str(name or "").endswith("/"))]
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


def _resolve_export_archive_path(problem: str, build_id: str, filename: str) -> Path | None:
    safe_build = str(build_id or "").strip()
    safe_name = Path(str(filename or "").strip()).name
    if (not safe_build) or (not safe_name):
        return None
    row = config.db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [safe_build])
    if row is None:
        return None
    build_ref = str(row["build_ref"] or "").strip().lower()
    if not build_ref:
        return None
    try:
        root = config.fs_manager.build_paths(build_ref).root.resolve()
    except Exception:
        return None
    export_dir = (root / "export").resolve()
    if root != export_dir and root not in export_dir.parents:
        return None
    if (not export_dir.exists()) or (not export_dir.is_dir()) or export_dir.is_symlink():
        return None
    candidate = (export_dir / safe_name).resolve()
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
    head_commit = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace = Path(ctx['workspace']['path'])
    generate_revision: int | None = _git_commit_count(workspace, head_commit) if head_commit else None
    generate_revision_display = f'v{generate_revision}' if isinstance(generate_revision, int) and generate_revision >= 0 else 'missing'
    active_build = _latest_workspace_committed_build(problem_id, int(workspace_id), head_commit, ok_only=True)
    build_status = 'ready' if active_build is not None else 'missing'
    if not head_commit:
        build_note = 'no committed revision yet; commit changes before generating package'
    elif active_build is None:
        build_note = 'no committed tests snapshot for this revision; Generate will build from committed revision'
    else:
        build_note = 'committed revision tests are ready for export'
    exports_rows = config.db.fetch_all('\n        SELECT id,build_id,export_type,filename,sha256,size_bytes,source_commit,created_at\n        FROM exports\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT 40\n        ', [ctx['problem']['id'], workspace_id])
    revision_cache: dict[str, int | None] = {}
    build_meta_cache: dict[str, dict[str, object] | None] = {}
    archive_summary_cache: dict[tuple[str, str], dict[str, object]] = {}
    exports: list[dict[str, object]] = []
    for row in exports_rows:
        item = dict(row)
        source_commit = str(item.get('source_commit') or '').strip()
        revision = None
        if source_commit:
            if source_commit in revision_cache:
                revision = revision_cache[source_commit]
            else:
                revision = _git_commit_count(workspace, source_commit)
                revision_cache[source_commit] = revision
        item['revision'] = revision
        item['revision_display'] = f'v{revision}' if isinstance(revision, int) and revision >= 0 else 'v?'
        stored_filename = Path(str(item.get("filename") or "").strip()).name
        fallback_stem = Path(str(ctx["problem"]["slug"] or "")).name or "problem"
        item['display_filename'] = stored_filename or f"{fallback_stem}-{item['revision_display']}.zip"
        build_id = str(item.get("build_id") or "").strip()
        build_meta = build_meta_cache.get(build_id)
        if build_id and (build_meta is None) and (build_id not in build_meta_cache):
            row_meta = config.db.fetch_one(
                "SELECT id,status,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
                [build_id, problem_id, workspace_id],
            )
            build_meta = dict(row_meta) if row_meta is not None else None
            build_meta_cache[build_id] = build_meta
        validation_status = _build_validation_status(build_meta)
        summary_bits: list[str] = [validation_status]
        summary_key = (build_id, str(item.get("filename") or "").strip())
        archive_summary = archive_summary_cache.get(summary_key)
        if archive_summary is None:
            archive_summary = _export_archive_summary(problem, summary_key[0], summary_key[1])
            archive_summary_cache[summary_key] = archive_summary
        if bool(archive_summary.get("available")):
            summary_bits.insert(0, "has pdf" if bool(archive_summary.get("has_pdf")) else "no pdf")
            solutions_total = archive_summary.get("solutions_total")
            solutions_correct = archive_summary.get("solutions_correct")
            tests_total = archive_summary.get("tests_total")
            if isinstance(solutions_total, int) and isinstance(solutions_correct, int):
                summary_bits.append(f"{_count_label(solutions_total, 'solution')} ({solutions_correct} correct)")
            if isinstance(tests_total, int):
                summary_bits.append(_count_label(tests_total, "test"))
        item["summary_display"] = f"{item['revision_display']} ({', '.join(summary_bits)})" if summary_bits else item["revision_display"]
        exports.append(item)
    export_events = _export_recent_events(
        problem_id,
        actor_user_id,
        problem_slug=str(ctx["problem"]["slug"]),
        username=str(ctx["user"]["username"]),
        limit=20,
    )
    return _template_response(
        request,
        'export.html',
        {
            'ctx': ctx,
            'active_build': active_build,
            'build_status': build_status,
            'build_note': build_note,
            'generate_revision_display': generate_revision_display,
            'exports': exports,
            'export_events': export_events,
        },
    )

def export_create(problem: str, user: str, build_id: str=Form(''), export_type: str=Form('icpc')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    _require_write_access(ctx)
    resolved_build_id = str(build_id or '').strip()
    requested_export_type = str(export_type or '').strip().lower()
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    head_commit = str(ctx['workspace'].get('head_commit') or '').strip()
    if not requested_export_type:
        requested_export_type = 'icpc'
    initial_details: dict[str, object] = {'status': 'running', 'build_id': resolved_build_id, 'export_type': requested_export_type, 'source_commit': head_commit, 'filename': '', 'error': ''}
    try:
        if requested_export_type != 'icpc':
            raise ValueError('unsupported package type (ICPC only)')
        if not head_commit:
            raise ValueError('no committed revision; commit changes first')
        started = _start_export_job(problem, user, actor_user_id=int(ctx['user']['id']), problem_id=problem_id, workspace_id=workspace_id, head_commit=head_commit, requested_build_id=resolved_build_id, requested_export_type=requested_export_type, initial_details=initial_details)
        msg = 'package generation queued' if started else 'package generation already running for this revision'
    except ValueError as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
        msg = str(exc)
    except Exception as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/export', status_code=303, message=msg)

def export_import(problem: str, user: str, package_upload: UploadFile | None=File(None), problem_slug: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    try:
        if package_upload is None:
            raise ValueError('package file is required')
        package_name = str(package_upload.filename or '').strip()
        if not package_name:
            raise ValueError('package filename is required')
        package_content = package_upload.file.read()
        actor_user = str(ctx['user'].get('username') or user).strip()
        imported = import_package_as_new_problem(
            actor_user_id=int(ctx['user']['id']),
            actor_user=actor_user,
            package_name=package_name,
            package_content=package_content,
            requested_slug=str(problem_slug or '').strip(),
            source_problem=str(problem or '').strip(),
        )
        target_problem = str(imported.get('target_problem') or '').strip()
        total_tests = int(imported.get('total_tests') or 0)
        package_format = str(imported.get("package_format") or "package").strip()
        msg = f"{package_format} package imported as {target_problem} ({_count_label(total_tests, 'test')})"
        language_warning = import_statement_language_warning(imported)
        if language_warning:
            msg = f"{msg}; warning: {language_warning}"
        return _redirect_response(f'/problems/{target_problem}/{actor_user}/statement', status_code=303, message=msg)
    except ValueError as exc:
        msg = str(exc)
    except Exception as exc:
        msg = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return _redirect_response(f'/problems/{problem}/{user}/export', status_code=303, message=msg)


def export_import_slug_hint(problem: str, user: str, filename: str = "", requested_slug: str = ""):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    actor_user = str(ctx["user"].get("username") or user).strip()
    payload = build_import_slug_hint(actor_user, filename, requested_slug)
    return JSONResponse(payload)

def artifact_file(problem: str, user: str, build_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _assert_workspace_artifact_access(ctx, build_id)
    rel_norm = str(rel_path or '').lstrip('/')
    if rel_norm.startswith("export/"):
        export_name = Path(rel_norm).name
        file_path = _resolve_export_archive_path(problem, build_id, export_name)
        if file_path is None:
            raise HTTPException(status_code=404, detail="artifact file not found")
    else:
        file_path = _safe_artifact_path(problem, build_id, rel_path)
    if rel_norm.startswith('export/'):
        export_name = Path(rel_norm).name
        download_name = _export_download_filename(ctx, build_id, export_name)
        if download_name:
            return FileResponse(file_path, filename=download_name)
    return _browser_file_response(file_path)

def run_artifact_file(problem: str, user: str, run_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    rel_norm = str(rel_path or '').strip().lstrip('/')
    if Path(rel_norm).name == 'compile.log':
        raise HTTPException(status_code=403, detail='compile.log download is disabled')
    try:
        file_path = _safe_run_artifact_path(ctx, run_id, rel_path)
    except HTTPException as exc:
        detail = str(getattr(exc, "detail", "") or "").strip().lower()
        if int(getattr(exc, "status_code", 500)) == 404 and detail.startswith("run artifact"):
            safe_run_id = _normalize_run_id_token(run_id)
            target = f"/problems/{problem}/{user}/run/details?run_id={safe_run_id or quote_plus(str(run_id or '').strip())}"
            return _redirect_response(
                target,
                status_code=303,
                message="Run artifact is not persisted; rerun verification to regenerate downloadable files.",
            )
        raise
    return _browser_file_response(file_path)
