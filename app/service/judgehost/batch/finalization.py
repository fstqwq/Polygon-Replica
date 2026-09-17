import heapq
import time
from typing import TYPE_CHECKING

from app.service.judgehost.batch.model import FinalizationClaim, JudgehostCaseRow
from app.service.judgehost.batch.snapshot import batch_snapshot, case_snapshot

if TYPE_CHECKING:
    from app.service.judgehost.batch.state import BatchState


class BatchFinalization:
    """Own independent case publication, batch closure, and failure retries."""

    def __init__(self, state: "BatchState") -> None:
        self._state = state

    def _has_terminal_work_locked(self, batch_id: int) -> bool:
        return bool(self._state._pending_publication_case_ids_by_batch.get(batch_id))

    def claim_case_publications(
        self, batch_id: int, *, case_ids: tuple[int, ...] | None = None
    ) -> tuple[JudgehostCaseRow, ...]:
        """Own only the selected cases; other cases can publish concurrently."""
        with self._state._lock:
            pending = self._state._pending_publication_case_ids_by_batch.get(batch_id)
            if not pending:
                return ()
            active = self._state._publishing_case_ids_by_batch[batch_id]
            selected = (pending if case_ids is None else pending.intersection(case_ids)) - active
            if not selected:
                return ()
            rows = tuple(
                case_snapshot(case)
                for case in self._state._sorted_cases_locked(selected)
            )
            pending.difference_update(selected)
            active.update(selected)
            return rows

    def complete_case_publications(
        self, batch_id: int, cases: tuple[JudgehostCaseRow, ...], *, retry: bool = False
    ) -> bool:
        """Release cases, retaining failed publication and concurrently added diagnostics."""
        with self._state._lock:
            batch = self._state._batches.get(batch_id)
            if batch is None:
                return False
            active = self._state._publishing_case_ids_by_batch[batch_id]
            pending = self._state._pending_publication_case_ids_by_batch[batch_id]
            for row in cases:
                case_id = row["id"]
                active.discard(case_id)
                case = self._state._cases.get(case_id)
                if case is None:
                    continue
                if retry or not case.completion_acknowledged or case.pending_diagnostics:
                    pending.add(case_id)
            if pending:
                self._schedule_retry_locked(batch_id, delay_sec=0.25 if retry else 0.0)
            elif not active and batch.status != "finalize-pending":
                self._state._finalization_retry_deadlines.pop(batch_id, None)
            return batch.status == "finalize-pending" and not active and not pending

    def retry_task_publication(self, task_id: str) -> None:
        with self._state._lock:
            for case_id in self._state._case_ids_by_task.get(task_id, ()):
                case = self._state._cases[case_id]
                if case.status in self._state._TERMINAL_CASE_STATUSES:
                    self._state._pending_publication_case_ids_by_batch[case.batch_id].add(case_id)
                    self._schedule_retry_locked(case.batch_id, delay_sec=0.25)

    def _schedule_retry_locked(self, batch_id: int, *, delay_sec: float) -> None:
        deadline = time.monotonic() + max(0.0, float(delay_sec))
        current = self._state._finalization_retry_deadlines.get(batch_id)
        if current is None or deadline < current:
            self._state._finalization_retry_deadlines[batch_id] = deadline
            heapq.heappush(self._state._finalization_retry_heap, (deadline, batch_id))

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
            terminal_transition = (
                batch.status == "finalize-pending"
                and counts.total > 0
                and counts.terminal == counts.total
                and batch.materialization_state != "materializing"
            )
            if (
                not terminal_transition
                or self._state._publishing_case_ids_by_batch.get(batch.batch_id)
            ):
                return None
            cases = tuple(
                case_snapshot(row)
                for row in self._state._sorted_cases_locked(
                    self._state._case_ids_by_batch[batch.batch_id]
                )
            )
            if any(not row["completion_acknowledged"] for row in cases):
                return None
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
            if batch.status == "finalizing":
                batch.status = "finalize-pending"
                batch.updated_at = now_text
                self._state._touch_batch_locked(batch)
            self._schedule_retry_locked(batch.batch_id, delay_sec=delay_sec)
            return True

    def publications_acknowledged(
        self, batch_id: int, *, case_ids: tuple[int, ...] | None = None
    ) -> bool:
        with self._state._lock:
            pending = self._state._pending_publication_case_ids_by_batch.get(batch_id, set())
            active = self._state._publishing_case_ids_by_batch.get(batch_id, set())
            selected = pending | active if case_ids is None else case_ids
            return all(
                (case := self._state._cases.get(case_id)) is None
                or case.completion_acknowledged
                for case_id in selected
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
            if self._has_terminal_work_locked(batch.batch_id):
                self._schedule_retry_locked(batch.batch_id, delay_sec=0.0)
            self._state._discard_batch_telemetry_locked(batch.batch_id)
            return True
