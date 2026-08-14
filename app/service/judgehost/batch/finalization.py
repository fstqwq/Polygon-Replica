import heapq
import time
from typing import TYPE_CHECKING

from app.service.judgehost.batch.model import ExecutionBatchFinalizationClaim

if TYPE_CHECKING:
    from app.service.judgehost.batch.state import BatchState


class BatchFinalization:
    """Own batch-level finalization claims and retry scheduling."""

    def __init__(self, state: "BatchState") -> None:
        self._state = state

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
            return None if batch is None else {field: getattr(batch, field) for field in fields}

    def claim_batch_finalization(
        self,
        batch_id: int,
        *,
        now_text: str,
    ) -> ExecutionBatchFinalizationClaim | None:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if batch is None:
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
            if (
                batch.status != "finalize-pending"
                or counts.total == 0
                or counts.terminal != counts.total
                or batch.materialization_state == "materializing"
            ):
                return None
            self._state._mutate_batch_locked(batch, status="finalizing", updated_at=now_text)
            cases = [
                self._state._case_row(row)
                for row in self._state._sorted_cases_locked(
                    self._state._case_ids_by_batch[batch.batch_id]
                )
            ]
            return {"batch": self._state._batch_row(batch), "cases": cases}

    def schedule_batch_finalization_retry(
        self,
        batch_id: int,
        *,
        now_text: str,
        delay_sec: float = 0.25,
    ) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if batch is None or batch.status not in {
                "open",
                "finalize-pending",
                "finalizing",
            }:
                return False
            if batch.status == "finalizing":
                self._state._mutate_batch_locked(
                    batch,
                    status="finalize-pending",
                    updated_at=now_text,
                )
            deadline = time.monotonic() + max(0.0, float(delay_sec))
            current = self._state._finalization_retry_deadlines.get(batch.batch_id)
            if current is None or deadline < current:
                self._state._finalization_retry_deadlines[batch.batch_id] = deadline
                heapq.heappush(self._state._finalization_retry_heap, (deadline, batch.batch_id))
            return True

    def due_batch_finalizations(self, *, limit: int) -> list[int]:
        due: list[int] = []
        now = time.monotonic()
        with self._state._lock:
            while self._state._finalization_retry_heap and len(due) < max(0, int(limit)):
                deadline, batch_id = self._state._finalization_retry_heap[0]
                if deadline > now:
                    break
                heapq.heappop(self._state._finalization_retry_heap)
                if self._state._finalization_retry_deadlines.get(batch_id) != deadline:
                    continue
                self._state._finalization_retry_deadlines.pop(batch_id, None)
                batch = self._state._batches.get(batch_id)
                if batch is not None and batch.status in {
                    "open",
                    "finalize-pending",
                }:
                    due.append(batch_id)
        return due

    def clear_batch_finalization_retry(self, batch_id: int) -> None:
        with self._state._lock:
            self._state._finalization_retry_deadlines.pop(int(batch_id), None)

    def set_batch_terminal_status(
        self,
        batch_id: int,
        *,
        status: str,
        completed_at: str,
        updated_at: str,
    ) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if batch is None or batch.status != "finalizing":
                return False
            self._state._mutate_batch_locked(
                batch,
                status=status,
                completed_at=completed_at,
                updated_at=updated_at,
            )
            self._state._finalization_retry_deadlines.pop(batch.batch_id, None)
            self._state._discard_batch_telemetry_locked(batch.batch_id)
            return True
