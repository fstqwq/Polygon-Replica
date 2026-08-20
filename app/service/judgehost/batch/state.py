"""Canonical process-local state for Judgehost batches and cases.

Ready indexes are derived data and never own batch state. Every record, lease,
receipt, retry, count, and index transition happens under one re-entrant lock.
File, SQLite, network, cache, and callback work is forbidden while that lock is
held.
"""

import heapq
import itertools
import statistics
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable

from app.service.judgehost.domjudge.identity import script_id
from app.service.judgehost.batch.model import (
    CaseSpec,
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
    HostTelemetryState,
)
from app.service.judgehost.batch.policy import (
    ProductionSchedulingPolicy,
    SchedulingPolicy,
)
from app.service.judgehost.batch.ready_index import ReadyBatchIndex, ReadyBatchKey
from app.service.judgehost.batch.snapshot import batch_snapshot, case_snapshot


class BatchState:
    """Canonical process-local state for execution batches and cases.

    Every mutable record and derived index belongs to this object and is guarded
    by its single re-entrant lock. Capability services compose this state. It is
    private to ``JudgehostBatchRuntime`` and is never exposed by the
    public Judgehost facade.
    """

    _MAX_ENTITY_ID = (1 << 63) - 1
    _AFFINITY_QUEUE_SIZE = 4
    _ACTIVE_BATCH_STATUSES = frozenset({"open"})
    _TERMINAL_CASE_STATUSES = frozenset({"reported", "cancelled"})
    _READY_CASE_STATUSES = frozenset({"staged", "cache-pending", "pending"})
    _PREREQUISITE_TASK_KINDS = ("main-correct", "generate-input")

    @staticmethod
    def _compile_submission_identity(
        submission: CompileSubmission,
    ) -> tuple[object, ...]:
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

    @staticmethod
    def _compile_submission_is_materialized(submission: CompileSubmission) -> bool:
        return submission.source_file.blob_ref is not None and all(
            payload.blob_ref is not None for _, payload in submission.extra_source_items
        )

    def __init__(
        self,
        lock: threading.RLock | None = None,
        *,
        id_base: int | None = None,
        scheduling_policy: SchedulingPolicy | None = None,
    ):
        self._lock = threading.RLock() if lock is None else lock
        self._ready_condition = threading.Condition(self._lock)
        self._ready_generation = 0
        self._id_base = max(1, int(id_base if id_base is not None else time.time_ns()))
        self._next_entity_id_value = self._id_base + 1
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
        self._batch_id_by_verification_program: dict[tuple[str, str], int] = {}
        self._closed_program_keys: set[tuple[str, str]] = set()
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
        self._materialization_generation_by_batch: dict[int, int] = {}
        self._finalization_generation_by_batch: dict[int, int] = {}
        self._active_finalization_generation_by_batch: dict[int, int] = {}
        # Materialization replaces the descriptor in this canonical map. Keeping
        # raw and materialized copies separately made warm program appends
        # observe a compile key without its submission.
        self._compile_submissions_by_key: dict[str, CompileSubmission] = {}
        self._compile_key_by_submit_id: dict[int, str] = {}
        self._batch_ids_by_compile_key: dict[str, set[int]] = defaultdict(set)
        self._batch_ids_by_verification: dict[str, set[int]] = defaultdict(set)
        self._verification_by_job_id: dict[int, str] = {}
        self._ready_batches = ReadyBatchIndex()
        self._cache_heaps_by_batch: dict[int, list[tuple[int, int, int, int]]] = (
            defaultdict(list)
        )
        self._runnable_heaps_by_batch: dict[int, list[tuple[int, int, int, int]]] = (
            defaultdict(list)
        )
        self._affinity_batches_by_host: dict[str, deque[int]] = defaultdict(deque)
        self._ready_prerequisites: dict[tuple[str, str], ReadyBatchIndex] = {}
        self._finalization_retry_heap: list[tuple[float, int]] = []
        self._finalization_retry_deadlines: dict[int, float] = {}
        self._host_telemetry: dict[str, HostTelemetryState] = {}
        self._telemetry_hosts_by_batch: dict[int, set[str]] = defaultdict(set)
        self._next_callback_receipt_id = itertools.count(1)
        self._case_id_by_callback_receipt: dict[int, int] = {}
        self._scheduling_policy = (
            ProductionSchedulingPolicy()
            if scheduling_policy is None
            else scheduling_policy
        )
        self._stolen_batch_by_host: dict[str, int] = {}
        self._compile_owner_by_batch: dict[int, str] = {}

    def _next_entity_ids_locked(self, count: int) -> tuple[int, ...]:
        if count < 0:
            raise ValueError("judgehost entity id reservation must be non-negative")
        if count == 0:
            return ()
        first_id = self._next_entity_id_value
        last_id = first_id + count - 1
        if not 0 < first_id <= last_id <= self._MAX_ENTITY_ID:
            raise OverflowError("judgehost entity id exceeds the signed 64-bit range")
        self._next_entity_id_value = last_id + 1
        return tuple(range(first_id, last_id + 1))

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
            self._batch_id_by_verification_program.clear()
            self._closed_program_keys.clear()
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
            self._materialization_generation_by_batch.clear()
            self._finalization_generation_by_batch.clear()
            self._active_finalization_generation_by_batch.clear()
            self._compile_submissions_by_key.clear()
            self._compile_key_by_submit_id.clear()
            self._batch_ids_by_compile_key.clear()
            self._batch_ids_by_verification.clear()
            self._verification_by_job_id.clear()
            self._ready_batches.clear()
            self._cache_heaps_by_batch.clear()
            self._runnable_heaps_by_batch.clear()
            self._affinity_batches_by_host.clear()
            self._ready_prerequisites.clear()
            self._finalization_retry_heap.clear()
            self._finalization_retry_deadlines.clear()
            self._host_telemetry.clear()
            self._telemetry_hosts_by_batch.clear()
            self._next_callback_receipt_id = itertools.count(1)
            self._case_id_by_callback_receipt.clear()
            self._stolen_batch_by_host.clear()
            self._compile_owner_by_batch.clear()
            self._ready_generation += 1
            self._ready_condition.notify_all()

    def activity_counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "cache_probes": sum(
                    counts.cache_probing for counts in self._batch_counts.values()
                ),
                "materializations": sum(
                    batch.materialization_state == "materializing"
                    for batch in self._batches.values()
                ),
                "leases": sum(counts.leased for counts in self._batch_counts.values()),
                "callbacks": len(self._case_id_by_callback_receipt),
                "reporting": sum(
                    counts.reporting for counts in self._batch_counts.values()
                ),
                "finalizations": len(self._active_finalization_generation_by_batch),
            }

    def pending_finalization_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                sorted(
                    batch.batch_id
                    for batch in self._batches.values()
                    if batch.status == "finalize-pending"
                )
            )

    @staticmethod
    def _priority(batch: ExecutionBatchRecord) -> int:
        return 0 if batch.service_class == "foreground" else 1

    @staticmethod
    def _status_attr(status: str) -> str:
        return status.replace("-", "_")

    def _case_heap_locked(
        self, case: CaseRecord
    ) -> list[tuple[int, int, int, int]] | None:
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

    def _batch_next_case_locked(
        self, batch: ExecutionBatchRecord, *, hostname: str = ""
    ) -> CaseRecord | None:
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
        if self._scheduling_policy.single_compile_owner and hostname:
            compile_owner = self._compile_owner_by_batch.get(batch.batch_id)
            if batch.compile_state == "unknown" and compile_owner not in {
                None,
                hostname,
            }:
                return None
        if batch.materialization_state == "unmaterialized":
            # A proactive cache probe can expose the first miss before any host has
            # selected this Batch. Keep it schedulable so fetch-work can materialize it.
            return pending
        if batch.materialization_state != "ready":
            return None
        return pending

    def _batch_heap_key_locked(
        self,
        batch: ExecutionBatchRecord,
    ) -> ReadyBatchKey | None:
        case = self._batch_next_case_locked(batch)
        if case is None:
            return None
        return (
            self._priority(batch),
            batch.dispatch_count,
            case.scope_sequence,
            -self._batch_counts[batch.batch_id].pending,
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
        index = self._ready_prerequisites.get(key)
        if ready:
            if index is None:
                index = ReadyBatchIndex()
                self._ready_prerequisites[key] = index
            index.update(batch.batch_id, self._batch_heap_key_locked(batch))
            return
        if index is None:
            return
        index.remove(batch.batch_id)
        if len(index) == 0:
            self._ready_prerequisites.pop(key, None)

    def _push_batch_ready_locked(self, batch: ExecutionBatchRecord) -> None:
        key = self._batch_heap_key_locked(batch)
        self._refresh_prerequisite_index_locked(batch, ready=key is not None)
        if self._ready_batches.update(batch.batch_id, key):
            self._ready_generation += 1
            self._ready_condition.notify_all()

    def _touch_batch_locked(self, batch: ExecutionBatchRecord) -> None:
        self._push_batch_ready_locked(batch)

    def _close_batch_locked(
        self, batch: ExecutionBatchRecord, *, updated_at: str
    ) -> None:
        if batch.status != "open":
            return
        self._index_batch_scripts_locked(batch, -1)
        batch.status = "finalize-pending"
        batch.updated_at = updated_at
        self._touch_batch_locked(batch)

    def _index_batch_scripts_locked(
        self, batch: ExecutionBatchRecord, delta: int
    ) -> None:
        for kind, script_hash in (
            ("compile", batch.compile_hash),
            ("run", batch.run_hash),
            ("compare", batch.compare_hash),
        ):
            if not script_hash:
                continue
            script_numeric_id = int(script_id(script_hash))
            count_key = (kind, script_numeric_id, script_hash)
            index_key = (kind, script_numeric_id)
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
        attr = BatchState._status_attr(status)
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
        if lease_owner:
            case.last_callback_hostname = lease_owner
        case.heap_generation += 1
        case.updated_at = updated_at
        if status not in {"leased", "reporting"}:
            case.lease_deadline_monotonic = None
            case.lease_budget_sec = 0.0
            case.lease_grace_sec = 0.0
        if status in {"leased", "reporting"} and lease_owner:
            self._leased_case_ids_by_host[lease_owner].add(case.id)
        self._push_case_locked(case)
        counts = self._batch_counts[case.batch_id]
        if (
            batch.status == "open"
            and (
                batch.verification_id in self._closed_verification_ids
                or (
                    batch.verification_id,
                    batch.verification_program_id,
                )
                in self._closed_program_keys
            )
            and counts.total > 0
            and counts.terminal == counts.total
            and batch.materialization_state != "materializing"
        ):
            self._close_batch_locked(batch, updated_at=updated_at)
        if refresh_batch and (
            old_status in self._READY_CASE_STATUSES
            or status in self._READY_CASE_STATUSES
        ):
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
        case_id: int,
        batch_id: int,
        source: CaseSpec,
        created_at: str,
    ) -> CaseRecord:
        testcase_id = source.testcase_id
        testcase_hash = source.testcase_hash
        if testcase_id is not None:
            existing_hash = self._testcase_hash_by_id.get(testcase_id)
            if existing_hash not in {None, testcase_hash}:
                raise RuntimeError("DOMjudge testcase id collision")
            self._testcase_hash_by_id[testcase_id] = testcase_hash
        case = CaseRecord(
            id=case_id,
            batch_id=batch_id,
            task_id=source.task_id,
            verification_task_id=source.verification_task_id,
            run_id=source.run_id,
            test_name=source.test_name,
            ordinal=source.ordinal,
            scope_sequence=source.scope_sequence,
            heap_generation=1,
            testcase_id=testcase_id,
            testcase_hash=testcase_hash,
            testcase_input_hash=source.testcase_input_hash,
            testcase_answer_hash=source.testcase_answer_hash,
            input_ref=source.input_ref,
            answer_ref=source.answer_ref,
            status=source.status,
            lease_owner=None,
            result=None,
            debug_text="",
            completion_acknowledged=False,
            last_callback_hostname="",
            callback_receipt_count=0,
            pending_diagnostics=[],
            cancel_requested=False,
            terminal_result=None,
            requeue_on_abort=False,
            claim_generation=0,
            created_at=created_at,
            updated_at=created_at,
        )
        self._cases[case_id] = case
        self._case_ids_by_batch[batch_id].add(case_id)
        self._case_ids_by_task[source.task_id].add(case_id)
        self._case_ids_by_run[source.run_id].add(case_id)
        if case.testcase_id is not None:
            self._case_ids_by_testcase[case.testcase_id].add(case_id)
        self._latest_case_id_by_task_test[(source.task_id, source.test_name)] = case_id
        self._batch_id_by_task[source.task_id] = batch_id
        self._batch_ids_by_run[source.run_id].add(batch_id)
        self._adjust_counts(self._batch_counts[batch_id], case.status, 1)
        self._adjust_counts(self._run_counts[source.run_id], case.status, 1)
        task_counts = self._task_case_counts[source.task_id]
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

    def _remove_case_from_host_telemetry_locked(self, case: CaseRecord) -> None:
        """Remove an expired case without counting it as judged."""
        owner = case.lease_owner
        if not owner:
            return
        telemetry = self._host_telemetry.get(owner)
        lease = None if telemetry is None else telemetry.active_batch
        if lease is None or lease.batch_id != case.batch_id:
            return
        lease.pending_case_ids.discard(case.id)
        if not lease.pending_case_ids:
            self._drop_host_telemetry_batch_locked(owner)

    def _discard_batch_telemetry_locked(self, batch_id: int) -> None:
        for hostname in self._telemetry_hosts_by_batch.pop(int(batch_id), set()):
            telemetry = self._host_telemetry.get(hostname)
            if (
                telemetry is not None
                and telemetry.active_batch is not None
                and telemetry.active_batch.batch_id == int(batch_id)
            ):
                telemetry.active_batch = None

    def _record_case_telemetry_locked(
        self,
        case: CaseRecord,
        report: CaseReportTelemetry,
    ) -> None:
        telemetry = self._host_telemetry.setdefault(
            report.hostname, HostTelemetryState()
        )
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
            # A successful report is the only progress signal that may move
            # the deadline of the remaining cases from this prefetch. Heartbeat
            # and fetch traffic intentionally do not extend a case lease.
            elapsed_budget = 0.0
            for remaining in self._sorted_cases_locked(lease.pending_case_ids):
                elapsed_budget += max(0.0, remaining.lease_budget_sec)
                remaining.lease_deadline_monotonic = (
                    report.reported_monotonic
                    + max(0.0, remaining.lease_grace_sec)
                    + elapsed_budget
                )
            return
        elapsed = max(0.0, lease.latest_reported_monotonic - lease.leased_monotonic)
        telemetry.recent_batch_avg_sec.append(elapsed / lease.case_count)
        telemetry.recent_avg_per_case_sec = float(
            statistics.median(telemetry.recent_batch_avg_sec)
        )
        self._drop_host_telemetry_batch_locked(report.hostname)

    def cases_for_batch(
        self, batch_id: int, *, status: str | None = None
    ) -> list[JudgehostCaseRow]:
        with self._lock:
            rows = self._sorted_cases_locked(
                self._case_ids_by_batch.get(int(batch_id), [])
            )
            if status:
                rows = [row for row in rows if row.status == status]
            return [case_snapshot(row) for row in rows]

    def cases_for_task(self, task_id: str) -> list[JudgehostCaseRow]:
        with self._lock:
            return [
                case_snapshot(row)
                for row in self._sorted_cases_locked(
                    self._case_ids_by_task.get(task_id, [])
                )
            ]

    def task_cases_terminal(self, task_id: str) -> bool:
        with self._lock:
            counts = self._task_case_counts.get(task_id)
            return bool(
                counts is not None and counts.total > 0 and counts.remaining == 0
            )

    def task_has_cache_pending_cases(self, task_id: str) -> bool:
        with self._lock:
            return any(
                self._cases[case_id].status == "cache-pending"
                for case_id in self._case_ids_by_task.get(task_id, ())
                if case_id in self._cases
            )

    def task_case_results(
        self, task_id: str
    ) -> list[tuple[JudgehostCaseRow, CaseResult | None]]:
        with self._lock:
            return [
                (case_snapshot(case), case.result)
                for case in self._sorted_cases_locked(
                    self._case_ids_by_task.get(task_id, ())
                )
            ]

    def fetch_batch(self, batch_id: int) -> ExecutionBatchRow | None:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            return None if batch is None else batch_snapshot(batch)

    def batch_for_task(self, task_id: str) -> ExecutionBatchRow | None:
        with self._lock:
            batch_id = self._batch_id_by_task.get(task_id)
            return (
                None if batch_id is None else batch_snapshot(self._batches[batch_id])
            )

    def batch_for_run(self, run_id: str) -> ExecutionBatchRow | None:
        with self._lock:
            batch_ids = self._batch_ids_by_run.get(run_id)
            if not batch_ids:
                return None
            return batch_snapshot(self._batches[max(batch_ids)])

    def fetch_case(self, case_id: int) -> JudgehostCaseRow | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            return None if case is None else case_snapshot(case)

    def cases_for_run(self, run_id: str) -> list[JudgehostCaseRow]:
        with self._lock:
            return [
                case_snapshot(row)
                for row in self._sorted_cases_locked(
                    self._case_ids_by_run.get(run_id, [])
                )
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
            return self._compile_submissions_by_key.get(compile_key)

    def testcase_refs(
        self,
        testcase_id: int,
    ) -> tuple[dict[str, object] | None, str]:
        token = int(testcase_id)
        with self._lock:
            candidates = [
                self._cases[case_id]
                for case_id in self._case_ids_by_testcase.get(token, ())
                if self._cases[case_id].status in {"leased", "reporting"}
            ]
            if not candidates:
                return None, "missing"
            case = max(candidates, key=lambda row: (row.updated_at, row.id))
            return (
                {"input_ref": case.input_ref, "answer_ref": case.answer_ref},
                "leased-testcase-id",
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
        requested_id: int,
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
                if script_id(script_hash) == int(requested_id):
                    matching[batch.batch_id] = script_hash
            if len(matching) != 1:
                return None
            return next(iter(matching.items()))

    def _remove_cases_locked(self, case_ids: set[int]) -> None:
        cases = [self._cases[case_id] for case_id in case_ids if case_id in self._cases]
        if not cases:
            return
        if any(case.callback_receipt_count > 0 for case in cases):
            raise RuntimeError(
                "cannot remove a judgehost case with an active callback receipt"
            )
        if any(case.pending_diagnostics for case in cases):
            raise RuntimeError(
                "cannot remove a judgehost case with pending diagnostics"
            )
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
                self._batch_id_by_task[task_id] = self._cases[
                    next(iter(retained))
                ].batch_id
            else:
                self._case_ids_by_task.pop(task_id, None)
                self._batch_id_by_task.pop(task_id, None)
                self._task_case_counts.pop(task_id, None)

        for run_id in affected_run_ids:
            retained = self._case_ids_by_run[run_id].difference(case_ids)
            if retained:
                self._case_ids_by_run[run_id] = retained
                self._batch_ids_by_run[run_id] = {
                    self._cases[case_id].batch_id for case_id in retained
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
        self._compile_owner_by_batch.pop(batch_id, None)
        if batch.status == "open":
            self._index_batch_scripts_locked(batch, -1)
        self._ready_batches.remove(batch_id)
        self._finalization_retry_deadlines.pop(batch_id, None)
        self._refresh_prerequisite_index_locked(batch, ready=False)
        program_key = (
            batch.verification_id,
            batch.verification_program_id,
        )
        self._batch_id_by_verification_program.pop(program_key, None)
        self._closed_program_keys.discard(program_key)
        self._case_ids_by_batch.pop(batch_id, None)
        self._batch_counts.pop(batch_id, None)
        self._batch_specs.pop(batch_id, None)
        self._materialization_generation_by_batch.pop(batch_id, None)
        self._finalization_generation_by_batch.pop(batch_id, None)
        self._active_finalization_generation_by_batch.pop(batch_id, None)
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
            self._verification_by_job_id.pop(batch.job_id, None)
