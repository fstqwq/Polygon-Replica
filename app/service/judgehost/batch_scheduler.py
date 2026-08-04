from __future__ import annotations

import heapq
import itertools
import statistics
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict
from collections.abc import Iterable

from app.service.judgehost.domjudge.client import domjudge_script_id
from app.service.judgehost.batch_scheduler_models import (
    CompileSubmission,
    JudgehostCaseRow,
    ExecutionBatchRow,
    CaseResult,
    CaseReportTelemetry,
    CaseRecord,
    ExecutionBatchSpec,
    ExecutionBatchRecord,
    StatusCounts,
    TaskCaseCounts,
    HostLeaseTelemetry,
    HostTelemetryRow,
    HostTelemetryState,
    VerificationCancellation,
)
from app.service.judgehost.identity import domjudge_job_id, domjudge_submit_id
from app.service.judgehost.batch_scheduler_results import BatchSchedulerResultMixin


class BatchScheduler(BatchSchedulerResultMixin):
    """Indexed process-local state for DOMjudge compatibility batches and cases."""

    _AFFINITY_QUEUE_SIZE = 4
    _ACTIVE_BATCH_STATUSES = frozenset({"open"})
    _TERMINAL_CASE_STATUSES = frozenset({"reported", "cancelled"})
    _PREREQUISITE_TASK_KINDS = ("main-correct", "generate-input")

    @staticmethod
    def _compile_submission_identity(submission: CompileSubmission) -> tuple[object, ...]:
        return (
            submission.compile_key,
            submission.submit_id,
            submission.source_name,
            submission.source_file.identity,
            submission.source_file.size,
            tuple(
                (name, payload.identity, payload.size)
                for name, payload in submission.extra_source_items
            ),
            submission.compile_files,
        )

    def __init__(self, lock: threading.RLock | None = None, *, id_base: int | None = None):
        self._lock = threading.RLock() if lock is None else lock
        self._id_base = max(1, int(id_base if id_base is not None else time.time() * 1000))
        self._entity_ids = itertools.count(self._id_base + 1)
        self._batches: dict[int, ExecutionBatchRecord] = {}
        self._cases: dict[int, CaseRecord] = {}
        self._case_ids_by_batch: dict[int, set[int]] = defaultdict(set)
        self._case_ids_by_task: dict[str, set[int]] = defaultdict(set)
        self._case_ids_by_run: dict[str, set[int]] = defaultdict(set)
        self._case_ids_by_testcase: dict[int, set[int]] = defaultdict(set)
        self._testcase_hash_by_id: dict[int, str] = {}
        self._latest_case_id_by_task_test: dict[tuple[str, str], int] = {}
        self._batch_id_by_task: dict[str, int] = {}
        self._batch_ids_by_run: dict[str, set[int]] = defaultdict(set)
        self._batch_id_by_logical_run: dict[tuple[str, str], int] = {}
        self._closed_logical_run_keys: set[tuple[str, str]] = set()
        self._closed_verification_ids: set[str] = set()
        self._script_hash_refcounts: dict[tuple[str, int, str], int] = defaultdict(int)
        self._script_hashes_by_id: dict[tuple[str, int], set[str]] = defaultdict(set)
        self._leased_case_ids_by_host: dict[str, set[int]] = defaultdict(set)
        self._empty_batch_ids: set[int] = set()
        self._batch_counts: dict[int, StatusCounts] = {}
        self._run_counts: dict[str, StatusCounts] = defaultdict(StatusCounts)
        self._task_case_counts: dict[str, TaskCaseCounts] = defaultdict(TaskCaseCounts)
        self._sequence = itertools.count()
        self._scope_sequence_by_verification: dict[str, int] = {}
        self._batch_specs: dict[int, ExecutionBatchSpec] = {}
        self._compile_submissions_by_key: dict[str, CompileSubmission] = {}
        self._materialized_compile_submissions_by_key: dict[str, CompileSubmission] = {}
        self._compile_key_by_submit_id: dict[int, str] = {}
        self._batch_ids_by_compile_key: dict[str, set[int]] = defaultdict(set)
        self._batch_ids_by_verification: dict[str, set[int]] = defaultdict(set)
        self._verification_by_domjudge_job_id: dict[int, str] = {}
        self._ready_batches: list[tuple[int, int, int, int]] = []
        self._cache_heaps_by_batch: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        self._runnable_heaps_by_batch: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        self._affinity_batches_by_host: dict[str, deque[int]] = defaultdict(deque)
        self._stolen_batch_by_host: dict[str, int] = {}
        self._batch_ids_in_heap: set[int] = set()
        self._ready_prerequisite_ids: dict[tuple[str, str], dict[int, None]] = defaultdict(dict)
        self._finalization_retry_heap: list[tuple[float, int]] = []
        self._finalization_retry_deadlines: dict[int, float] = {}
        self._host_telemetry: dict[str, HostTelemetryState] = {}
        self._telemetry_hosts_by_batch: dict[int, set[str]] = defaultdict(set)

    def reset(self) -> None:
        with self._lock:
            self._batches.clear()
            self._cases.clear()
            self._case_ids_by_batch.clear()
            self._case_ids_by_task.clear()
            self._case_ids_by_run.clear()
            self._case_ids_by_testcase.clear()
            self._testcase_hash_by_id.clear()
            self._latest_case_id_by_task_test.clear()
            self._batch_id_by_task.clear()
            self._batch_ids_by_run.clear()
            self._batch_id_by_logical_run.clear()
            self._closed_logical_run_keys.clear()
            self._closed_verification_ids.clear()
            self._script_hash_refcounts.clear()
            self._script_hashes_by_id.clear()
            self._leased_case_ids_by_host.clear()
            self._empty_batch_ids.clear()
            self._batch_counts.clear()
            self._run_counts.clear()
            self._task_case_counts.clear()
            self._scope_sequence_by_verification.clear()
            self._batch_specs.clear()
            self._compile_submissions_by_key.clear()
            self._materialized_compile_submissions_by_key.clear()
            self._compile_key_by_submit_id.clear()
            self._batch_ids_by_compile_key.clear()
            self._batch_ids_by_verification.clear()
            self._verification_by_domjudge_job_id.clear()
            self._ready_batches.clear()
            self._cache_heaps_by_batch.clear()
            self._runnable_heaps_by_batch.clear()
            self._affinity_batches_by_host.clear()
            self._stolen_batch_by_host.clear()
            self._batch_ids_in_heap.clear()
            self._ready_prerequisite_ids.clear()
            self._finalization_retry_heap.clear()
            self._finalization_retry_deadlines.clear()
            self._host_telemetry.clear()
            self._telemetry_hosts_by_batch.clear()

    @staticmethod
    def _priority(batch: ExecutionBatchRecord) -> int:
        return 0 if batch.service_class == "foreground" else 1

    @staticmethod
    def _batch_row(batch: ExecutionBatchRecord) -> ExecutionBatchRow:
        row = asdict(batch)
        row.pop("has_been_dispatched")
        return row  # type: ignore[return-value]

    @staticmethod
    def _case_row(case: CaseRecord) -> JudgehostCaseRow:
        row = asdict(case)
        row.pop("heap_generation")
        result = row.pop("result")
        row.pop("terminal_result")
        row.pop("requeue_on_abort")
        row.pop("claim_generation")
        result_fields = (
            "runresult", "runtime_sec", "cpu_sec", "wall_sec", "memory_kb",
            "output_run_ref", "output_error_ref", "output_system_ref", "output_diff_ref",
            "metadata_ref", "compare_metadata_ref", "team_message_ref", "score_text",
        )
        if result is None:
            for field in result_fields:
                row[field] = None
        else:
            for field in result_fields:
                row[field] = result[field]
        return row  # type: ignore[return-value]

    @staticmethod
    def _status_attr(status: str) -> str:
        return status.replace("-", "_")

    def _case_heap_locked(self, case: CaseRecord) -> list[tuple[int, int, int, int]] | None:
        if case.status == "cache-pending":
            return self._cache_heaps_by_batch[case.batch_id]
        if case.status == "pending":
            return self._runnable_heaps_by_batch[case.batch_id]
        return None

    def _push_case_locked(self, case: CaseRecord) -> None:
        heap = self._case_heap_locked(case)
        if heap is None:
            return
        heapq.heappush(
            heap,
            (case.scope_sequence, case.ordinal, case.id, case.heap_generation),
        )

    def _peek_case_heap_locked(
        self,
        batch_id: int,
        *,
        status: str,
    ) -> CaseRecord | None:
        heap = (
            self._cache_heaps_by_batch[batch_id]
            if status == "cache-pending"
            else self._runnable_heaps_by_batch[batch_id]
        )
        while heap:
            _scope, _ordinal, case_id, generation = heap[0]
            case = self._cases.get(case_id)
            if (
                case is not None
                and case.batch_id == batch_id
                and case.status == status
                and case.heap_generation == generation
            ):
                return case
            heapq.heappop(heap)
        return None

    def _compact_case_heap_locked(self, batch_id: int, *, status: str) -> None:
        heap = (
            self._cache_heaps_by_batch[batch_id]
            if status == "cache-pending"
            else self._runnable_heaps_by_batch[batch_id]
        )
        counts = self._batch_counts[batch_id]
        live = counts.cache_pending if status == "cache-pending" else counts.pending
        if len(heap) <= max(64, live * 4):
            return
        heap[:] = [
            (case.scope_sequence, case.ordinal, case.id, case.heap_generation)
            for case_id in self._case_ids_by_batch[batch_id]
            if (case := self._cases.get(case_id)) is not None and case.status == status
        ]
        heapq.heapify(heap)

    def _batch_next_case_locked(self, batch: ExecutionBatchRecord, *, hostname: str = "") -> CaseRecord | None:
        if batch.status != "open":
            return None
        cached = self._peek_case_heap_locked(batch.batch_id, status="cache-pending")
        if cached is not None:
            return cached
        pending = self._peek_case_heap_locked(batch.batch_id, status="pending")
        if pending is None:
            return None
        if batch.compile_state == "failed":
            return None
        if batch.materialization_state == "unmaterialized":
            # A proactive cache probe can expose the first miss before any host has
            # selected this Batch. Keep it schedulable so fetch-work can materialize it.
            return pending
        if batch.materialization_state != "ready":
            return None
        if batch.compile_state == "unknown" and batch.compile_owner not in {None, hostname or None}:
            return None
        if batch.compile_state == "unknown" and batch.compile_owner is not None and self._batch_counts[batch.batch_id].leased:
            return None
        return pending

    def _batch_heap_key_locked(
        self,
        batch: ExecutionBatchRecord,
    ) -> tuple[int, int, int, int] | None:
        case = self._batch_next_case_locked(batch)
        if case is None:
            return None
        return (
            self._priority(batch),
            int(batch.has_been_dispatched),
            case.scope_sequence,
            batch.batch_id,
        )

    def _refresh_prerequisite_index_locked(
        self,
        batch: ExecutionBatchRecord,
        *,
        ready: bool,
    ) -> None:
        if batch.task_kind not in self._PREREQUISITE_TASK_KINDS:
            return
        key = (batch.verification_id, batch.task_kind)
        index = self._ready_prerequisite_ids[key]
        if ready:
            index.setdefault(batch.batch_id, None)
        else:
            index.pop(batch.batch_id, None)
            if not index:
                self._ready_prerequisite_ids.pop(key, None)

    def _push_batch_ready_locked(self, batch: ExecutionBatchRecord) -> None:
        key = self._batch_heap_key_locked(batch)
        self._refresh_prerequisite_index_locked(batch, ready=key is not None)
        if key is None:
            return
        if batch.batch_id in self._batch_ids_in_heap:
            return
        self._batch_ids_in_heap.add(batch.batch_id)
        heapq.heappush(self._ready_batches, key)

    def _touch_batch_locked(self, batch: ExecutionBatchRecord) -> None:
        self._push_batch_ready_locked(batch)

    def _mutate_batch_locked(self, batch: ExecutionBatchRecord, **changes: object) -> None:
        for field, value in changes.items():
            setattr(batch, field, value)
        self._touch_batch_locked(batch)

    def _close_batch_locked(self, batch: ExecutionBatchRecord, *, updated_at: str) -> None:
        if batch.status != "open":
            return
        self._index_batch_scripts_locked(batch, -1)
        batch.status = "finalize-pending"
        batch.updated_at = updated_at
        self._touch_batch_locked(batch)

    def _index_batch_scripts_locked(self, batch: ExecutionBatchRecord, delta: int) -> None:
        for kind, script_hash in (
            ("compile", batch.compile_hash),
            ("run", batch.run_hash),
            ("compare", batch.compare_hash),
        ):
            if not script_hash:
                continue
            script_id = int(domjudge_script_id(script_hash))
            count_key = (kind, script_id, script_hash)
            index_key = (kind, script_id)
            previous = self._script_hash_refcounts[count_key]
            current = previous + delta
            if current < 0:
                raise RuntimeError("judgehost script hash index underflow")
            if current == 0:
                self._script_hash_refcounts.pop(count_key, None)
                hashes = self._script_hashes_by_id[index_key]
                hashes.discard(script_hash)
                if not hashes:
                    self._script_hashes_by_id.pop(index_key, None)
                continue
            self._script_hash_refcounts[count_key] = current
            if previous == 0:
                self._script_hashes_by_id[index_key].add(script_hash)

    @staticmethod
    def _adjust_counts(counts: StatusCounts, status: str, delta: int) -> None:
        attr = BatchScheduler._status_attr(status)
        setattr(counts, attr, getattr(counts, attr) + delta)

    def _transition_case_locked(
        self,
        case: CaseRecord,
        status: str,
        *,
        lease_owner: str | None,
        updated_at: str,
        refresh_batch: bool = True,
    ) -> None:
        old_status = case.status
        if old_status == status:
            return
        old_owner = case.lease_owner
        batch = self._batches[case.batch_id]
        self._adjust_counts(self._batch_counts[case.batch_id], old_status, -1)
        self._adjust_counts(self._batch_counts[case.batch_id], status, 1)
        self._adjust_counts(self._run_counts[case.run_id], old_status, -1)
        self._adjust_counts(self._run_counts[case.run_id], status, 1)
        task_counts = self._task_case_counts[case.task_id]
        if (
            old_status not in self._TERMINAL_CASE_STATUSES
            and status in self._TERMINAL_CASE_STATUSES
        ):
            task_counts.remaining -= 1
            if task_counts.remaining < 0:
                raise RuntimeError("judgehost task remaining case count underflow")
        if old_status in {"leased", "reporting"} and old_owner:
            leased_ids = self._leased_case_ids_by_host[old_owner]
            leased_ids.discard(case.id)
            if not leased_ids:
                self._leased_case_ids_by_host.pop(old_owner, None)
        case.status = status
        case.lease_owner = lease_owner
        case.heap_generation += 1
        case.updated_at = updated_at
        if status in {"leased", "reporting"} and lease_owner:
            self._leased_case_ids_by_host[lease_owner].add(case.id)
        self._push_case_locked(case)
        counts = self._batch_counts[case.batch_id]
        if (
            batch.status == "open"
            and (
                batch.verification_id in self._closed_verification_ids
                or (batch.verification_id, batch.logical_run_id) in self._closed_logical_run_keys
            )
            and counts.total > 0
            and counts.terminal == counts.total
            and batch.materialization_state != "materializing"
        ):
            self._close_batch_locked(batch, updated_at=updated_at)
        if refresh_batch:
            self._refresh_batches_locked({batch.batch_id})

    def _refresh_batches_locked(
        self,
        batch_ids: set[int],
        *,
        updated_at: str | None = None,
    ) -> None:
        for batch_id in batch_ids:
            batch = self._batches.get(batch_id)
            if batch is None:
                continue
            if updated_at is not None:
                batch.updated_at = updated_at
            self._compact_case_heap_locked(batch_id, status="cache-pending")
            self._compact_case_heap_locked(batch_id, status="pending")
            self._touch_batch_locked(batch)

    def _insert_case_locked(
        self,
        *,
        batch_id: int,
        task_id: str,
        run_id: str,
        test_name: str,
        ordinal: int,
        scope_sequence: int,
        source: dict[str, object],
        status: str,
        created_at: str,
    ) -> CaseRecord:
        case_id = next(self._entity_ids)
        testcase_id = source["testcase_id"]
        testcase_hash = str(source["testcase_hash"])
        if testcase_id is not None:
            numeric_testcase_id = int(testcase_id)
            existing_hash = self._testcase_hash_by_id.get(numeric_testcase_id)
            if existing_hash not in {None, testcase_hash}:
                raise RuntimeError("DOMjudge testcase id collision")
            self._testcase_hash_by_id[numeric_testcase_id] = testcase_hash
        case = CaseRecord(
            id=case_id,
            batch_id=batch_id,
            task_id=task_id,
            run_id=run_id,
            test_name=test_name,
            ordinal=ordinal,
            scope_sequence=scope_sequence,
            heap_generation=1,
            testcase_id=testcase_id,  # type: ignore[arg-type]
            testcase_hash=testcase_hash,
            testcase_input_hash=str(source["testcase_input_hash"]),
            testcase_answer_hash=str(source["testcase_answer_hash"]),
            input_ref=str(source["input_ref"]),
            answer_ref=str(source["answer_ref"]),
            status=status,
            lease_owner=None,
            result=None,
            debug_text="",
            verification_published=False,
            cancel_requested=False,
            terminal_result=None,
            requeue_on_abort=False,
            claim_generation=0,
            created_at=created_at,
            updated_at=created_at,
        )
        self._cases[case_id] = case
        self._case_ids_by_batch[batch_id].add(case_id)
        self._case_ids_by_task[task_id].add(case_id)
        self._case_ids_by_run[run_id].add(case_id)
        if case.testcase_id is not None:
            self._case_ids_by_testcase[case.testcase_id].add(case_id)
        self._latest_case_id_by_task_test[(task_id, test_name)] = case_id
        self._batch_id_by_task[task_id] = batch_id
        self._batch_ids_by_run[run_id].add(batch_id)
        self._adjust_counts(self._batch_counts[batch_id], case.status, 1)
        self._adjust_counts(self._run_counts[run_id], case.status, 1)
        task_counts = self._task_case_counts[task_id]
        task_counts.total += 1
        if case.status not in self._TERMINAL_CASE_STATUSES:
            task_counts.remaining += 1
        self._empty_batch_ids.discard(batch_id)
        self._push_case_locked(case)
        return case

    def _sorted_cases_locked(self, case_ids: Iterable[int]) -> list[CaseRecord]:
        return sorted(
            (self._cases[case_id] for case_id in case_ids if case_id in self._cases),
            key=lambda row: (row.ordinal, row.id),
        )

    def _peek_ready_batch_locked(self) -> ExecutionBatchRecord | None:
        while self._ready_batches:
            entry = self._ready_batches[0]
            batch_id = entry[-1]
            batch = self._batches.get(batch_id)
            key = None if batch is None else self._batch_heap_key_locked(batch)
            if key is not None and batch is not None and key == entry:
                return batch
            heapq.heappop(self._ready_batches)
            self._batch_ids_in_heap.discard(batch_id)
            if batch is not None and key is not None:
                self._push_batch_ready_locked(batch)
        return None

    def _take_ready_batch_locked(self) -> ExecutionBatchRecord | None:
        batch = self._peek_ready_batch_locked()
        if batch is None:
            return None
        heapq.heappop(self._ready_batches)
        self._batch_ids_in_heap.discard(batch.batch_id)
        batch.has_been_dispatched = True
        self._push_batch_ready_locked(batch)
        return batch

    def _prune_host_context_locked(self, hostname: str) -> deque[int]:
        queue_ids = self._affinity_batches_by_host[hostname]
        retained = deque(
            batch_id
            for batch_id in queue_ids
            if (batch := self._batches.get(batch_id)) is not None and batch.status == "open"
        )
        if retained:
            self._affinity_batches_by_host[hostname] = retained
        else:
            self._affinity_batches_by_host.pop(hostname, None)
        stolen_id = self._stolen_batch_by_host.get(hostname)
        if stolen_id is not None:
            stolen = self._batches.get(stolen_id)
            if stolen is None or stolen.status != "open":
                self._stolen_batch_by_host.pop(hostname, None)
        return retained

    def _remember_host_batch_locked(
        self,
        hostname: str,
        batch: ExecutionBatchRecord,
        *,
        foreground: bool = False,
    ) -> None:
        queue_ids = self._prune_host_context_locked(hostname)
        if batch.batch_id in queue_ids:
            return
        if foreground:
            self._stolen_batch_by_host[hostname] = batch.batch_id
            return
        if len(queue_ids) < self._AFFINITY_QUEUE_SIZE:
            queue_ids.append(batch.batch_id)
            self._affinity_batches_by_host[hostname] = queue_ids
            if self._stolen_batch_by_host.get(hostname) == batch.batch_id:
                self._stolen_batch_by_host.pop(hostname, None)
            return
        self._stolen_batch_by_host[hostname] = batch.batch_id

    def _ready_host_batch_locked(
        self,
        hostname: str,
        batch_ids: Iterable[int],
    ) -> ExecutionBatchRecord | None:
        for batch_id in batch_ids:
            batch = self._batches.get(batch_id)
            if batch is not None and self._batch_next_case_locked(batch, hostname=hostname) is not None:
                return batch
        return None

    def _ready_prerequisite_locked(
        self,
        *,
        verification_id: str,
        task_kind: str,
        hostname: str,
    ) -> ExecutionBatchRecord | None:
        index = self._ready_prerequisite_ids.get((verification_id, task_kind))
        if not index:
            return None
        for batch_id in tuple(index):
            batch = self._batches.get(batch_id)
            if batch is None or batch.status != "open":
                index.pop(batch_id, None)
                continue
            if self._batch_next_case_locked(batch, hostname=hostname) is None:
                continue
            batch.has_been_dispatched = True
            return batch
        if not index:
            self._ready_prerequisite_ids.pop((verification_id, task_kind), None)
        return None

    def _unblocking_batch_locked(
        self,
        hostname: str,
        affinity_ids: Iterable[int],
    ) -> ExecutionBatchRecord | None:
        for batch_id in affinity_ids:
            blocked = self._batches.get(batch_id)
            if blocked is None or blocked.status != "open":
                continue
            task_kinds = (
                ("generate-input",)
                if blocked.task_kind == "main-correct"
                else self._PREREQUISITE_TASK_KINDS
                if blocked.task_kind not in {"generate-input", "compile-only"}
                else ()
            )
            for task_kind in task_kinds:
                batch = self._ready_prerequisite_locked(
                    verification_id=blocked.verification_id,
                    task_kind=task_kind,
                    hostname=hostname,
                )
                if batch is not None:
                    return batch
        return None

    def host_context_batches(self, hostname: str) -> list[ExecutionBatchRow]:
        with self._lock:
            affinity_ids = self._prune_host_context_locked(hostname)
            batch_ids = list(affinity_ids)
            stolen_id = self._stolen_batch_by_host.get(hostname)
            if stolen_id is not None and stolen_id not in batch_ids:
                batch_ids.append(stolen_id)
            return [
                self._batch_row(self._batches[batch_id])
                for batch_id in batch_ids
                if batch_id in self._batches
            ]

    def select_ready_batch(self, hostname: str) -> ExecutionBatchRow | None:
        with self._lock:
            leased_batch_ids = {
                self._cases[case_id].batch_id
                for case_id in self._leased_case_ids_by_host.get(hostname, ())
                if case_id in self._cases
            }
            if leased_batch_ids:
                if len(leased_batch_ids) != 1:
                    return None
                leased_batch = self._batches.get(next(iter(leased_batch_ids)))
                if (
                    leased_batch is None
                    or self._batch_next_case_locked(leased_batch, hostname=hostname) is None
                ):
                    return None
                return self._batch_row(leased_batch)

            global_batch = self._peek_ready_batch_locked()
            if global_batch is not None and global_batch.service_class == "foreground":
                selected = self._take_ready_batch_locked()
                if selected is not None:
                    self._remember_host_batch_locked(hostname, selected, foreground=True)
                    return self._batch_row(selected)

            affinity_ids = self._prune_host_context_locked(hostname)
            affinity_batch = self._ready_host_batch_locked(hostname, affinity_ids)
            if affinity_batch is not None:
                return self._batch_row(affinity_batch)

            stolen_id = self._stolen_batch_by_host.get(hostname)
            stolen = None if stolen_id is None else self._batches.get(stolen_id)
            if stolen is not None and self._batch_next_case_locked(stolen, hostname=hostname) is not None:
                return self._batch_row(stolen)
            if stolen_id is not None:
                self._stolen_batch_by_host.pop(hostname, None)

            unblocking = self._unblocking_batch_locked(hostname, affinity_ids)
            if unblocking is not None:
                self._remember_host_batch_locked(hostname, unblocking)
                return self._batch_row(unblocking)

            selected = self._take_ready_batch_locked()
            if selected is None:
                return None
            self._remember_host_batch_locked(hostname, selected)
            return self._batch_row(selected)

    def host_leased_case_count(self, hostname: str) -> int:
        with self._lock:
            return len(self._leased_case_ids_by_host.get(hostname, ()))

    def batch_case_count(self, batch_id: int, *, status: str) -> int:
        """Return a case-state count without scanning a batch's cases."""
        with self._lock:
            counts = self._batch_counts.get(int(batch_id))
            return 0 if counts is None else int(getattr(counts, self._status_attr(status)))

    def active_lease_counts(self) -> dict[str, int]:
        with self._lock:
            return {
                hostname: len(case_ids)
                for hostname, case_ids in self._leased_case_ids_by_host.items()
                if case_ids
            }

    def _drop_host_telemetry_batch_locked(self, hostname: str) -> None:
        telemetry = self._host_telemetry.get(hostname)
        if telemetry is None or telemetry.active_batch is None:
            return
        batch_id = telemetry.active_batch.batch_id
        telemetry.active_batch = None
        hostnames = self._telemetry_hosts_by_batch.get(batch_id)
        if hostnames is None:
            return
        hostnames.discard(hostname)
        if not hostnames:
            self._telemetry_hosts_by_batch.pop(batch_id, None)

    def _discard_batch_telemetry_locked(self, batch_id: int) -> None:
        for hostname in self._telemetry_hosts_by_batch.pop(int(batch_id), set()):
            telemetry = self._host_telemetry.get(hostname)
            if (
                telemetry is not None
                and telemetry.active_batch is not None
                and telemetry.active_batch.batch_id == int(batch_id)
            ):
                telemetry.active_batch = None

    def record_batch_leased(
        self,
        hostname: str,
        batch_id: int,
        case_ids: list[int],
        *,
        leased_monotonic: float,
    ) -> None:
        pending_case_ids = {int(case_id) for case_id in case_ids}
        if not pending_case_ids:
            return
        with self._lock:
            self._drop_host_telemetry_batch_locked(hostname)
            telemetry = self._host_telemetry.setdefault(hostname, HostTelemetryState())
            telemetry.active_batch = HostLeaseTelemetry(
                batch_id=int(batch_id),
                pending_case_ids=pending_case_ids,
                case_count=len(pending_case_ids),
                leased_monotonic=float(leased_monotonic),
                latest_reported_monotonic=float(leased_monotonic),
            )
            self._telemetry_hosts_by_batch[int(batch_id)].add(hostname)

    def _record_case_telemetry_locked(
        self,
        case: CaseRecord,
        report: CaseReportTelemetry,
    ) -> None:
        telemetry = self._host_telemetry.setdefault(report.hostname, HostTelemetryState())
        telemetry.judged_case_count += 1
        if (
            telemetry.last_judging_monotonic is None
            or report.reported_monotonic >= telemetry.last_judging_monotonic
        ):
            telemetry.last_judging_at = report.reported_at
            telemetry.last_judging_monotonic = report.reported_monotonic
            telemetry.last_judging = {
                "verification_id": report.verification_id,
                "problem_slug": report.problem_slug,
                "task_kind": report.task_kind,
                "source_label": report.source_label,
                "test_name": report.test_name,
            }
        lease = telemetry.active_batch
        if (
            lease is None
            or lease.batch_id != case.batch_id
            or case.id not in lease.pending_case_ids
        ):
            return
        lease.pending_case_ids.remove(case.id)
        lease.latest_reported_monotonic = max(
            lease.latest_reported_monotonic,
            report.reported_monotonic,
        )
        if lease.pending_case_ids:
            return
        elapsed = max(0.0, lease.latest_reported_monotonic - lease.leased_monotonic)
        telemetry.recent_batch_avg_sec.append(elapsed / lease.case_count)
        telemetry.recent_avg_per_case_sec = float(
            statistics.median(telemetry.recent_batch_avg_sec)
        )
        self._drop_host_telemetry_batch_locked(report.hostname)

    def host_telemetry_snapshot(self) -> dict[str, HostTelemetryRow]:
        with self._lock:
            return {
                hostname: {
                    "judged_case_count": telemetry.judged_case_count,
                    "last_judging_at": telemetry.last_judging_at,
                    "last_judging": (
                        None if telemetry.last_judging is None else dict(telemetry.last_judging)
                    ),
                    "recent_avg_per_case_sec": telemetry.recent_avg_per_case_sec,
                }
                for hostname, telemetry in self._host_telemetry.items()
            }

    def cases_for_batch(self, batch_id: int, *, status: str | None = None) -> list[JudgehostCaseRow]:
        with self._lock:
            rows = self._sorted_cases_locked(self._case_ids_by_batch.get(int(batch_id), []))
            if status:
                rows = [row for row in rows if row.status == status]
            return [self._case_row(row) for row in rows]

    def cases_for_task(self, task_id: str) -> list[JudgehostCaseRow]:
        with self._lock:
            return [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_task.get(task_id, []))
            ]

    def task_cases_terminal(self, task_id: str) -> bool:
        with self._lock:
            counts = self._task_case_counts.get(task_id)
            return bool(counts is not None and counts.total > 0 and counts.remaining == 0)

    def task_has_cache_pending_cases(self, task_id: str) -> bool:
        with self._lock:
            return any(
                self._cases[case_id].status == "cache-pending"
                for case_id in self._case_ids_by_task.get(task_id, ())
                if case_id in self._cases
            )

    def task_case_results(self, task_id: str) -> list[tuple[JudgehostCaseRow, CaseResult | None]]:
        with self._lock:
            return [
                (self._case_row(case), case.result)
                for case in self._sorted_cases_locked(self._case_ids_by_task.get(task_id, ()))
            ]

    def fetch_batch(self, batch_id: int) -> ExecutionBatchRow | None:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            return None if batch is None else self._batch_row(batch)

    def scope_sequence(self, verification_id: str) -> int:
        token = verification_id or "__direct__"
        with self._lock:
            sequence = self._scope_sequence_by_verification.get(token)
            if sequence is None:
                sequence = next(self._sequence)
                self._scope_sequence_by_verification[token] = sequence
            return sequence

    def forget_scope(self, verification_id: str) -> None:
        token = verification_id or "__direct__"
        with self._lock:
            self._scope_sequence_by_verification.pop(token, None)
            if not self._batch_ids_by_verification.get(token):
                self._closed_verification_ids.discard(token)
                self._closed_logical_run_keys = {
                    key for key in self._closed_logical_run_keys if key[0] != token
                }

    def finish_verification_execution(self, verification_id: str, *, now_text: str) -> list[int]:
        """Close one execution scope and expose its terminal Batches for finalization."""
        token = verification_id or "__direct__"
        with self._lock:
            self._closed_verification_ids.add(token)
            ready: list[int] = []
            for batch_id in self._batch_ids_by_verification.get(token, ()):
                batch = self._batches[batch_id]
                self._closed_logical_run_keys.add((token, batch.logical_run_id))
                counts = self._batch_counts[batch_id]
                if (
                    batch.status == "open"
                    and counts.total > 0
                    and counts.terminal == counts.total
                    and batch.materialization_state != "materializing"
                ):
                    self._close_batch_locked(batch, updated_at=now_text)
                if batch.status == "finalize-pending":
                    ready.append(batch_id)
            return ready

    def request_verification_cancel(
        self,
        verification_id: str,
        *,
        now_text: str,
    ) -> VerificationCancellation:
        """Close admission and cancel every not-yet-running Case in one operation."""
        token = verification_id or "__direct__"
        with self._lock:
            self._closed_verification_ids.add(token)
            batch_ids = tuple(sorted(self._batch_ids_by_verification.get(token, ())))
            batch_id_set = set(batch_ids)
            task_ids: set[str] = set()
            awaiting_task_ids: set[str] = set()
            cancelled_count = 0
            awaiting_receipt_count = 0

            for hostname, queue_ids in tuple(self._affinity_batches_by_host.items()):
                retained = deque(batch_id for batch_id in queue_ids if batch_id not in batch_id_set)
                if retained:
                    self._affinity_batches_by_host[hostname] = retained
                else:
                    self._affinity_batches_by_host.pop(hostname, None)
            for hostname, batch_id in tuple(self._stolen_batch_by_host.items()):
                if batch_id in batch_id_set:
                    self._stolen_batch_by_host.pop(hostname, None)

            for batch_id in batch_ids:
                batch = self._batches[batch_id]
                self._closed_logical_run_keys.add((token, batch.logical_run_id))
                for case_id in tuple(self._case_ids_by_batch.get(batch_id, ())):
                    case = self._cases[case_id]
                    task_ids.add(case.task_id)
                    if case.status in self._TERMINAL_CASE_STATUSES:
                        continue
                    if case.status in {"leased", "reporting", "cache-probing"}:
                        case.cancel_requested = True
                        case.terminal_result = None
                        awaiting_task_ids.add(case.task_id)
                        awaiting_receipt_count += 1
                        continue
                    case.result = None
                    self._transition_case_locked(
                        case,
                        "cancelled",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
                    cancelled_count += 1
            self._refresh_batches_locked(batch_id_set, updated_at=now_text)
            return VerificationCancellation(
                batch_ids=batch_ids,
                task_ids=tuple(sorted(task_ids)),
                awaiting_task_ids=tuple(sorted(awaiting_task_ids)),
                cancelled_case_count=cancelled_count,
                awaiting_receipt_count=awaiting_receipt_count,
            )

    def finish_logical_runs(
        self,
        verification_id: str,
        logical_run_ids: Iterable[str],
        *,
        now_text: str,
    ) -> list[int]:
        """Stop logical-run admission and expose terminal Batches for finalization."""
        token = verification_id or "__direct__"
        with self._lock:
            ready: list[int] = []
            for logical_run_id in dict.fromkeys(logical_run_ids):
                key = (token, logical_run_id)
                self._closed_logical_run_keys.add(key)
                batch_id = self._batch_id_by_logical_run.get(key)
                if batch_id is None:
                    continue
                batch = self._batches[batch_id]
                counts = self._batch_counts[batch_id]
                if (
                    batch.status == "open"
                    and counts.total > 0
                    and counts.terminal == counts.total
                    and batch.materialization_state != "materializing"
                ):
                    self._close_batch_locked(batch, updated_at=now_text)
                if batch.status == "finalize-pending":
                    ready.append(batch_id)
            return ready

    def batch_spec(self, batch_id: int) -> ExecutionBatchSpec | None:
        with self._lock:
            return self._batch_specs.get(int(batch_id))

    def compile_submission_for_batch(self, batch_id: int) -> CompileSubmission | None:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None:
                return None
            return self._compile_submissions_by_key.get(batch.compile_key)

    def publish_materialized_compile_submission(
        self,
        compile_key: str,
        submission: CompileSubmission,
    ) -> None:
        with self._lock:
            original = self._compile_submissions_by_key.get(compile_key)
            if original is None:
                raise RuntimeError("compile submission disappeared during materialization")
            if (
                self._compile_submission_identity(submission)
                != self._compile_submission_identity(original)
                or submission.source_file.blob_ref is None
                or any(payload.blob_ref is None for _, payload in submission.extra_source_items)
            ):
                raise RuntimeError("materialized compile submission identity changed")
            self._compile_submissions_by_key[compile_key] = submission
            self._materialized_compile_submissions_by_key[compile_key] = submission

    def claim_materialization(self, batch_id: int, *, now_text: str) -> bool:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None or batch.status != "open" or batch.materialization_state != "unmaterialized":
                return False
            self._mutate_batch_locked(batch, materialization_state="materializing", updated_at=now_text)
            return True

    def finish_materialization(
        self,
        batch_id: int,
        *,
        success: bool,
        error_text: str,
        now_text: str,
    ) -> bool:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None or batch.status != "open" or batch.materialization_state != "materializing":
                return False
            self._mutate_batch_locked(
                batch,
                materialization_state="ready" if success else "failed",
                failure_runresult=batch.failure_runresult if success else "internal-error",
                failure_text=batch.failure_text if success else error_text,
                updated_at=now_text,
            )
            counts = self._batch_counts[batch.batch_id]
            if (
                (
                    batch.verification_id in self._closed_verification_ids
                    or (batch.verification_id, batch.logical_run_id) in self._closed_logical_run_keys
                )
                and counts.total > 0
                and counts.terminal == counts.total
            ):
                self._close_batch_locked(batch, updated_at=now_text)
            return True

    def batch_for_task(self, task_id: str) -> ExecutionBatchRow | None:
        with self._lock:
            batch_id = self._batch_id_by_task.get(task_id)
            return None if batch_id is None else self._batch_row(self._batches[batch_id])

    def batch_for_run(self, run_id: str) -> ExecutionBatchRow | None:
        with self._lock:
            batch_ids = self._batch_ids_by_run.get(run_id)
            if not batch_ids:
                return None
            return self._batch_row(self._batches[max(batch_ids)])

    def fetch_case(self, case_id: int) -> JudgehostCaseRow | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            return None if case is None else self._case_row(case)

    def cases_for_run(self, run_id: str) -> list[JudgehostCaseRow]:
        with self._lock:
            return [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_run.get(run_id, []))
            ]

    def source_submission(
        self,
        submit_id: str,
        *,
        contest_id: str | None = None,
    ) -> CompileSubmission | None:
        if not submit_id.isdigit():
            return None
        numeric_submit_id = int(submit_id)
        if not 0 <= numeric_submit_id < (1 << 63):
            return None
        with self._lock:
            compile_key = self._compile_key_by_submit_id.get(numeric_submit_id)
            if compile_key is None:
                return None
            batch_ids = self._batch_ids_by_compile_key.get(compile_key, set())
            if contest_id is not None and not any(
                self._batches[batch_id].contest_id == contest_id
                for batch_id in batch_ids
                if batch_id in self._batches
            ):
                return None
            return self._materialized_compile_submissions_by_key.get(
                compile_key,
                self._compile_submissions_by_key.get(compile_key),
            )

    def testcase_refs(
        self,
        testcase_id: int,
        *,
        hostname: str,
    ) -> tuple[dict[str, object] | None, str]:
        safe_host = str(hostname or "").strip()
        token = int(testcase_id)
        with self._lock:
            if not safe_host:
                return None, "missing-host"
            candidates = [
                self._cases[case_id]
                for case_id in self._case_ids_by_testcase.get(token, ())
                if self._cases[case_id].status in {"leased", "reporting"}
                and self._cases[case_id].lease_owner == safe_host
            ]
            if not candidates:
                return None, "missing"
            case = max(candidates, key=lambda row: (row.updated_at, row.id))
            return (
                {"input_ref": case.input_ref, "answer_ref": case.answer_ref},
                "leased-host-testcase-id",
            )

    def active_script_hashes(self, kind: str, script_id: int) -> set[str]:
        if kind not in {"compile", "run", "compare"}:
            return set()
        with self._lock:
            return set(self._script_hashes_by_id.get((kind, int(script_id)), ()))

    def leased_script_hash_for_host(
        self,
        hostname: str,
        *,
        kind: str,
        script_id: int,
    ) -> tuple[int, str] | None:
        field_by_kind = {
            "compile": "compile_hash",
            "run": "run_hash",
            "compare": "compare_hash",
        }
        field = field_by_kind.get(kind)
        if field is None:
            return None
        with self._lock:
            matching: dict[int, str] = {}
            for case_id in self._leased_case_ids_by_host.get(hostname, ()):
                case = self._cases.get(case_id)
                if case is None or case.status not in {"leased", "reporting"}:
                    continue
                batch = self._batches.get(case.batch_id)
                if batch is None:
                    continue
                script_hash = str(getattr(batch, field))
                if domjudge_script_id(script_hash) == int(script_id):
                    matching[batch.batch_id] = script_hash
            if len(matching) != 1:
                return None
            return next(iter(matching.items()))

    def create_batch_with_cases(
        self,
        *,
        task_id: str,
        run_id: str,
        logical_run_id: str,
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
        self._validate_case_rows(case_rows, default_task_id=task_id, default_run_id=run_id)
        if service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        if not logical_run_id:
            raise RuntimeError("missing judgehost logical run id")
        if task_kind not in {"compile-only", "generate-input", "main-correct", "solution-run"}:
            raise RuntimeError("invalid judgehost task kind")
        for script_hash in (compile_hash, run_hash, compare_hash):
            if script_hash:
                domjudge_script_id(script_hash)
        if compile_submission.compile_key != compile_key:
            raise RuntimeError("compile submission key mismatch")
        if compile_submission.submit_id != domjudge_submit_id(compile_key):
            raise RuntimeError("compile submission id mismatch")
        if len(execution_signature) != 64 or any(
            char not in "0123456789abcdef" for char in execution_signature
        ):
            raise RuntimeError("invalid judgehost execution signature")
        with self._lock:
            self._validate_testcase_identities_locked(case_rows)
            if verification_id in self._closed_verification_ids:
                raise RuntimeError("judgehost verification execution is closed")
            logical_run_key = (verification_id, logical_run_id)
            if logical_run_key in self._closed_logical_run_keys:
                raise RuntimeError("judgehost logical run execution is closed")
            existing_batch_id = self._batch_id_by_logical_run.get(logical_run_key)
            if existing_batch_id is not None:
                existing_batch = self._batches[existing_batch_id]
                identity = (
                    existing_batch.execution_signature,
                    existing_batch.task_kind,
                    existing_batch.compile_key,
                    existing_batch.contest_id,
                    existing_batch.mode,
                    existing_batch.source_name,
                    existing_batch.compile_hash,
                    existing_batch.run_hash,
                    existing_batch.compare_hash,
                    existing_batch.source_hash,
                    existing_batch.compile_config_json,
                    existing_batch.run_config_json,
                    existing_batch.compare_config_json,
                    existing_batch.expected_behavior,
                    existing_batch.verification_source,
                    existing_batch.bypass_case_result_cache,
                    existing_batch.service_class,
                    self._batch_specs[existing_batch_id],
                    self._compile_submission_identity(
                        self._compile_submissions_by_key[existing_batch.compile_key]
                    ),
                )
                requested_identity = (
                    execution_signature,
                    task_kind,
                    compile_key,
                    contest_id,
                    mode,
                    source_name,
                    compile_hash,
                    run_hash,
                    compare_hash,
                    source_hash,
                    compile_config_json,
                    run_config_json,
                    compare_config_json,
                    expected_behavior,
                    verification_source,
                    int(bypass_case_result_cache),
                    service_class,
                    batch_spec,
                    self._compile_submission_identity(compile_submission),
                )
                if identity != requested_identity:
                    raise RuntimeError("judgehost logical run execution identity changed")
                if existing_batch.status != "open":
                    raise RuntimeError("judgehost logical run execution is closed")
                self._append_cases_to_batch_locked(
                    batch=existing_batch,
                    case_rows=case_rows,
                    now_text=created_at,
                )
                return existing_batch_id
            protocol_job_id = domjudge_job_id(verification_id)
            existing_verification_id = self._verification_by_domjudge_job_id.get(protocol_job_id)
            if existing_verification_id not in {None, verification_id}:
                raise RuntimeError("DOMjudge batch id collision")
            existing_compile_key = self._compile_key_by_submit_id.get(compile_submission.submit_id)
            if existing_compile_key not in {None, compile_key}:
                raise RuntimeError("DOMjudge submit id collision")
            existing_submission = self._compile_submissions_by_key.get(compile_key)
            if (
                existing_submission is not None
                and self._compile_submission_identity(existing_submission)
                != self._compile_submission_identity(compile_submission)
            ):
                raise RuntimeError("compile submission identity changed")
            case_task_ids = {str(row.get("task_id") or task_id) for row in case_rows}
            if any(case_task_id in self._batch_id_by_task for case_task_id in case_task_ids):
                raise RuntimeError("judgehost task cases already belong to another batch")
            if task_id in self._batch_id_by_task or run_id in self._batch_ids_by_run:
                raise RuntimeError("judgehost batch identity already exists")
            batch_id = next(self._entity_ids)
            batch = ExecutionBatchRecord(
                batch_id=batch_id,
                logical_run_id=logical_run_id,
                execution_signature=str(execution_signature),
                task_kind=task_kind,
                verification_id=verification_id,
                domjudge_job_id=protocol_job_id,
                compile_key=compile_key,
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
                bypass_case_result_cache=int(bypass_case_result_cache),
                compile_success=None,
                compile_state="unknown",
                compile_owner=None,
                materialization_state="unmaterialized",
                service_class=service_class,
                has_been_dispatched=False,
                compile_output_b64=None,
                compile_metadata_b64=None,
                debug_text="",
                failure_runresult="",
                failure_text="",
                status="open",
                created_at=created_at,
                updated_at=created_at,
                completed_at=None,
            )
            self._batches[batch_id] = batch
            self._batch_specs[batch_id] = batch_spec
            if compile_key not in self._materialized_compile_submissions_by_key:
                self._compile_submissions_by_key[compile_key] = compile_submission
            self._compile_key_by_submit_id[compile_submission.submit_id] = compile_key
            self._batch_ids_by_compile_key[compile_key].add(batch_id)
            self._batch_ids_by_verification[verification_id].add(batch_id)
            self._verification_by_domjudge_job_id[protocol_job_id] = verification_id
            self._batch_counts[batch_id] = StatusCounts()
            self._batch_id_by_logical_run[logical_run_key] = batch_id
            self._index_batch_scripts_locked(batch, 1)
            self._empty_batch_ids.add(batch_id)
            for case_row in case_rows:
                self._insert_case_locked(
                    batch_id=batch_id,
                    task_id=str(case_row.get("task_id") or task_id),
                    run_id=str(case_row.get("run_id") or run_id),
                    test_name=str(case_row["test_name"]),
                    ordinal=int(case_row["ordinal"]),
                    scope_sequence=int(case_row.get("scope_sequence") or 1),
                    source=case_row,
                    status=str(case_row.get("status") or "staged"),
                    created_at=created_at,
                )
            return batch_id

    @staticmethod
    def _validate_case_rows(
        case_rows: list[dict[str, object]],
        *,
        default_task_id: str = "",
        default_run_id: str = "",
    ) -> None:
        required_fields = {
            "test_name",
            "ordinal",
            "testcase_id",
            "testcase_hash",
            "testcase_input_hash",
            "testcase_answer_hash",
            "input_ref",
            "answer_ref",
        }
        identities: set[tuple[str, str]] = set()
        for row in case_rows:
            missing = required_fields.difference(row)
            if missing:
                raise RuntimeError(f"invalid judgehost case spec: missing {sorted(missing)[0]}")
            task_id = str(row.get("task_id") or default_task_id)
            run_id = str(row.get("run_id") or default_run_id)
            test_name = str(row["test_name"])
            if not task_id or not run_id or not test_name:
                raise RuntimeError("invalid judgehost case identity")
            identity = (task_id, test_name)
            if identity in identities:
                raise RuntimeError("duplicate judgehost task case")
            identities.add(identity)
            int(row["ordinal"])
            int(row.get("scope_sequence") or 1)
            testcase_id = row["testcase_id"]
            if testcase_id is not None:
                int(testcase_id)
            status = str(row.get("status") or "staged")
            if status not in {"staged", "cache-pending", "pending"}:
                raise RuntimeError("invalid judgehost case status")

    @staticmethod
    def _case_identity(row: dict[str, object]) -> tuple[object, ...]:
        return (
            row.get("run_id"),
            row.get("test_name"),
            int(row["ordinal"]),
            int(row.get("scope_sequence") or 1),
            row.get("testcase_id"),
            row.get("testcase_hash"),
            row.get("testcase_input_hash"),
            row.get("testcase_answer_hash"),
            row.get("input_ref"),
            row.get("answer_ref"),
        )

    def _append_cases_to_batch_locked(
        self,
        *,
        batch: ExecutionBatchRecord,
        case_rows: list[dict[str, object]],
        now_text: str,
    ) -> None:
        if batch.status != "open" or batch.verification_id in self._closed_verification_ids:
            raise RuntimeError("judgehost verification execution is closed")
        self._validate_case_rows(case_rows)
        self._validate_testcase_identities_locked(case_rows)
        rows_by_task: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in case_rows:
            case_task_id = str(row.get("task_id") or "")
            if case_task_id:
                rows_by_task[case_task_id].append(row)
        for case_task_id, requested_rows in rows_by_task.items():
            existing_batch_id = self._batch_id_by_task.get(case_task_id)
            if existing_batch_id is not None and existing_batch_id != batch.batch_id:
                raise RuntimeError("judgehost task cases already belong to another batch")
            if existing_batch_id is None:
                continue
            existing_rows = [
                self._case_row(self._cases[case_id])
                for case_id in self._case_ids_by_task[case_task_id]
            ]
            requested = sorted((self._case_identity(row) for row in requested_rows), key=repr)
            existing = sorted((self._case_identity(row) for row in existing_rows), key=repr)
            if requested != existing:
                raise RuntimeError("judgehost task case set is immutable")
        for row in case_rows:
            case_task_id = str(row.get("task_id") or "")
            case_run_id = str(row.get("run_id") or "")
            test_name = str(row.get("test_name") or "")
            pair = (case_task_id, test_name)
            if not case_task_id or not case_run_id or not test_name:
                continue
            existing_case_id = self._latest_case_id_by_task_test.get(pair)
            if existing_case_id is not None:
                if self._cases[existing_case_id].batch_id != batch.batch_id:
                    raise RuntimeError("judgehost task cases already belong to another batch")
                continue
            self._insert_case_locked(
                batch_id=batch.batch_id,
                task_id=case_task_id,
                run_id=case_run_id,
                test_name=test_name,
                ordinal=int(row["ordinal"]),
                scope_sequence=int(row.get("scope_sequence") or 1),
                source=row,
                status="staged",
                created_at=now_text,
            )

    def _validate_testcase_identities_locked(self, case_rows: list[dict[str, object]]) -> None:
        requested: dict[int, str] = {}
        for row in case_rows:
            testcase_id = row.get("testcase_id")
            if testcase_id is None:
                continue
            numeric_id = int(testcase_id)
            testcase_hash = str(row.get("testcase_hash") or "")
            known_hash = requested.get(numeric_id, self._testcase_hash_by_id.get(numeric_id))
            if known_hash not in {None, testcase_hash}:
                raise RuntimeError("DOMjudge testcase id collision")
            requested[numeric_id] = testcase_hash

    def activate_task_cases(self, task_id: str, *, now_text: str) -> bool:
        with self._lock:
            case_ids = tuple(self._case_ids_by_task.get(task_id, ()))
            if not case_ids:
                return False
            affected_batch_ids: set[int] = set()
            for case_id in case_ids:
                case = self._cases[case_id]
                if case.status == "staged":
                    self._transition_case_locked(
                        case,
                        "cache-pending",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
                    affected_batch_ids.add(case.batch_id)
            self._refresh_batches_locked(affected_batch_ids)
            return True

    def cancel_staged_task_cases(self, task_id: str, *, now_text: str) -> None:
        with self._lock:
            affected_batch_ids: set[int] = set()
            for case_id in tuple(self._case_ids_by_task.get(task_id, ())):
                case = self._cases[case_id]
                if case.status == "staged":
                    self._transition_case_locked(
                        case,
                        "cancelled",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_batch=False,
                    )
                    affected_batch_ids.add(case.batch_id)
            self._refresh_batches_locked(affected_batch_ids)

    def lease_cases(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
        now_text: str,
    ) -> list[JudgehostCaseRow]:
        cap = max(1, min(256, int(limit)))
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if (
                batch is None
                or batch.status != "open"
                or batch.materialization_state != "ready"
                or batch.compile_state == "failed"
                or (batch.compile_state == "unknown" and batch.compile_owner not in {None, hostname})
                or (batch.compile_state == "unknown" and batch.compile_owner is not None and self._batch_counts[batch.batch_id].leased)
            ):
                return []
            first = self._peek_case_heap_locked(batch.batch_id, status="pending")
            if first is None:
                return []
            if batch.compile_state == "unknown":
                cap = 1
                batch.compile_owner = hostname
                batch.updated_at = now_text
            leased: list[JudgehostCaseRow] = []
            while len(leased) < cap:
                case = self._peek_case_heap_locked(batch.batch_id, status="pending")
                if case is None:
                    break
                heapq.heappop(self._runnable_heaps_by_batch[batch.batch_id])
                self._transition_case_locked(
                    case,
                    "leased",
                    lease_owner=hostname,
                    updated_at=now_text,
                    refresh_batch=False,
                )
                leased.append(self._case_row(case))
            self._refresh_batches_locked({batch.batch_id})
            return leased
