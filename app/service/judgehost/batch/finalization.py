import heapq
import time
from typing import TYPE_CHECKING

from app.service.judgehost.batch.model import FinalizationClaim
from app.service.judgehost.batch.snapshot import batch_snapshot, case_snapshot

if TYPE_CHECKING:
    from app.service.judgehost.batch.state import BatchState


class BatchFinalization:
    """Own batch-level finalization claims and retry scheduling."""

    def __init__(self, state: "BatchState") -> None:
        self._state = state

    def _has_terminal_work_locked(self, batch_id: int) -> bool:
        return any(
            case.status in {"reported", "cancelled"}
            and (not case.completion_acknowledged or bool(case.pending_diagnostics))
            for case_id in self._state._case_ids_by_batch[batch_id]
            if (case := self._state._cases.get(case_id)) is not None
        )

    def _needs_another_finalization_locked(self, batch_id: int) -> bool:
        batch = self._state._batches[batch_id]
        counts = self._state._batch_counts[batch_id]
        terminal_transition_ready = (
            batch.status == "open"
            and batch.verification_id in self._state._closed_verification_ids
            and counts.total > 0
            and counts.terminal == counts.total
            and batch.materialization_state != "materializing"
        )
        return bool(
            terminal_transition_ready
            or batch.status in {"finalize-pending", "finalizing"}
            or self._has_terminal_work_locked(batch_id)
        )

    def _schedule_retry_locked(self, batch_id: int, *, delay_sec: float) -> None:
        deadline = time.monotonic() + max(0.0, float(delay_sec))
        current = self._state._finalization_retry_deadlines.get(batch_id)
        if current is None or deadline < current:
            self._state._finalization_retry_deadlines[batch_id] = deadline
            heapq.heappush(self._state._finalization_retry_heap, (deadline, batch_id))

    def batch_finalize_row(self, batch_id: int) -> dict[str, object] | None:
        fields = (
            "execution_signature",
            "status",
            "compile_success",
            "compile_output_b64",
            "compile_metadata_b64",
            "debug_text",
            "run_config_json",
            "source_name",
            "compile_hash",
            "run_hash",
            "compare_hash",
            "materialization_state",
            "failure_runresult",
            "failure_text",
        )
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            return (
                None
                if batch is None
                else {field: getattr(batch, field) for field in fields}
            )

    def claim_batch_finalization(
        self,
        batch_id: int,
        *,
        now_text: str,
    ) -> FinalizationClaim | None:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if (
                batch is None
                or batch.batch_id
                in self._state._active_finalization_generation_by_batch
            ):
                return None
            counts = self._state._batch_counts[batch.batch_id]
            if (
                batch.status == "open"
                and batch.verification_id in self._state._closed_verification_ids
                and counts.total > 0
                and counts.terminal == counts.total
                and batch.materialization_state != "materializing"
            ):
                self._state._close_batch_locked(batch, updated_at=now_text)
            terminal_transition = (
                batch.status == "finalize-pending"
                and counts.total > 0
                and counts.terminal == counts.total
                and batch.materialization_state != "materializing"
            )
            cases = tuple(
                case_snapshot(row)
                for row in self._state._sorted_cases_locked(
                    self._state._case_ids_by_batch[batch.batch_id]
                )
            )
            has_terminal_work = self._has_terminal_work_locked(batch.batch_id)
            if not terminal_transition and not has_terminal_work:
                return None
            if terminal_transition:
                batch.status = "finalizing"
                batch.updated_at = now_text
                self._state._touch_batch_locked(batch)
            generation = (
                self._state._finalization_generation_by_batch.get(batch.batch_id, 0) + 1
            )
            self._state._finalization_generation_by_batch[batch.batch_id] = generation
            self._state._active_finalization_generation_by_batch[batch.batch_id] = (
                generation
            )
            return FinalizationClaim(
                batch_id=batch.batch_id,
                generation=generation,
                terminal_transition=terminal_transition,
                batch=batch_snapshot(batch),
                cases=cases,
            )

    def abort_batch_finalization(
        self,
        claim: FinalizationClaim,
        *,
        now_text: str,
        delay_sec: float = 0.25,
    ) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(claim.batch_id)
            if batch is None:
                return False
            if (
                self._state._active_finalization_generation_by_batch.get(claim.batch_id)
                != claim.generation
            ):
                return False
            self._state._active_finalization_generation_by_batch.pop(
                claim.batch_id, None
            )
            if claim.terminal_transition and batch.status == "finalizing":
                batch.status = "finalize-pending"
                batch.updated_at = now_text
                self._state._touch_batch_locked(batch)
            self._schedule_retry_locked(batch.batch_id, delay_sec=delay_sec)
            return True

    def complete_batch_finalization(self, claim: FinalizationClaim) -> bool:
        """Release a publication-only claim after all external work succeeds."""

        with self._state._lock:
            if claim.terminal_transition:
                return False
            batch = self._state._batches.get(claim.batch_id)
            if batch is None:
                return False
            if (
                self._state._active_finalization_generation_by_batch.get(claim.batch_id)
                != claim.generation
            ):
                return False
            self._state._active_finalization_generation_by_batch.pop(
                claim.batch_id, None
            )

            if self._needs_another_finalization_locked(batch.batch_id):
                # The claim contains an immutable snapshot. A callback or cache
                # probe can publish another terminal Case while the snapshot is
                # being persisted outside the runtime lock. Never let completion
                # of the older snapshot clear that newer work.
                self._schedule_retry_locked(batch.batch_id, delay_sec=0.0)
            else:
                self._state._finalization_retry_deadlines.pop(claim.batch_id, None)
            return True

    def requires_completion_ack(self, batch_id: int) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            return bool(
                batch is not None
                and batch.status == "open"
                and batch.verification_id
                not in self._state._closed_verification_ids
            )

    def verification_cancellation_requested(self, batch_id: int) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            return bool(
                batch is not None
                and batch.verification_id
                in self._state._cancelled_verification_ids
            )

    def schedule_batch_finalization_retry(
        self,
        batch_id: int,
        *,
        delay_sec: float = 0.25,
    ) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if batch is None or (
                batch.status not in {"open", "finalize-pending", "finalizing"}
                and not self._has_terminal_work_locked(batch.batch_id)
            ):
                return False
            self._schedule_retry_locked(batch.batch_id, delay_sec=delay_sec)
            return True

    def due_batch_finalizations(self, *, limit: int) -> list[int]:
        due: list[int] = []
        now = time.monotonic()
        with self._state._lock:
            while self._state._finalization_retry_heap and len(due) < max(
                0, int(limit)
            ):
                deadline, batch_id = self._state._finalization_retry_heap[0]
                if deadline > now:
                    break
                heapq.heappop(self._state._finalization_retry_heap)
                if self._state._finalization_retry_deadlines.get(batch_id) != deadline:
                    continue
                self._state._finalization_retry_deadlines.pop(batch_id, None)
                batch = self._state._batches.get(batch_id)
                if batch is not None and (
                    batch.status in {"open", "finalize-pending", "finalizing"}
                    or self._has_terminal_work_locked(batch.batch_id)
                ):
                    due.append(batch_id)
        return due

    def clear_batch_finalization_retry(self, batch_id: int) -> None:
        with self._state._lock:
            self._state._finalization_retry_deadlines.pop(int(batch_id), None)

    def set_batch_terminal_status(
        self,
        claim: FinalizationClaim,
        *,
        status: str,
        completed_at: str,
        updated_at: str,
    ) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(claim.batch_id)
            if (
                batch is None
                or batch.status != "finalizing"
                or not claim.terminal_transition
                or self._state._active_finalization_generation_by_batch.get(
                    claim.batch_id
                )
                != claim.generation
            ):
                return False
            batch.status = status
            batch.completed_at = completed_at
            batch.updated_at = updated_at
            self._state._touch_batch_locked(batch)
            self._state._finalization_retry_deadlines.pop(batch.batch_id, None)
            self._state._active_finalization_generation_by_batch.pop(
                batch.batch_id, None
            )
            self._state._discard_batch_telemetry_locked(batch.batch_id)
            return True
