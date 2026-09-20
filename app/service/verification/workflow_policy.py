from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.service.execution.codec import compile_diagnostics_payload
from app.service.execution.model import JsonValue
from app.service.execution.policy import normalize_execution_result
from app.service.execution.test_rows import ExecutionTestRow, build_execution_test_row
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.lifecycle import (
    EMPTY_TASK_RESULT,
    PlannedTask,
    TASK_GENERATE_INPUT,
    TASK_MAIN_CORRECT,
    TASK_SOLUTION_RUN,
    VerificationCompileSpec,
    VerificationProgram,
    verification_task_id,
)
from app.service.verification.plan import VerificationTestPlan
from app.service.verification.result_match import analyze_program_result
from app.service.verification.sanity import SANITY_PENDING, SANITY_SKIPPED, planned_sanity_checks
from app.service.verification.types import VerificationProgramSummary, VerificationRuntimeColumn, VerificationTaskRow
from app.service.verification.types import Kind, VerificationStatus, VerificationTaskStatus

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


def build_graph(
    *,
    verification_id: str,
    accepted_source_path: str,
    source_file_by_path: dict[str, PayloadFile],
    test_plan_by_name: dict[str, VerificationTestPlan],
    targets: Sequence[Mapping[str, object]],
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
        expected_value = target.get("expected_behavior")
        expected_behavior = normalize_expected_behavior(
            expected_value if isinstance(expected_value, str) else "unknown"
        )
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
    generator_owner_by_invocation: dict[tuple[VerificationCompileSpec, str, bool], str] = {}
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
        invocation_key = (
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
                result=(
                    EMPTY_TASK_RESULT if owner_generate_id is None
                    else normalize_execution_result(verdict="SK", feedback=duplicate_feedback)
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

def visible_programs(
    programs: tuple[VerificationProgram, ...],
) -> list[VerificationProgram]:
    return [program for program in programs if program.kind == TASK_SOLUTION_RUN]


def _runtime_programs(
    programs: tuple[VerificationProgram, ...],
) -> list[VerificationProgram]:
    return [program for program in programs if program.kind != TASK_GENERATE_INPUT]


def _task_row_to_test_row(row: VerificationTaskRow) -> ExecutionTestRow:
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


def _program_summary(
    *,
    program: VerificationProgram,
    rows: list[VerificationTaskRow],
    test_names: list[str],
    mode: str,
    pass_limit: int,
    artifact_verification_id: str,
    fail_flag: bool,
) -> tuple[VerificationProgramSummary, str]:
    ordered_rows = sorted(rows, key=lambda item: (str(item["test_name"]), str(item["id"])))
    tests: list[ExecutionTestRow] = []
    compile_log = ""
    compile_diagnostics: list[dict[str, JsonValue]] = []
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
            time_user_ms = test_row["time_user_ms"]
            memory_kb = test_row["memory_kb"]
            if not isinstance(time_user_ms, int) or isinstance(time_user_ms, bool):
                raise RuntimeError("verification test CPU time must be an integer")
            if not isinstance(memory_kb, int) or isinstance(memory_kb, bool):
                raise RuntimeError("verification test memory must be an integer")
            max_time_ms = max(max_time_ms, time_user_ms)
            max_memory_kb = max(max_memory_kb, memory_kb)
        if (not compile_log) and str(row["compile_log"] or ""):
            compile_log = str(row["compile_log"] or "")
        task_diagnostics = compile_diagnostics_payload(row["result"].compile.diagnostics)
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
    summary: VerificationProgramSummary = {
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
    return summary, run_status


def runtime_threshold_columns_from_tasks(
    *,
    artifact_verification_id: str,
    mode: str,
    pass_limit: int,
    programs: tuple[VerificationProgram, ...],
    rows: list[VerificationTaskRow],
    test_names: list[str],
    fail_flag: bool,
) -> list[VerificationRuntimeColumn]:
    columns: list[VerificationRuntimeColumn] = []
    for program in _runtime_programs(programs):
        grouped_rows = [row for row in rows if str(row["program_id"] or "") == program.program_id]
        run_summary, run_status = _program_summary(
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
                "summary_has_tl": "TL" in analyze_program_result(run_status, run_summary).failed_codes,
            }
        )
    return columns


def effective_verification_kind(
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


def sanity_plan_for_verification_kind(kind: str, test_plans: list[VerificationTestPlan]) -> tuple[list[str], str]:
    if kind != Kind.ALL.value:
        return ([], SANITY_SKIPPED)
    checks = planned_sanity_checks(test_plans)
    return (checks, SANITY_PENDING if checks else "")
