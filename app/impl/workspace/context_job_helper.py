from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from app.impl.auth.shared import parse_iso_utc
from app.impl.runtime.config import config

from app.main_util import compact_error_text
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.store import (
    allocate_verification_id as _store_allocate_verification_id,
    load_verification_run,
    save_verification_run_summary,
    verification_stage_results,
    verification_run_root,
)
from app.service.verification.types import Kind

from .context_run_detail import normalize_run_id_token
from .context_ui import page_ctx
from .context_verification import (
    latest_workspace_stage_verification,
    _verification_solution_match,
)
from .problem_config import normalize_problem_mode

_C = config.constants
_BACKEND_NAME = config.judgehost_task_service.backend_name()

VerificationSummary = dict[str, object]
VerificationRunRow = dict[str, object]


class VerificationFailureError(RuntimeError):
    def __init__(self, *, verification_id: str, reason: str, status: str = "", failed_test: str = "") -> None:
        safe_reason = reason or "verification failed"
        super().__init__(f"verification failed: {safe_reason}")
        self.verification_id = verification_id
        self.reason = safe_reason
        self.status = status
        self.failed_test = failed_test


def _verification_id_for_run(run_id: str, verification_id: str) -> str:
    safe_verification_id = normalize_run_id_token(verification_id)
    if safe_verification_id:
        return safe_verification_id
    if not run_id:
        raise RuntimeError("run id is required")
    return f"ver-{run_id}"


def _verification_kind_for_source(verification_source: str) -> str:
    return Kind.VERIFICATION.value


def _ensure_implicit_verification(
    problem: str,
    user: str,
    *,
    ctx: dict | None = None,
    force: bool = False,
    for_verification: bool = False,
    verification_id: str = "",
) -> tuple[str, bool]:
    local_ctx = ctx or page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(local_ctx["problem"]["id"])
    workspace = local_ctx["workspace"]
    workspace_id = int(workspace["id"])
    head_commit = workspace.get("head_commit") or ""
    branch = workspace.get("branch") or "main"
    dirty = bool(workspace.get("dirty"))
    safe_target_verification_id = normalize_run_id_token(verification_id)
    latest_ok = latest_workspace_stage_verification(problem_id, workspace_id, ok_only=True)
    if (not safe_target_verification_id) and (not force) and latest_ok is not None:
        latest_id = latest_ok["id"]
        latest_commit = latest_ok["source_commit"]
        latest_ref = latest_ok["source_ref"]
        latest_commit_upper = latest_commit.upper()
        same_ref = bool(latest_id) and (latest_ref == branch)
        matches_head = bool(latest_commit) and (
            latest_commit == head_commit or ((not head_commit) and (latest_commit_upper == "HEAD"))
        )
        if same_ref and (not dirty) and matches_head:
            return (latest_id, False)
        # Dirty v0 workspaces store build.source_commit as "HEAD".
        if same_ref and dirty and (matches_head or (latest_commit_upper == "HEAD")):
            created_at = parse_iso_utc(latest_ok["created_at"])
            if created_at is not None and (
                datetime.now(timezone.utc) - created_at
            ).total_seconds() <= _C.IMPLICIT_BUILD_DIRTY_REUSE_SEC:
                return (latest_id, False)
    if for_verification:
        created_verification_id = config.verification_service.run_verification(
            problem,
            user,
            verification_id=safe_target_verification_id,
        )
    else:
        created_verification_id = config.verification_service.run_verification(problem, user)
    if not created_verification_id:
        raise RuntimeError("verification failed: verification id is missing")
    row = config.verification_service.workspace_verification_meta(problem_id, workspace_id, created_verification_id)
    status = "" if row is None else row["status"]
    if status and status not in {"ok", "failed", "cancelled"}:
        try:
            waited = config.verification_service.wait_for_terminal_status(
                created_verification_id,
                timeout_sec=30.0,
            )
            if waited:
                status = waited
        except Exception:
            pass
    if status == "ok":
        return (created_verification_id, True)
    if for_verification:
        verification_summary = config.verification_service.verification_summary(created_verification_id)
        if verification_summary:
            stage_results = verification_stage_results(verification_summary)
            generate_stage = stage_results.get("generate_input")
            solve_stage = stage_results.get("solve_main")
            generate_status = generate_stage.get("status", "") if generate_stage else ""
            solve_status = solve_stage.get("status", "") if solve_stage else ""
            if generate_status == "ok" and solve_status == "ok":
                return (created_verification_id, True)
    failed_test, reason = _parse_verification_failure_context(problem_id, workspace_id, created_verification_id)
    if reason:
        raise VerificationFailureError(
            verification_id=created_verification_id,
            reason=reason,
            status=status,
            failed_test=failed_test,
        )
    if status:
        raise VerificationFailureError(
            verification_id=created_verification_id,
            reason=f"verification status is {status}",
            status=status,
            failed_test=failed_test,
        )
    raise VerificationFailureError(
        verification_id=created_verification_id,
        reason="verification metadata missing",
        status=status,
        failed_test=failed_test,
    )


def allocate_run_id() -> str:
    return f"r-{secrets.token_hex(6)}"


def allocate_verification_id() -> str:
    return _store_allocate_verification_id(config.db)


def _parse_verification_failure_context(problem_id: int, workspace_id: int, verification_id: str) -> tuple[str, str]:
    if not verification_id:
        return ("", "")
    row = config.verification_service.workspace_verification_meta(
        int(problem_id),
        int(workspace_id),
        verification_id,
    )
    if row is None:
        return ("", "")
    status = row["status"]
    summary = config.verification_service.verification_summary(verification_id)
    failed_test_raw = summary.get("failed_test", "")
    failed_test = ""
    if failed_test_raw:
        candidate = Path(failed_test_raw).name
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in", candidate):
            failed_test = candidate
    failed_step = summary.get("failed_step", "")
    artifact_verification_error = compact_error_text(summary.get("error", ""))
    reason = ""
    if artifact_verification_error:
        reason = artifact_verification_error
    elif failed_step and failed_test_raw:
        reason = compact_error_text(f"{failed_step} failed on {failed_test_raw}")
    elif failed_step:
        reason = compact_error_text(f"{failed_step} failed")
    elif status and status != "ok":
        reason = f"verification status is {status}"
    return (failed_test, reason)


def _extract_failed_test_name_from_error(error_text: str) -> str:
    match = re.search(r"([A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in)", error_text)
    if not match:
        return ""
    token = Path(match.group(1)).name
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in", token):
        return token
    return ""


def _synthesize_failed_run_tests(
    *,
    preferred_test: str = "",
    error_text: str = "",
    test_names: list[str] | None = None,
) -> list[dict]:
    normalized_tests: list[str] = []
    if test_names is not None:
        for raw in test_names:
            token = Path(raw).name
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in", token) and token not in normalized_tests:
                normalized_tests.append(token)
    if not normalized_tests:
        for raw in [preferred_test, "001.in"]:
            token = Path(raw).name
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in", token):
                normalized_tests.append(token)
                break
    if not normalized_tests:
        return []
    feedback = compact_error_text(error_text)
    result_rows: list[dict] = []
    for test_name in normalized_tests:
        pass_row: dict[str, object] = {"pass": 1, "verdict": "FL", "time_ms": 0, "memory_kb": 0}
        if feedback:
            pass_row["feedback"] = feedback
        test_row: dict[str, object] = {
            "test": test_name,
            "passes": [pass_row],
            "verdict": "FL",
            "sandbox_status": "fail",
            "time_ms": 0,
            "memory_kb": 0,
            "feedback_files": [],
        }
        if feedback:
            test_row["message"] = feedback
        result_rows.append(test_row)
    return result_rows


def record_async_run_failure(
    problem: str,
    user: str,
    run_id: str,
    *,
    mode: str,
    source_label: str,
    error: str,
    verification_id: str,
    artifact_verification_id: str = "",
    expected_behavior: str = "unknown",
    verification_source: str = "run.execute",
    synthesize_failed_tests: bool = True,
    failure_stage: str = "",
    execution_skipped: bool = False,
    synthesized_test_names: list[str] | None = None,
) -> None:
    if not run_id:
        return
    safe_mode = normalize_problem_mode(mode, _C.GENERAL_CONFIG_DEFAULTS["mode"])
    safe_source = source_label or "upload"
    safe_error = error or "verification failed"
    safe_expected = normalize_expected_behavior(expected_behavior)
    effective_verification_id = verification_id
    if not re.fullmatch("[A-Za-z0-9._-]{1,80}", effective_verification_id):
        effective_verification_id = ""
    try:
        ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    except Exception:
        return
    resolved_verification_id = _verification_id_for_run(run_id, effective_verification_id)
    run_root = verification_run_root(config.fs_manager, resolved_verification_id, run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    compile_log_name = "compile.log"
    compile_log_text = safe_error + "\n"
    (run_root / compile_log_name).write_text(compile_log_text, encoding="utf-8")
    failed_test, build_reason = _parse_verification_failure_context(
        int(ctx["problem"]["id"]),
        int(ctx["workspace"]["id"]),
        artifact_verification_id,
    )
    if not failed_test:
        failed_test = _extract_failed_test_name_from_error(build_reason or safe_error)
    failure_reason = build_reason or safe_error
    tests_payload = (
        _synthesize_failed_run_tests(
            preferred_test=failed_test,
            error_text=failure_reason,
            test_names=synthesized_test_names,
        )
        if synthesize_failed_tests
        else []
    )
    summary: VerificationSummary = {
        "error": safe_error,
        "mode": safe_mode,
        "source": safe_source,
        "tests": tests_payload,
        "tests_total": len(tests_payload),
        "compile_log": compile_log_name,
        "compile_diagnostics": [],
        "toolchain_digest": "unknown",
        "sandbox_backend": _BACKEND_NAME,
        "verification_backend": _BACKEND_NAME,
        "limits": {},
        "usage": {"tests": len(tests_payload)},
    }
    if failure_stage:
        summary["failure_stage"] = failure_stage
    if execution_skipped:
        summary["execution_skipped"] = True
        if failure_reason:
            summary["execution_skipped_reason"] = failure_reason
        if not failure_stage:
            summary["failure_stage"] = "build"
    try:
        save_verification_run_summary(
            config.db,
            config.fs_manager,
            verification_id=resolved_verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            kind=_verification_kind_for_source(verification_source),
            mode=safe_mode,
            verification_source=verification_source,
            source_paths=[safe_source],
            run_id=run_id,
            run_status="failed",
            source_label=safe_source,
            expected_behavior=safe_expected,
            run_summary=summary,
            artifact_path=str(run_root),
            error_text=safe_error,
            finished=True,
        )
    except Exception:
        pass
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _update_verification_run_match(
    problem_id: int,
    workspace_id: int,
    run_id: str,
    *,
    verification_id: str,
    expected_behavior: str,
    verification_source: str = "run.execute",
) -> dict[str, object]:
    safe_verification_id = normalize_run_id_token(verification_id) or run_id
    safe_expected = normalize_expected_behavior(expected_behavior)
    resolved_verification_id = _verification_id_for_run(run_id, safe_verification_id)
    verification_summary = config.verification_service.verification_summary(resolved_verification_id)
    run_row = load_verification_run(
        config.db,
        verification_id=resolved_verification_id,
        run_id=run_id,
    )
    if run_row:
        summary_obj = run_row["summary"]
        run_status = run_row["status"]
        source_label = run_row["source_label"]
        artifact_path = run_row["artifact_path"]
    else:
        summary_obj = {}
        run_status = "missing"
        source_label = run_id
        artifact_path = str(config.fs_manager.prepare_verification_run_root(resolved_verification_id, run_id))
    mode = verification_summary.get("mode", "pass-fail") if verification_summary else "pass-fail"
    matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, run_status, summary_obj)
    try:
        save_verification_run_summary(
            config.db,
            config.fs_manager,
            verification_id=resolved_verification_id,
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            kind=_verification_kind_for_source(verification_source),
            mode=mode,
            verification_source=verification_source,
            source_paths=[source_label],
            run_id=run_id,
            run_status=run_status,
            source_label=source_label,
            expected_behavior=safe_expected,
            run_summary=summary_obj,
            artifact_path=artifact_path,
            error_text=summary_obj.get("error", ""),
            finished=run_status not in {"queued", "pending", "running"},
        )
    except Exception:
        pass
    return {
        "run_id": run_id,
        "status": run_status,
        "expected_behavior": safe_expected,
        "matched": bool(matched),
        "completed": bool(completed),
        "passed_all_tests": bool(observed_pass),
        "reason": reason,
    }


def annotate_verification_run_result(
    problem_id: int,
    workspace_id: int,
    run_id: str,
    *,
    verification_id: str,
    expected_behavior: str,
    verification_source: str = "run.execute",
) -> dict[str, object]:
    return _update_verification_run_match(
        problem_id,
        workspace_id,
        run_id,
        verification_id=verification_id,
        expected_behavior=expected_behavior,
        verification_source=verification_source,
    )
