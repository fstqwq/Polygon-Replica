from __future__ import annotations

import secrets
from pathlib import Path

from app.impl.runtime.config import config
from app.service.disk.verification_store import VerificationStore
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.types import Kind, Status

from .context_run_detail import normalize_run_id_token
from .context_ui import page_ctx
from .context_verification import _verification_solution_match


def allocate_run_id() -> str:
    return f"r-{secrets.token_hex(6)}"


def allocate_verification_id() -> str:
    return VerificationStore(config.db).allocate_id()


def _verification_id_for_run(run_id: str, verification_id: str) -> str:
    safe_verification_id = normalize_run_id_token(verification_id)
    if safe_verification_id:
        return safe_verification_id
    if not run_id:
        raise RuntimeError("run id is required")
    return f"ver-{run_id}"


def _failed_test_name(error_text: str, synthesized_test_names: list[str] | None) -> str:
    if synthesized_test_names:
        for raw in synthesized_test_names:
            token = Path(str(raw or "")).name
            if token.endswith(".in"):
                return token
    for token in error_text.split():
        name = Path(token.strip()).name
        if name.endswith(".in"):
            return name
    return "001.in"


def _artifact_failure_details(artifact_verification_id: str) -> tuple[str, str]:
    safe_verification_id = normalize_run_id_token(artifact_verification_id)
    if not safe_verification_id:
        return ("", "")
    metadata = config.verification_service.verification_metadata(safe_verification_id)
    metadata_error = str(metadata.get("error") or "")
    failed_test = Path(str(metadata.get("failed_test") or "")).name
    if failed_test.endswith(".in"):
        return (metadata_error, failed_test)
    record = config.verification_service.verification_record(safe_verification_id)
    if record is None:
        return (metadata_error, "")
    return (metadata_error or str(record.get("fail_reason") or ""), "")


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
    del verification_source
    del failure_stage
    del execution_skipped
    if not run_id:
        return
    safe_error = str(error or "verification failed")
    safe_source = str(source_label or "upload")
    safe_expected = normalize_expected_behavior(expected_behavior)
    resolved_verification_id = _verification_id_for_run(run_id, verification_id)
    artifact_error, artifact_failed_test = _artifact_failure_details(artifact_verification_id)
    detail_error = artifact_error or safe_error
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    config.verification_service.begin_verification_record(
        verification_id=resolved_verification_id,
        problem_id=problem_id,
        workspace_id=workspace_id,
        signature="",
        kind=Kind.CUSTOM.value,
        status=Status.FAILED.value,
    )
    task_store = VerificationTaskStore(config.db)
    task_id = task_store.allocate_id()
    test_name = artifact_failed_test or _failed_test_name(detail_error, synthesized_test_names if synthesize_failed_tests else None)
    config.verification_service.persist_verification_metadata(
        resolved_verification_id,
        {
            "mode": str(mode or "pass-fail"),
            "error": detail_error,
            "failed_test": test_name,
            "artifact_verification_id": normalize_run_id_token(artifact_verification_id),
            "kind": Kind.CUSTOM.value,
            "source_paths": [safe_source],
        },
    )
    task_store.replace_graph(
        resolved_verification_id,
        tasks=[
            {
                "id": task_id,
                "task_kind": "solution-run",
                "source_path": safe_source,
                "logical_run_id": run_id,
                "test_name": test_name,
                "expected_behavior": safe_expected,
                "status": VerificationTaskStore.TASK_PENDING,
            }
        ],
        edges=[],
    )
    task_store.save_task_result(
        task_id,
        status=VerificationTaskStore.TASK_FAILED,
        verdict="FL",
        run_id=run_id,
        judgehost_task_id="",
        runtime_sec=0.0,
        cpu_sec=0.0,
        wall_sec=0.0,
        memory_kb=0,
        compile_log=detail_error,
        compile_log_truncated=False,
        compile_log_total_chars=len(detail_error),
        diagnostics_json="[]",
        diagnostics_truncated=False,
        diagnostics_total=0,
        error_text=detail_error,
        error_text_truncated=False,
        error_text_total_chars=len(detail_error),
        feedback_text=detail_error,
        feedback_text_truncated=False,
        feedback_text_total_chars=len(detail_error),
        output_ref="",
    )
    config.verification_service.update_verification_record_status(
        resolved_verification_id,
        status=Status.FAILED.value,
        fail_reason=detail_error,
        finished=True,
    )


def annotate_verification_run_result(
    problem_id: int,
    workspace_id: int,
    run_id: str,
    *,
    verification_id: str,
    expected_behavior: str,
    verification_source: str = "run.execute",
) -> dict[str, object]:
    del problem_id
    del workspace_id
    del verification_source
    safe_expected = normalize_expected_behavior(expected_behavior)
    resolved_verification_id = _verification_id_for_run(run_id, verification_id)
    rows = VerificationTaskStore(config.db).list_rows(resolved_verification_id)
    matching_rows = [row for row in rows if str(row["logical_run_id"] or "") == run_id]
    if not matching_rows:
        return {
            "run_id": run_id,
            "status": "missing",
            "expected_behavior": safe_expected,
            "matched": False,
            "completed": False,
            "passed_all_tests": False,
            "reason": "run missing",
        }
    if any(str(row["status"] or "") == VerificationTaskStore.TASK_FAILED for row in matching_rows):
        run_status = "failed"
    elif all(str(row["status"] or "") == VerificationTaskStore.TASK_DONE for row in matching_rows):
        run_status = "ok"
    else:
        run_status = "running"
    summary = {
        "source": str(matching_rows[0]["source_path"] or ""),
        "tests": [
            {
                "test": str(row["test_name"] or ""),
                "verdict": str(row["verdict"] or ""),
                "time_ms": int(round(float(row["runtime_sec"] or 0.0) * 1000.0)),
                "memory_kb": int(row["memory_kb"] or 0),
                "message": str(row["feedback_text"] or row["error_text"] or ""),
                "output_ref": str(row["output_ref"] or ""),
            }
            for row in matching_rows
        ],
        "error": str(matching_rows[0]["error_text"] or ""),
    }
    matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, run_status, summary)
    return {
        "run_id": run_id,
        "status": run_status,
        "expected_behavior": safe_expected,
        "matched": bool(matched),
        "completed": bool(completed),
        "passed_all_tests": bool(observed_pass),
        "reason": reason,
    }
