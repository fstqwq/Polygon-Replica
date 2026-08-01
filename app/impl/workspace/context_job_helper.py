from __future__ import annotations

import secrets
from pathlib import Path

from app.impl.runtime.config import config
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.types import Kind, Status

from .context_run_detail import normalize_run_id_token
from .context_ui import page_ctx


def allocate_run_id() -> str:
    return f"r-{secrets.token_hex(6)}"


def allocate_verification_id() -> str:
    return config.verification_service.allocate_verification_id()


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
    detail = config.verification_service.verification_detail(safe_verification_id)
    metadata_error = str(detail.get("error") or "")
    failed_test = Path(str(detail.get("failed_test") or "")).name
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
    task_store = config.verification_task_store
    task_id = task_store.allocate_id()
    test_name = artifact_failed_test or _failed_test_name(detail_error, synthesized_test_names if synthesize_failed_tests else None)
    config.verification_service.begin_verification_record(
        verification_id=resolved_verification_id,
        problem_id=problem_id,
        workspace_id=workspace_id,
        signature="",
        kind=Kind.CUSTOM.value,
        status=Status.FAILED.value,
        detail={
            "mode": str(mode or "pass-fail"),
            "error": detail_error,
            "failed_test": test_name,
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
        diagnostics_json="[]",
        error_text=detail_error,
        feedback_text=detail_error,
        output_ref="",
    )
    config.verification_service.update_verification_record_status(
        resolved_verification_id,
        status=Status.FAILED.value,
        fail_reason=detail_error,
        finished=True,
    )
