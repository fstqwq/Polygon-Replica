import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.config import ConfigValues
from app.service.execution.identity import new_run_id
from app.service.execution.policy import normalize_execution_result
from app.service.judgehost.api import Judgehost
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.problem.runtime_config import ProblemMode
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.problem.source_file import resolve_source
from app.service.repository.workspace import WorkspaceService
from app.service.verification.completion import verification_task_fail_reason
from app.service.verification.execution import (
    VerificationCoordinatorFailure,
    VerificationExecutionCallbacks,
    VerificationExecutionService,
)
from app.service.verification.execution_plan import VerificationExecutionPlanner
from app.service.verification.identity import (
    canonical_verification_id,
    new_verification_id,
)
from app.service.verification.lifecycle import (
    ActivationPlan,
    SanityFinish,
    TASK_GENERATE_INPUT,
    TASK_MAIN_CORRECT,
    TASK_SOLUTION_RUN,
    VerificationAdmission,
    VerificationProgram,
)
from app.service.verification.payload import prepared_payload_for_uploaded_source
from app.service.verification.plan import VerificationTestPlan
from app.service.verification.runtime_threshold import time_limit_ms_from_run_config_json
from app.service.verification.sanity import (
    SANITY_FAILED,
    SANITY_PASSED,
    SANITY_RUNNING,
    SANITY_SKIPPED,
    SANITY_WARNING,
    VerificationSanityService,
)
from app.service.verification.service import VerificationService
from app.service.verification.signature import (
    VerificationManifest,
    verification_manifest,
)
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_scheduler import TaskPublishResult
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.types import Kind, VerificationStatus, VerificationTaskStatus
from app.service.verification.workflow_policy import (
    VerificationTaskCounts,
    build_graph,
    effective_verification_kind,
    runtime_threshold_columns_from_tasks,
    sanity_plan_for_verification_kind,
    verification_summary_from_tasks,
    visible_programs,
)

_ARTIFACT_READY_TIMEOUT_SEC = 2.0
_ARTIFACT_READY_INTERVAL_SEC = 0.05


@dataclass(frozen=True)
class TaskExecutionContext:
    problem: str
    user: str
    verification_id: str
    problem_mode: ProblemMode
    pass_limit: int
    snapshot_root: Path
    artifact_file_by_test_ref: dict[tuple[str, str], PayloadFile]
    program_by_id: dict[str, VerificationProgram]
    execution_template_by_program_id: dict[str, dict[str, object]]
    test_plan_by_name: dict[str, VerificationTestPlan]
    run_verification_payload_base: dict[str, object]
    generate_verification_payload_base: dict[str, object]
    bypass_case_result_cache: bool
    service_class: str
    judgehost: Judgehost
    runtime_blob_store: RuntimeBlobStore
    verification_service: VerificationService
    task_store: VerificationTaskStore


def _require_online_judgehost(judgehost: Judgehost) -> None:
    try:
        status = cast(dict[str, object], judgehost.status())
    except Exception:
        status = {}
    hosts_online = status.get("hosts_online")
    if (
        not isinstance(hosts_online, int)
        or isinstance(hosts_online, bool)
        or hosts_online <= 0
    ):
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
    verification_service: VerificationService,
    runtime_blob_store: RuntimeBlobStore,
) -> PayloadFile:
    cache_key = (test_name, ref_key)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        ref = verification_service.verification_artifact_ref(
            verification_id,
            test_name,
            ref_key,
        )
        if ref:
            payload = runtime_blob_store.descriptor(ref)
            if payload is not None:
                if cache is not None:
                    cache[cache_key] = payload
                return payload
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.001, min(float(interval_sec), deadline - time.monotonic())))
    raise RuntimeError(f"{label} is missing")


def _generate_feedback_by_test(
    rows: list[VerificationTaskRow],
    judgehost: Judgehost,
) -> dict[str, str]:
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
                feedback_blob = judgehost.case_feedback_blob_for_task(
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


def _uploaded_source_files(
    targets: list[dict[str, object]],
    runtime_blob_store: RuntimeBlobStore,
) -> dict[str, PayloadFile]:
    values: dict[str, PayloadFile] = {}
    for target in targets:
        source_path = str(target.get("path") or "")
        upload_name = str(target.get("upload_filename") or "")
        raw_content = target.get("upload_content")
        if (not source_path) or (not upload_name) or (raw_content is None):
            continue
        if not isinstance(raw_content, (bytes, bytearray)):
            raise RuntimeError("uploaded verification source payload is invalid")
        values[source_path] = runtime_blob_store.put_bytes(bytes(raw_content))
    return values


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
    prepared = execution.judgehost.prepare_execution_template(
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
            owner = next(
                (
                    row
                    for row in execution.task_store.list_rows(
                        execution.verification_id
                    )
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
            answer_file=execution.runtime_blob_store.put_bytes(b""),
            verification_payload_base=execution.generate_verification_payload_base,
            extra_source_files=dict(compile_spec.extra_source_files),
            manual_validate_only=compile_spec.manual_validate_only,
        )
        execution_template = _execution_template(
            execution,
            program=program,
        )
        task_run_id = str(prepared.get("run_id") or run_id)
        judgehost_task_id = execution.judgehost.enqueue_task(
            verification_task_id=task_id,
            problem=execution.problem,
            username=execution.user,
            artifact_verification_id=execution.verification_id,
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
            service_class=execution.service_class,
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
            verification_service=execution.verification_service,
            runtime_blob_store=execution.runtime_blob_store,
        )
        verification_source: str
        if task_kind == TASK_MAIN_CORRECT:
            answer_file = execution.runtime_blob_store.put_bytes(b"")
            verification_source = TASK_MAIN_CORRECT
            expected_behavior = "accepted"
        else:
            answer_file = _verification_required_file(
                execution.verification_id,
                test_name,
                "answer_ref",
                label=f"verification answer {test_name}",
                cache=execution.artifact_file_by_test_ref,
                verification_service=execution.verification_service,
                runtime_blob_store=execution.runtime_blob_store,
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
        judgehost_task_id = execution.judgehost.enqueue_task(
            verification_task_id=task_id,
            problem=execution.problem,
            username=execution.user,
            artifact_verification_id=execution.verification_id,
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
            service_class=execution.service_class,
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
    service_class: str = "background",
    planner: VerificationExecutionPlanner,
    sanity_service: VerificationSanityService,
    verification_service: VerificationService,
    execution_service: VerificationExecutionService,
    judgehost: Judgehost,
    workspace_service: WorkspaceService,
    storage_layout: StorageLayout,
    runtime_blob_store: RuntimeBlobStore,
    task_store: VerificationTaskStore,
    config_values: ConfigValues,
) -> None:
    del actor_user_id, signature, source_commit, kind
    snapshot_root: Path | None = snapshot_root_override
    execution_plan = None
    try:
        workspace_path: Path | None = None
        if workspace_id is not None:
            workspace_path_text = workspace_service.workspace_path(
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
        layout = storage_layout.prepare_verification_layout(verification_id)
        if snapshot_root is None:
            assert workspace_path is not None
            snapshot_root = workspace_service.create_snapshot(
                workspace_path,
                None,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
            )
        execution_manifest = (
            verification_manifest(snapshot_root) if manifest is None else manifest
        )
        execution_plan = planner.build(
            snapshot_root,
            manifest=execution_manifest,
            sample_only=bool(sample_only),
        )
        _require_online_judgehost(judgehost)
    except Exception as exc:
        transition = execution_service.fail_verification(
            verification_id,
            reason=str(exc) or "verification planning failed",
        ).transition
        if transition.outcome == "missing":
            raise RuntimeError("verification was not admitted") from exc
        # All task rows, final detail, and status are durable before the
        # quiet-window cleanup can retire process-local judgehost records.
        judgehost.schedule_verification_cleanup(verification_id)
        if (
            snapshot_root is not None
            and snapshot_root.exists()
            and not retain_snapshot_override
        ):
            shutil.rmtree(snapshot_root.parent, ignore_errors=True)
        return
    assert execution_plan is not None
    assert snapshot_root is not None
    try:
        verification_mode = execution_plan.problem_mode
        verification_pass_limit = execution_plan.pass_limit
        source_file_by_path = dict(execution_plan.source_file_by_path)
        source_file_by_path.update(_uploaded_source_files(targets, runtime_blob_store))
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
        effective_kind = effective_verification_kind(
            sample_only=bool(sample_only),
            requested_test_names=list(requested_test_names),
            available_test_names=list(execution_plan.test_names),
        )
        selected_test_plans = [execution_plan.test_plan_by_name[name] for name in test_names if name in execution_plan.test_plan_by_name]
        if skip_sanity:
            sanity_checks: list[str] = []
            sanity_status = SANITY_SKIPPED
        else:
            sanity_checks, sanity_status = sanity_plan_for_verification_kind(effective_kind, selected_test_plans)
        graph = build_graph(
            verification_id=verification_id,
            accepted_source_path=execution_plan.accepted_source_path,
            source_file_by_path=source_file_by_path,
            test_plan_by_name=execution_plan.test_plan_by_name,
            targets=targets,
            test_names=test_names,
        )
        solution_programs = visible_programs(graph.programs)
        activation = verification_service.activate_verification(
            ActivationPlan.build(
                verification_id,
                detail={
                    "mode": verification_mode,
                    "pass_limit": verification_pass_limit,
                    "source_paths": [item.source_path for item in solution_programs],
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
            problem_mode=verification_mode,
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
            service_class=service_class,
            judgehost=judgehost,
            runtime_blob_store=runtime_blob_store,
            verification_service=verification_service,
            task_store=task_store,
        )

        def _refresh_state() -> tuple[
            str,
            dict[str, object],
            VerificationTaskCounts,
            list[VerificationTaskRow],
            bool,
            str,
        ]:
            snapshot = verification_service.verification_snapshot(
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
            _task_status, summary, counts = verification_summary_from_tasks(
                verification_id=verification_id,
                artifact_verification_id=verification_id,
                mode=verification_mode,
                pass_limit=verification_pass_limit,
                programs=graph.programs,
                rows=rows,
                test_names=test_names,
                parent_status=parent_status,
                fail_reason=fail_reason,
                display_limit=config_values.integer(
                    "AUX_DISPLAY_TEXT_LIMIT_BYTES"
                ),
            )
            if rows and counts["total"] <= 0:
                raise RuntimeError("verification task graph has rows but computed zero task counts")
            summary["status"] = status
            if fail_reason:
                summary["error"] = fail_reason
            return status, summary, counts, rows, fail_flag, fail_reason

        _refresh_state()
        callbacks = VerificationExecutionCallbacks(
            publish_task=lambda row: _publish_task(row, execution=execution),
            probe_task_case_cache=judgehost.probe_task_case_cache,
            close_programs=lambda program_ids: judgehost.close_programs(
                verification_id,
                program_ids,
            ),
            reconcile_expired_leases=lambda: judgehost.reconcile_expired_verification_leases(
                verification_id,
            ),
        )
        execution_service.run(
            verification_id,
            callbacks=callbacks,
            edges=graph.edges,
        )
        _status, summary, _counts, rows, fail_flag, fail_reason = _refresh_state()
        snapshot = verification_service.verification_snapshot(verification_id)
        if snapshot is None:
            raise RuntimeError("verification disappeared after scheduling")
        detail = snapshot["detail"]
        if (
            _status == VerificationStatus.RUNNING.value
            and str(detail.get("sanity_status") or "") == SANITY_RUNNING
            and sanity_checks
        ):
            accepted_source_file = source_file_by_path.get(execution_plan.accepted_source_path)
            sanity_result = sanity_service.run(
                problem=problem,
                user=user,
                verification_id=verification_id,
                logs_dir=layout.logs,
                test_plans=selected_test_plans,
                accepted_source_label=execution_plan.accepted_source_path,
                accepted_source_name=accepted_source_file.path.name if accepted_source_file is not None else "",
                accepted_source_file=accepted_source_file,
                run_verification_payload_base=execution_plan.run_verification_payload_base,
                generate_feedback_by_test=_generate_feedback_by_test(rows, judgehost),
                runtime_columns=runtime_threshold_columns_from_tasks(
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
                service_class=execution.service_class,
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
            finished = verification_service.finish_sanity(
                SanityFinish.build(
                    verification_id,
                    detail=updated_detail,
                )
            )
            _status, summary, _counts, rows, fail_flag, fail_reason = _refresh_state()
            snapshot = verification_service.verification_snapshot(
                verification_id
            )
            if snapshot is None:
                raise RuntimeError("verification disappeared after sanity checks")
            if finished.outcome == "transitioned":
                summary["error"] = ""
        # Schedule only after final detail and status writes are durable.
        judgehost.schedule_verification_cleanup(verification_id)
    except VerificationCoordinatorFailure:
        judgehost.schedule_verification_cleanup(
            verification_id
        )
        raise
    except Exception as exc:
        failure_reason = str(exc) or "verification execution failed"
        transition = execution_service.fail_verification(
            verification_id,
            reason=failure_reason,
        ).transition
        if transition.outcome != "missing":
            judgehost.schedule_verification_cleanup(
                verification_id
            )
        raise
    finally:
        if snapshot_root.exists() and not retain_snapshot_override:
            shutil.rmtree(snapshot_root.parent, ignore_errors=True)


class VerificationWorkflow:
    """Plan and execute one admitted verification through explicit ports."""

    def __init__(
        self,
        *,
        planner: VerificationExecutionPlanner,
        sanity_service: VerificationSanityService,
        verification_service: VerificationService,
        execution_service: VerificationExecutionService,
        judgehost: Judgehost,
        workspace_service: WorkspaceService,
        storage_layout: StorageLayout,
        runtime_blob_store: RuntimeBlobStore,
        task_store: VerificationTaskStore,
        config_values: ConfigValues,
    ) -> None:
        self._planner = planner
        self._sanity_service = sanity_service
        self._verification_service = verification_service
        self._execution_service = execution_service
        self._judgehost = judgehost
        self._workspace_service = workspace_service
        self._storage_layout = storage_layout
        self._runtime_blob_store = runtime_blob_store
        self._task_store = task_store
        self._config_values = config_values

    def run_workspace(
        self,
        problem: str,
        username: str,
        commit: str | None = None,
        *,
        sample_only: bool = False,
        verification_id: str = "",
        service_class: str = "background",
    ) -> str:
        """Admit and synchronously execute one workspace verification."""

        context = self._workspace_service.workspace_context(
            problem,
            username,
            include_recent=False,
        )
        workspace = Path(str(context["workspace"]["path"])).resolve()
        problem_id = int(context["problem"]["id"])
        workspace_id = int(context["workspace"]["id"])
        actor_user_id = self._workspace_service.global_user_context(username)["id"]
        status = self._workspace_service.read_workspace_status(workspace)
        workspace_head = str(status.get("head_commit") or "")
        workspace_dirty = bool(status.get("dirty"))
        source_commit = ""
        if commit:
            source_commit = self._workspace_service.resolve_commit(workspace, commit)
            snapshot = self._workspace_service.create_snapshot(
                workspace,
                source_commit,
            )
            workspace_dirty = False
        else:
            snapshot = self._workspace_service.create_snapshot(
                workspace,
                None,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
            )
        try:
            manifest = verification_manifest(snapshot)
            target_id = (
                canonical_verification_id(verification_id)
                if verification_id
                else new_verification_id()
            )
            kind = Kind.SAMPLE.value if sample_only else Kind.ALL.value
            admission = self._verification_service.admit_verification(
                VerificationAdmission(
                    verification_id=target_id,
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    signature=manifest.signature,
                    source_commit=source_commit,
                    kind=kind,
                )
            )
        except Exception:
            shutil.rmtree(snapshot.parent, ignore_errors=True)
            raise
        if admission.outcome != "admitted":
            shutil.rmtree(snapshot.parent, ignore_errors=True)
            raise RuntimeError(f"verification already exists: {target_id}")
        self.run(
            problem,
            username,
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_head=source_commit or workspace_head,
            workspace_dirty=workspace_dirty,
            targets=[],
            verification_id=target_id,
            signature=manifest.signature,
            source_commit=source_commit,
            kind=kind,
            sample_only=sample_only,
            snapshot_root_override=snapshot,
            manifest=manifest,
            service_class=service_class,
        )
        return target_id

    def run(
        self,
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
        service_class: str = "background",
    ) -> None:
        run_workspace_verification_dag(
            problem,
            user,
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
            targets=targets,
            verification_id=verification_id,
            signature=signature,
            source_commit=source_commit,
            kind=kind,
            sample_only=sample_only,
            snapshot_root_override=snapshot_root_override,
            retain_snapshot_override=retain_snapshot_override,
            manifest=manifest,
            selected_test_names=selected_test_names,
            bypass_case_result_cache=bypass_case_result_cache,
            skip_sanity=skip_sanity,
            service_class=service_class,
            planner=self._planner,
            sanity_service=self._sanity_service,
            verification_service=self._verification_service,
            execution_service=self._execution_service,
            judgehost=self._judgehost,
            workspace_service=self._workspace_service,
            storage_layout=self._storage_layout,
            runtime_blob_store=self._runtime_blob_store,
            task_store=self._task_store,
            config_values=self._config_values,
        )
