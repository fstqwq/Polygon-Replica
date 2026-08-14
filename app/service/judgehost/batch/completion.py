import heapq
from typing import TYPE_CHECKING

from app.service.judgehost.batch.model import (
    CaseExecutionRow,
    CaseClaim,
    CaseCallbackReceipt,
    CaseClaimBusy,
    CaseRecord,
    CaseReportTelemetry,
    CaseResult,
    ExecutionBatchRecord,
    JudgehostCaseRow,
    PendingCaseDiagnostic,
    ProgramTerminalClaim,
    ProgramTerminalClaimOutcome,
)
from app.service.judgehost.batch.snapshot import case_snapshot
from app.service.judgehost.domjudge.case_result import build_case_result
from app.service.judgehost.domjudge.result import verdict_from_runresult
from app.service.platform.error_text import bounded_display_text
from app.service.platform.hashing import canonical_json, sha256_hex_json

if TYPE_CHECKING:
    from app.service.judgehost.batch.state import BatchState


_PENDING_DIAGNOSTIC_KINDS = frozenset({"debug-info", "internal-error"})
_PENDING_DIAGNOSTIC_MAX_ITEMS = 32


def _pending_diagnostic_payload(
    diagnostics: list[PendingCaseDiagnostic],
) -> dict[str, object]:
    return {
        "items": [
            {
                "kind": item.kind,
                "hostname": item.hostname,
                "text": item.text,
                "received_at": item.received_at,
                "digest": item.digest,
            }
            for item in diagnostics
        ]
    }


def _pending_diagnostic_size(diagnostics: list[PendingCaseDiagnostic]) -> int:
    return len(
        canonical_json(
            _pending_diagnostic_payload(diagnostics),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _new_pending_diagnostic(
    *,
    kind: str,
    hostname: str,
    text: str,
    received_at: str,
    limit_bytes: int,
) -> PendingCaseDiagnostic:
    if kind not in _PENDING_DIAGNOSTIC_KINDS:
        raise ValueError(f"unknown judgehost diagnostic kind: {kind}")
    if not hostname or len(hostname) > 255:
        raise ValueError("judgehost diagnostic hostname is invalid")
    if not received_at or len(received_at) > 64:
        raise ValueError("judgehost diagnostic received-at timestamp is invalid")
    normalized_text = bounded_display_text(
        text,
        limit_bytes=max(1, int(limit_bytes)),
    )
    if not normalized_text:
        raise ValueError("judgehost diagnostic text is required")
    digest = sha256_hex_json(
        {"kind": kind, "hostname": hostname, "text": normalized_text},
        ensure_ascii=False,
    )
    return PendingCaseDiagnostic(
        kind=kind,
        hostname=hostname,
        text=normalized_text,
        received_at=received_at,
        digest=digest,
    )


def _fit_single_pending_diagnostic(
    diagnostic: PendingCaseDiagnostic,
    *,
    limit_bytes: int,
) -> PendingCaseDiagnostic | None:
    def _candidate(prefix_chars: int) -> PendingCaseDiagnostic:
        prefix = diagnostic.text[:prefix_chars].rstrip()
        return PendingCaseDiagnostic(
            kind=diagnostic.kind,
            hostname=diagnostic.hostname,
            text=f"{prefix}..." if prefix else "...",
            received_at=diagnostic.received_at,
            digest=diagnostic.digest,
        )

    shortest = _candidate(0)
    if _pending_diagnostic_size([shortest]) > limit_bytes:
        return None
    low = 0
    high = max(0, len(diagnostic.text) - 1)
    best = shortest
    while low <= high:
        middle = (low + high) // 2
        candidate = _candidate(middle)
        if _pending_diagnostic_size([candidate]) <= limit_bytes:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


class BatchCompletion:
    """Apply callback claims, execution results, and publication acknowledgements."""

    def __init__(self, state: "BatchState") -> None:
        self._state = state

    def _program_failure_case_result_locked(
        self,
        batch: ExecutionBatchRecord,
        *,
        test_name: str,
        feedback_text: str,
        compile_log: str = "",
        compile_diagnostics: tuple[dict[str, object], ...] = (),
    ) -> CaseResult:
        return build_case_result(
            test_name=test_name,
            runresult=batch.failure_runresult,
            verdict=verdict_from_runresult(batch.failure_runresult),
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
            feedback_text=feedback_text,
            feedback_files=(),
            answer_correct=False,
            compile_log=compile_log,
            compile_diagnostics=compile_diagnostics,
        )

    def _install_program_failure_locked(
        self,
        batch: ExecutionBatchRecord,
        *,
        compile_log: str = "",
        compile_diagnostics: tuple[dict[str, object], ...] = (),
        updated_at: str,
    ) -> None:
        if batch.program_failure_result is not None:
            return
        batch.program_failure_result = self._program_failure_case_result_locked(
            batch,
            test_name="",
            feedback_text=batch.failure_text,
            compile_log=compile_log,
            compile_diagnostics=compile_diagnostics,
        )
        for case_id in tuple(self._state._case_ids_by_batch[batch.batch_id]):
            case = self._state._cases[case_id]
            if case.status in self._state._TERMINAL_CASE_STATUSES:
                continue
            if case.cancel_requested:
                case.terminal_result = None
                if case.status == "staged":
                    continue
                if case.status in {"reporting", "cache-probing"}:
                    continue
                case.cancel_requested = False
                case.requeue_on_abort = False
                case.result = None
                self._state._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                    refresh_batch=False,
                )
                continue
            case_feedback = batch.failure_text
            if case.debug_text:
                if case_feedback in case.debug_text:
                    case_feedback = case.debug_text
                elif case.debug_text not in case_feedback:
                    case_feedback = self._merge_debug_text(
                        case_feedback,
                        case.debug_text,
                    )
            result = self._program_failure_case_result_locked(
                batch,
                test_name=case.test_name,
                feedback_text=case_feedback,
                compile_log=compile_log,
                compile_diagnostics=compile_diagnostics,
            )
            if case.status == "staged":
                case.terminal_result = result
                continue
            if case.status == "reporting":
                # add-judging-run already claimed the canonical candidate.
                # The program failure may close every other Case, but it must
                # not replace a result whose report callback linearized first.
                continue
            if case.status == "cache-probing":
                case.terminal_result = result
                continue
            case.result = result
            case.terminal_result = None
            case.requeue_on_abort = False
            self._state._transition_case_locked(
                case,
                "reported",
                lease_owner=case.lease_owner,
                updated_at=updated_at,
                refresh_batch=False,
            )
        self._state._refresh_batches_locked({batch.batch_id}, updated_at=updated_at)

    @staticmethod
    def _program_terminal_claim(
        outcome: ProgramTerminalClaimOutcome,
        *,
        case_id: int,
        batch_id: int,
    ) -> ProgramTerminalClaim:
        return ProgramTerminalClaim(
            outcome=outcome,
            case_id=int(case_id),
            batch_id=int(batch_id),
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
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return False
            batch = self._state._batches.get(case.batch_id)
            if batch is None:
                return False
            if not self._callback_receipt_matches_locked(
                case,
                hostname=hostname,
                receipt_generation=receipt_generation,
            ) or case.status not in {
                "leased",
                "reporting",
                *self._state._TERMINAL_CASE_STATUSES,
            }:
                return False
            if batch.failure_runresult or batch.compile_state == "failed":
                return True
            if batch.compile_state == "succeeded":
                evidence_changed = False
                if not batch.compile_output_b64 and compile_output_b64:
                    batch.compile_output_b64 = compile_output_b64
                    evidence_changed = True
                if not batch.compile_metadata_b64 and compile_metadata_b64:
                    batch.compile_metadata_b64 = compile_metadata_b64
                    evidence_changed = True
                if evidence_changed:
                    batch.updated_at = updated_at
                    self._state._touch_batch_locked(batch)
                return True
            if batch.status != "open" or case.status not in {"leased", "reporting"}:
                return False
            batch.compile_success = 1
            batch.compile_state = "succeeded"
            batch.compile_output_b64 = compile_output_b64
            batch.compile_metadata_b64 = compile_metadata_b64
            batch.updated_at = updated_at
            self._state._touch_batch_locked(batch)
            return True

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
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case_id,
                    batch_id=0,
                )
            batch = self._state._batches.get(case.batch_id)
            if batch is None:
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case.id,
                    batch_id=case.batch_id,
                )
            if not self._callback_receipt_matches_locked(
                case,
                hostname=hostname,
                receipt_generation=receipt_generation,
            ):
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if batch.program_failure_result is not None or batch.failure_runresult:
                return self._program_terminal_claim(
                    "idempotent",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if case.status in {"reporting", *self._state._TERMINAL_CASE_STATUSES}:
                return self._program_terminal_claim(
                    "late",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if (
                batch.status != "open"
                or case.status != "leased"
                or case.lease_owner != hostname
            ):
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if case.cancel_requested:
                case.cancel_requested = False
                case.terminal_result = None
                case.requeue_on_abort = False
                case.result = None
                self._state._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
                return self._program_terminal_claim(
                    "cancelled",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if batch.compile_state == "succeeded":
                contradiction = (
                    "judgehost protocol contradiction: compilation failure "
                    "reported after compilation succeeded"
                )
                if failure_text and failure_text not in contradiction:
                    contradiction = self._merge_debug_text(
                        contradiction,
                        failure_text,
                    )
                batch.failure_runresult = "internal-error"
                batch.failure_text = contradiction
                batch.updated_at = updated_at
                self._state._touch_batch_locked(batch)
                self._install_program_failure_locked(
                    batch,
                    updated_at=updated_at,
                )
                return self._program_terminal_claim(
                    "claimed",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            feedback = failure_text or "compilation failed"
            batch.compile_success = 0
            batch.compile_state = "failed"
            batch.compile_output_b64 = compile_output_b64
            batch.compile_metadata_b64 = compile_metadata_b64
            batch.failure_runresult = "compiler-error"
            batch.failure_text = feedback
            batch.updated_at = updated_at
            self._state._touch_batch_locked(batch)
            self._install_program_failure_locked(
                batch,
                compile_log=compile_log,
                compile_diagnostics=compile_diagnostics,
                updated_at=updated_at,
            )
            return self._program_terminal_claim(
                "claimed",
                case_id=case.id,
                batch_id=batch.batch_id,
            )

    @staticmethod
    def _append_pending_diagnostic_locked(
        case: CaseRecord,
        diagnostic: PendingCaseDiagnostic,
        *,
        limit_bytes: int,
    ) -> str:
        if not case.verification_task_id:
            return "not-applicable"
        limit = max(1, int(limit_bytes))
        if any(
            existing.digest == diagnostic.digest
            for existing in case.pending_diagnostics
        ):
            return "duplicate"
        retained = [*case.pending_diagnostics, diagnostic]
        while len(retained) > 1 and (
            len(retained) > _PENDING_DIAGNOSTIC_MAX_ITEMS
            or _pending_diagnostic_size(retained) > limit
        ):
            retained.pop(0)
        if _pending_diagnostic_size(retained) > limit:
            bounded = _fit_single_pending_diagnostic(
                retained[-1],
                limit_bytes=limit,
            )
            if bounded is None:
                return "not-applicable"
            retained = [bounded]
        case.pending_diagnostics[:] = retained
        return "persisted"

    @staticmethod
    def _callback_receipt_matches_locked(
        case: CaseRecord,
        *,
        hostname: str,
        receipt_generation: int,
    ) -> bool:
        expected_hostname = case.lease_owner or case.last_callback_hostname
        if not expected_hostname or expected_hostname != hostname:
            return False
        if case.claim_generation == int(receipt_generation):
            return True
        return (
            case.claim_generation == int(receipt_generation) + 1
            and case.status in {"reporting", "reported", "cancelled"}
            and case.last_callback_hostname == hostname
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
        diagnostic = _new_pending_diagnostic(
            kind="internal-error",
            hostname=hostname,
            text=diagnostic_text,
            received_at=updated_at,
            limit_bytes=diagnostic_limit_bytes,
        )
        diagnostic_digest = sha256_hex_json(
            {"case_id": int(case_id), "digest": diagnostic.digest},
            ensure_ascii=False,
        )
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case_id,
                    batch_id=0,
                )
            batch = self._state._batches.get(case.batch_id)
            if batch is None:
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case.id,
                    batch_id=case.batch_id,
                )
            if not self._callback_receipt_matches_locked(
                case,
                hostname=hostname,
                receipt_generation=receipt_generation,
            ):
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if batch.program_failure_diagnostic_digest == diagnostic_digest:
                return self._program_terminal_claim(
                    "idempotent",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if case.status in {"reporting", *self._state._TERMINAL_CASE_STATUSES}:
                self._append_pending_diagnostic_locked(
                    case,
                    diagnostic,
                    limit_bytes=diagnostic_limit_bytes,
                )
                return self._program_terminal_claim(
                    "late",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if (
                batch.status != "open"
                or case.status != "leased"
                or case.lease_owner != hostname
            ):
                return self._program_terminal_claim(
                    "rejected",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if case.cancel_requested:
                self._append_pending_diagnostic_locked(
                    case,
                    diagnostic,
                    limit_bytes=diagnostic_limit_bytes,
                )
                case.cancel_requested = False
                case.terminal_result = None
                case.requeue_on_abort = False
                case.result = None
                self._state._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
                return self._program_terminal_claim(
                    "cancelled",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            if batch.program_failure_result is not None or batch.failure_runresult:
                self._append_pending_diagnostic_locked(
                    case,
                    diagnostic,
                    limit_bytes=diagnostic_limit_bytes,
                )
                return self._program_terminal_claim(
                    "late",
                    case_id=case.id,
                    batch_id=batch.batch_id,
                )
            case.debug_text = self._merge_debug_text(
                case.debug_text,
                diagnostic_text,
            )
            program_feedback = failure_text
            if case.debug_text:
                if program_feedback in case.debug_text:
                    program_feedback = case.debug_text
                elif case.debug_text not in program_feedback:
                    program_feedback = self._merge_debug_text(
                        program_feedback,
                        case.debug_text,
                    )
            batch.failure_runresult = "internal-error"
            batch.failure_text = program_feedback
            batch.program_failure_diagnostic_digest = diagnostic_digest
            batch.updated_at = updated_at
            self._state._touch_batch_locked(batch)
            self._install_program_failure_locked(batch, updated_at=updated_at)
            return self._program_terminal_claim(
                "claimed",
                case_id=case.id,
                batch_id=batch.batch_id,
            )

    def record_batch_failure(
        self,
        batch_id: int,
        *,
        runresult: str,
        error_text: str,
        updated_at: str,
    ) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if batch is None or batch.status != "open":
                return False
            if batch.program_failure_result is not None:
                return True
            if not batch.failure_runresult:
                batch.failure_runresult = runresult
                batch.failure_text = error_text
                batch.updated_at = updated_at
                self._state._touch_batch_locked(batch)
            elif (
                batch.failure_runresult == runresult
                and error_text
                and not batch.failure_text
            ):
                batch.failure_text = error_text
                batch.updated_at = updated_at
                self._state._touch_batch_locked(batch)
            self._install_program_failure_locked(batch, updated_at=updated_at)
            return True

    def case_execution_row(self, case_id: int) -> CaseExecutionRow | None:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return None
            batch = self._state._batches[case.batch_id]
            row: CaseExecutionRow = {
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
                "last_callback_hostname": case.last_callback_hostname,
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
            return row

    def case_output_for_task(
        self, task_id: str, test_name: str
    ) -> dict[str, object] | None:
        with self._state._lock:
            case_id = self._state._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            case = self._state._cases[case_id]
            output_ref = "" if case.result is None else case.result.output_run_ref
            return {
                "id": case.id,
                "output_run_ref": output_ref,
            }

    def case_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        with self._state._lock:
            case_id = self._state._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            case = self._state._cases[case_id]
            batch = self._state._batches[case.batch_id]
            return {
                **case_snapshot(case),
                "batch_id": batch.batch_id,
                "batch_status": batch.status,
                "compile_success": batch.compile_success,
            }

    def case_result_for_task(self, task_id: str, test_name: str) -> CaseResult | None:
        with self._state._lock:
            case_id = self._state._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            return self._state._cases[case_id].result

    def acquire_case_callback_receipt(self, case_id: int) -> CaseCallbackReceipt | None:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return None
            batch = self._state._batches.get(case.batch_id)
            if batch is None:
                return None
            receipt_id = next(self._state._next_callback_receipt_id)
            self._state._case_id_by_callback_receipt[receipt_id] = case.id
            case.callback_receipt_count += 1
            return CaseCallbackReceipt(
                receipt_id=receipt_id,
                case_id=case.id,
                batch_id=case.batch_id,
                verification_id=batch.verification_id,
                verification_task_id=case.verification_task_id,
                task_id=case.task_id,
                run_id=case.run_id,
                test_name=case.test_name,
                status=case.status,
                lease_owner=case.lease_owner or "",
                last_callback_hostname=case.last_callback_hostname,
                completion_acknowledged=case.completion_acknowledged,
                claim_generation=case.claim_generation,
            )

    def release_case_callback_receipt(self, receipt_id: int) -> None:
        with self._state._lock:
            case_id = self._state._case_id_by_callback_receipt.pop(
                int(receipt_id), None
            )
            if case_id is None:
                raise RuntimeError("unknown judgehost callback receipt")
            case = self._state._cases.get(case_id)
            if case is None or case.callback_receipt_count <= 0:
                raise RuntimeError("judgehost callback receipt counter underflow")
            case.callback_receipt_count -= 1

    def claim_case_reporting(
        self,
        case_id: int,
        *,
        hostname: str,
        receipt_generation: int,
        now_text: str,
    ) -> CaseClaim | None:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None or case.status in self._state._TERMINAL_CASE_STATUSES:
                return None
            if case.status == "reporting":
                raise CaseClaimBusy("judgehost case result is already being processed")
            if (
                case.status != "leased"
                or case.lease_owner != hostname
                or case.claim_generation != int(receipt_generation)
            ):
                return None
            case.claim_generation += 1
            self._state._transition_case_locked(
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
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if (
                case is None
                or case.status != "reporting"
                or case.claim_generation != int(generation)
                or case.lease_owner != lease_owner
            ):
                return False
            batch = self._state._batches.get(case.batch_id)
            if (
                batch is None
                or batch.status != "open"
                or batch.compile_state == "failed"
            ):
                return False
            if batch.compile_state == "unknown":
                batch.compile_success = 1
                batch.compile_state = "succeeded"
                batch.updated_at = updated_at
                self._state._touch_batch_locked(batch)
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
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if batch is None or batch.status != "open":
                return claimed
            while len(claimed) < max(0, int(limit)):
                case = self._state._peek_case_heap_locked(
                    batch.batch_id, status="cache-pending"
                )
                if case is None:
                    break
                heapq.heappop(self._state._cache_heaps_by_batch[batch.batch_id])
                case.claim_generation += 1
                self._state._transition_case_locked(
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
                claimed.append((claim, case_snapshot(case)))
            self._state._refresh_batches_locked({batch.batch_id}, updated_at=now_text)
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
            self._state._transition_case_locked(
                case, "cancelled", lease_owner=None, updated_at=updated_at
            )
            return "cancelled"
        case.result = terminal_result or result
        result_owner = case.lease_owner if case.status == "reporting" else None
        self._state._transition_case_locked(
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
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if (
                case is None
                or case.status not in {"reporting", "cache-probing"}
                or case.claim_generation != int(generation)
            ):
                return None
            if report_telemetry is not None:
                if (
                    case.status != "reporting"
                    or case.lease_owner != report_telemetry.hostname
                ):
                    return None
                self._state._record_case_telemetry_locked(case, report_telemetry)
            return self._finish_claim_locked(case, result=result, updated_at=updated_at)

    def commit_cancelled_receipt(
        self,
        case_id: int,
        *,
        generation: int,
        updated_at: str,
        report_telemetry: CaseReportTelemetry,
    ) -> bool:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if (
                case is None
                or case.status != "reporting"
                or case.claim_generation != int(generation)
                or case.lease_owner != report_telemetry.hostname
                or not case.cancel_requested
            ):
                return False
            self._state._record_case_telemetry_locked(case, report_telemetry)
            case.cancel_requested = False
            case.terminal_result = None
            case.requeue_on_abort = False
            case.result = None
            self._state._transition_case_locked(
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
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
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
                self._state._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif terminal_result is not None:
                case.result = terminal_result
                self._state._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            else:
                self._state._transition_case_locked(
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
        with self._state._lock:
            for claim, result in outcomes:
                case = self._state._cases.get(claim.case_id)
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
                self._state._transition_case_locked(
                    case,
                    status,
                    lease_owner=None,
                    updated_at=updated_at,
                    refresh_batch=False,
                )
                finished[case.id] = status
                affected_batch_ids.add(case.batch_id)
            self._state._refresh_batches_locked(
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
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
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
                self._state._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif terminal_result is not None:
                case.result = terminal_result
                self._state._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif case.status == "cache-probing":
                self._state._transition_case_locked(
                    case,
                    "cache-pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif case.requeue_on_abort:
                self._state._transition_case_locked(
                    case,
                    "pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            else:
                self._state._transition_case_locked(
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
        with self._state._lock:
            for claim in claims:
                case = self._state._cases.get(int(claim.case_id))
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
                self._state._transition_case_locked(
                    case,
                    status,
                    lease_owner=None,
                    updated_at=updated_at,
                    refresh_batch=False,
                )
                affected_batch_ids.add(case.batch_id)
                aborted += 1
            self._state._refresh_batches_locked(
                affected_batch_ids, updated_at=updated_at
            )
        return aborted

    def acknowledge_case_completion(self, case_id: int) -> bool:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return False
            if case.status not in self._state._TERMINAL_CASE_STATUSES:
                return False
            case.completion_acknowledged = True
            return True

    def acknowledge_case_completions(self, case_ids: list[int]) -> int:
        marked = 0
        with self._state._lock:
            for case_id in dict.fromkeys(int(raw_case_id) for raw_case_id in case_ids):
                case = self._state._cases.get(case_id)
                if (
                    case is None
                    or case.status not in self._state._TERMINAL_CASE_STATUSES
                ):
                    continue
                if not case.completion_acknowledged:
                    case.completion_acknowledged = True
                    marked += 1
        return marked

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
        item = _new_pending_diagnostic(
            kind=kind,
            hostname=hostname,
            text=text,
            received_at=now_text,
            limit_bytes=diagnostic_limit_bytes,
        )
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return None
            if not self._callback_receipt_matches_locked(
                case,
                hostname=hostname,
                receipt_generation=receipt_generation,
            ):
                return "rejected"
            case.updated_at = now_text
            if case.status in {"reporting", "reported", "cancelled"}:
                if not case.verification_task_id:
                    return "ignored"
                outcome = self._append_pending_diagnostic_locked(
                    case,
                    item,
                    limit_bytes=diagnostic_limit_bytes,
                )
                return "pending" if outcome != "not-applicable" else "ignored"
            if case.status != "leased" or case.lease_owner != hostname:
                return "rejected"
            case.debug_text = self._merge_debug_text(case.debug_text, item.text)
            return "primary"

    def pending_case_diagnostics(
        self, case_id: int
    ) -> tuple[PendingCaseDiagnostic, ...]:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            return () if case is None else tuple(case.pending_diagnostics)

    def acknowledge_case_diagnostic(
        self,
        case_id: int,
        diagnostic: PendingCaseDiagnostic,
    ) -> bool:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return False
            try:
                case.pending_diagnostics.remove(diagnostic)
            except ValueError:
                return False
            return True

    def case_debug_context(self, case_id: int) -> dict[str, object] | None:
        with self._state._lock:
            case = self._state._cases.get(int(case_id))
            if case is None:
                return None
            return {
                "batch_id": case.batch_id,
                "case_debug_text": case.debug_text,
                "batch_debug_text": self._state._batches[case.batch_id].debug_text,
            }

    def batch_debug_context(self, batch_id: int) -> dict[str, object] | None:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            return (
                None
                if batch is None
                else {"batch_id": batch.batch_id, "debug_text": batch.debug_text}
            )

    def append_debug_text(
        self,
        *,
        case_id: int | None,
        batch_id: int | None,
        debug_text: str,
        now_text: str,
    ) -> None:
        with self._state._lock:
            if case_id is not None and int(case_id) in self._state._cases:
                case = self._state._cases[int(case_id)]
                case.debug_text = self._merge_debug_text(case.debug_text, debug_text)
                case.updated_at = now_text
            if batch_id is not None and int(batch_id) in self._state._batches:
                batch = self._state._batches[int(batch_id)]
                batch.debug_text = self._merge_debug_text(batch.debug_text, debug_text)
                batch.updated_at = now_text

    @staticmethod
    def _merge_debug_text(current: str, incoming: str) -> str:
        merged = incoming if not current else f"{current}\n{incoming}"
        return merged[-4000:]

    def case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        with self._state._lock:
            result: dict[str, dict[str, int]] = {}
            for run_id in (run_id for run_id in run_ids if run_id):
                counts = self._state._run_counts.get(run_id)
                if counts is None or counts.total == 0:
                    continue
                result[run_id] = {
                    "total": counts.total,
                    "reported": counts.reported,
                    "leased": counts.leased + counts.reporting,
                }
            return result
