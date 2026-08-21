import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

from app.db import now_iso
from app.service.execution.limits import VERIFICATION_RUNTIME_BATCH_SIZE
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.finalization.service import JudgehostBatchFinalizer
from app.service.judgehost.maintenance.terminal_cleanup import JudgehostTerminalCleanup
from app.service.judgehost.task.registry import JudgehostTaskRegistry


logger = logging.getLogger(__name__)

_IDLE_RETRY_SEC = 0.25
_DISCOVERY_INTERVAL_SEC = 0.5


@dataclass
class _CancellationRequest:
    verification_id: str
    reason: str
    queued_monotonic: float
    processed_cases: int = 0
    processed_tasks: int = 0
    slices: int = 0


class JudgehostCancellationDrain:
    """Deduplicated background retirement for durably cancelled Verifications."""

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        tasks: JudgehostTaskRegistry,
        batch_finalizer: JudgehostBatchFinalizer,
        terminal_cleanup: JudgehostTerminalCleanup,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._tasks = tasks
        self._batch_finalizer = batch_finalizer
        self._terminal_cleanup = terminal_cleanup
        self._condition = threading.Condition(threading.Lock())
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._requests: dict[str, _CancellationRequest] = {}
        self._drained: set[str] = set()
        self._active_verification_id = ""
        self._started = False
        self._resetting = False

    def _ensure_started_locked(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(
            target=self._run,
            name="judgehost-cancellation-drain",
            daemon=True,
        ).start()

    def start(self) -> None:
        with self._condition:
            self._ensure_started_locked()

    def schedule(self, verification_id: str, *, reason: str) -> None:
        if not verification_id:
            raise RuntimeError("verification id is required")
        if not reason:
            raise RuntimeError("judgehost cancellation reason is required")
        with self._condition:
            while self._resetting:
                self._condition.wait()
            request = self._requests.get(verification_id)
            self._drained.discard(verification_id)
            if request is None:
                request = _CancellationRequest(
                    verification_id=verification_id,
                    reason=reason,
                    queued_monotonic=time.monotonic(),
                )
                self._requests[verification_id] = request
            if (
                verification_id != self._active_verification_id
                and verification_id not in self._queued
            ):
                self._queue.append(verification_id)
                self._queued.add(verification_id)
            self._ensure_started_locked()
            self._condition.notify()

    def pause(self) -> None:
        with self._condition:
            self._resetting = True
            self._queue.clear()
            self._queued.clear()
            while self._active_verification_id:
                self._condition.wait()
            self._requests.clear()
            self._drained.clear()

    def resume(self) -> None:
        with self._condition:
            self._resetting = False
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait(timeout=_DISCOVERY_INTERVAL_SEC)
                    if not self._queue and not self._resetting:
                        for verification_id in (
                            self._batch_runtime.cancelled_verification_ids()
                        ):
                            if (
                                verification_id in self._drained
                                or verification_id in self._requests
                                or verification_id in self._queued
                            ):
                                continue
                            self._requests[verification_id] = _CancellationRequest(
                                verification_id=verification_id,
                                reason="verification cancelled",
                                queued_monotonic=time.monotonic(),
                            )
                            self._queue.append(verification_id)
                            self._queued.add(verification_id)
                verification_id = self._queue.popleft()
                self._queued.discard(verification_id)
                request = self._requests.get(verification_id)
                if request is None or self._resetting:
                    continue
                self._active_verification_id = verification_id
            failed = False
            completed = False
            awaiting_receipts = 0
            made_progress = False
            try:
                completed, awaiting_receipts, made_progress = self._drain_slice(request)
            except Exception:
                failed = True
                logger.exception(
                    "Judgehost cancellation drain slice failed verification_id=%s",
                    verification_id,
                )
            with self._condition:
                self._active_verification_id = ""
                self._condition.notify_all()
                if self._resetting:
                    continue
                if completed:
                    self._requests.pop(verification_id, None)
                    self._drained.add(verification_id)
                    elapsed = time.monotonic() - request.queued_monotonic
                    logger.info(
                        "Judgehost cancellation drain completed "
                        "verification_id=%s cases=%s tasks=%s slices=%s elapsed_sec=%.3f",
                        verification_id,
                        request.processed_cases,
                        request.processed_tasks,
                        request.slices,
                        elapsed,
                    )
                    continue
                if verification_id not in self._queued:
                    self._queue.append(verification_id)
                    self._queued.add(verification_id)
                self._condition.notify()
            if failed or awaiting_receipts or not made_progress:
                time.sleep(_IDLE_RETRY_SEC)

    def _drain_slice(
        self,
        request: _CancellationRequest,
    ) -> tuple[bool, int, bool]:
        started = time.monotonic()
        if request.slices == 0:
            logger.info(
                "Judgehost cancellation drain started "
                "verification_id=%s queue_delay_sec=%.3f",
                request.verification_id,
                started - request.queued_monotonic,
            )
        case_outcome = self._batch_runtime.drain_verification_cancel_slice(
            request.verification_id,
            now_text=now_iso(),
            limit=VERIFICATION_RUNTIME_BATCH_SIZE,
        )
        task_count, remaining_task_count = self._tasks.cancel_verification_tasks(
            request.verification_id,
            reason=request.reason,
            now_text=now_iso(),
            limit=VERIFICATION_RUNTIME_BATCH_SIZE,
        )
        retired_all = True
        retired_count = 0
        terminal_batch_ids = case_outcome.terminal_batch_ids
        for batch_id in terminal_batch_ids[:VERIFICATION_RUNTIME_BATCH_SIZE]:
            retired = self._batch_finalizer.retire_cancelled_batch(batch_id)
            retired_all = retired and retired_all
            retired_count += int(retired)
        if len(terminal_batch_ids) > VERIFICATION_RUNTIME_BATCH_SIZE:
            retired_all = False

        request.processed_cases += case_outcome.processed_case_count
        request.processed_tasks += task_count
        request.slices += 1
        completed = (
            not case_outcome.has_remaining_runtime
            and remaining_task_count == 0
            and retired_all
        )
        if completed:
            self._terminal_cleanup.schedule(request.verification_id)
        logger.debug(
            "Judgehost cancellation drain slice "
            "verification_id=%s cases=%s tasks=%s awaiting_receipts=%s "
            "remaining_tasks=%s terminal_batches=%s complete=%s elapsed_sec=%.3f",
            request.verification_id,
            case_outcome.processed_case_count,
            task_count,
            case_outcome.awaiting_receipt_count,
            remaining_task_count,
            len(terminal_batch_ids),
            completed,
            time.monotonic() - started,
        )
        return (
            completed,
            case_outcome.awaiting_receipt_count,
            bool(case_outcome.processed_case_count or task_count or retired_count),
        )
