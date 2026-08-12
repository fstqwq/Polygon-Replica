from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.db import now_iso
from app.impl.runtime.config import config
from app.impl.workspace.sanity_checks import (
    SANITY_FAILED,
    SANITY_PENDING,
    SANITY_PASSED,
    SANITY_RUNNING,
    SANITY_SKIPPED,
    SANITY_WARNING,
    planned_sanity_checks,
    run_verification_sanity_checks,
)
from app.impl.workspace.runtime_threshold import time_limit_ms_from_run_config_json
from app.impl.workspace.verification_dag_plan import build_verification_execution_plan
from app.impl.workspace.verification_payload import prepared_payload_for_uploaded_source
from app.service.execution.identity import new_run_id
from app.service.execution.policy import normalize_execution_result
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.completion import verification_task_fail_reason
from app.service.verification.execution import (
    VerificationCoordinatorFailure,
    VerificationExecutionCallbacks,
)
from app.service.verification.failure_display import verification_solution_failure_hint
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    SanityFinish,
    TASK_GENERATE_INPUT,
    TASK_MAIN_CORRECT,
    TASK_SOLUTION_RUN,
    VerificationCompileSpec,
    VerificationProgram,
    verification_task_id,
)
from app.service.verification.plan import VerificationTestPlan
from app.service.verification.result_match import run_actual_failed_codes
from app.service.verification.signature import (
    VerificationManifest,
    verification_manifest,
)
from app.service.problem.source_file import resolve_source
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_scheduler import TaskPublishResult
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.execution.test_rows import build_execution_test_row
from app.service.verification.types import Kind, VerificationStatus, VerificationTaskStatus

_C = config.config_values

_ARTIFACT_READY_TIMEOUT_SEC = 2.0
_ARTIFACT_READY_INTERVAL_SEC = 0.05

_COMPILE_DIAGNOSTICS_LIMIT = 64


@dataclass(frozen=True)
class VerificationGraph:
    tasks: tuple[PlannedTask, ...]
    programs: tuple[VerificationProgram, ...]
    test_names: list[str]
    accepted_source_path: str

    @property
    def edges(self) -> list[tuple[str, str]]:
        return [
            (task.predecessor_task_id, task.task_id)
            for task in self.tasks
            if task.predecessor_task_id is not None
        ]


@dataclass(frozen=True)
class TaskExecutionContext:
    problem: str
    user: str
    verification_id: str
    mode: str
    pass_limit: int
    snapshot_root: Path
    artifact_file_by_test_ref: dict[tuple[str, str], PayloadFile]
    program_by_id: dict[str, VerificationProgram]
    execution_template_by_program_id: dict[str, dict[str, object]]
    test_plan_by_name: dict[str, VerificationTestPlan]
    run_verification_payload_base: dict[str, object]
    generate_verification_payload_base: dict[str, object]
    bypass_case_result_cache: bool
    task_store: VerificationTaskStore | None = None


def _require_online_judgehost() -> None:
    try:
        status = cast(dict[str, object], config.judgehost_task_service.status())
    except Exception:
        status = {}
    if int(status.get("hosts_online") or 0) <= 0:
        raise RuntimeError("judgehost is offline")


def _verification_required_file(
    verification_id: str,
    test_name: str,
    ref_key: str,
    *,
    label: str,
    cache: dict[tuple[str, str], PayloadFile] | None = None,
    timeout_sec: float = _ARTIFACT_READY_TIMEOUT_SEC,
    interval_sec: float = _ARTIFACT_READY_INTERVAL_SEC,
) -> PayloadFile:
    cache_key = (test_name, ref_key)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        ref = config.verification_service.verification_artifact_ref(verification_id, test_name, ref_key)
        if ref:
            payload = config.runtime_blob_store.descriptor(ref)
            if payload is not None:
                if cache is not None:
                    cache[cache_key] = payload
                return payload
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.001, min(float(interval_sec), deadline - time.monotonic())))
    raise RuntimeError(f"{label} is missing")


def _build_graph(
    *,
    verification_id: str,
    accepted_source_path: str,
    source_file_by_path: dict[str, PayloadFile],
    test_plan_by_name: dict[str, VerificationTestPlan],
    targets: list[dict[str, object]],
    test_names: list[str],
) -> VerificationGraph:
    accepted_source_file = source_file_by_path.get(accepted_source_path)
    if accepted_source_file is None:
        raise RuntimeError("accepted verification source is missing")
    programs: list[VerificationProgram] = [
        VerificationProgram(
            program_id="accepted",
            kind=TASK_MAIN_CORRECT,
            source_path=accepted_source_path,
            compile_spec=VerificationCompileSpec(
                source_name=Path(accepted_source_path).name,
                source_file=accepted_source_file,
            ),
            expected_behavior="accepted",
        )
    ]
    target_slot = 0
    accepted_target_seen = False
    for target in targets:
        source_path = str(target.get("path") or "")
        if not source_path:
            continue
        expected_behavior = normalize_expected_behavior(target.get("expected_behavior") or "unknown")
        if source_path == accepted_source_path and expected_behavior == "accepted":
            if str(target.get("program_id") or "") != "accepted":
                raise RuntimeError(
                    "accepted verification target has invalid program identity"
                )
            if accepted_target_seen:
                raise RuntimeError("accepted verification target is duplicated")
            accepted_target_seen = True
            continue
        expected_program_id = f"solution-{target_slot}"
        program_id = str(target.get("program_id") or "")
        if program_id != expected_program_id:
            raise RuntimeError(
                f"verification target {source_path} must use program "
                f"{expected_program_id}"
            )
        target_slot += 1
        source_file = source_file_by_path.get(source_path)
        if source_file is None:
            raise RuntimeError(f"verification source is missing: {source_path}")
        programs.append(
            VerificationProgram(
                program_id=program_id,
                kind=TASK_SOLUTION_RUN,
                source_path=source_path,
                compile_spec=VerificationCompileSpec(
                    source_name=Path(source_path).name,
                    source_file=source_file,
                ),
                expected_behavior=expected_behavior,
            )
        )
    tasks: list[PlannedTask] = []
    main_ids: dict[str, str] = {}
    generator_program_ids: dict[tuple[str, str], str] = {}
    generator_compile_identity_by_program: dict[
        str,
        tuple[str, str, tuple[tuple[str, str], ...], bool],
    ] = {}
    generator_owner_by_invocation: dict[tuple[object, ...], str] = {}
    generator_test_name_by_id: dict[str, str] = {}
    for test_name in test_names:
        test_plan = test_plan_by_name.get(test_name)
        if test_plan is None:
            raise RuntimeError(f"verification test plan missing for {test_name}")
        generator_compile_spec = VerificationCompileSpec(
            source_name=test_plan.execution_source_name,
            source_file=test_plan.execution_source_file,
            extra_source_files=tuple(sorted(test_plan.extra_source_files.items())),
            manual_validate_only=test_plan.source_kind == "manual",
        )
        generator_compile_identity = (
            generator_compile_spec.source_name,
            generator_compile_spec.source_file.identity,
            tuple(
                (name, source.identity)
                for name, source in generator_compile_spec.extra_source_files
            ),
            generator_compile_spec.manual_validate_only,
        )
        generator_program_identity = (
            test_plan.source_kind,
            test_plan.display_source_path,
        )
        generator_program_id = generator_program_ids.get(
            generator_program_identity
        )
        if generator_program_id is None:
            generator_program_id = f"generator-{len(generator_program_ids)}"
            generator_program_ids[
                generator_program_identity
            ] = generator_program_id
            generator_compile_identity_by_program[
                generator_program_id
            ] = generator_compile_identity
            programs.append(
                VerificationProgram(
                    program_id=generator_program_id,
                    kind=TASK_GENERATE_INPUT,
                    source_path=test_plan.display_source_path,
                    compile_spec=generator_compile_spec,
                    expected_behavior="accepted",
                )
            )
        elif (
            generator_compile_identity_by_program[generator_program_id]
            != generator_compile_identity
        ):
            raise RuntimeError(
                f"verification generator program {generator_program_id} "
                "changed compile specification"
            )
        generate_id = verification_task_id(
            verification_id,
            generator_program_id,
            test_name,
        )
        invocation_key: tuple[object, ...] = (
            generator_compile_spec,
            test_plan.execution_input_file.identity,
            test_plan.source_kind == "manual",
        )
        owner_generate_id = generator_owner_by_invocation.get(invocation_key)
        generator_owner_by_invocation.setdefault(invocation_key, generate_id)
        duplicate_feedback = ""
        if owner_generate_id is not None:
            duplicate_feedback = (
                "duplicate generator invocation; skipped, same as "
                f"{generator_test_name_by_id[owner_generate_id]}"
            )
        generator_test_name_by_id[generate_id] = test_name
        tasks.append(
            PlannedTask(
                task_id=generate_id,
                predecessor_task_id=owner_generate_id,
                task_kind=TASK_GENERATE_INPUT,
                source_path=test_plan.display_source_path,
                program_id=generator_program_id,
                test_name=test_name,
                expected_behavior="accepted",
                result=normalize_execution_result(
                    verdict="SK" if owner_generate_id is not None else "",
                    feedback=duplicate_feedback,
                ),
            )
        )
        main_id = verification_task_id(
            verification_id,
            "accepted",
            test_name,
        )
        tasks.append(
            PlannedTask(
                task_id=main_id,
                predecessor_task_id=generate_id,
                task_kind=TASK_MAIN_CORRECT,
                source_path=accepted_source_path,
                program_id="accepted",
                test_name=test_name,
                expected_behavior="accepted",
            )
        )
        main_ids[test_name] = main_id
    for program in programs:
        if program.kind != TASK_SOLUTION_RUN:
            continue
        for test_name in test_names:
            task_id = verification_task_id(
                verification_id,
                program.program_id,
                test_name,
            )
            tasks.append(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=main_ids[test_name],
                    task_kind=TASK_SOLUTION_RUN,
                    source_path=program.source_path,
                    program_id=program.program_id,
                    test_name=test_name,
                    expected_behavior=program.expected_behavior,
                )
            )
    return VerificationGraph(
        tasks=tuple(tasks),
        programs=tuple(programs),
        test_names=test_names,
        accepted_source_path=accepted_source_path,
    )


def _uploaded_source_files(targets: list[dict[str, object]]) -> dict[str, PayloadFile]:
    values: dict[str, PayloadFile] = {}
    for target in targets:
        source_path = str(target.get("path") or "")
        upload_name = str(target.get("upload_filename") or "")
        raw_content = target.get("upload_content")
        if (not source_path) or (not upload_name) or (raw_content is None):
            continue
        if not isinstance(raw_content, (bytes, bytearray)):
            raise RuntimeError("uploaded verification source payload is invalid")
        values[source_path] = config.runtime_blob_store.put_bytes(bytes(raw_content))
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
        if status == VerificationTaskStatus.LEASED:
            display_status = "running"
        task_kind = str(row["task_kind"])
        counts["total"] = int(counts["total"]) + 1
        if display_status in counts:
            counts[display_status] = int(counts[display_status]) + 1
        kind_counts = by_kind.get(task_kind)
        if kind_counts is not None and display_status in kind_counts:
            kind_counts[display_status] = int(kind_counts[display_status]) + 1
    return counts


def _visible_programs(
    programs: tuple[VerificationProgram, ...],
) -> list[VerificationProgram]:
    return [program for program in programs if program.kind == TASK_SOLUTION_RUN]


def _runtime_programs(
    programs: tuple[VerificationProgram, ...],
) -> list[VerificationProgram]:
    return [program for program in programs if program.kind != TASK_GENERATE_INPUT]


def _task_row_to_test_row(row: VerificationTaskRow) -> dict[str, object]:
    runtime_ms = 0 if row["runtime_sec"] is None else max(0, int(round(float(row["runtime_sec"]) * 1000.0)))
    cpu_ms = runtime_ms if row["cpu_sec"] is None else max(0, int(round(float(row["cpu_sec"]) * 1000.0)))
    wall_ms = cpu_ms if row["wall_sec"] is None else max(0, int(round(float(row["wall_sec"]) * 1000.0)))
    memory_kb = 0 if row["memory_kb"] is None else max(0, int(row["memory_kb"]))
    feedback_text = str(row["feedback_text"] or row["error_text"] or "")
    return build_execution_test_row(
        test_name=str(row["test_name"]),
        verdict=str(row["verdict"] or "--"),
        time_ms=runtime_ms,
        time_user_ms=cpu_ms,
        time_wall_ms=wall_ms,
        memory_kb=memory_kb,
        message=feedback_text,
        output_ref=str(row["output_ref"] or ""),
        feedback_files=[],
        answer_correct=bool(row.get("answer_correct")),
    )


def _generate_feedback_by_test(rows: list[VerificationTaskRow]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        if str(row["task_kind"] or "") != TASK_GENERATE_INPUT:
            continue
        if row["status"] != VerificationTaskStatus.DONE:
            continue
        test_name = str(row["test_name"] or "")
        if not test_name:
            continue
        feedback_text = ""
        judgehost_task_id = str(row["judgehost_task_id"] or "")
        if judgehost_task_id:
            try:
                feedback_blob = config.judgehost_task_service.domjudge_case_feedback_blob_for_task(
                    judgehost_task_id,
                    test_name,
                )
            except Exception:
                feedback_blob = None
            if feedback_blob:
                feedback_text = feedback_blob.decode("utf-8", errors="replace")
        if not feedback_text:
            feedback_text = str(row["feedback_text"] or "")
        if feedback_text:
            result[test_name] = feedback_text
    return result


def _program_summary(
    *,
    program: VerificationProgram,
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
    compile_diagnostics: list[dict[str, object]] = []
    error_text = ""
    max_time_ms = 0
    max_memory_kb = 0
    saw_pending = False
    saw_queued = False
    saw_running = False
    saw_failed = False
    saw_cancelled = False
    saw_done = False
    skipped_test_count = 0
    for row in ordered_rows:
        status = str(row["status"])
        if status == VerificationTaskStatus.PENDING:
            saw_pending = True
        elif status == VerificationTaskStatus.QUEUED:
            saw_queued = True
        elif status == VerificationTaskStatus.LEASED:
            saw_running = True
        elif status == VerificationTaskStatus.FAILED:
            saw_failed = True
        elif status == VerificationTaskStatus.CANCELLED:
            saw_cancelled = True
        elif status == VerificationTaskStatus.DONE:
            saw_done = True
        is_skipped = str(row["verdict"] or "").upper() == "SK"
        if is_skipped and status == VerificationTaskStatus.DONE:
            skipped_test_count += 1
        elif status in {VerificationTaskStatus.DONE, VerificationTaskStatus.FAILED}:
            test_row = _task_row_to_test_row(row)
            tests.append(test_row)
            max_time_ms = max(max_time_ms, int(test_row.get("time_user_ms") or 0))
            max_memory_kb = max(max_memory_kb, int(test_row.get("memory_kb") or 0))
        if (not compile_log) and str(row["compile_log"] or ""):
            compile_log = str(row["compile_log"] or "")
        task_diagnostics_json = str(row["diagnostics_json"] or "[]")
        try:
            task_diagnostics = cast(list[dict[str, object]], json.loads(task_diagnostics_json))
        except Exception:
            task_diagnostics = []
        if task_diagnostics:
            remaining = max(0, _COMPILE_DIAGNOSTICS_LIMIT - len(compile_diagnostics))
            if remaining > 0:
                compile_diagnostics.extend(task_diagnostics[:remaining])
        if (not error_text) and str(row["error_text"] or ""):
            error_text = str(row["error_text"] or "")
    if fail_flag and (saw_cancelled or saw_pending) and (not saw_failed):
        saw_failed = True
    if saw_running:
        run_status = VerificationStatus.RUNNING.value
    elif saw_queued:
        run_status = VerificationStatus.QUEUED.value
    elif saw_pending:
        run_status = "pending"
    elif saw_failed or saw_cancelled:
        run_status = VerificationStatus.FAILED.value
    elif saw_done and len(tests) + skipped_test_count >= len(test_names):
        run_status = VerificationStatus.OK.value
    elif saw_done:
        run_status = VerificationStatus.RUNNING.value
    else:
        run_status = "pending"
    summary = {
        "artifact_verification_id": artifact_verification_id,
        "mode": mode,
        "pass_limit": pass_limit,
        "source": program.source_path,
        "selected_tests": list(test_names),
        "selected_tests_count": len(test_names),
        "task_kind": program.kind,
        "tests": tests,
        "tests_total": len(test_names),
        "skipped_tests": skipped_test_count,
        "compile_log": compile_log,
        "compile_diagnostics": list(compile_diagnostics),
        "error": error_text,
        "usage": {
            "tests": len(tests),
            "time_ms_total": max_time_ms,
            "time_user_ms_total": max_time_ms,
            "time_wall_ms_total": max_time_ms,
            "memory_kb_peak": max_memory_kb,
        },
    }
    completed = run_status in {VerificationStatus.OK.value, VerificationStatus.FAILED.value}
    matched = completed and run_status == VerificationStatus.OK.value
    observed_pass = bool(
        matched
        and tests
        and all(str(item.get("verdict") or "").upper() in {"OK", "AC"} for item in tests)
    )
    reason = "" if matched or not completed else error_text or "program task failed"
    return (summary, run_status, bool(matched), bool(completed), bool(observed_pass), reason)


def _verification_summary_from_tasks(
    *,
    verification_id: str,
    artifact_verification_id: str,
    mode: str,
    pass_limit: int,
    programs: tuple[VerificationProgram, ...],
    rows: list[VerificationTaskRow],
    test_names: list[str],
    parent_status: VerificationStatus,
    fail_reason: str,
) -> tuple[str, dict[str, object], dict[str, object]]:
    display_limit = int(_C.snapshot()["AUX_DISPLAY_TEXT_LIMIT_BYTES"])
    visible_programs = _visible_programs(programs)
    counts = _task_counts(rows)
    running_tasks = [
        _task_running_entry(row)
        for row in rows
        if row["status"] == VerificationTaskStatus.LEASED
    ]
    first_solution_error = ""
    has_pending_or_running = bool(int(counts["pending"]) or int(counts["queued"]) or int(counts["running"]))
    all_matched = True
    for program in visible_programs:
        grouped_rows = [row for row in rows if str(row["program_id"] or "") == program.program_id]
        run_summary, run_status, matched, completed, observed_pass, reason = _program_summary(
            program=program,
            rows=grouped_rows,
            test_names=test_names,
            mode=mode,
            pass_limit=pass_limit,
            artifact_verification_id=artifact_verification_id,
            fail_flag=parent_status == VerificationStatus.FAILED,
        )
        reason_text = reason
        if (not matched) and completed and (not reason_text):
            reason_text = verification_solution_failure_hint(
                program.source_path,
                "",
                str(run_summary.get("error") or ""),
                limit_bytes=display_limit,
            )
        if (not matched) and completed and (not first_solution_error):
            first_solution_error = reason_text or verification_solution_failure_hint(
                program.source_path,
                "",
                str(run_summary.get("error") or ""),
                limit_bytes=display_limit,
            )
        all_matched = all_matched and bool(matched)
    if parent_status in {VerificationStatus.FAILED, VerificationStatus.CANCELLED}:
        verification_status = parent_status.value
        verification_error = (
            fail_reason
            or first_solution_error
            or f"verification {parent_status.value}"
        )
    elif has_pending_or_running:
        verification_status = VerificationStatus.RUNNING.value
        verification_error = ""
    elif int(counts["cancelled"]) > 0:
        verification_status = VerificationStatus.FAILED.value
        verification_error = fail_reason or "verification task cancelled"
    elif visible_programs and all_matched:
        verification_status = VerificationStatus.OK.value
        verification_error = ""
    elif (not visible_programs) and int(counts["total"]) > 0:
        verification_status = VerificationStatus.OK.value
        verification_error = ""
    else:
        verification_status = VerificationStatus.FAILED.value
        verification_error = first_solution_error or "verification failed"
    summary = {
        "verification_id": verification_id,
        "artifact_verification_id": artifact_verification_id,
        "task_graph": True,
        "status": verification_status,
        "error": verification_error,
        "task_counts": counts,
        "running_tasks": running_tasks,
        "fail_flag": parent_status == VerificationStatus.FAILED,
        "fail_reason": fail_reason,
        "source_paths": [item.source_path for item in visible_programs],
        "test_names": list(test_names),
        "mode": mode,
        "pass_limit": pass_limit,
        "updated_at": now_iso(),
        "finished_at": now_iso() if parent_status not in {
            VerificationStatus.QUEUED, VerificationStatus.RUNNING
        } and not has_pending_or_running else "",
    }
    return (verification_status, summary, counts)


def _runtime_threshold_columns_from_tasks(
    *,
    artifact_verification_id: str,
    mode: str,
    pass_limit: int,
    programs: tuple[VerificationProgram, ...],
    rows: list[VerificationTaskRow],
    test_names: list[str],
    fail_flag: bool,
) -> list[dict[str, object]]:
    columns: list[dict[str, object]] = []
    for program in _runtime_programs(programs):
        grouped_rows = [row for row in rows if str(row["program_id"] or "") == program.program_id]
        run_summary, run_status, _matched, _completed, _observed_pass, _reason = _program_summary(
            program=program,
            rows=grouped_rows,
            test_names=test_names,
            mode=mode,
            pass_limit=pass_limit,
            artifact_verification_id=artifact_verification_id,
            fail_flag=fail_flag,
        )
        columns.append(
            {
                "source": program.source_path,
                "summary": run_summary,
                "summary_has_tl": "TL" in run_actual_failed_codes(run_status, run_summary),
            }
        )
    return columns


def _effective_verification_kind(
    *,
    sample_only: bool,
    requested_test_names: list[str],
    available_test_names: list[str],
) -> str:
    if sample_only:
        return Kind.SAMPLE.value
    if requested_test_names:
        requested_set = {name for name in requested_test_names}
        available_set = {name for name in available_test_names}
        if requested_set != available_set:
            return Kind.CUSTOM.value
        if len(requested_test_names) != len(available_test_names):
            return Kind.CUSTOM.value
        return Kind.ALL.value
    return Kind.ALL.value


def _sanity_plan_for_verification_kind(kind: str, test_plans: list[VerificationTestPlan]) -> tuple[list[str], str]:
    if kind != Kind.ALL.value:
        return ([], SANITY_SKIPPED)
    checks = planned_sanity_checks(test_plans)
    return (checks, SANITY_PENDING if checks else "")


def _execution_template(
    execution: TaskExecutionContext,
    *,
    program: VerificationProgram,
) -> dict[str, object]:
    cached = execution.execution_template_by_program_id.get(program.program_id)
    if cached is not None:
        return cached
    compile_spec = program.compile_spec
    verification_payload_base = (
        execution.generate_verification_payload_base
        if program.kind == TASK_GENERATE_INPUT
        else execution.run_verification_payload_base
    )
    prepared = config.judgehost_task_service.prepare_execution_template(
        mode=execution.mode,
        upload_file=compile_spec.source_file,
        upload_filename=compile_spec.source_name,
        verification_payload=verification_payload_base,
        expected_behavior=program.expected_behavior,
        verification_source=program.kind,
        task_kind=program.kind,
        extra_source_files=dict(compile_spec.extra_source_files),
        manual_validate_only=compile_spec.manual_validate_only,
        compile_only=False,
    )
    execution.execution_template_by_program_id[program.program_id] = prepared
    return prepared


def _empty_task_result(
    *,
    task_id: str,
    status: VerificationTaskStatus,
    verdict: str,
    run_id: str,
    judgehost_task_id: str,
    error_text: str,
    fail_reason: str = "",
) -> TaskCompletion:
    return TaskCompletion(
        task_id=task_id,
        status=status,
        run_id=run_id,
        judgehost_task_id=judgehost_task_id,
        result=normalize_execution_result(verdict=verdict, error=error_text),
        fail_reason=fail_reason,
    )


def _skipped_downstream_task_result(task_row: VerificationTaskRow) -> TaskPublishResult:
    task_id = str(task_row["id"])
    return TaskPublishResult(
        task_id=task_id,
        run_id="",
        judgehost_task_id="",
        terminal_result=TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStatus.DONE,
            run_id="",
            judgehost_task_id="",
            result=normalize_execution_result(
                verdict="SK",
                feedback="skipped because generate-input was skipped",
            ),
        ),
    )


def _publish_generate_task(task_row: VerificationTaskRow, *, execution: TaskExecutionContext, test_plan: VerificationTestPlan) -> TaskPublishResult:
    task_id = str(task_row["id"])
    test_name = str(task_row["test_name"])
    program_id = str(task_row["program_id"])
    run_id = new_run_id()
    try:
        program = execution.program_by_id.get(program_id)
        if program is None or program.kind != TASK_GENERATE_INPUT:
            raise RuntimeError("verification generator program is unavailable")
        compile_spec = program.compile_spec
        predecessor_task_id = str(task_row["predecessor_task_id"] or "")
        if predecessor_task_id and str(task_row["verdict"] or "").upper() == "SK":
            task_store = execution.task_store or config.verification_task_store
            owner = None
            if task_store is not None:
                owner = next(
                    (
                        row
                        for row in task_store.list_rows(execution.verification_id)
                        if str(row["id"] or "") == predecessor_task_id
                    ),
                    None,
                )
            if owner is None or owner["status"] != VerificationTaskStatus.DONE:
                reason = "duplicate generator owner result is unavailable"
                result = _empty_task_result(
                    task_id=task_id,
                    status=VerificationTaskStatus.FAILED,
                    verdict="FL",
                    run_id=run_id,
                    judgehost_task_id="",
                    error_text=reason,
                    fail_reason=verification_task_fail_reason(task_row, error_text=reason),
                )
                return TaskPublishResult(
                    task_id=task_id,
                    run_id=run_id,
                    judgehost_task_id="",
                    terminal_result=result,
                )
            owner_output_ref = str(owner["output_ref"] or "")
            feedback_text = str(task_row["feedback_text"] or "")
            if not feedback_text:
                feedback_text = (
                    "duplicate generator invocation; skipped, same as "
                    f"{owner['test_name']}"
                )
            return TaskPublishResult(
                task_id=task_id,
                run_id=run_id,
                judgehost_task_id="",
                terminal_result=TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id=run_id,
                    judgehost_task_id="",
                    result=normalize_execution_result(
                        verdict="SK",
                        feedback=feedback_text,
                    ),
                    input_ref=owner_output_ref,
                ),
            )
        prepared = prepared_payload_for_uploaded_source(
            source_label=compile_spec.source_name,
            run_id=run_id,
            test_name=test_name,
            input_file=test_plan.execution_input_file,
            answer_file=config.runtime_blob_store.put_bytes(b""),
            verification_payload_base=execution.generate_verification_payload_base,
            extra_source_files=dict(compile_spec.extra_source_files),
            manual_validate_only=compile_spec.manual_validate_only,
        )
        execution_template = _execution_template(
            execution,
            program=program,
        )
        task_run_id = str(prepared.get("run_id") or run_id)
        judgehost_task_id = config.judgehost_task_service.enqueue_task(
            verification_task_id=task_id,
            problem=execution.problem,
            username=execution.user,
            artifact_verification_id=execution.verification_id,
            mode=execution.mode,
            submission_path=None,
            upload_content=None,
            upload_file=compile_spec.source_file,
            upload_filename=compile_spec.source_name,
            run_id=task_run_id,
            selected_tests=[],
            verification_id=execution.verification_id,
            verification_program_id=program_id,
            expected_behavior="accepted",
            verification_source=TASK_GENERATE_INPUT,
            task_kind=TASK_GENERATE_INPUT,
            bypass_case_result_cache=execution.bypass_case_result_cache,
            compile_only=False,
            persist_verification_run=False,
            prepared_payload=prepared,
            execution_template=execution_template,
        )
        return TaskPublishResult(
            task_id=task_id,
            run_id=task_run_id,
            judgehost_task_id=judgehost_task_id,
        )
    except Exception as exc:
        result = _empty_task_result(
            task_id=task_id,
            status=VerificationTaskStatus.FAILED,
            verdict="FL",
            run_id=run_id,
            judgehost_task_id="",
            error_text=str(exc),
            fail_reason=verification_task_fail_reason(task_row, error_text=str(exc)),
        )
        return TaskPublishResult(
            task_id=task_id,
            run_id=run_id,
            judgehost_task_id="",
            terminal_result=result,
        )


def _publish_run_task(task_row: VerificationTaskRow, *, execution: TaskExecutionContext) -> TaskPublishResult:
    if str(task_row["verdict"] or "").upper() == "SK":
        return _skipped_downstream_task_result(task_row)
    task_id = str(task_row["id"])
    task_kind = str(task_row["task_kind"])
    source_path = str(task_row["source_path"])
    test_name = str(task_row["test_name"])
    program_id = str(task_row["program_id"] or "")
    run_id = new_run_id()
    try:
        program = execution.program_by_id.get(program_id)
        if (
            program is None
            or program.kind != task_kind
            or program.source_path != source_path
        ):
            raise RuntimeError("verification program is unavailable")
        compile_spec = program.compile_spec
        input_file = _verification_required_file(
            execution.verification_id,
            test_name,
            "input_ref",
            label=f"verification test {test_name}",
            cache=execution.artifact_file_by_test_ref,
        )
        if task_kind == TASK_MAIN_CORRECT:
            answer_file = config.runtime_blob_store.put_bytes(b"")
            verification_source = TASK_MAIN_CORRECT
            expected_behavior = "accepted"
        else:
            answer_file = _verification_required_file(
                execution.verification_id,
                test_name,
                "answer_ref",
                label=f"verification answer {test_name}",
                cache=execution.artifact_file_by_test_ref,
            )
            verification_source = TASK_SOLUTION_RUN
            expected_behavior = normalize_expected_behavior(task_row["expected_behavior"])
        prepared = prepared_payload_for_uploaded_source(
            source_label=source_path,
            run_id=run_id,
            test_name=test_name,
            input_file=input_file,
            answer_file=answer_file,
            verification_payload_base=execution.run_verification_payload_base,
        )
        execution_template = _execution_template(
            execution,
            program=program,
        )
        judgehost_task_id = config.judgehost_task_service.enqueue_task(
            verification_task_id=task_id,
            problem=execution.problem,
            username=execution.user,
            artifact_verification_id=execution.verification_id,
            mode=execution.mode,
            submission_path=None,
            upload_content=None,
            upload_file=compile_spec.source_file,
            upload_filename=compile_spec.source_name,
            run_id=run_id,
            selected_tests=[test_name],
            verification_id=execution.verification_id,
            verification_program_id=program_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            bypass_case_result_cache=execution.bypass_case_result_cache,
            compile_only=False,
            persist_verification_run=False,
            prepared_payload=prepared,
            execution_template=execution_template,
        )
        return TaskPublishResult(task_id=task_id, run_id=run_id, judgehost_task_id=judgehost_task_id)
    except Exception as exc:
        fail_reason = verification_task_fail_reason(
            task_row,
            error_text=str(exc),
        )
        result = _empty_task_result(
            task_id=task_id,
            status=VerificationTaskStatus.FAILED,
            verdict="FL",
            run_id=run_id,
            judgehost_task_id="",
            error_text=str(exc),
            fail_reason=fail_reason,
        )
        return TaskPublishResult(
            task_id=task_id,
            run_id=run_id,
            judgehost_task_id="",
            terminal_result=result,
        )


def _publish_task(task_row: VerificationTaskRow, *, execution: TaskExecutionContext) -> TaskPublishResult:
    if (
        str(task_row["task_kind"] or "") != TASK_GENERATE_INPUT
        and str(task_row["verdict"] or "").upper() == "SK"
    ):
        return _skipped_downstream_task_result(task_row)
    test_name = str(task_row["test_name"])
    test_plan = execution.test_plan_by_name.get(test_name)
    if test_plan is None:
        missing_reason = verification_task_fail_reason(
            task_row,
            error_text=f"verification test plan missing for {test_name}",
        )
        result = _empty_task_result(
            task_id=str(task_row["id"]),
            status=VerificationTaskStatus.FAILED,
            verdict="FL",
            run_id="",
            judgehost_task_id="",
            error_text=f"verification test plan missing for {test_name}",
            fail_reason=missing_reason,
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
    workspace_id: int | None,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, object]],
    verification_id: str,
    signature: str = "",
    source_commit: str = "",
    kind: str = Kind.ALL.value,
    sample_only: bool = False,
    snapshot_root_override: Path | None = None,
    retain_snapshot_override: bool = False,
    manifest: VerificationManifest | None = None,
    selected_test_names: list[str] | None = None,
    bypass_case_result_cache: bool = False,
    skip_sanity: bool = False,
) -> None:
    del signature, source_commit, kind
    task_store = config.verification_task_store
    snapshot_root: Path | None = snapshot_root_override
    execution_plan = None
    try:
        workspace_path: Path | None = None
        if workspace_id is not None:
            workspace_path_text = config.workspace_service.workspace_path(
                int(problem_id), int(workspace_id)
            )
            if workspace_path_text:
                workspace_path = Path(workspace_path_text).resolve()
        if snapshot_root is None:
            if workspace_path is None:
                raise RuntimeError("workspace metadata missing")
            if (
                (not workspace_path.exists())
                or (not workspace_path.is_dir())
                or workspace_path.is_symlink()
            ):
                raise RuntimeError("workspace path is unavailable")
        layout = config.fs_manager.prepare_verification_layout(verification_id)
        if snapshot_root is None:
            assert workspace_path is not None
            snapshot_root = config.workspace_service.create_snapshot(
                workspace_path,
                None,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
            )
        execution_manifest = (
            verification_manifest(snapshot_root) if manifest is None else manifest
        )
        execution_plan = build_verification_execution_plan(
            snapshot_root,
            manifest=execution_manifest,
            sample_only=bool(sample_only),
        )
        _require_online_judgehost()
    except Exception as exc:
        transition = config.verification_execution_service.fail_verification(
            verification_id,
            reason=str(exc) or "verification planning failed",
        ).transition
        if transition.outcome == "missing":
            raise RuntimeError("verification was not admitted") from exc
        # All task rows, final detail, and status are durable before the
        # quiet-window cleanup can retire process-local judgehost records.
        config.judgehost_task_service.schedule_verification_cleanup(verification_id)
        if (
            snapshot_root is not None
            and snapshot_root.exists()
            and not retain_snapshot_override
        ):
            import shutil

            shutil.rmtree(snapshot_root.parent, ignore_errors=True)
        return
    assert execution_plan is not None
    assert snapshot_root is not None
    try:
        verification_mode = execution_plan.mode
        verification_pass_limit = execution_plan.pass_limit
        source_file_by_path = dict(execution_plan.source_file_by_path)
        source_file_by_path.update(_uploaded_source_files(targets))
        for target in targets:
            source_path = str(target.get("path") or "")
            if not source_path or source_path in source_file_by_path:
                continue
            source_file_by_path[source_path] = RuntimeBlobStore.describe_file(
                resolve_source(execution_plan.snapshot_root, source_path)
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
            available_test_names=list(execution_plan.test_names),
        )
        selected_test_plans = [execution_plan.test_plan_by_name[name] for name in test_names if name in execution_plan.test_plan_by_name]
        if skip_sanity:
            sanity_checks, sanity_status = ([], SANITY_SKIPPED)
        else:
            sanity_checks, sanity_status = _sanity_plan_for_verification_kind(effective_kind, selected_test_plans)
        graph = _build_graph(
            verification_id=verification_id,
            accepted_source_path=execution_plan.accepted_source_path,
            source_file_by_path=source_file_by_path,
            test_plan_by_name=execution_plan.test_plan_by_name,
            targets=targets,
            test_names=test_names,
        )
        visible_programs = _visible_programs(graph.programs)
        activation = config.verification_service.activate_verification(
            ActivationPlan.build(
                verification_id,
                detail={
                    "mode": verification_mode,
                    "pass_limit": verification_pass_limit,
                    "source_paths": [item.source_path for item in visible_programs],
                    "selected_test_names": list(test_names),
                    "bypass_case_result_cache": bool(bypass_case_result_cache),
                    "sanity_checks": list(sanity_checks),
                    "sanity_status": sanity_status,
                    "run_config_json": str(
                        execution_plan.run_verification_payload_base.get(
                            "run_config_json"
                        )
                        or ""
                    ),
                    "tests_meta_rows": list(execution_plan.tests_meta_rows),
                },
                programs=graph.programs,
                tasks=graph.tasks,
            )
        )
        if activation.outcome != "activated":
            if activation.outcome == "missing":
                raise RuntimeError("verification was not admitted")
            return
        execution = TaskExecutionContext(
            problem=problem,
            user=user,
            verification_id=verification_id,
            mode=verification_mode,
            pass_limit=verification_pass_limit,
            snapshot_root=execution_plan.snapshot_root,
            artifact_file_by_test_ref={},
            program_by_id={
                program.program_id: program
                for program in graph.programs
            },
            execution_template_by_program_id={},
            test_plan_by_name=execution_plan.test_plan_by_name,
            run_verification_payload_base=execution_plan.run_verification_payload_base,
            generate_verification_payload_base=execution_plan.generate_verification_payload_base,
            bypass_case_result_cache=bool(bypass_case_result_cache),
            task_store=task_store,
        )

        def _refresh_state() -> tuple[
            str,
            dict[str, object],
            dict[str, object],
            list[VerificationTaskRow],
            bool,
            str,
        ]:
            snapshot = config.verification_service.verification_snapshot(
                verification_id
            )
            if snapshot is None:
                raise RuntimeError("verification disappeared while running")
            rows = cast(list[VerificationTaskRow], snapshot["tasks"])
            record = snapshot["record"]
            parent_status = VerificationStatus(record["status"])
            status = parent_status.value
            fail_reason = str(record["fail_reason"])
            fail_flag = parent_status == VerificationStatus.FAILED
            _task_status, summary, counts = _verification_summary_from_tasks(
                verification_id=verification_id,
                artifact_verification_id=verification_id,
                mode=verification_mode,
                pass_limit=verification_pass_limit,
                programs=graph.programs,
                rows=rows,
                test_names=test_names,
                parent_status=parent_status,
                fail_reason=fail_reason,
            )
            if rows and int(counts["total"]) <= 0:
                raise RuntimeError("verification task graph has rows but computed zero task counts")
            summary["status"] = status
            if fail_reason:
                summary["error"] = fail_reason
            return status, summary, counts, rows, fail_flag, fail_reason

        _refresh_state()
        callbacks = VerificationExecutionCallbacks(
            publish_task=lambda row: _publish_task(row, execution=execution),
            probe_task_case_cache=config.judgehost_task_service.probe_task_case_cache,
            close_programs=lambda program_ids: config.judgehost_task_service.close_programs(
                verification_id,
                program_ids,
            ),
            reconcile_expired_leases=lambda: config.judgehost_task_service.reconcile_expired_verification_leases(
                verification_id,
            ),
        )
        config.verification_execution_service.run(
            verification_id,
            callbacks=callbacks,
            edges=graph.edges,
        )
        _status, summary, _counts, rows, fail_flag, fail_reason = _refresh_state()
        snapshot = config.verification_service.verification_snapshot(verification_id)
        if snapshot is None:
            raise RuntimeError("verification disappeared after scheduling")
        detail = snapshot["detail"]
        if (
            _status == VerificationStatus.RUNNING.value
            and str(detail.get("sanity_status") or "") == SANITY_RUNNING
            and sanity_checks
        ):
            accepted_source_file = source_file_by_path.get(execution_plan.accepted_source_path)
            sanity_result = run_verification_sanity_checks(
                problem=problem,
                user=user,
                verification_id=verification_id,
                mode=verification_mode,
                logs_dir=layout.logs,
                test_plans=selected_test_plans,
                accepted_source_label=execution_plan.accepted_source_path,
                accepted_source_name=accepted_source_file.path.name if accepted_source_file is not None else "",
                accepted_source_file=accepted_source_file,
                run_verification_payload_base=execution_plan.run_verification_payload_base,
                generate_feedback_by_test=_generate_feedback_by_test(rows),
                runtime_columns=_runtime_threshold_columns_from_tasks(
                    artifact_verification_id=verification_id,
                    mode=verification_mode,
                    pass_limit=verification_pass_limit,
                    programs=graph.programs,
                    rows=rows,
                    test_names=test_names,
                    fail_flag=fail_flag,
                ),
                time_limit_ms=time_limit_ms_from_run_config_json(
                    str(execution_plan.run_verification_payload_base.get("run_config_json") or ""),
                ),
                bypass_case_result_cache=execution.bypass_case_result_cache,
            )
            updated_detail = dict(detail)
            updated_detail["sanity_status"] = sanity_result.status
            updated_detail["sanity_checked_count"] = int(sanity_result.checked_count)
            updated_detail["validation_status"] = sanity_result.status
            updated_detail["validated_count"] = int(sanity_result.checked_count)
            updated_detail["sanity_check_results"] = [
                {
                    "name": item.name,
                    "status": item.status,
                    "checked_count": int(item.checked_count),
                    "messages": [
                        {
                            "severity": message.severity,
                            "test_name": message.test_name,
                            "message": message.message,
                        }
                        for message in item.messages
                    ],
                }
                for item in sanity_result.check_results
            ]
            if sanity_result.status == SANITY_PASSED:
                updated_detail.pop("failed_step", None)
                updated_detail.pop("failed_check", None)
                updated_detail.pop("failed_test", None)
                updated_detail.pop("error", None)
            else:
                updated_detail.pop("failed_step", None)
                updated_detail.pop("failed_test", None)
                updated_detail.pop("failed_check", None)
                updated_detail.pop("error", None)
            if sanity_result.status in {SANITY_WARNING, SANITY_FAILED}:
                updated_detail["failed_step"] = "sanity"
                updated_detail["failed_check"] = sanity_result.check_name
                updated_detail["failed_test"] = sanity_result.failed_test
                updated_detail["error"] = sanity_result.error
            finished = config.verification_service.finish_sanity(
                SanityFinish.build(
                    verification_id,
                    detail=updated_detail,
                )
            )
            _status, summary, _counts, rows, fail_flag, fail_reason = _refresh_state()
            snapshot = config.verification_service.verification_snapshot(
                verification_id
            )
            if snapshot is None:
                raise RuntimeError("verification disappeared after sanity checks")
            if finished.outcome == "transitioned":
                summary["error"] = ""
        # Schedule only after final detail and status writes are durable.
        config.judgehost_task_service.schedule_verification_cleanup(verification_id)
    except VerificationCoordinatorFailure:
        config.judgehost_task_service.schedule_verification_cleanup(
            verification_id
        )
        raise
    except Exception as exc:
        failure_reason = str(exc) or "verification execution failed"
        transition = config.verification_execution_service.fail_verification(
            verification_id,
            reason=failure_reason,
        ).transition
        if transition.outcome != "missing":
            config.judgehost_task_service.schedule_verification_cleanup(
                verification_id
            )
        raise
    finally:
        if snapshot_root.exists() and not retain_snapshot_override:
            import shutil

            shutil.rmtree(snapshot_root.parent, ignore_errors=True)
