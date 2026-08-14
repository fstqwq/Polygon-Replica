from collections import deque
from typing import TYPE_CHECKING

from app.service.judgehost.batch.model import HostLeaseRelease, VerificationCancellation

if TYPE_CHECKING:
    from app.service.judgehost.batch.state import BatchState


class BatchMaintenance:
    """Own cancellation, lease release, quiet cleanup, and runtime reset."""

    def __init__(self, state: "BatchState") -> None:
        self._state = state

    def reset(self) -> None:
        self._state.reset()

    def request_verification_cancel(
        self,
        verification_id: str,
        *,
        now_text: str,
    ) -> VerificationCancellation:
        """Close admission and cancel every not-yet-running Case in one operation."""
        token = verification_id or "__direct__"
        with self._state._lock:
            self._state._closed_verification_ids.add(token)
            batch_ids = tuple(sorted(self._state._batch_ids_by_verification.get(token, ())))
            batch_id_set = set(batch_ids)
            task_ids: set[str] = set()
            awaiting_task_ids: set[str] = set()
            cancelled_count = 0
            awaiting_receipt_count = 0

            for hostname, queue_ids in tuple(self._state._affinity_batches_by_host.items()):
                retained = deque(batch_id for batch_id in queue_ids if batch_id not in batch_id_set)
                if retained:
                    self._state._affinity_batches_by_host[hostname] = retained
                else:
                    self._state._affinity_batches_by_host.pop(hostname, None)
            for batch_id in batch_ids:
                batch = self._state._batches[batch_id]
                self._state._closed_program_keys.add((token, batch.verification_program_id))
                for case_id in tuple(self._state._case_ids_by_batch.get(batch_id, ())):
                    case = self._state._cases[case_id]
                    task_ids.add(case.task_id)
                    if case.status in self._state._TERMINAL_CASE_STATUSES:
                        continue
                    if case.status == "staged":
                        case.cancel_requested = True
                        case.terminal_result = None
                        continue
                    if case.status in {"leased", "reporting", "cache-probing"}:
                        case.cancel_requested = True
                        case.terminal_result = None
                        awaiting_task_ids.add(case.task_id)
                        awaiting_receipt_count += 1
                        continue
                    case.result = None
                    self._state._transition_case_locked(
                        case,
                        "cancelled",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
                    cancelled_count += 1
            self._state._refresh_batches_locked(batch_id_set, updated_at=now_text)
            return VerificationCancellation(
                batch_ids=batch_ids,
                task_ids=tuple(sorted(task_ids)),
                awaiting_task_ids=tuple(sorted(awaiting_task_ids)),
                cancelled_case_count=cancelled_count,
                awaiting_receipt_count=awaiting_receipt_count,
            )

    def release_host_leases(
        self,
        hostname: str,
        *,
        now_text: str,
        verification_id: str = "",
    ) -> HostLeaseRelease:
        with self._state._lock:
            if not verification_id:
                self._state._drop_host_telemetry_batch_locked(hostname)
                self._state._stolen_batch_by_host.pop(hostname, None)
            affinity_ids = (
                () if verification_id else self._state._affinity_batches_by_host.pop(hostname, ())
            )
            context_batch_ids = set(affinity_ids)
            affected_batch_ids = set(context_batch_ids)
            compile_owner_batch_ids = {
                batch_id
                for batch_id, owner in self._state._compile_owner_by_batch.items()
                if owner == hostname
                and (
                    not verification_id
                    or (
                        batch_id in self._state._batches
                        and self._state._batches[batch_id].verification_id == verification_id
                    )
                )
            }
            for batch_id in compile_owner_batch_ids:
                self._state._compile_owner_by_batch.pop(batch_id, None)
            affected_batch_ids.update(compile_owner_batch_ids)
            terminal_task_ids: set[str] = set()
            case_ids = [
                case_id
                for case_id in self._state._leased_case_ids_by_host.get(hostname, ())
                if not verification_id
                or (
                    case_id in self._state._cases
                    and self._state._batches[self._state._cases[case_id].batch_id].verification_id
                    == verification_id
                )
            ]
            workdirs: set[tuple[int, int]] = set()
            for case_id in case_ids:
                case = self._state._cases[case_id]
                batch = self._state._batches[case.batch_id]
                submission = self._state._compile_submissions_by_key[batch.compile_key]
                workdirs.add((batch.job_id, submission.submit_id))
                if case.status == "reporting":
                    case.requeue_on_abort = True
                    leased_ids = self._state._leased_case_ids_by_host[hostname]
                    leased_ids.discard(case.id)
                    if not leased_ids:
                        self._state._leased_case_ids_by_host.pop(hostname, None)
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
                    self._state._transition_case_locked(
                        case,
                        status,
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
                    if (
                        status in self._state._TERMINAL_CASE_STATUSES
                        and self._state._task_case_counts[case.task_id].remaining == 0
                    ):
                        terminal_task_ids.add(case.task_id)
                affected_batch_ids.add(case.batch_id)
            self._state._refresh_batches_locked(affected_batch_ids, updated_at=now_text)
            terminal_batch_ids = tuple(
                sorted(
                    batch_id
                    for batch_id in affected_batch_ids
                    if self._state._batches.get(batch_id) is not None
                    and self._state._batches[batch_id].status == "finalize-pending"
                )
            )
            return HostLeaseRelease(
                affinity_count=len(context_batch_ids),
                lease_count=len(case_ids),
                terminal_batch_ids=terminal_batch_ids,
                terminal_task_ids=tuple(sorted(terminal_task_ids)),
                workdirs=tuple(sorted(workdirs)),
            )

    def forget_runs(self, run_ids: list[str]) -> int:
        safe_run_ids = {run_id for run_id in run_ids if run_id}
        if not safe_run_ids:
            return 0
        with self._state._lock:
            affected_batches = {
                batch_id
                for run_id in safe_run_ids
                for batch_id in self._state._batch_ids_by_run.get(run_id, ())
            }
            case_ids = {
                case_id
                for run_id in safe_run_ids
                for case_id in self._state._case_ids_by_run.get(run_id, ())
            }
            self._state._remove_cases_locked(case_ids)
            for batch_id in affected_batches:
                if batch_id in self._state._batches:
                    self._state._touch_batch_locked(self._state._batches[batch_id])
            empty_batches = tuple(
                batch_id
                for batch_id in affected_batches
                if batch_id in self._state._empty_batch_ids
            )
            for batch_id in empty_batches:
                self._state._remove_batch_locked(batch_id)
            return len(empty_batches)

    def forget_runs_if_quiet(self, run_ids: list[str]) -> int | None:
        safe_run_ids = {run_id for run_id in run_ids if run_id}
        if not safe_run_ids:
            return 0
        with self._state._lock:
            affected_batches = {
                batch_id
                for run_id in safe_run_ids
                for batch_id in self._state._batch_ids_by_run.get(run_id, ())
            }
            case_ids = {
                case_id
                for run_id in safe_run_ids
                for case_id in self._state._case_ids_by_run.get(run_id, ())
            }
            cases = [
                self._state._cases[case_id] for case_id in case_ids if case_id in self._state._cases
            ]
            if any(
                self._state._batches[batch_id].status == "finalizing"
                for batch_id in affected_batches
                if batch_id in self._state._batches
            ):
                return None
            if any(
                case.callback_receipt_count > 0
                or bool(case.pending_diagnostics)
                or case.status not in self._state._TERMINAL_CASE_STATUSES
                or not case.completion_acknowledged
                for case in cases
            ):
                return None
            self._state._remove_cases_locked(case_ids)
            for batch_id in affected_batches:
                if batch_id in self._state._batches:
                    self._state._touch_batch_locked(self._state._batches[batch_id])
            empty_batches = tuple(
                batch_id
                for batch_id in affected_batches
                if batch_id in self._state._empty_batch_ids
            )
            for batch_id in empty_batches:
                self._state._remove_batch_locked(batch_id)
            return len(empty_batches)

    def cancel_all_inflight(self, *, now_text: str) -> list[int]:
        with self._state._lock:
            batch_ids = sorted(
                batch_id
                for batch_id, batch in self._state._batches.items()
                if batch.status in {"open", "finalize-pending", "finalizing"}
            )
            for batch_id in batch_ids:
                for case_id in tuple(self._state._case_ids_by_batch[batch_id]):
                    case = self._state._cases[case_id]
                    if case.status in self._state._TERMINAL_CASE_STATUSES:
                        continue
                    if case.status == "staged":
                        case.cancel_requested = True
                        case.terminal_result = None
                        case.requeue_on_abort = False
                        continue
                    case.cancel_requested = False
                    case.terminal_result = None
                    case.requeue_on_abort = False
                    case.result = None
                    self._state._transition_case_locked(
                        case,
                        "cancelled",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
            self._state._refresh_batches_locked(set(batch_ids), updated_at=now_text)
            return batch_ids
