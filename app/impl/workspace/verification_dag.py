from __future__ import annotations

import base64
import json
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.db import now_iso
from app.impl.runtime.config import config
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.source import resolve_source
from app.service.verification.task_metadata import canonical_diagnostics, canonical_truncated_text, diagnostics_json_text
from app.service.verification.task_scheduler import (
    TaskExecutionResult,
    TaskPublishResult,
    VerificationRuntimeCallbacks,
    VerificationRuntimeCoordinator,
    register_verification_runtime_coordinator,
    unregister_verification_runtime_coordinator,
)
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.test_rows import build_verification_test_row
from app.service.verification.types import Kind, Status
from app.service.verification.types import is_cancel_reason

from .context_job_helper import allocate_run_id
from .context_operation import audit
from .context_run_detail import normalize_run_id_token
from .context_verification import _verification_solution_failure_hint, _verification_solution_match
from .sanity_checks import (
    SANITY_FAILED,
    SANITY_PENDING,
    SANITY_PASSED,
    SANITY_RUNNING,
    SANITY_SKIPPED,
    effective_verification_status,
    planned_sanity_checks,
    run_verification_sanity_checks,
)
from .verification_dag_plan import VerificationTestPlan, build_verification_execution_plan
from .problem_config import normalize_pass_limit, normalize_problem_mode, read_problem_config

_C = config.constants

TASK_GENERATE_INPUT = "generate-input"
TASK_MAIN_CORRECT = "main-correct"
TASK_SOLUTION_RUN = "solution-run"

_COMPILE_LOG_CHAR_LIMIT = 16384
_ERROR_TEXT_CHAR_LIMIT = 4096
_FEEDBACK_TEXT_CHAR_LIMIT = 4096
_COMPILE_DIAGNOSTICS_LIMIT = 64


@dataclass(frozen=True)
class LogicalRunSpec:
    logical_run_id: str
    source_path: str
    expected_behavior: str
    task_kind: str


@dataclass(frozen=True)
class VerificationGraph:
    tasks: list[dict[str, object]]
    edges: list[tuple[str, str]]
    logical_runs: list[LogicalRunSpec]
    test_names: list[str]
    accepted_source_path: str


@dataclass(frozen=True)
class TaskExecutionContext:
    problem: str
    user: str
    verification_id: str
    mode: str
    pass_limit: int
    snapshot_root: Path
    layout_root: Path
    tests_root: Path
    answers_root: Path
    source_file_by_path: dict[str, Path]
    test_plan_by_name: dict[str, VerificationTestPlan]
    run_verification_payload_base: dict[str, object]
    generate_verification_payload_base: dict[str, object]


@dataclass(frozen=True)
class _TaskSummaryParts:
    verdict: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    compile_log: str
    compile_log_truncated: bool
    compile_log_total_chars: int
    diagnostics_json: str
    diagnostics_truncated: bool
    diagnostics_total: int
    error_text: str
    error_text_truncated: bool
    error_text_total_chars: int
    feedback_text: str
    feedback_text_truncated: bool
    feedback_text_total_chars: int
    output_ref: str


def _workspace_mode_and_pass_limit(problem_id: int, workspace_id: int) -> tuple[str, int]:
    default_mode = str(_C.GENERAL_CONFIG_DEFAULTS.get("mode") or "pass-fail")
    default_pass_limit = int(_C.GENERAL_CONFIG_DEFAULTS.get("pass_limit") or 1)
    workspace_path_text = config.workspace_service.workspace_path(int(problem_id), int(workspace_id))
    if not workspace_path_text:
        return (default_mode, default_pass_limit)
    workspace_path = Path(workspace_path_text).resolve()
    _payload, general_cfg, _cfg_path = read_problem_config(workspace_path)
    return (
        normalize_problem_mode(general_cfg.get("mode"), default_mode),
        normalize_pass_limit(general_cfg.get("pass_limit"), default_pass_limit),
    )


def _verification_cancel_requested(problem_id: int, actor_user_id: int, verification_id: str, *, limit: int = 240) -> bool:
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return False
    rows = config.workspace_service.audit_details(
        problem_id=int(problem_id),
        actor_user_id=int(actor_user_id),
        action="run.cancel",
        limit=max(40, int(limit)),
    )
    for payload_text in rows:
        try:
            details = cast(dict[str, object], json.loads(payload_text))
        except Exception:
            continue
        if normalize_run_id_token(details.get("verification_id")) == safe_verification_id:
            return True
    return False


def _require_online_judgehost() -> None:
    try:
        status = cast(dict[str, object], config.judgehost_task_service.status())
    except Exception:
        status = {}
    if int(status.get("hosts_online") or 0) <= 0:
        raise RuntimeError("judgehost is offline")


def _answer_name(test_name: str) -> str:
    stem = Path(test_name).stem
    if not stem:
        raise RuntimeError("test name is required")
    return f"{stem}.ans"


def _read_required_bytes(path: Path, *, label: str) -> bytes:
    if (not path.exists()) or (not path.is_file()) or path.is_symlink():
        raise RuntimeError(f"{label} is missing")
    return path.read_bytes()


def _build_graph(
    *,
    task_store: VerificationTaskStore,
    accepted_source_path: str,
    test_plan_by_name: dict[str, VerificationTestPlan],
    targets: list[dict[str, object]],
    test_names: list[str],
) -> VerificationGraph:
    logical_runs: list[LogicalRunSpec] = []
    accepted_selected_run_id = ""
    for target in targets:
        source_path = str(target.get("path") or "")
        if not source_path:
            continue
        expected_behavior = normalize_expected_behavior(target.get("expected_behavior") or "unknown")
        logical_run_id = normalize_run_id_token(target.get("run_id"))
        if not logical_run_id:
            logical_run_id = allocate_run_id()
            target["run_id"] = logical_run_id
        run_task_kind = TASK_SOLUTION_RUN
        if source_path == accepted_source_path and expected_behavior == "accepted":
            run_task_kind = TASK_MAIN_CORRECT
            accepted_selected_run_id = logical_run_id
        logical_runs.append(
            LogicalRunSpec(
                logical_run_id=logical_run_id,
                source_path=source_path,
                expected_behavior=expected_behavior,
                task_kind=run_task_kind,
            )
        )
    tasks: list[dict[str, object]] = []
    edges: list[tuple[str, str]] = []
    queue_index = 1
    main_ids: dict[str, str] = {}
    for test_name in test_names:
        test_plan = test_plan_by_name.get(test_name)
        if test_plan is None:
            raise RuntimeError(f"verification test plan missing for {test_name}")
        generate_id = task_store.allocate_id()
        tasks.append(
            {
                "id": generate_id,
                "task_kind": TASK_GENERATE_INPUT,
                "source_path": test_plan.display_source_path,
                "logical_run_id": "",
                "test_name": test_name,
                "expected_behavior": "accepted",
                "queue_index": queue_index,
                "status": VerificationTaskStore.TASK_PENDING,
            }
        )
        queue_index += 1
        main_id = task_store.allocate_id()
        tasks.append(
            {
                "id": main_id,
                "task_kind": TASK_MAIN_CORRECT,
                "source_path": accepted_source_path,
                "logical_run_id": accepted_selected_run_id,
                "test_name": test_name,
                "expected_behavior": "accepted",
                "queue_index": queue_index,
                "status": VerificationTaskStore.TASK_PENDING,
            }
        )
        queue_index += 1
        edges.append((generate_id, main_id))
        main_ids[test_name] = main_id
    for logical_run in logical_runs:
        if logical_run.task_kind == TASK_MAIN_CORRECT:
            continue
        for test_name in test_names:
            task_id = task_store.allocate_id()
            tasks.append(
                {
                    "id": task_id,
                    "task_kind": TASK_SOLUTION_RUN,
                    "source_path": logical_run.source_path,
                    "logical_run_id": logical_run.logical_run_id,
                    "test_name": test_name,
                    "expected_behavior": logical_run.expected_behavior,
                    "queue_index": queue_index,
                    "status": VerificationTaskStore.TASK_PENDING,
                }
            )
            queue_index += 1
            edges.append((main_ids[test_name], task_id))
    return VerificationGraph(
        tasks=tasks,
        edges=edges,
        logical_runs=logical_runs,
        test_names=test_names,
        accepted_source_path=accepted_source_path,
    )


def _materialize_uploaded_sources(*, layout_root: Path, targets: list[dict[str, object]]) -> dict[str, Path]:
    values: dict[str, Path] = {}
    for target in targets:
        source_path = str(target.get("path") or "")
        upload_name = str(target.get("upload_filename") or "")
        raw_content = target.get("upload_content")
        if (not source_path) or (not upload_name) or (raw_content is None):
            continue
        if not isinstance(raw_content, (bytes, bytearray)):
            raise RuntimeError("uploaded verification source payload is invalid")
        target_path = (layout_root / source_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(bytes(raw_content))
        values[source_path] = target_path
    return values


def _task_kind_label(task_kind: str) -> str:
    if task_kind == TASK_GENERATE_INPUT:
        return "Generate input"
    if task_kind == TASK_MAIN_CORRECT:
        return "Main correct"
    return "Solution run"


def _task_running_entry(row: VerificationTaskRow) -> dict[str, str]:
    source_path = str(row["source_path"] or "")
    source_label = Path(source_path).name if source_path else "-"
    test_name = str(row["test_name"] or "")
    kind = str(row["task_kind"] or "")
    if source_path:
        label = f"{_task_kind_label(kind)}: {source_label} / {test_name}"
    else:
        label = f"{_task_kind_label(kind)}: {test_name}"
    return {
        "task_id": str(row["id"]),
        "task_kind": kind,
        "task_kind_label": _task_kind_label(kind),
        "source_path": source_path,
        "source_label": source_label,
        "test_name": test_name,
        "label": label,
    }


def _empty_counts() -> dict[str, object]:
    by_kind = {
        TASK_GENERATE_INPUT: {status: 0 for status in ("pending", "queued", "running", "done", "failed", "cancelled")},
        TASK_MAIN_CORRECT: {status: 0 for status in ("pending", "queued", "running", "done", "failed", "cancelled")},
        TASK_SOLUTION_RUN: {status: 0 for status in ("pending", "queued", "running", "done", "failed", "cancelled")},
    }
    return {
        "total": 0,
        "pending": 0,
        "queued": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "by_kind": by_kind,
    }


def _task_counts(rows: list[VerificationTaskRow]) -> dict[str, object]:
    counts = _empty_counts()
    by_kind = cast(dict[str, dict[str, int]], counts["by_kind"])
    for row in rows:
        status = str(row["status"])
        display_status = status
        if status == VerificationTaskStore.TASK_LEASED:
            display_status = "running"
        task_kind = str(row["task_kind"])
        counts["total"] = int(counts["total"]) + 1
        if display_status in counts:
            counts[display_status] = int(counts[display_status]) + 1
        kind_counts = by_kind.get(task_kind)
        if kind_counts is not None and display_status in kind_counts:
            kind_counts[display_status] = int(kind_counts[display_status]) + 1
    return counts


def _visible_logical_runs(logical_runs: list[LogicalRunSpec]) -> list[LogicalRunSpec]:
    return [
        logical_run
        for logical_run in logical_runs
        if logical_run.task_kind != TASK_MAIN_CORRECT
    ]


def _task_row_to_test_row(row: VerificationTaskRow) -> dict[str, object]:
    runtime_ms = 0 if row["runtime_sec"] is None else max(0, int(round(float(row["runtime_sec"]) * 1000.0)))
    cpu_ms = runtime_ms if row["cpu_sec"] is None else max(0, int(round(float(row["cpu_sec"]) * 1000.0)))
    wall_ms = cpu_ms if row["wall_sec"] is None else max(0, int(round(float(row["wall_sec"]) * 1000.0)))
    memory_kb = 0 if row["memory_kb"] is None else max(0, int(row["memory_kb"]))
    feedback_text = str(row["feedback_text"] or row["error_text"] or "")
    return build_verification_test_row(
        test_name=str(row["test_name"]),
        verdict=str(row["verdict"] or "--"),
        time_ms=runtime_ms,
        time_user_ms=cpu_ms,
        time_wall_ms=wall_ms,
        memory_kb=memory_kb,
        message=feedback_text,
        output_ref=str(row["output_ref"] or ""),
        feedback_files=[],
    )


def _logical_run_summary(
    *,
    logical_run: LogicalRunSpec,
    rows: list[VerificationTaskRow],
    test_names: list[str],
    mode: str,
    pass_limit: int,
    artifact_verification_id: str,
    fail_flag: bool,
) -> tuple[dict[str, object], str, bool, bool, bool, str]:
    ordered_rows = sorted(rows, key=lambda item: (str(item["test_name"]), str(item["id"])))
    tests: list[dict[str, object]] = []
    compile_log = ""
    compile_log_truncated = False
    compile_log_total_chars = 0
    compile_diagnostics: list[dict[str, object]] = []
    diagnostics_total = 0
    diagnostics_truncated = False
    error_text = ""
    max_time_ms = 0
    max_memory_kb = 0
    saw_pending = False
    saw_queued = False
    saw_running = False
    saw_failed = False
    saw_cancelled = False
    saw_done = False
    for row in ordered_rows:
        status = str(row["status"])
        if status == VerificationTaskStore.TASK_PENDING:
            saw_pending = True
        elif status == VerificationTaskStore.TASK_QUEUED:
            saw_queued = True
        elif status == VerificationTaskStore.TASK_LEASED:
            saw_running = True
        elif status == VerificationTaskStore.TASK_FAILED:
            saw_failed = True
        elif status == VerificationTaskStore.TASK_CANCELLED:
            saw_cancelled = True
        elif status == VerificationTaskStore.TASK_DONE:
            saw_done = True
        if status in {VerificationTaskStore.TASK_DONE, VerificationTaskStore.TASK_FAILED}:
            test_row = _task_row_to_test_row(row)
            tests.append(test_row)
            max_time_ms = max(max_time_ms, int(test_row.get("time_user_ms") or 0))
            max_memory_kb = max(max_memory_kb, int(test_row.get("memory_kb") or 0))
        if (not compile_log) and str(row["compile_log"] or ""):
            compile_log = str(row["compile_log"] or "")
            compile_log_truncated = bool(row["compile_log_truncated"])
            compile_log_total_chars = int(row["compile_log_total_chars"] or len(compile_log))
        task_diagnostics_json = str(row["diagnostics_json"] or "[]")
        try:
            task_diagnostics = cast(list[dict[str, object]], json.loads(task_diagnostics_json))
        except Exception:
            task_diagnostics = []
        if task_diagnostics:
            diagnostics_total += len(task_diagnostics)
            remaining = max(0, _COMPILE_DIAGNOSTICS_LIMIT - len(compile_diagnostics))
            if remaining > 0:
                compile_diagnostics.extend(task_diagnostics[:remaining])
            if len(task_diagnostics) > remaining:
                diagnostics_truncated = True
        if (not error_text) and str(row["error_text"] or ""):
            error_text = str(row["error_text"] or "")
    if diagnostics_total > len(compile_diagnostics):
        diagnostics_truncated = True
    if fail_flag and (saw_cancelled or saw_pending) and (not saw_failed):
        saw_failed = True
    if saw_running:
        run_status = Status.RUNNING.value
    elif saw_queued:
        run_status = Status.QUEUED.value
    elif saw_pending:
        run_status = Status.PENDING.value
    elif saw_failed or saw_cancelled:
        run_status = Status.FAILED.value
    elif saw_done and len(tests) >= len(test_names):
        run_status = Status.OK.value
    elif saw_done:
        run_status = Status.RUNNING.value
    else:
        run_status = Status.PENDING.value
    summary = {
        "artifact_verification_id": artifact_verification_id,
        "mode": mode,
        "pass_limit": pass_limit,
        "source": logical_run.source_path,
        "selected_tests": list(test_names),
        "selected_tests_count": len(test_names),
        "task_kind": logical_run.task_kind,
        "tests": tests,
        "tests_total": len(test_names),
        "compile_log": compile_log,
        "compile_log_truncated": bool(compile_log_truncated),
        "compile_log_total_chars": int(compile_log_total_chars),
        "compile_diagnostics": list(compile_diagnostics),
        "compile_diagnostics_total": diagnostics_total,
        "compile_diagnostics_truncated": bool(diagnostics_truncated),
        "compile_diagnostics_limit": _COMPILE_DIAGNOSTICS_LIMIT,
        "error": error_text,
        "usage": {
            "tests": len(tests),
            "time_ms_total": max_time_ms,
            "time_user_ms_total": max_time_ms,
            "time_wall_ms_total": max_time_ms,
            "memory_kb_peak": max_memory_kb,
        },
    }
    matched, completed, observed_pass, reason = _verification_solution_match(
        logical_run.expected_behavior,
        run_status,
        summary,
    )
    return (summary, run_status, bool(matched), bool(completed), bool(observed_pass), reason)


def _verification_summary_from_tasks(
    *,
    verification_id: str,
    artifact_verification_id: str,
    mode: str,
    pass_limit: int,
    logical_runs: list[LogicalRunSpec],
    rows: list[VerificationTaskRow],
    test_names: list[str],
    fail_flag: bool,
    fail_reason: str,
) -> tuple[str, dict[str, object], dict[str, object]]:
    visible_logical_runs = _visible_logical_runs(logical_runs)
    all_logical_runs = list(logical_runs)
    counts = _task_counts(rows)
    running_tasks = [_task_running_entry(row) for row in rows if str(row["status"]) == VerificationTaskStore.TASK_LEASED]
    first_solution_error = ""
    has_pending_or_running = bool(int(counts["pending"]) or int(counts["queued"]) or int(counts["running"]))
    all_matched = True
    for logical_run in all_logical_runs:
        grouped_rows = [row for row in rows if str(row["logical_run_id"] or "") == logical_run.logical_run_id]
        run_summary, run_status, matched, completed, observed_pass, reason = _logical_run_summary(
            logical_run=logical_run,
            rows=grouped_rows,
            test_names=test_names,
            mode=mode,
            pass_limit=pass_limit,
            artifact_verification_id=artifact_verification_id,
            fail_flag=fail_flag,
        )
        if logical_run.task_kind == TASK_MAIN_CORRECT:
            continue
        reason_text = reason
        if (not matched) and completed and (not reason_text):
            reason_text = _verification_solution_failure_hint(logical_run.source_path, "", str(run_summary.get("error") or ""))
        if (not matched) and completed and (not first_solution_error):
            first_solution_error = reason_text or _verification_solution_failure_hint(
                logical_run.source_path,
                "",
                str(run_summary.get("error") or ""),
            )
        all_matched = all_matched and bool(matched)
    if fail_flag and is_cancel_reason(fail_reason):
        verification_status = Status.FAILED.value
        verification_error = fail_reason or "verification cancelled"
    elif fail_flag:
        verification_status = Status.FAILED.value
        verification_error = fail_reason or first_solution_error or "verification failed"
    elif has_pending_or_running:
        verification_status = Status.RUNNING.value
        verification_error = ""
    elif int(counts["cancelled"]) > 0:
        verification_status = Status.FAILED.value
        verification_error = fail_reason or "verification cancelled"
    elif visible_logical_runs and all_matched:
        verification_status = Status.OK.value
        verification_error = ""
    elif (not visible_logical_runs) and int(counts["total"]) > 0:
        verification_status = Status.OK.value
        verification_error = ""
    else:
        verification_status = Status.FAILED.value
        verification_error = first_solution_error or "verification failed"
    summary = {
        "verification_id": verification_id,
        "artifact_verification_id": artifact_verification_id,
        "task_graph": True,
        "status": verification_status,
        "error": verification_error,
        "task_counts": counts,
        "running_tasks": running_tasks,
        "fail_flag": bool(fail_flag),
        "fail_reason": fail_reason,
        "source_paths": [item.source_path for item in visible_logical_runs],
        "test_names": list(test_names),
        "mode": mode,
        "pass_limit": pass_limit,
        "updated_at": now_iso(),
        "finished_at": now_iso() if verification_status in {Status.OK.value, Status.FAILED.value} and not has_pending_or_running else "",
    }
    return (verification_status, summary, counts)


def _logical_runs_from_task_rows(rows: list[VerificationTaskRow]) -> list[LogicalRunSpec]:
    ordered_rows = sorted(rows, key=lambda item: (int(item["queue_index"]), str(item["id"])))
    logical_runs: list[LogicalRunSpec] = []
    seen: set[str] = set()
    for row in ordered_rows:
        logical_run_id = normalize_run_id_token(str(row["logical_run_id"] or ""))
        if (not logical_run_id) or logical_run_id in seen:
            continue
        seen.add(logical_run_id)
        logical_runs.append(
            LogicalRunSpec(
                logical_run_id=logical_run_id,
                source_path=str(row["source_path"] or ""),
                expected_behavior=normalize_expected_behavior(str(row["expected_behavior"] or "unknown")),
                task_kind=str(row["task_kind"] or ""),
            )
        )
    return logical_runs


def _test_names_from_task_rows(rows: list[VerificationTaskRow]) -> list[str]:
    ordered_rows = sorted(rows, key=lambda item: (int(item["queue_index"]), str(item["id"])))
    values: list[str] = []
    for row in ordered_rows:
        test_name = str(row["test_name"] or "")
        if test_name and test_name not in values:
            values.append(test_name)
    return values


def _effective_verification_kind(
    *,
    sample_only: bool,
    requested_test_names: list[str],
    test_names: list[str],
) -> str:
    if sample_only:
        return Kind.SAMPLE.value
    if requested_test_names and len(requested_test_names) < len(test_names):
        return Kind.CUSTOM.value
    return Kind.ALL.value


def _test_payload_entry(
    *,
    test_name: str,
    input_bytes: bytes,
    answer_bytes: bytes,
) -> dict[str, str]:
    return {
        "name": test_name,
        "input_b64": base64.b64encode(input_bytes).decode("ascii"),
        "answer_name": _answer_name(test_name),
        "answer_b64": base64.b64encode(answer_bytes).decode("ascii"),
    }


def _prepared_payload_for_uploaded_source(
    *,
    source_label: str,
    run_id: str,
    test_name: str,
    input_bytes: bytes,
    answer_bytes: bytes,
    verification_payload_base: dict[str, object],
    extra_sources_b64: dict[str, str] | None = None,
    manual_validate_only: bool = False,
) -> dict[str, object]:
    verification_payload = copy.deepcopy(verification_payload_base)
    verification_payload["tests"] = [_test_payload_entry(test_name=test_name, input_bytes=input_bytes, answer_bytes=answer_bytes)]
    prepared: dict[str, object] = {
        "run_id": run_id,
        "verification_payload": verification_payload,
        "source_label": source_label,
    }
    if extra_sources_b64:
        prepared["extra_sources_b64"] = dict(extra_sources_b64)
    if manual_validate_only:
        prepared["manual_validate_only"] = True
    return prepared


def _resolve_output_blob(output_ref: str) -> bytes | None:
    if not output_ref:
        return None
    try:
        return config.judgehost_task_service.resolve_artifact_blob(output_ref)
    except Exception:
        return None


def _source_bytes_for_path(execution: TaskExecutionContext, source_path: str) -> tuple[str, bytes]:
    source_file = execution.source_file_by_path.get(source_path)
    if source_file is None:
        raise RuntimeError(f"verification source is missing: {source_path}")
    return (source_file.name, source_file.read_bytes())


def _verdict_from_summary(summary: dict[str, object], run_status: str) -> str:
    tests = cast(list[dict[str, object]], summary.get("tests") or [])
    if tests:
        verdict = str(tests[-1].get("verdict") or "").upper()
        if verdict:
            return verdict
    diagnostics = cast(list[dict[str, object]], summary.get("compile_diagnostics") or [])
    if diagnostics:
        return "CE"
    return "AC" if run_status == Status.OK.value else "FL"


def _summary_parts(summary: dict[str, object], *, run_status: str, error_text: str) -> _TaskSummaryParts:
    verdict = _verdict_from_summary(summary, run_status)
    compile_log_source = str(summary.get("error") or error_text or "")
    compile_log_meta = canonical_truncated_text(compile_log_source, limit=_COMPILE_LOG_CHAR_LIMIT)
    diagnostics_meta = canonical_diagnostics(
        cast(list[dict[str, object]] | list[object] | None, summary.get("compile_diagnostics") or []),
        list_limit=_COMPILE_DIAGNOSTICS_LIMIT,
        message_limit=_ERROR_TEXT_CHAR_LIMIT,
    )
    diagnostics_json = diagnostics_json_text(diagnostics_meta["rows"])
    error_value = str(error_text or summary.get("error") or "")
    error_text_meta = canonical_truncated_text(error_value, limit=_ERROR_TEXT_CHAR_LIMIT)
    tests = cast(list[dict[str, object]], summary.get("tests") or [])
    feedback_source = ""
    output_ref = ""
    runtime_sec: float | None = None
    cpu_sec: float | None = None
    wall_sec: float | None = None
    memory_kb: int | None = None
    if tests:
        test_row = dict(tests[-1])
        feedback_source = str(test_row.get("message") or "")
        output_ref = str(test_row.get("output_ref") or "")
        cpu_ms = int(test_row.get("time_user_ms") or test_row.get("time_ms") or 0)
        wall_ms = int(test_row.get("time_wall_ms") or cpu_ms)
        runtime_ms = int(test_row.get("time_ms") or cpu_ms)
        memory_kb = int(test_row.get("memory_kb") or 0)
        runtime_sec = float(runtime_ms) / 1000.0
        cpu_sec = float(cpu_ms) / 1000.0
        wall_sec = float(wall_ms) / 1000.0
    feedback_meta = canonical_truncated_text(feedback_source, limit=_FEEDBACK_TEXT_CHAR_LIMIT)
    return _TaskSummaryParts(
        verdict=verdict,
        runtime_sec=runtime_sec,
        cpu_sec=cpu_sec,
        wall_sec=wall_sec,
        memory_kb=memory_kb,
        compile_log=compile_log_meta["text"],
        compile_log_truncated=bool(compile_log_meta["truncated"]),
        compile_log_total_chars=int(compile_log_meta["total_chars"]),
        diagnostics_json=diagnostics_json,
        diagnostics_truncated=bool(diagnostics_meta["truncated"]),
        diagnostics_total=int(diagnostics_meta["total"]),
        error_text=error_text_meta["text"],
        error_text_truncated=bool(error_text_meta["truncated"]),
        error_text_total_chars=int(error_text_meta["total_chars"]),
        feedback_text=feedback_meta["text"],
        feedback_text_truncated=bool(feedback_meta["truncated"]),
        feedback_text_total_chars=int(feedback_meta["total_chars"]),
        output_ref=output_ref,
    )


def _empty_task_result(
    *,
    task_id: str,
    status: str,
    verdict: str,
    run_id: str,
    judgehost_task_id: str,
    error_text: str,
    fail_flag_reason: str = "",
) -> TaskExecutionResult:
    error_meta = canonical_truncated_text(error_text, limit=_ERROR_TEXT_CHAR_LIMIT)
    return TaskExecutionResult(
        task_id=task_id,
        status=status,
        verdict=verdict,
        run_id=run_id,
        judgehost_task_id=judgehost_task_id,
        runtime_sec=None,
        cpu_sec=None,
        wall_sec=None,
        memory_kb=None,
        compile_log="",
        compile_log_truncated=False,
        compile_log_total_chars=0,
        diagnostics_json="[]",
        diagnostics_truncated=False,
        diagnostics_total=0,
        error_text=error_meta["text"],
        error_text_truncated=bool(error_meta["truncated"]),
        error_text_total_chars=int(error_meta["total_chars"]),
        feedback_text="",
        feedback_text_truncated=False,
        feedback_text_total_chars=0,
        output_ref="",
        fail_flag_reason=fail_flag_reason,
    )


def _publish_generate_task(task_row: VerificationTaskRow, *, execution: TaskExecutionContext, test_plan: VerificationTestPlan) -> TaskPublishResult:
    task_id = str(task_row["id"])
    test_name = str(task_row["test_name"])
    run_id = allocate_run_id()
    try:
        if test_plan.source_kind == "manual":
            (execution.tests_root / test_name).write_bytes(test_plan.execution_input_bytes)
        prepared = _prepared_payload_for_uploaded_source(
            source_label=test_plan.execution_source_name,
            run_id=run_id,
            test_name=test_name,
            input_bytes=test_plan.execution_input_bytes,
            answer_bytes=b"",
            verification_payload_base=execution.generate_verification_payload_base,
            extra_sources_b64=test_plan.extra_sources_b64,
            manual_validate_only=test_plan.source_kind == "manual",
        )
        judgehost_task_id = config.judgehost_task_service.enqueue_task(
            problem=execution.problem,
            username=execution.user,
            artifact_verification_id=execution.verification_id,
            mode=execution.mode,
            submission_path=None,
            upload_content=test_plan.execution_source_bytes,
            upload_filename=test_plan.execution_source_name,
            run_id=str(prepared.get("run_id") or run_id),
            selected_tests=[],
            verification_id=execution.verification_id,
            verification_run_ids=[str(prepared.get("run_id") or run_id)],
            expected_behavior="accepted",
            verification_source=TASK_GENERATE_INPUT,
            task_kind=TASK_GENERATE_INPUT,
            force_recompile=False,
            compile_only=False,
            persist_verification_run=False,
            prepared_payload=prepared,
        )
        return TaskPublishResult(
            task_id=task_id,
            run_id=str(prepared.get("run_id") or run_id),
            judgehost_task_id=judgehost_task_id,
        )
    except Exception as exc:
        result = _empty_task_result(
            task_id=task_id,
            status=VerificationTaskStore.TASK_FAILED,
            verdict="FL",
            run_id=run_id,
            judgehost_task_id="",
            error_text=str(exc),
            fail_flag_reason=str(exc),
        )
        return TaskPublishResult(
            task_id=task_id,
            run_id=run_id,
            judgehost_task_id="",
            terminal_result=result,
        )


def _publish_run_task(task_row: VerificationTaskRow, *, execution: TaskExecutionContext) -> TaskPublishResult:
    task_id = str(task_row["id"])
    task_kind = str(task_row["task_kind"])
    source_path = str(task_row["source_path"])
    test_name = str(task_row["test_name"])
    logical_run_id = str(task_row["logical_run_id"] or "")
    run_id = allocate_run_id()
    try:
        input_bytes = _read_required_bytes(execution.tests_root / test_name, label=f"verification test {test_name}")
        if task_kind == TASK_MAIN_CORRECT:
            answer_bytes = b""
            verification_source = TASK_MAIN_CORRECT
            expected_behavior = "accepted"
        else:
            answer_bytes = _read_required_bytes(execution.answers_root / _answer_name(test_name), label=f"verification answer {test_name}")
            verification_source = TASK_SOLUTION_RUN
            expected_behavior = normalize_expected_behavior(task_row["expected_behavior"])
        source_name, source_bytes = _source_bytes_for_path(execution, source_path)
        prepared = _prepared_payload_for_uploaded_source(
            source_label=source_path,
            run_id=run_id,
            test_name=test_name,
            input_bytes=input_bytes,
            answer_bytes=answer_bytes,
            verification_payload_base=execution.run_verification_payload_base,
        )
        judgehost_task_id = config.judgehost_task_service.enqueue_task(
            problem=execution.problem,
            username=execution.user,
            artifact_verification_id=execution.verification_id,
            mode=execution.mode,
            submission_path=None,
            upload_content=source_bytes,
            upload_filename=source_name,
            run_id=run_id,
            selected_tests=[test_name],
            verification_id=execution.verification_id,
            verification_run_ids=[logical_run_id or run_id],
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            force_recompile=False,
            compile_only=False,
            persist_verification_run=False,
            prepared_payload=prepared,
        )
        return TaskPublishResult(task_id=task_id, run_id=run_id, judgehost_task_id=judgehost_task_id)
    except Exception as exc:
        fail_flag_reason = str(exc) if task_kind == TASK_MAIN_CORRECT else ""
        result = _empty_task_result(
            task_id=task_id,
            status=VerificationTaskStore.TASK_FAILED,
            verdict="FL",
            run_id=run_id,
            judgehost_task_id="",
            error_text=str(exc),
            fail_flag_reason=fail_flag_reason,
        )
        return TaskPublishResult(
            task_id=task_id,
            run_id=run_id,
            judgehost_task_id="",
            terminal_result=result,
        )


def _publish_task(task_row: VerificationTaskRow, *, execution: TaskExecutionContext) -> TaskPublishResult:
    test_name = str(task_row["test_name"])
    test_plan = execution.test_plan_by_name.get(test_name)
    if test_plan is None:
        result = _empty_task_result(
            task_id=str(task_row["id"]),
            status=VerificationTaskStore.TASK_FAILED,
            verdict="FL",
            run_id="",
            judgehost_task_id="",
            error_text=f"verification test plan missing for {test_name}",
            fail_flag_reason=f"verification test plan missing for {test_name}",
        )
        return TaskPublishResult(
            task_id=str(task_row["id"]),
            run_id="",
            judgehost_task_id="",
            terminal_result=result,
        )
    if str(task_row["task_kind"]) == TASK_GENERATE_INPUT:
        return _publish_generate_task(task_row, execution=execution, test_plan=test_plan)
    return _publish_run_task(task_row, execution=execution)

def run_workspace_verification_dag(
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, object]],
    verification_id: str,
    signature: str = "",
    kind: str = Kind.ALL.value,
    sample_only: bool = False,
    snapshot_root_override: Path | None = None,
    selected_test_names: list[str] | None = None,
) -> None:
    workspace_path_text = config.workspace_service.workspace_path(int(problem_id), int(workspace_id))
    if not workspace_path_text:
        raise RuntimeError("workspace metadata missing")
    workspace_path = Path(workspace_path_text).resolve()
    if (not workspace_path.exists()) or (not workspace_path.is_dir()) or workspace_path.is_symlink():
        raise RuntimeError("workspace path is unavailable")
    task_store = VerificationTaskStore(config.db)
    layout = config.fs_manager.prepare_verification_layout(verification_id)
    snapshot_root = snapshot_root_override
    if snapshot_root is None:
        snapshot_root = config.workspace_service.create_snapshot(
            workspace_path,
            None,
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
        )
    execution_plan = None
    try:
        execution_plan = build_verification_execution_plan(
            snapshot_root,
            bin_dir=layout.bin,
            sample_only=bool(sample_only),
        )
        _require_online_judgehost()
    except Exception as exc:
        verification_mode, verification_pass_limit = _workspace_mode_and_pass_limit(problem_id, workspace_id)
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            kind=kind,
            status=Status.FAILED.value,
        )
        config.verification_service.persist_verification_metadata(
            verification_id,
            {
                "mode": verification_mode,
                "pass_limit": verification_pass_limit,
                "task_graph": True,
                "signature": signature,
                "kind": kind,
                "source_paths": [str(item.get("path") or "") for item in targets if str(item.get("path") or "")],
                "selected_test_names": list(selected_test_names or []),
                "error": str(exc),
            },
        )
        config.verification_service.update_verification_record_status(
            verification_id,
            status=Status.FAILED.value,
            fail_reason=str(exc),
            finished=True,
        )
        audit(
            actor_user_id,
            problem_id,
            "verification.start",
            {
                "verification_id": verification_id,
                "status": "failed",
                "artifact_verification_id": verification_id,
                "error": str(exc),
            },
        )
        if snapshot_root.exists():
            import shutil

            shutil.rmtree(snapshot_root.parent, ignore_errors=True)
        return
    assert execution_plan is not None
    try:
        verification_mode = execution_plan.mode
        verification_pass_limit = execution_plan.pass_limit
        source_file_by_path = dict(execution_plan.source_file_by_path)
        source_file_by_path.update(_materialize_uploaded_sources(layout_root=layout.root, targets=targets))
        snapshot_resolved = execution_plan.snapshot_root.resolve()
        for target in targets:
            source_path = str(target.get("path") or "")
            if not source_path or source_path in source_file_by_path:
                continue
            source_file_by_path[source_path] = resolve_source(
                execution_plan.snapshot_root,
                source_path,
                snapshot_resolved=snapshot_resolved,
            )
        requested_test_names = selected_test_names or []
        if requested_test_names:
            selected_name_set = {str(name) for name in requested_test_names if str(name)}
            test_names = [name for name in execution_plan.test_names if name in selected_name_set]
            if not test_names:
                raise RuntimeError("selected tests are unavailable")
        else:
            test_names = list(execution_plan.test_names)
        effective_kind = _effective_verification_kind(
            sample_only=bool(sample_only),
            requested_test_names=list(requested_test_names),
            test_names=test_names,
        )
        selected_test_plans = [execution_plan.test_plan_by_name[name] for name in test_names if name in execution_plan.test_plan_by_name]
        sanity_checks = planned_sanity_checks(selected_test_plans)
        sanity_status = SANITY_PENDING if sanity_checks else ""
        graph = _build_graph(
            task_store=task_store,
            accepted_source_path=execution_plan.accepted_source_path,
            test_plan_by_name=execution_plan.test_plan_by_name,
            targets=targets,
            test_names=test_names,
        )
        visible_logical_runs = _visible_logical_runs(graph.logical_runs)
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            kind=effective_kind,
            status=Status.RUNNING.value,
        )
        config.verification_service.persist_verification_metadata(
            verification_id,
            {
                "mode": verification_mode,
                "pass_limit": verification_pass_limit,
                "task_graph": True,
                "signature": signature,
                "kind": effective_kind,
                "source_paths": [item.source_path for item in visible_logical_runs],
                "selected_test_names": list(test_names),
                "sanity_checks": list(sanity_checks),
                "sanity_status": sanity_status,
            },
        )
        (layout.logs / "run_config.json").write_text(
            str(execution_plan.run_verification_payload_base.get("run_config_json") or ""),
            encoding="utf-8",
        )
        (layout.logs / "tests_meta.json").write_text(
            json.dumps(execution_plan.tests_meta_rows, indent=2),
            encoding="utf-8",
        )
        task_store.replace_graph(verification_id, tasks=graph.tasks, edges=graph.edges)
        execution = TaskExecutionContext(
            problem=problem,
            user=user,
            verification_id=verification_id,
            mode=verification_mode,
            pass_limit=verification_pass_limit,
            snapshot_root=execution_plan.snapshot_root,
            layout_root=layout.root,
            tests_root=layout.tests,
            answers_root=layout.ans,
            source_file_by_path=source_file_by_path,
            test_plan_by_name=execution_plan.test_plan_by_name,
            run_verification_payload_base=execution_plan.run_verification_payload_base,
            generate_verification_payload_base=execution_plan.generate_verification_payload_base,
        )

        def _refresh_state() -> dict[str, object]:
            rows = task_store.list_rows(verification_id)
            fail_flag, fail_reason = task_store.fail_state(verification_id)
            task_status, summary, counts = _verification_summary_from_tasks(
                verification_id=verification_id,
                artifact_verification_id=verification_id,
                mode=verification_mode,
                pass_limit=verification_pass_limit,
                logical_runs=graph.logical_runs,
                rows=rows,
                test_names=test_names,
                fail_flag=fail_flag,
                fail_reason=fail_reason,
            )
            if rows and int(counts["total"]) <= 0:
                raise RuntimeError("verification task graph has rows but computed zero task counts")
            status, finished = effective_verification_status(
                task_status=task_status,
                counts=counts,
                sanity_checks=sanity_checks,
                sanity_status=sanity_status,
            )
            config.verification_service.update_verification_record_status(
                verification_id,
                status=status,
                fail_reason=str(summary.get("error") or ""),
                finished=finished,
            )
            return counts

        def _cancel_queued_tasks(reason: str) -> None:
            statuses_by_run: dict[str, set[str]] = {}
            for row in task_store.list_rows(verification_id):
                run_id = normalize_run_id_token(str(row["run_id"] or ""))
                if not run_id:
                    continue
                status = str(row["status"] or "")
                run_statuses = statuses_by_run.get(run_id)
                if run_statuses is None:
                    run_statuses = set()
                    statuses_by_run[run_id] = run_statuses
                run_statuses.add(status)
            run_ids: list[str] = []
            for run_id, statuses in statuses_by_run.items():
                if VerificationTaskStore.TASK_QUEUED not in statuses:
                    continue
                if VerificationTaskStore.TASK_LEASED in statuses:
                    continue
                run_ids.append(run_id)
            if not run_ids:
                return
            config.judgehost_task_service.cancel_tasks_for_runs(run_ids, reason=reason)
            config.judgehost_task_service.cancel_domjudge_jobs_for_runs(run_ids, final_status=Status.FAILED.value)

        def _cancel_active_tasks(reason: str) -> None:
            run_ids: list[str] = []
            for row in task_store.list_rows(verification_id):
                if str(row["status"] or "") not in {
                    VerificationTaskStore.TASK_QUEUED,
                    VerificationTaskStore.TASK_LEASED,
                }:
                    continue
                run_id = normalize_run_id_token(str(row["run_id"] or ""))
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
            if not run_ids:
                return
            config.judgehost_task_service.cancel_tasks_for_runs(run_ids, reason=reason)
            config.judgehost_task_service.cancel_domjudge_jobs_for_runs(run_ids, final_status=Status.FAILED.value)

        _refresh_state()
        callbacks = VerificationRuntimeCallbacks(
            publish_task=lambda row: _publish_task(row, execution=execution),
            resolve_case_result=lambda judgehost_task_id, test_name: config.judgehost_task_service.poll_task_case_result(
                judgehost_task_id,
                test_name,
            ),
            cancel_queued_tasks=_cancel_queued_tasks,
            persist_state=_refresh_state,
        )
        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=task_store,
            callbacks=callbacks,
            edges=graph.edges,
        )
        register_verification_runtime_coordinator(verification_id, coordinator)
        try:
            coordinator.run()
        except Exception as exc:
            _cancel_active_tasks(str(exc) or "verification scheduler failed")
            task_store.set_fail_flag(verification_id, reason=str(exc) or "verification scheduler failed")
            task_store.cancel_unfinished_tasks(verification_id, reason=str(exc) or "verification scheduler failed")
            _refresh_state()
            raise
        finally:
            unregister_verification_runtime_coordinator(verification_id)
        _refresh_state()
        record = config.verification_service.verification_record(verification_id) or {}
        metadata = config.verification_service.verification_metadata(verification_id)
        rows = task_store.list_rows(verification_id)
        fail_flag, fail_reason = task_store.fail_state(verification_id)
        _status, summary, _counts = _verification_summary_from_tasks(
            verification_id=verification_id,
            artifact_verification_id=verification_id,
            mode=normalize_problem_mode(metadata.get("mode"), verification_mode),
            pass_limit=normalize_pass_limit(metadata.get("pass_limit"), verification_pass_limit),
            logical_runs=graph.logical_runs,
            rows=rows,
            test_names=test_names,
            fail_flag=fail_flag,
            fail_reason=fail_reason,
        )
        if _status == Status.OK.value and sanity_checks:
            sanity_status = SANITY_RUNNING
            updated_metadata = dict(metadata)
            updated_metadata["sanity_status"] = sanity_status
            config.verification_service.persist_verification_metadata(verification_id, updated_metadata)
            sanity_result = run_verification_sanity_checks(
                problem=problem,
                user=user,
                verification_id=verification_id,
                mode=verification_mode,
                logs_dir=layout.logs,
                test_plans=selected_test_plans,
            )
            metadata = config.verification_service.verification_metadata(verification_id)
            updated_metadata = dict(metadata)
            sanity_status = sanity_result.status
            updated_metadata["sanity_status"] = sanity_status
            updated_metadata["sanity_checked_count"] = int(sanity_result.checked_count)
            updated_metadata["validation_status"] = sanity_result.status
            updated_metadata["validated_count"] = int(sanity_result.checked_count)
            if sanity_result.status == SANITY_FAILED:
                updated_metadata["failed_step"] = "sanity"
                updated_metadata["failed_check"] = sanity_result.check_name
                updated_metadata["failed_test"] = sanity_result.failed_test
                updated_metadata["error"] = sanity_result.error
                config.verification_service.persist_verification_metadata(verification_id, updated_metadata)
                config.verification_service.update_verification_record_status(
                    verification_id,
                    status=Status.FAILED.value,
                    fail_reason=sanity_result.error,
                    finished=True,
                )
                record["status"] = Status.FAILED.value
                record["fail_reason"] = sanity_result.error
                summary["status"] = Status.FAILED.value
                summary["error"] = sanity_result.error
            else:
                updated_metadata.pop("failed_step", None)
                updated_metadata.pop("failed_check", None)
                updated_metadata.pop("failed_test", None)
                if sanity_result.status == SANITY_PASSED:
                    updated_metadata.pop("error", None)
                config.verification_service.persist_verification_metadata(verification_id, updated_metadata)
                config.verification_service.update_verification_record_status(
                    verification_id,
                    status=Status.OK.value,
                    fail_reason="",
                    finished=True,
                )
                record["status"] = Status.OK.value
                record["fail_reason"] = ""
                summary["status"] = Status.OK.value
        elif sanity_checks and sanity_status == SANITY_PENDING:
            metadata = config.verification_service.verification_metadata(verification_id)
            updated_metadata = dict(metadata)
            updated_metadata["sanity_status"] = SANITY_SKIPPED
            config.verification_service.persist_verification_metadata(verification_id, updated_metadata)
            sanity_status = SANITY_SKIPPED
        audit(
            actor_user_id,
            problem_id,
            "verification.start",
            {
                "verification_id": verification_id,
                "status": summary.get("status") or record.get("status") or "",
                "artifact_verification_id": verification_id,
                "error": summary.get("error") or "",
                "task_counts": summary.get("task_counts") or {},
                "fail_flag": bool(summary.get("fail_flag")),
                "fail_reason": summary.get("fail_reason") or "",
            },
        )
    finally:
        if snapshot_root.exists():
            import shutil

            shutil.rmtree(snapshot_root.parent, ignore_errors=True)
