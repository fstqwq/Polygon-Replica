from collections.abc import Iterable

from app.service.judgehost.batch.admission import BatchAdmission
from app.service.judgehost.batch.completion import BatchCompletion
from app.service.judgehost.batch.dispatch import BatchDispatch
from app.service.judgehost.batch.finalization import BatchFinalization
from app.service.judgehost.batch.maintenance import BatchMaintenance
from app.service.judgehost.batch.model import (
    CaseExecutionRow,
    CaseCallbackReceipt,
    CaseClaim,
    CaseReportTelemetry,
    CaseResult,
    CompileSubmission,
    ExecutionBatchFinalizationClaim,
    ExecutionBatchRow,
    ExecutionBatchSpec,
    HostLeaseRelease,
    HostTelemetryRow,
    JudgehostCaseRow,
    PendingCaseDiagnostic,
    ProgramTerminalClaim,
    VerificationCancellation,
)
from app.service.judgehost.batch.policy import SchedulingPolicy
from app.service.judgehost.batch.state import BatchState


class JudgehostBatchRuntime:
    """Aggregate root for process-local Judgehost batch execution state."""

    def __init__(
        self,
        *,
        id_base: int | None = None,
        scheduling_policy: SchedulingPolicy | None = None,
    ) -> None:
        self._state = BatchState(
            id_base=id_base,
            scheduling_policy=scheduling_policy,
        )
        self._admission = BatchAdmission(self._state)
        self._dispatch = BatchDispatch(self._state)
        self._completion = BatchCompletion(self._state)
        self._finalization = BatchFinalization(self._state)
        self._maintenance = BatchMaintenance(self._state)

    def reset(self) -> None:
        self._maintenance.reset()

    def host_context_batches(self, hostname: str) -> list[ExecutionBatchRow]:
        return self._dispatch.host_context_batches(hostname)

    def select_ready_batch(self, hostname: str) -> ExecutionBatchRow | None:
        return self._dispatch.select_ready_batch(hostname)

    def wait_for_ready_batch(self, timeout_sec: float) -> bool:
        return self._dispatch.wait_for_ready_batch(timeout_sec)

    def host_leased_case_count(self, hostname: str) -> int:
        return self._dispatch.host_leased_case_count(hostname)

    def batch_case_count(self, batch_id: int, *, status: str) -> int:
        return self._dispatch.batch_case_count(batch_id, status=status)

    def batch_dispatch_count(self, batch_id: int) -> int:
        return self._dispatch.batch_dispatch_count(batch_id)

    def active_lease_counts(self) -> dict[str, int]:
        return self._dispatch.active_lease_counts()

    def cases_for_host(self, hostname: str) -> list[JudgehostCaseRow]:
        return self._dispatch.cases_for_host(hostname)

    def record_batch_leased(
        self,
        hostname: str,
        batch_id: int,
        case_ids: list[int],
        *,
        leased_monotonic: float,
    ) -> None:
        self._dispatch.record_batch_leased(
            hostname, batch_id, case_ids, leased_monotonic=leased_monotonic
        )

    def host_telemetry_snapshot(self) -> dict[str, HostTelemetryRow]:
        return self._dispatch.host_telemetry_snapshot()

    def cases_for_batch(
        self, batch_id: int, *, status: str | None = None
    ) -> list[JudgehostCaseRow]:
        return self._state.cases_for_batch(batch_id, status=status)

    def cases_for_task(self, task_id: str) -> list[JudgehostCaseRow]:
        return self._state.cases_for_task(task_id)

    def task_cases_terminal(self, task_id: str) -> bool:
        return self._state.task_cases_terminal(task_id)

    def task_has_cache_pending_cases(self, task_id: str) -> bool:
        return self._state.task_has_cache_pending_cases(task_id)

    def task_case_results(self, task_id: str) -> list[tuple[JudgehostCaseRow, CaseResult | None]]:
        return self._state.task_case_results(task_id)

    def fetch_batch(self, batch_id: int) -> ExecutionBatchRow | None:
        return self._state.fetch_batch(batch_id)

    def scope_sequence(self, verification_id: str) -> int:
        return self._admission.scope_sequence(verification_id)

    def forget_scope(self, verification_id: str) -> None:
        self._admission.forget_scope(verification_id)

    def finish_verification_execution(self, verification_id: str, *, now_text: str) -> list[int]:
        return self._admission.finish_verification_execution(verification_id, now_text=now_text)

    def request_verification_cancel(
        self, verification_id: str, *, now_text: str
    ) -> VerificationCancellation:
        return self._maintenance.request_verification_cancel(verification_id, now_text=now_text)

    def finish_programs(
        self,
        verification_id: str,
        verification_program_ids: Iterable[str],
        *,
        now_text: str,
    ) -> list[int]:
        return self._admission.finish_programs(
            verification_id, verification_program_ids, now_text=now_text
        )

    def batch_spec(self, batch_id: int) -> ExecutionBatchSpec | None:
        return self._dispatch.batch_spec(batch_id)

    def compile_submission_for_batch(self, batch_id: int) -> CompileSubmission | None:
        return self._dispatch.compile_submission_for_batch(batch_id)

    def publish_materialized_compile_submission(
        self, compile_key: str, submission: CompileSubmission
    ) -> None:
        self._dispatch.publish_materialized_compile_submission(compile_key, submission)

    def claim_materialization(self, batch_id: int, *, now_text: str) -> bool:
        return self._dispatch.claim_materialization(batch_id, now_text=now_text)

    def finish_materialization(
        self, batch_id: int, *, success: bool, error_text: str, now_text: str
    ) -> bool:
        return self._dispatch.finish_materialization(
            batch_id, success=success, error_text=error_text, now_text=now_text
        )

    def batch_for_task(self, task_id: str) -> ExecutionBatchRow | None:
        return self._state.batch_for_task(task_id)

    def batch_for_run(self, run_id: str) -> ExecutionBatchRow | None:
        return self._state.batch_for_run(run_id)

    def fetch_case(self, case_id: int) -> JudgehostCaseRow | None:
        return self._state.fetch_case(case_id)

    def cases_for_run(self, run_id: str) -> list[JudgehostCaseRow]:
        return self._state.cases_for_run(run_id)

    def source_submission(
        self, submit_id: str, *, contest_id: str | None = None
    ) -> CompileSubmission | None:
        return self._state.source_submission(submit_id, contest_id=contest_id)

    def testcase_refs(self, testcase_id: int) -> tuple[dict[str, object] | None, str]:
        return self._state.testcase_refs(testcase_id)

    def active_script_hashes(self, kind: str, script_id: int) -> set[str]:
        return self._state.active_script_hashes(kind, script_id)

    def leased_script_hash_for_host(
        self, hostname: str, *, kind: str, script_id: int
    ) -> tuple[int, str] | None:
        return self._state.leased_script_hash_for_host(
            hostname,
            kind=kind,
            requested_id=script_id,
        )

    def create_batch_with_cases(
        self,
        *,
        task_id: str,
        run_id: str,
        verification_program_id: str,
        execution_signature: str,
        task_kind: str,
        verification_id: str,
        compile_key: str,
        compile_submission: CompileSubmission,
        contest_id: str,
        mode: str,
        source_name: str,
        compile_hash: str,
        run_hash: str,
        compare_hash: str,
        source_hash: str,
        compile_config_json: str,
        run_config_json: str,
        compare_config_json: str,
        expected_behavior: str,
        verification_source: str,
        bypass_case_result_cache: int,
        service_class: str,
        batch_spec: ExecutionBatchSpec,
        created_at: str,
        case_rows: list[dict[str, object]],
    ) -> int:
        return self._admission.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            verification_program_id=verification_program_id,
            execution_signature=execution_signature,
            task_kind=task_kind,
            verification_id=verification_id,
            compile_key=compile_key,
            compile_submission=compile_submission,
            contest_id=contest_id,
            mode=mode,
            source_name=source_name,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            source_hash=source_hash,
            compile_config_json=compile_config_json,
            run_config_json=run_config_json,
            compare_config_json=compare_config_json,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            bypass_case_result_cache=bypass_case_result_cache,
            service_class=service_class,
            batch_spec=batch_spec,
            created_at=created_at,
            case_rows=case_rows,
        )

    def activate_task_cases(self, task_id: str, *, now_text: str) -> bool:
        return self._admission.activate_task_cases(task_id, now_text=now_text)

    def discard_staged_task_cases(self, task_id: str, *, batch_id: int | None = None) -> int:
        return self._admission.discard_staged_task_cases(task_id, batch_id=batch_id)

    def lease_cases(
        self, batch_id: int, *, hostname: str, limit: int, now_text: str
    ) -> list[JudgehostCaseRow]:
        return self._dispatch.lease_cases(
            batch_id, hostname=hostname, limit=limit, now_text=now_text
        )

    def batch_finalize_row(self, batch_id: int) -> dict[str, object] | None:
        return self._finalization.batch_finalize_row(batch_id)

    def claim_batch_finalization(
        self, batch_id: int, *, now_text: str
    ) -> ExecutionBatchFinalizationClaim | None:
        return self._finalization.claim_batch_finalization(batch_id, now_text=now_text)

    def schedule_batch_finalization_retry(
        self, batch_id: int, *, now_text: str, delay_sec: float = 0.25
    ) -> bool:
        return self._finalization.schedule_batch_finalization_retry(
            batch_id, now_text=now_text, delay_sec=delay_sec
        )

    def due_batch_finalizations(self, *, limit: int) -> list[int]:
        return self._finalization.due_batch_finalizations(limit=limit)

    def clear_batch_finalization_retry(self, batch_id: int) -> None:
        self._finalization.clear_batch_finalization_retry(batch_id)

    def set_batch_terminal_status(
        self, batch_id: int, *, status: str, completed_at: str, updated_at: str
    ) -> bool:
        return self._finalization.set_batch_terminal_status(
            batch_id, status=status, completed_at=completed_at, updated_at=updated_at
        )

    def record_compile_success(
        self,
        case_id: int,
        *,
        hostname: str,
        receipt_generation: int,
        compile_output_b64: str,
        compile_metadata_b64: str,
        updated_at: str,
    ) -> bool:
        return self._completion.record_compile_success(
            case_id,
            hostname=hostname,
            receipt_generation=receipt_generation,
            compile_output_b64=compile_output_b64,
            compile_metadata_b64=compile_metadata_b64,
            updated_at=updated_at,
        )

    def claim_compile_failure(
        self,
        case_id: int,
        *,
        hostname: str,
        receipt_generation: int,
        compile_output_b64: str,
        compile_metadata_b64: str,
        failure_text: str,
        compile_log: str,
        compile_diagnostics: tuple[dict[str, object], ...],
        updated_at: str,
    ) -> ProgramTerminalClaim:
        return self._completion.claim_compile_failure(
            case_id,
            hostname=hostname,
            receipt_generation=receipt_generation,
            compile_output_b64=compile_output_b64,
            compile_metadata_b64=compile_metadata_b64,
            failure_text=failure_text,
            compile_log=compile_log,
            compile_diagnostics=compile_diagnostics,
            updated_at=updated_at,
        )

    def claim_internal_error(
        self,
        case_id: int,
        *,
        hostname: str,
        failure_text: str,
        diagnostic_text: str,
        receipt_generation: int,
        diagnostic_limit_bytes: int,
        updated_at: str,
    ) -> ProgramTerminalClaim:
        return self._completion.claim_internal_error(
            case_id,
            hostname=hostname,
            failure_text=failure_text,
            diagnostic_text=diagnostic_text,
            receipt_generation=receipt_generation,
            diagnostic_limit_bytes=diagnostic_limit_bytes,
            updated_at=updated_at,
        )

    def record_batch_failure(
        self, batch_id: int, *, runresult: str, error_text: str, updated_at: str
    ) -> bool:
        return self._completion.record_batch_failure(
            batch_id, runresult=runresult, error_text=error_text, updated_at=updated_at
        )

    def case_execution_row(self, case_id: int) -> CaseExecutionRow | None:
        return self._completion.case_execution_row(case_id)

    def case_output_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        return self._completion.case_output_for_task(task_id, test_name)

    def case_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        return self._completion.case_for_task(task_id, test_name)

    def case_result_for_task(self, task_id: str, test_name: str) -> CaseResult | None:
        return self._completion.case_result_for_task(task_id, test_name)

    def acquire_case_callback_receipt(self, case_id: int) -> CaseCallbackReceipt | None:
        return self._completion.acquire_case_callback_receipt(case_id)

    def release_case_callback_receipt(self, receipt_id: int) -> None:
        self._completion.release_case_callback_receipt(receipt_id)

    def claim_case_reporting(
        self, case_id: int, *, hostname: str, receipt_generation: int, now_text: str
    ) -> CaseClaim | None:
        return self._completion.claim_case_reporting(
            case_id,
            hostname=hostname,
            receipt_generation=receipt_generation,
            now_text=now_text,
        )

    def observe_compile_success_from_case_claim(
        self, case_id: int, *, generation: int, lease_owner: str, updated_at: str
    ) -> bool:
        return self._completion.observe_compile_success_from_case_claim(
            case_id,
            generation=generation,
            lease_owner=lease_owner,
            updated_at=updated_at,
        )

    def claim_cache_cases(
        self, batch_id: int, *, hostname: str, limit: int, now_text: str
    ) -> list[tuple[CaseClaim, JudgehostCaseRow]]:
        return self._completion.claim_cache_cases(
            batch_id, hostname=hostname, limit=limit, now_text=now_text
        )

    def commit_case_result(
        self,
        case_id: int,
        *,
        generation: int,
        result: CaseResult,
        updated_at: str,
        report_telemetry: CaseReportTelemetry | None = None,
    ) -> str | None:
        return self._completion.commit_case_result(
            case_id,
            generation=generation,
            result=result,
            updated_at=updated_at,
            report_telemetry=report_telemetry,
        )

    def commit_cancelled_receipt(
        self,
        case_id: int,
        *,
        generation: int,
        updated_at: str,
        report_telemetry: CaseReportTelemetry,
    ) -> bool:
        return self._completion.commit_cancelled_receipt(
            case_id,
            generation=generation,
            updated_at=updated_at,
            report_telemetry=report_telemetry,
        )

    def finish_cache_miss(self, case_id: int, *, generation: int, updated_at: str) -> bool:
        return self._completion.finish_cache_miss(
            case_id, generation=generation, updated_at=updated_at
        )

    def finish_cache_claims(
        self, outcomes: list[tuple[CaseClaim, CaseResult | None]], *, updated_at: str
    ) -> dict[int, str]:
        return self._completion.finish_cache_claims(outcomes, updated_at=updated_at)

    def abort_case_claim(self, case_id: int, *, generation: int, updated_at: str) -> bool:
        return self._completion.abort_case_claim(
            case_id, generation=generation, updated_at=updated_at
        )

    def abort_cache_claims(self, claims: list[CaseClaim], *, updated_at: str) -> int:
        return self._completion.abort_cache_claims(claims, updated_at=updated_at)

    def acknowledge_case_completion(self, case_id: int) -> bool:
        return self._completion.acknowledge_case_completion(case_id)

    def acknowledge_case_completions(self, case_ids: list[int]) -> int:
        return self._completion.acknowledge_case_completions(case_ids)

    def record_case_diagnostic(
        self,
        case_id: int,
        *,
        kind: str,
        hostname: str,
        text: str,
        receipt_generation: int,
        diagnostic_limit_bytes: int,
        now_text: str,
    ) -> str | None:
        return self._completion.record_case_diagnostic(
            case_id,
            kind=kind,
            hostname=hostname,
            text=text,
            receipt_generation=receipt_generation,
            diagnostic_limit_bytes=diagnostic_limit_bytes,
            now_text=now_text,
        )

    def pending_case_diagnostics(self, case_id: int) -> tuple[PendingCaseDiagnostic, ...]:
        return self._completion.pending_case_diagnostics(case_id)

    def acknowledge_case_diagnostic(self, case_id: int, diagnostic: PendingCaseDiagnostic) -> bool:
        return self._completion.acknowledge_case_diagnostic(case_id, diagnostic)

    def case_debug_context(self, case_id: int) -> dict[str, object] | None:
        return self._completion.case_debug_context(case_id)

    def batch_debug_context(self, batch_id: int) -> dict[str, object] | None:
        return self._completion.batch_debug_context(batch_id)

    def append_debug_text(
        self,
        *,
        case_id: int | None,
        batch_id: int | None,
        debug_text: str,
        now_text: str,
    ) -> None:
        self._completion.append_debug_text(
            case_id=case_id, batch_id=batch_id, debug_text=debug_text, now_text=now_text
        )

    def case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        return self._completion.case_progress_for_runs(run_ids)

    def release_host_leases(
        self, hostname: str, *, now_text: str, verification_id: str = ""
    ) -> HostLeaseRelease:
        return self._maintenance.release_host_leases(
            hostname, now_text=now_text, verification_id=verification_id
        )

    def forget_runs(self, run_ids: list[str]) -> int:
        return self._maintenance.forget_runs(run_ids)

    def forget_runs_if_quiet(self, run_ids: list[str]) -> int | None:
        return self._maintenance.forget_runs_if_quiet(run_ids)

    def cancel_all_inflight(self, *, now_text: str) -> list[int]:
        return self._maintenance.cancel_all_inflight(now_text=now_text)
