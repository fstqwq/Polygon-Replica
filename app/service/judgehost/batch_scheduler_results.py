from __future__ import annotations

import heapq
import time

from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.batch_scheduler_models import (
    CaseClaim,
    CaseClaimBusy,
    CaseReportTelemetry,
    CaseResult,
    HostLeaseRelease,
    JudgehostCaseRow,
    ExecutionBatchFinalizationClaim,
)


class BatchSchedulerResultMixin:
    """Apply callback, publication, cancellation, and terminal transitions."""

    def batch_finalize_row(self, batch_id: int) -> dict[str, object] | None:
        fields = (
            "execution_signature", "status", "compile_success",
            "compile_output_b64", "compile_metadata_b64", "debug_text",
            "run_config_json", "source_name", "compile_hash", "run_hash",
            "compare_hash", "materialization_state", "failure_runresult", "failure_text",
        )
        with self._lock:
            batch = self._batches.get(int(batch_id))
            return None if batch is None else {field: getattr(batch, field) for field in fields}

    def claim_batch_finalization(
        self,
        batch_id: int,
        *,
        now_text: str,
    ) -> ExecutionBatchFinalizationClaim | None:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None:
                return None
            counts = self._batch_counts[batch.batch_id]
            if (
                batch.status == "open"
                and batch.verification_id in self._closed_verification_ids
                and counts.total > 0
                and counts.terminal == counts.total
                and batch.materialization_state != "materializing"
            ):
                self._close_batch_locked(batch, updated_at=now_text)
            if (
                batch.status != "finalize-pending"
                or counts.total == 0
                or counts.terminal != counts.total
                or batch.materialization_state == "materializing"
            ):
                return None
            self._mutate_batch_locked(batch, status="finalizing", updated_at=now_text)
            cases = [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_batch[batch.batch_id])
            ]
            return {"batch": self._batch_row(batch), "cases": cases}

    def abort_batch_finalization(
        self,
        batch_id: int,
        *,
        now_text: str,
        delay_sec: float = 0.25,
    ) -> bool:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None or batch.status != "finalizing":
                return False
            self._mutate_batch_locked(batch, status="finalize-pending", updated_at=now_text)
            deadline = time.monotonic() + max(0.0, float(delay_sec))
            current = self._finalization_retry_deadlines.get(batch.batch_id)
            if current is None or deadline < current:
                self._finalization_retry_deadlines[batch.batch_id] = deadline
                heapq.heappush(self._finalization_retry_heap, (deadline, batch.batch_id))
            return True

    def due_batch_finalizations(self, *, limit: int) -> list[int]:
        due: list[int] = []
        now = time.monotonic()
        with self._lock:
            while self._finalization_retry_heap and len(due) < max(0, int(limit)):
                deadline, batch_id = self._finalization_retry_heap[0]
                if deadline > now:
                    break
                heapq.heappop(self._finalization_retry_heap)
                if self._finalization_retry_deadlines.get(batch_id) != deadline:
                    continue
                self._finalization_retry_deadlines.pop(batch_id, None)
                batch = self._batches.get(batch_id)
                if batch is not None and batch.status == "finalize-pending":
                    due.append(batch_id)
        return due

    def clear_batch_finalization_retry(self, batch_id: int) -> None:
        with self._lock:
            self._finalization_retry_deadlines.pop(int(batch_id), None)

    def set_batch_terminal_status(
        self,
        batch_id: int,
        *,
        status: str,
        completed_at: str,
        updated_at: str,
    ) -> bool:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None or batch.status != "finalizing":
                return False
            self._mutate_batch_locked(
                batch,
                status=status,
                completed_at=completed_at,
                updated_at=updated_at,
            )
            self._finalization_retry_deadlines.pop(batch.batch_id, None)
            self._discard_batch_telemetry_locked(batch.batch_id)
            return True

    def record_compile_result(
        self,
        batch_id: int,
        *,
        compile_success: int,
        compile_output_b64: str,
        compile_metadata_b64: str,
        failure_text: str = "",
        compile_log: str = "",
        compile_diagnostics: tuple[dict[str, object], ...] = (),
        updated_at: str,
    ) -> bool:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None or batch.status != "open":
                return False
            if batch.compile_state == "failed":
                return compile_success != 1
            if batch.compile_state == "succeeded" and compile_success == 1:
                return True
            failure_runresult = batch.failure_runresult
            if compile_success != 1 and not failure_runresult:
                failure_runresult = "compiler-error"
            self._mutate_batch_locked(
                batch,
                compile_success=compile_success,
                compile_state="succeeded" if compile_success == 1 else "failed",
                compile_output_b64=compile_output_b64,
                compile_metadata_b64=compile_metadata_b64,
                failure_runresult=failure_runresult,
                failure_text=batch.failure_text,
                updated_at=updated_at,
            )
            if compile_success != 1:
                feedback = failure_text or "compilation failed"
                for case_id in tuple(self._case_ids_by_batch[batch.batch_id]):
                    case = self._cases[case_id]
                    if case.status in self._TERMINAL_CASE_STATUSES:
                        continue
                    result = build_case_result(
                        test_name=case.test_name,
                        runresult="compiler-error",
                        verdict="CE",
                        runtime_sec=0.0,
                        cpu_sec=0.0,
                        wall_sec=0.0,
                        memory_kb=0,
                        score_text="",
                        output_run_ref="",
                        output_error_ref="",
                        output_system_ref="",
                        output_diff_ref="",
                        metadata_ref="",
                        compare_metadata_ref="",
                        team_message_ref="",
                        feedback_text=feedback,
                        feedback_files=(),
                        answer_correct=False,
                        compile_log=compile_log,
                        compile_diagnostics=compile_diagnostics,
                    )
                    if case.status in {"reporting", "cache-probing"}:
                        if not case.cancel_requested:
                            case.terminal_result = result
                        continue
                    case.result = result
                    self._transition_case_locked(
                        case,
                        "reported",
                        lease_owner=case.lease_owner,
                        updated_at=updated_at,
                        refresh_batch=False,
                    )
                self._refresh_batches_locked({batch.batch_id}, updated_at=updated_at)
            return True

    def record_batch_failure(
        self,
        batch_id: int,
        *,
        runresult: str,
        error_text: str,
        updated_at: str,
    ) -> bool:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None or batch.status != "open":
                return False
            if not batch.failure_runresult:
                batch.failure_runresult = runresult
                batch.failure_text = error_text
                batch.updated_at = updated_at
                self._touch_batch_locked(batch)
            elif batch.failure_runresult == runresult and error_text and not batch.failure_text:
                batch.failure_text = error_text
                batch.updated_at = updated_at
                self._touch_batch_locked(batch)
            return True

    def case_execution_row(self, case_id: int) -> dict[str, object] | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            if case is None:
                return None
            batch = self._batches[case.batch_id]
            return {
                "id": case.id,
                "batch_id": case.batch_id,
                "task_id": case.task_id,
                "test_name": case.test_name,
                "testcase_hash": case.testcase_hash,
                "testcase_input_hash": case.testcase_input_hash,
                "testcase_answer_hash": case.testcase_answer_hash,
                "input_ref": case.input_ref,
                "answer_ref": case.answer_ref,
                "case_status": case.status,
                "case_lease_owner": case.lease_owner,
                "run_id": case.run_id,
                "mode": batch.mode,
                "source_name": batch.source_name,
                "batch_status": batch.status,
                "execution_signature": batch.execution_signature,
                "source_hash": batch.source_hash,
                "compile_hash": batch.compile_hash,
                "run_hash": batch.run_hash,
                "compare_hash": batch.compare_hash,
                "compile_config_json": batch.compile_config_json,
                "run_config_json": batch.run_config_json,
                "compare_config_json": batch.compare_config_json,
                "compile_success": batch.compile_success,
            }

    def case_output_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        with self._lock:
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            case = self._cases[case_id]
            output_ref = "" if case.result is None else case.result.output_run_ref
            return {
                "id": case.id,
                "output_run_ref": output_ref,
            }

    def case_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        with self._lock:
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            case = self._cases[case_id]
            batch = self._batches[case.batch_id]
            return {
                **self._case_row(case),
                "batch_id": batch.batch_id,
                "batch_status": batch.status,
                "compile_success": batch.compile_success,
            }

    def case_result_for_task(self, task_id: str, test_name: str) -> CaseResult | None:
        with self._lock:
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            return self._cases[case_id].result

    def claim_case_reporting(
        self,
        case_id: int,
        *,
        hostname: str,
        now_text: str,
    ) -> CaseClaim | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            if case is None or case.status in self._TERMINAL_CASE_STATUSES:
                return None
            if case.status == "reporting":
                raise CaseClaimBusy("judgehost case result is already being processed")
            if case.status != "leased" or case.lease_owner != hostname:
                return None
            case.claim_generation += 1
            self._transition_case_locked(
                case,
                "reporting",
                lease_owner=hostname,
                updated_at=now_text,
            )
            return CaseClaim(
                case_id=case.id,
                generation=case.claim_generation,
                batch_id=case.batch_id,
                task_id=case.task_id,
                test_name=case.test_name,
                cancel_requested=case.cancel_requested,
            )

    def observe_compile_success_from_case_claim(
        self,
        case_id: int,
        *,
        generation: int,
        lease_owner: str,
        updated_at: str,
    ) -> bool:
        """Treat a valid run callback as proof that compilation succeeded."""
        with self._lock:
            case = self._cases.get(int(case_id))
            if (
                case is None
                or case.status != "reporting"
                or case.claim_generation != int(generation)
                or case.lease_owner != lease_owner
            ):
                return False
            batch = self._batches.get(case.batch_id)
            if batch is None or batch.status != "open" or batch.compile_state == "failed":
                return False
            if batch.compile_state == "unknown":
                self._mutate_batch_locked(
                    batch,
                    compile_success=1,
                    compile_state="succeeded",
                    updated_at=updated_at,
                )
            return batch.compile_state == "succeeded"

    def claim_cache_cases(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
        now_text: str,
    ) -> list[tuple[CaseClaim, JudgehostCaseRow]]:
        claimed: list[tuple[CaseClaim, JudgehostCaseRow]] = []
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None or batch.status != "open":
                return claimed
            while len(claimed) < max(0, int(limit)):
                case = self._peek_case_heap_locked(batch.batch_id, status="cache-pending")
                if case is None:
                    break
                heapq.heappop(self._cache_heaps_by_batch[batch.batch_id])
                case.claim_generation += 1
                self._transition_case_locked(
                    case,
                    "cache-probing",
                    lease_owner=hostname,
                    updated_at=now_text,
                    refresh_batch=False,
                )
                claim = CaseClaim(
                    case_id=case.id,
                    generation=case.claim_generation,
                    batch_id=case.batch_id,
                    task_id=case.task_id,
                    test_name=case.test_name,
                    cancel_requested=case.cancel_requested,
                )
                claimed.append((claim, self._case_row(case)))
            self._refresh_batches_locked({batch.batch_id}, updated_at=now_text)
        return claimed

    def _finish_claim_locked(
        self,
        case,
        *,
        result: CaseResult,
        updated_at: str,
    ) -> str:
        cancel_requested = case.cancel_requested
        terminal_result = case.terminal_result
        case.cancel_requested = False
        case.terminal_result = None
        case.requeue_on_abort = False
        if cancel_requested:
            case.result = None
            self._transition_case_locked(case, "cancelled", lease_owner=None, updated_at=updated_at)
            return "cancelled"
        case.result = terminal_result or result
        result_owner = case.lease_owner if case.status == "reporting" else None
        self._transition_case_locked(
            case,
            "reported",
            lease_owner=result_owner,
            updated_at=updated_at,
        )
        return "reported"

    def commit_case_result(
        self,
        case_id: int,
        *,
        generation: int,
        result: CaseResult,
        updated_at: str,
        report_telemetry: CaseReportTelemetry | None = None,
    ) -> str | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            if (
                case is None
                or case.status not in {"reporting", "cache-probing"}
                or case.claim_generation != int(generation)
            ):
                return None
            if report_telemetry is not None:
                if case.status != "reporting" or case.lease_owner != report_telemetry.hostname:
                    return None
                self._record_case_telemetry_locked(case, report_telemetry)
            return self._finish_claim_locked(case, result=result, updated_at=updated_at)

    def commit_cancelled_receipt(
        self,
        case_id: int,
        *,
        generation: int,
        updated_at: str,
        report_telemetry: CaseReportTelemetry,
    ) -> bool:
        with self._lock:
            case = self._cases.get(int(case_id))
            if (
                case is None
                or case.status != "reporting"
                or case.claim_generation != int(generation)
                or case.lease_owner != report_telemetry.hostname
                or not case.cancel_requested
            ):
                return False
            self._record_case_telemetry_locked(case, report_telemetry)
            case.cancel_requested = False
            case.terminal_result = None
            case.requeue_on_abort = False
            case.result = None
            self._transition_case_locked(
                case,
                "cancelled",
                lease_owner=None,
                updated_at=updated_at,
            )
            return True

    def finish_cache_miss(
        self,
        case_id: int,
        *,
        generation: int,
        updated_at: str,
    ) -> bool:
        with self._lock:
            case = self._cases.get(int(case_id))
            if (
                case is None
                or case.status != "cache-probing"
                or case.claim_generation != int(generation)
            ):
                return False
            cancel_requested = case.cancel_requested
            terminal_result = case.terminal_result
            case.cancel_requested = False
            case.terminal_result = None
            if cancel_requested:
                self._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif terminal_result is not None:
                case.result = terminal_result
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            else:
                self._transition_case_locked(
                    case,
                    "pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            return True

    def finish_cache_claims(
        self,
        outcomes: list[tuple[CaseClaim, CaseResult | None]],
        *,
        updated_at: str,
    ) -> dict[int, str]:
        """Commit one cache probe batch and refresh each ready index once."""
        finished: dict[int, str] = {}
        affected_batch_ids: set[int] = set()
        with self._lock:
            for claim, result in outcomes:
                case = self._cases.get(claim.case_id)
                if (
                    case is None
                    or case.status != "cache-probing"
                    or case.claim_generation != claim.generation
                ):
                    continue
                cancel_requested = case.cancel_requested
                terminal_result = case.terminal_result
                case.cancel_requested = False
                case.terminal_result = None
                case.requeue_on_abort = False
                if cancel_requested:
                    case.result = None
                    status = "cancelled"
                elif terminal_result is not None:
                    case.result = terminal_result
                    status = "reported"
                elif result is None:
                    case.result = None
                    status = "pending"
                else:
                    case.result = result
                    status = "reported"
                self._transition_case_locked(
                    case,
                    status,
                    lease_owner=None,
                    updated_at=updated_at,
                    refresh_batch=False,
                )
                finished[case.id] = status
                affected_batch_ids.add(case.batch_id)
            self._refresh_batches_locked(
                affected_batch_ids,
                updated_at=updated_at,
            )
        return finished

    def abort_case_claim(
        self,
        case_id: int,
        *,
        generation: int,
        updated_at: str,
    ) -> bool:
        with self._lock:
            case = self._cases.get(int(case_id))
            if (
                case is None
                or case.status not in {"reporting", "cache-probing"}
                or case.claim_generation != int(generation)
            ):
                return False
            cancel_requested = case.cancel_requested
            terminal_result = case.terminal_result
            case.cancel_requested = False
            case.terminal_result = None
            if cancel_requested:
                self._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif terminal_result is not None:
                case.result = terminal_result
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif case.status == "cache-probing":
                self._transition_case_locked(
                    case,
                    "cache-pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif case.requeue_on_abort:
                self._transition_case_locked(
                    case,
                    "pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            else:
                self._transition_case_locked(
                    case,
                    "leased",
                    lease_owner=case.lease_owner,
                    updated_at=updated_at,
                )
            case.requeue_on_abort = False
            return True

    def abort_cache_claims(
        self,
        claims: list[CaseClaim],
        *,
        updated_at: str,
    ) -> int:
        aborted = 0
        affected_batch_ids: set[int] = set()
        with self._lock:
            for claim in claims:
                case = self._cases.get(int(claim.case_id))
                if (
                    case is None
                    or case.status != "cache-probing"
                    or case.claim_generation != int(claim.generation)
                ):
                    continue
                cancel_requested = case.cancel_requested
                terminal_result = case.terminal_result
                case.cancel_requested = False
                case.terminal_result = None
                case.requeue_on_abort = False
                if cancel_requested:
                    status = "cancelled"
                    case.result = None
                elif terminal_result is not None:
                    status = "reported"
                    case.result = terminal_result
                else:
                    status = "cache-pending"
                self._transition_case_locked(
                    case,
                    status,
                    lease_owner=None,
                    updated_at=updated_at,
                    refresh_batch=False,
                )
                affected_batch_ids.add(case.batch_id)
                aborted += 1
            self._refresh_batches_locked(affected_batch_ids, updated_at=updated_at)
        return aborted

    def request_batch_case_results(
        self,
        batch_id: int,
        *,
        results: dict[int, CaseResult],
        updated_at: str,
    ) -> set[str]:
        affected_tasks: set[str] = set()
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None:
                return affected_tasks
            affected = False
            for case_id, result in results.items():
                case = self._cases.get(int(case_id))
                if (
                    case is None
                    or case.batch_id != batch.batch_id
                    or case.status in self._TERMINAL_CASE_STATUSES
                ):
                    continue
                affected_tasks.add(case.task_id)
                affected = True
                if case.status in {"reporting", "cache-probing"}:
                    if not case.cancel_requested:
                        case.terminal_result = result
                    continue
                case.result = result
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=case.lease_owner,
                    updated_at=updated_at,
                    refresh_batch=False,
                )
            if affected:
                self._refresh_batches_locked({batch.batch_id}, updated_at=updated_at)
        return affected_tasks

    def mark_case_verification_published(self, task_id: str, test_name: str) -> bool:
        with self._lock:
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return False
            case = self._cases[case_id]
            if case.status not in self._TERMINAL_CASE_STATUSES:
                return False
            case.verification_published = True
            return True

    def mark_cases_verification_published(self, case_ids: list[int]) -> int:
        marked = 0
        with self._lock:
            for case_id in dict.fromkeys(int(raw_case_id) for raw_case_id in case_ids):
                case = self._cases.get(case_id)
                if case is None or case.status not in self._TERMINAL_CASE_STATUSES:
                    continue
                if not case.verification_published:
                    case.verification_published = True
                    marked += 1
        return marked

    def case_debug_context(self, case_id: int) -> dict[str, object] | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            if case is None:
                return None
            return {
                "batch_id": case.batch_id,
                "case_debug_text": case.debug_text,
                "batch_debug_text": self._batches[case.batch_id].debug_text,
            }

    def batch_debug_context(self, batch_id: int) -> dict[str, object] | None:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            return None if batch is None else {"batch_id": batch.batch_id, "debug_text": batch.debug_text}

    def append_debug_text(
        self,
        *,
        case_id: int | None,
        batch_id: int | None,
        debug_text: str,
        now_text: str,
    ) -> None:
        with self._lock:
            if case_id is not None and int(case_id) in self._cases:
                case = self._cases[int(case_id)]
                case.debug_text = self._merge_debug_text(case.debug_text, debug_text)
                case.updated_at = now_text
            if batch_id is not None and int(batch_id) in self._batches:
                batch = self._batches[int(batch_id)]
                batch.debug_text = self._merge_debug_text(batch.debug_text, debug_text)
                batch.updated_at = now_text

    @staticmethod
    def _merge_debug_text(current: str, incoming: str) -> str:
        merged = incoming if not current else f"{current}\n{incoming}"
        return merged[-4000:]

    def case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        with self._lock:
            result: dict[str, dict[str, int]] = {}
            for run_id in (run_id for run_id in run_ids if run_id):
                counts = self._run_counts.get(run_id)
                if counts is None or counts.total == 0:
                    continue
                result[run_id] = {
                    "total": counts.total,
                    "reported": counts.reported,
                    "leased": counts.leased + counts.reporting,
                }
            return result

    def release_host_leases(
        self,
        hostname: str,
        *,
        now_text: str,
        verification_id: str = "",
    ) -> HostLeaseRelease:
        with self._lock:
            if not verification_id:
                self._drop_host_telemetry_batch_locked(hostname)
            affinity_ids = () if verification_id else self._affinity_batches_by_host.pop(hostname, ())
            context_batch_ids = set(affinity_ids)
            affected_batch_ids = set(context_batch_ids)
            terminal_task_ids: set[str] = set()
            case_ids = [
                case_id
                for case_id in self._leased_case_ids_by_host.get(hostname, ())
                if not verification_id
                or (
                    case_id in self._cases
                    and self._batches[self._cases[case_id].batch_id].verification_id == verification_id
                )
            ]
            workdirs: set[tuple[int, int]] = set()
            for case_id in case_ids:
                case = self._cases[case_id]
                batch = self._batches[case.batch_id]
                submission = self._compile_submissions_by_key[batch.compile_key]
                workdirs.add((batch.domjudge_job_id, submission.submit_id))
                if case.status == "reporting":
                    case.requeue_on_abort = True
                    leased_ids = self._leased_case_ids_by_host[hostname]
                    leased_ids.discard(case.id)
                    if not leased_ids:
                        self._leased_case_ids_by_host.pop(hostname, None)
                elif case.status == "leased":
                    cancelled = case.cancel_requested
                    terminal_result = case.terminal_result
                    case.cancel_requested = False
                    case.terminal_result = None
                    case.requeue_on_abort = False
                    if cancelled:
                        case.result = None
                        status = "cancelled"
                    elif terminal_result is not None:
                        case.result = terminal_result
                        status = "reported"
                    else:
                        case.result = None
                        status = "pending"
                    self._transition_case_locked(
                        case,
                        status,
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
                    if (
                        status in self._TERMINAL_CASE_STATUSES
                        and self._task_case_counts[case.task_id].remaining == 0
                    ):
                        terminal_task_ids.add(case.task_id)
                affected_batch_ids.add(case.batch_id)
            self._refresh_batches_locked(affected_batch_ids, updated_at=now_text)
            terminal_batch_ids = tuple(sorted(
                batch_id
                for batch_id in affected_batch_ids
                if self._batches.get(batch_id) is not None
                and self._batches[batch_id].status == "finalize-pending"
            ))
            return HostLeaseRelease(
                affinity_count=len(context_batch_ids),
                lease_count=len(case_ids),
                terminal_batch_ids=terminal_batch_ids,
                terminal_task_ids=tuple(sorted(terminal_task_ids)),
                workdirs=tuple(sorted(workdirs)),
            )

    def _remove_cases_locked(self, case_ids: set[int]) -> None:
        cases = [self._cases[case_id] for case_id in case_ids if case_id in self._cases]
        if not cases:
            return
        affected_batch_ids = {case.batch_id for case in cases}
        affected_task_ids = {case.task_id for case in cases}
        affected_run_ids = {case.run_id for case in cases}
        affected_pairs = {(case.task_id, case.test_name) for case in cases}

        for case in cases:
            if case.status in {"leased", "reporting"} and case.lease_owner:
                leased_ids = self._leased_case_ids_by_host[case.lease_owner]
                leased_ids.discard(case.id)
                if not leased_ids:
                    self._leased_case_ids_by_host.pop(case.lease_owner, None)
            self._adjust_counts(self._batch_counts[case.batch_id], case.status, -1)
            self._adjust_counts(self._run_counts[case.run_id], case.status, -1)
            task_counts = self._task_case_counts[case.task_id]
            task_counts.total -= 1
            if case.status not in self._TERMINAL_CASE_STATUSES:
                task_counts.remaining -= 1
            if task_counts.total < 0 or task_counts.remaining < 0:
                raise RuntimeError("judgehost task case count underflow")
            if case.testcase_id is not None:
                testcase_cases = self._case_ids_by_testcase[case.testcase_id]
                testcase_cases.discard(case.id)
                if not testcase_cases:
                    self._case_ids_by_testcase.pop(case.testcase_id, None)

        for batch_id in affected_batch_ids:
            retained = self._case_ids_by_batch[batch_id].difference(case_ids)
            self._case_ids_by_batch[batch_id] = retained
            if retained:
                self._empty_batch_ids.discard(batch_id)
            else:
                self._empty_batch_ids.add(batch_id)

        for task_id in affected_task_ids:
            retained = self._case_ids_by_task[task_id].difference(case_ids)
            if retained:
                self._case_ids_by_task[task_id] = retained
                self._batch_id_by_task[task_id] = self._cases[next(iter(retained))].batch_id
            else:
                self._case_ids_by_task.pop(task_id, None)
                self._batch_id_by_task.pop(task_id, None)
                self._task_case_counts.pop(task_id, None)

        for run_id in affected_run_ids:
            retained = self._case_ids_by_run[run_id].difference(case_ids)
            if retained:
                self._case_ids_by_run[run_id] = retained
                self._batch_ids_by_run[run_id] = {
                    self._cases[case_id].batch_id
                    for case_id in retained
                }
            else:
                self._case_ids_by_run.pop(run_id, None)
                self._batch_ids_by_run.pop(run_id, None)
                self._run_counts.pop(run_id, None)

        for pair in affected_pairs:
            self._latest_case_id_by_task_test.pop(pair, None)
        for task_id in affected_task_ids:
            for case_id in self._case_ids_by_task.get(task_id, ()):
                case = self._cases[case_id]
                pair = (case.task_id, case.test_name)
                if pair in affected_pairs:
                    self._latest_case_id_by_task_test[pair] = max(
                        case_id,
                        self._latest_case_id_by_task_test.get(pair, 0),
                    )
        for case in cases:
            self._cases.pop(case.id, None)

    def _remove_batch_locked(self, batch_id: int) -> None:
        batch = self._batches.pop(batch_id)
        if batch.status == "open":
            self._index_batch_scripts_locked(batch, -1)
        self._ready_batches.remove(batch_id)
        self._finalization_retry_deadlines.pop(batch_id, None)
        self._refresh_prerequisite_index_locked(batch, ready=False)
        logical_run_key = (batch.verification_id, batch.logical_run_id)
        self._batch_id_by_logical_run.pop(logical_run_key, None)
        self._closed_logical_run_keys.discard(logical_run_key)
        self._case_ids_by_batch.pop(batch_id, None)
        self._batch_counts.pop(batch_id, None)
        self._batch_specs.pop(batch_id, None)
        self._cache_heaps_by_batch.pop(batch_id, None)
        self._runnable_heaps_by_batch.pop(batch_id, None)
        self._empty_batch_ids.discard(batch_id)
        compile_batches = self._batch_ids_by_compile_key[batch.compile_key]
        compile_batches.discard(batch_id)
        if not compile_batches:
            self._batch_ids_by_compile_key.pop(batch.compile_key, None)
            submission = self._compile_submissions_by_key.get(batch.compile_key)
            if submission is None:
                raise RuntimeError("judgehost compile submission is missing")
            self._compile_key_by_submit_id.pop(submission.submit_id, None)
            if not self._compile_submission_is_materialized(submission):
                self._compile_submissions_by_key.pop(batch.compile_key, None)
        verification_batches = self._batch_ids_by_verification[batch.verification_id]
        verification_batches.discard(batch_id)
        if not verification_batches:
            self._batch_ids_by_verification.pop(batch.verification_id, None)
            self._verification_by_domjudge_job_id.pop(batch.domjudge_job_id, None)

    def forget_runs(self, run_ids: list[str]) -> int:
        safe_run_ids = {run_id for run_id in run_ids if run_id}
        if not safe_run_ids:
            return 0
        with self._lock:
            affected_batches = {
                batch_id
                for run_id in safe_run_ids
                for batch_id in self._batch_ids_by_run.get(run_id, ())
            }
            case_ids = {
                case_id
                for run_id in safe_run_ids
                for case_id in self._case_ids_by_run.get(run_id, ())
            }
            self._remove_cases_locked(case_ids)
            for batch_id in affected_batches:
                if batch_id in self._batches:
                    self._touch_batch_locked(self._batches[batch_id])
            empty_batches = tuple(batch_id for batch_id in affected_batches if batch_id in self._empty_batch_ids)
            for batch_id in empty_batches:
                self._remove_batch_locked(batch_id)
            return len(empty_batches)

    def cancel_all_inflight(self, *, now_text: str) -> list[int]:
        with self._lock:
            batch_ids = sorted(
                batch_id
                for batch_id, batch in self._batches.items()
                if batch.status in {"open", "finalize-pending", "finalizing"}
            )
            for batch_id in batch_ids:
                for case_id in tuple(self._case_ids_by_batch[batch_id]):
                    case = self._cases[case_id]
                    if case.status in self._TERMINAL_CASE_STATUSES:
                        continue
                    case.cancel_requested = False
                    case.terminal_result = None
                    case.requeue_on_abort = False
                    case.result = None
                    self._transition_case_locked(
                        case,
                        "cancelled",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
            self._refresh_batches_locked(set(batch_ids), updated_at=now_text)
            return batch_ids
