from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from collections.abc import Iterator

from app.service.platform.rwlock import WriterPriorityRWLock

from .domjudge.client import domjudge_script_id
from .job_scheduler_models import (
    JudgehostCaseRow,
    JudgehostJobAppendResult,
    JudgehostJobRow,
    CaseRecord,
    JobSpec,
    JobRecord,
    StatusCounts,
)
from .job_scheduler_results import JobSchedulerResultMixin


class _ExclusiveLockAdapter:
    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock

    @contextmanager
    def read_lock(self) -> Iterator[None]:
        with self._lock:
            yield

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        with self._lock:
            yield


class JobScheduler(JobSchedulerResultMixin):
    """Indexed process-local state for DOMjudge compatibility jobs and cases."""

    _ACTIVE_JOB_STATUSES = frozenset({"open"})
    _TERMINAL_CASE_STATUSES = frozenset({"reported", "cancelled"})

    def __init__(self, lock: threading.RLock | None = None, *, id_base: int | None = None):
        self._lock = WriterPriorityRWLock() if lock is None else _ExclusiveLockAdapter(lock)
        self._id_base = max(1, int(id_base if id_base is not None else time.time() * 1000))
        self._entity_ids = itertools.count(self._id_base + 1)
        self._jobs: dict[int, JobRecord] = {}
        self._cases: dict[int, CaseRecord] = {}
        self._case_ids_by_job: dict[int, list[int]] = defaultdict(list)
        self._case_ids_by_task: dict[str, list[int]] = defaultdict(list)
        self._case_ids_by_run: dict[str, list[int]] = defaultdict(list)
        self._case_ids_by_testcase: dict[int, set[int]] = defaultdict(set)
        self._latest_case_id_by_task_test: dict[tuple[str, str], int] = {}
        self._job_id_by_task: dict[str, int] = {}
        self._job_ids_by_run: dict[str, set[int]] = defaultdict(set)
        self._job_ids_by_group: dict[str, set[int]] = defaultdict(set)
        self._appendable_job_id_by_group: dict[str, int] = {}
        self._script_hash_refcounts: dict[tuple[str, int, str], int] = defaultdict(int)
        self._script_hashes_by_id: dict[tuple[str, int], set[str]] = defaultdict(set)
        self._leased_case_ids_by_host: dict[str, set[int]] = defaultdict(set)
        self._empty_job_ids: set[int] = set()
        self._job_counts: dict[int, StatusCounts] = {}
        self._run_counts: dict[str, StatusCounts] = defaultdict(StatusCounts)
        self._sequence = itertools.count()
        self._scope_sequence_by_verification: dict[str, int] = {}
        self._job_specs: dict[int, JobSpec] = {}
        self._ready_jobs: list[tuple[int, int, int, int, int, int]] = []
        self._cache_heaps_by_job: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        self._runnable_heaps_by_job: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        self._active_job_by_host: dict[str, int] = {}
        self._ready_job_ids: set[int] = set()
        self._group_activity_guard = threading.Lock()
        self._group_activity_locks: dict[str, tuple[threading.RLock, int]] = {}

    @contextmanager
    def group_activity(self, group_key: str) -> Iterator[None]:
        token = group_key or "__ungrouped__"
        with self._group_activity_guard:
            lock, users = self._group_activity_locks.get(token, (threading.RLock(), 0))
            self._group_activity_locks[token] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._group_activity_guard:
                current_lock, current_users = self._group_activity_locks[token]
                if current_users == 1:
                    self._group_activity_locks.pop(token)
                else:
                    self._group_activity_locks[token] = (current_lock, current_users - 1)

    def reset(self) -> None:
        with self._lock.write_lock():
            self._jobs.clear()
            self._cases.clear()
            self._case_ids_by_job.clear()
            self._case_ids_by_task.clear()
            self._case_ids_by_run.clear()
            self._case_ids_by_testcase.clear()
            self._latest_case_id_by_task_test.clear()
            self._job_id_by_task.clear()
            self._job_ids_by_run.clear()
            self._job_ids_by_group.clear()
            self._appendable_job_id_by_group.clear()
            self._script_hash_refcounts.clear()
            self._script_hashes_by_id.clear()
            self._leased_case_ids_by_host.clear()
            self._empty_job_ids.clear()
            self._job_counts.clear()
            self._run_counts.clear()
            self._scope_sequence_by_verification.clear()
            self._job_specs.clear()
            self._ready_jobs.clear()
            self._cache_heaps_by_job.clear()
            self._runnable_heaps_by_job.clear()
            self._active_job_by_host.clear()
            self._ready_job_ids.clear()

    @staticmethod
    def _priority(job: JobRecord) -> int:
        return 0 if job.service_class == "foreground" else 1

    @staticmethod
    def _job_row(job: JobRecord) -> JudgehostJobRow:
        row = asdict(job)
        row.pop("admission_sequence")
        row.pop("generation")
        return row  # type: ignore[return-value]

    @staticmethod
    def _case_row(case: CaseRecord) -> JudgehostCaseRow:
        row = asdict(case)
        row.pop("heap_generation")
        return row  # type: ignore[return-value]

    @staticmethod
    def _status_attr(status: str) -> str:
        return status.replace("-", "_")

    def _case_heap_locked(self, case: CaseRecord) -> list[tuple[int, int, int, int]] | None:
        if case.status == "cache-pending":
            return self._cache_heaps_by_job[case.job_id]
        if case.status == "pending":
            return self._runnable_heaps_by_job[case.job_id]
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
        job_id: int,
        *,
        status: str,
    ) -> CaseRecord | None:
        heap = (
            self._cache_heaps_by_job[job_id]
            if status == "cache-pending"
            else self._runnable_heaps_by_job[job_id]
        )
        while heap:
            _scope, _ordinal, case_id, generation = heap[0]
            case = self._cases.get(case_id)
            if (
                case is not None
                and case.job_id == job_id
                and case.status == status
                and case.heap_generation == generation
            ):
                return case
            heapq.heappop(heap)
        return None

    def _compact_case_heap_locked(self, job_id: int, *, status: str) -> None:
        heap = (
            self._cache_heaps_by_job[job_id]
            if status == "cache-pending"
            else self._runnable_heaps_by_job[job_id]
        )
        counts = self._job_counts[job_id]
        live = counts.cache_pending if status == "cache-pending" else counts.pending
        if len(heap) <= max(64, live * 4):
            return
        heap[:] = [
            (case.scope_sequence, case.ordinal, case.id, case.heap_generation)
            for case_id in self._case_ids_by_job[job_id]
            if (case := self._cases.get(case_id)) is not None and case.status == status
        ]
        heapq.heapify(heap)

    def _job_next_case_locked(self, job: JobRecord, *, hostname: str = "") -> CaseRecord | None:
        if job.status != "open":
            return None
        cached = self._peek_case_heap_locked(job.job_id, status="cache-pending")
        if cached is not None:
            return cached
        if job.materialization_state != "ready":
            return None
        if job.compile_state == "failed":
            return None
        if job.compile_state == "unknown" and job.compile_owner not in {None, hostname or None}:
            return None
        if job.compile_state == "unknown" and job.compile_owner is not None and self._job_counts[job.job_id].leased:
            return None
        return self._peek_case_heap_locked(job.job_id, status="pending")

    def _job_heap_key_locked(self, job: JobRecord) -> tuple[int, int, int, int, int, int] | None:
        case = self._job_next_case_locked(job)
        if case is None:
            return None
        return (
            self._priority(job),
            case.scope_sequence,
            case.ordinal,
            job.admission_sequence,
            job.job_id,
            job.generation,
        )

    def _push_job_ready_locked(self, job: JobRecord) -> None:
        key = self._job_heap_key_locked(job)
        if key is None:
            self._ready_job_ids.discard(job.job_id)
            return
        self._ready_job_ids.add(job.job_id)
        heapq.heappush(self._ready_jobs, key)

    def _touch_job_locked(self, job: JobRecord) -> None:
        job.generation += 1
        self._push_job_ready_locked(job)
        if len(self._ready_jobs) > max(1024, len(self._ready_job_ids) * 4):
            self._ready_jobs = [
                key
                for ready_job_id in self._ready_job_ids
                if (ready_job := self._jobs.get(ready_job_id)) is not None
                if (key := self._job_heap_key_locked(ready_job)) is not None
            ]
            heapq.heapify(self._ready_jobs)

    def _mutate_job_locked(self, job: JobRecord, **changes: object) -> None:
        for field, value in changes.items():
            setattr(job, field, value)
        self._touch_job_locked(job)

    def _index_job_scripts_locked(self, job: JobRecord, delta: int) -> None:
        for kind, script_hash in (
            ("compile", job.compile_hash),
            ("run", job.run_hash),
            ("compare", job.compare_hash),
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
        attr = JobScheduler._status_attr(status)
        setattr(counts, attr, getattr(counts, attr) + delta)

    def _transition_case_locked(
        self,
        case: CaseRecord,
        status: str,
        *,
        lease_owner: str | None,
        updated_at: str,
        refresh_job: bool = True,
    ) -> None:
        old_status = case.status
        old_owner = case.lease_owner
        job = self._jobs[case.job_id]
        self._adjust_counts(self._job_counts[case.job_id], old_status, -1)
        self._adjust_counts(self._job_counts[case.job_id], status, 1)
        self._adjust_counts(self._run_counts[case.run_id], old_status, -1)
        self._adjust_counts(self._run_counts[case.run_id], status, 1)
        if old_status == "leased" and old_owner:
            leased_ids = self._leased_case_ids_by_host[old_owner]
            leased_ids.discard(case.id)
            if not leased_ids:
                self._leased_case_ids_by_host.pop(old_owner, None)
        case.status = status
        case.lease_owner = lease_owner
        case.heap_generation += 1
        case.updated_at = updated_at
        if status == "leased" and lease_owner:
            self._leased_case_ids_by_host[lease_owner].add(case.id)
            self._active_job_by_host[lease_owner] = case.job_id
        self._push_case_locked(case)
        if refresh_job:
            self._refresh_jobs_locked({job.job_id})

    def _refresh_jobs_locked(
        self,
        job_ids: set[int],
        *,
        updated_at: str | None = None,
    ) -> None:
        for job_id in job_ids:
            job = self._jobs.get(job_id)
            if job is None:
                continue
            if updated_at is not None:
                job.updated_at = updated_at
            self._compact_case_heap_locked(job_id, status="cache-pending")
            self._compact_case_heap_locked(job_id, status="pending")
            self._touch_job_locked(job)

    def _insert_case_locked(
        self,
        *,
        job_id: int,
        task_id: str,
        run_id: str,
        test_name: str,
        ordinal: int,
        scope_sequence: int,
        source: dict[str, object],
        status: str,
        created_at: str,
        compiler_error: bool = False,
    ) -> CaseRecord:
        case_id = next(self._entity_ids)
        case = CaseRecord(
            id=case_id,
            job_id=job_id,
            task_id=task_id,
            run_id=run_id,
            test_name=test_name,
            ordinal=ordinal,
            scope_sequence=scope_sequence,
            heap_generation=1,
            testcase_id=source["testcase_id"],  # type: ignore[arg-type]
            testcase_hash=str(source["testcase_hash"]),
            testcase_input_hash=str(source["testcase_input_hash"]),
            testcase_answer_hash=str(source["testcase_answer_hash"]),
            input_ref=str(source["input_ref"]),
            answer_ref=str(source["answer_ref"]),
            status="reported" if compiler_error else status,
            lease_owner=None,
            runresult="compiler-error" if compiler_error else None,
            runtime_sec=0.0 if compiler_error else None,
            cpu_sec=0.0 if compiler_error else None,
            wall_sec=0.0 if compiler_error else None,
            memory_kb=0 if compiler_error else None,
            output_run_rel="" if compiler_error else None,
            output_error_rel="" if compiler_error else None,
            output_system_rel="" if compiler_error else None,
            output_diff_rel="" if compiler_error else None,
            metadata_rel="" if compiler_error else None,
            compare_metadata_rel="" if compiler_error else None,
            team_message_rel="" if compiler_error else None,
            score_text="" if compiler_error else None,
            debug_text="",
            verification_published=False,
            created_at=created_at,
            updated_at=created_at,
        )
        self._cases[case_id] = case
        self._case_ids_by_job[job_id].append(case_id)
        self._case_ids_by_task[task_id].append(case_id)
        self._case_ids_by_run[run_id].append(case_id)
        if case.testcase_id is not None:
            self._case_ids_by_testcase[case.testcase_id].add(case_id)
        self._latest_case_id_by_task_test[(task_id, test_name)] = case_id
        self._job_id_by_task[task_id] = job_id
        self._job_ids_by_run[run_id].add(job_id)
        self._adjust_counts(self._job_counts[job_id], case.status, 1)
        self._adjust_counts(self._run_counts[run_id], case.status, 1)
        self._empty_job_ids.discard(job_id)
        self._push_case_locked(case)
        return case

    def _sorted_cases_locked(self, case_ids: list[int]) -> list[CaseRecord]:
        return sorted(
            (self._cases[case_id] for case_id in case_ids if case_id in self._cases),
            key=lambda row: (row.ordinal, row.id),
        )

    def active_job_for_host(self, hostname: str) -> JudgehostJobRow | None:
        with self._lock.read_lock():
            job_id = self._active_job_by_host.get(hostname)
            job = None if job_id is None else self._jobs.get(job_id)
            return None if job is None or job.status != "open" else self._job_row(job)

    def _peek_ready_job_locked(self) -> JobRecord | None:
        while self._ready_jobs:
            _service, _scope, _ordinal, _admission, job_id, generation = self._ready_jobs[0]
            job = self._jobs.get(job_id)
            key = None if job is None else self._job_heap_key_locked(job)
            if key is not None and job is not None and job.generation == generation and key == self._ready_jobs[0]:
                return job
            heapq.heappop(self._ready_jobs)
        return None

    def claim_ready_job(self, hostname: str) -> JudgehostJobRow | None:
        with self._lock.write_lock():
            active_id = self._active_job_by_host.get(hostname)
            active = None if active_id is None else self._jobs.get(active_id)
            global_job = self._peek_ready_job_locked()
            active_case = None if active is None else self._job_next_case_locked(active, hostname=hostname)
            # Cooperative preemption happens between fetch batches. A daemon
            # may extend its active Job batch, but cannot switch Jobs while it
            # still owns Cases from that Job.
            if self._leased_case_ids_by_host.get(hostname):
                return None if active_case is None else self._job_row(active)
            if active is not None and active_case is not None:
                foreground_waiting = (
                    active.service_class == "background"
                    and global_job is not None
                    and global_job.service_class == "foreground"
                    and not self._leased_case_ids_by_host.get(hostname)
                )
                if not foreground_waiting:
                    return self._job_row(active)
            if global_job is None:
                if active_case is None:
                    self._active_job_by_host.pop(hostname, None)
                return None
            # Claiming selects work but does not change readiness. The first
            # Case/Job transition invalidates this generation; keeping the key
            # here also makes a failed cache setup naturally retryable.
            self._active_job_by_host[hostname] = global_job.job_id
            return self._job_row(global_job)

    def host_leased_case_count(self, hostname: str) -> int:
        with self._lock.read_lock():
            return len(self._leased_case_ids_by_host.get(hostname, ()))

    def job_case_count(self, job_id: int, *, status: str) -> int:
        """Return a case-state count without scanning a job's cases."""
        with self._lock.read_lock():
            counts = self._job_counts.get(int(job_id))
            return 0 if counts is None else int(getattr(counts, self._status_attr(status)))

    def active_lease_counts(self) -> dict[str, int]:
        with self._lock.read_lock():
            return {
                hostname: len(case_ids)
                for hostname, case_ids in self._leased_case_ids_by_host.items()
                if case_ids
            }

    def cases_for_job(self, job_id: int, *, status: str | None = None) -> list[JudgehostCaseRow]:
        with self._lock.read_lock():
            rows = self._sorted_cases_locked(self._case_ids_by_job.get(int(job_id), []))
            if status:
                rows = [row for row in rows if row.status == status]
            return [self._case_row(row) for row in rows]

    def cases_for_task(self, task_id: str) -> list[JudgehostCaseRow]:
        with self._lock.read_lock():
            return [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_task.get(task_id, []))
            ]

    def fetch_job(self, job_id: int) -> JudgehostJobRow | None:
        with self._lock.read_lock():
            job = self._jobs.get(int(job_id))
            return None if job is None else self._job_row(job)

    def scope_sequence(self, verification_id: str) -> int:
        token = verification_id or "__direct__"
        with self._lock.write_lock():
            sequence = self._scope_sequence_by_verification.get(token)
            if sequence is None:
                sequence = next(self._sequence)
                self._scope_sequence_by_verification[token] = sequence
            return sequence

    def forget_scope(self, verification_id: str) -> None:
        token = verification_id or "__direct__"
        with self._lock.write_lock():
            self._scope_sequence_by_verification.pop(token, None)

    def job_spec(self, job_id: int) -> JobSpec | None:
        with self._lock.read_lock():
            return self._job_specs.get(int(job_id))

    def claim_materialization(self, job_id: int, *, now_text: str) -> bool:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if job is None or job.status != "open" or job.materialization_state != "unmaterialized":
                return False
            self._mutate_job_locked(job, materialization_state="materializing", updated_at=now_text)
            return True

    def finish_materialization(
        self,
        job_id: int,
        *,
        source_path: str,
        work_root: str,
        success: bool,
        now_text: str,
    ) -> bool:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if job is None or job.status != "open" or job.materialization_state != "materializing":
                return False
            if not success and job.group_key:
                if self._appendable_job_id_by_group.get(job.group_key) == job.job_id:
                    self._appendable_job_id_by_group.pop(job.group_key, None)
            self._mutate_job_locked(
                job,
                source_path=source_path,
                work_root=work_root,
                materialization_state="ready" if success else "failed",
                updated_at=now_text,
            )
            return True

    def job_for_task(self, task_id: str) -> JudgehostJobRow | None:
        with self._lock.read_lock():
            job_id = self._job_id_by_task.get(task_id)
            return None if job_id is None else self._job_row(self._jobs[job_id])

    def job_for_run(self, run_id: str) -> JudgehostJobRow | None:
        with self._lock.read_lock():
            job_ids = self._job_ids_by_run.get(run_id)
            if not job_ids:
                return None
            return self._job_row(self._jobs[max(job_ids)])

    def job_for_group_key(self, group_key: str) -> JudgehostJobRow | None:
        with self._lock.read_lock():
            job_id = self._appendable_job_id_by_group.get(group_key)
            if job_id is None:
                return None
            job = self._jobs[job_id]
            return self._job_row(job)

    def fetch_case(self, case_id: int) -> JudgehostCaseRow | None:
        with self._lock.read_lock():
            case = self._cases.get(int(case_id))
            return None if case is None else self._case_row(case)

    def cases_for_run(self, run_id: str) -> list[JudgehostCaseRow]:
        with self._lock.read_lock():
            return [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_run.get(run_id, []))
            ]

    def source_file_job(
        self,
        submit_id: str,
        *,
        contest_id: str | None = None,
    ) -> dict[str, object] | None:
        with self._lock.read_lock():
            job = self._jobs.get(int(submit_id)) if submit_id.isdigit() else None
            if job is None:
                return None
            if contest_id is not None and job.contest_id != contest_id:
                return None
            return {"source_name": job.source_name, "source_path": job.source_path}

    def testcase_refs(
        self,
        testcase_id: int,
        *,
        hostname: str,
    ) -> tuple[dict[str, object] | None, str]:
        safe_host = str(hostname or "").strip()
        token = int(testcase_id)
        with self._lock.read_lock():
            direct = self._cases.get(token)
            if direct is not None and direct.status == "leased":
                return (
                    {"input_ref": direct.input_ref, "answer_ref": direct.answer_ref},
                    "leased-case-id",
                )
            if not safe_host:
                return None, "missing-host"
            candidates = [
                self._cases[case_id]
                for case_id in self._case_ids_by_testcase.get(token, ())
                if self._cases[case_id].status == "leased"
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
        with self._lock.read_lock():
            return set(self._script_hashes_by_id.get((kind, int(script_id)), ()))

    def create_job_with_cases(
        self,
        *,
        task_id: str,
        run_id: str,
        group_key: str,
        contest_id: str,
        mode: str,
        source_name: str,
        source_path: str,
        work_root: str,
        compile_hash: str,
        run_hash: str,
        compare_hash: str,
        source_hash: str,
        compile_config_json: str,
        run_config_json: str,
        compare_config_json: str,
        expected_behavior: str,
        verification_source: str,
        force_recompile: int,
        service_class: str,
        job_spec: JobSpec,
        created_at: str,
        case_rows: list[dict[str, object]],
    ) -> int:
        self._validate_case_rows(case_rows, default_task_id=task_id, default_run_id=run_id)
        if service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        for script_hash in (compile_hash, run_hash, compare_hash):
            if script_hash:
                domjudge_script_id(script_hash)
        with self._lock.write_lock():
            if group_key:
                existing_job_id = self._appendable_job_id_by_group.get(group_key)
                if existing_job_id is not None:
                    existing_job = self._jobs[existing_job_id]
                    if (
                        existing_job.status == "open"
                        and existing_job.compile_state != "failed"
                        and existing_job.materialization_state != "failed"
                    ):
                        raise RuntimeError("appendable judgehost group job already exists")
            case_task_ids = {str(row.get("task_id") or task_id) for row in case_rows}
            if any(case_task_id in self._job_id_by_task for case_task_id in case_task_ids):
                raise RuntimeError("judgehost task cases already belong to another job")
            if task_id in self._job_id_by_task or run_id in self._job_ids_by_run:
                raise RuntimeError("judgehost job identity already exists")
            job_id = next(self._entity_ids)
            job = JobRecord(
                job_id=job_id,
                task_id=task_id,
                run_id=run_id,
                group_key=str(group_key),
                submit_id=str(job_id),
                contest_id=contest_id,
                mode=mode,
                source_name=source_name,
                source_path=source_path,
                work_root=work_root,
                compile_hash=compile_hash,
                run_hash=run_hash,
                compare_hash=compare_hash,
                source_hash=source_hash,
                compile_config_json=compile_config_json,
                run_config_json=run_config_json,
                compare_config_json=compare_config_json,
                expected_behavior=expected_behavior,
                verification_source=verification_source,
                force_recompile=int(force_recompile),
                compile_success=None,
                compile_state="unknown",
                compile_owner=None,
                materialization_state=(
                    "ready" if source_path and work_root else "unmaterialized"
                ),
                service_class=service_class,
                admission_sequence=next(self._sequence),
                generation=1,
                compile_output_b64=None,
                compile_metadata_b64=None,
                debug_text="",
                status="open",
                created_at=created_at,
                updated_at=created_at,
                completed_at=None,
            )
            self._jobs[job_id] = job
            self._job_specs[job_id] = job_spec
            self._job_counts[job_id] = StatusCounts()
            if job.group_key:
                self._job_ids_by_group[job.group_key].add(job_id)
                self._appendable_job_id_by_group[job.group_key] = job_id
            self._index_job_scripts_locked(job, 1)
            self._empty_job_ids.add(job_id)
            for case_row in case_rows:
                self._insert_case_locked(
                    job_id=job_id,
                    task_id=str(case_row.get("task_id") or task_id),
                    run_id=str(case_row.get("run_id") or run_id),
                    test_name=str(case_row["test_name"]),
                    ordinal=int(case_row["ordinal"]),
                    scope_sequence=int(case_row.get("scope_sequence") or 1),
                    source=case_row,
                    status=str(case_row.get("status") or "staged"),
                    created_at=created_at,
                )
            return job_id

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
            if status not in {"staged", "cache-pending", "pending", "leased", "reported", "cancelled"}:
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

    def append_cases_to_job(
        self,
        *,
        job_id: int,
        case_rows: list[dict[str, object]],
        now_text: str,
    ) -> JudgehostJobAppendResult:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if job is None:
                return {"job_id": 0, "outcome": "closed", "inserted": 0}
            if (
                job.status != "open"
                or job.compile_state == "failed"
                or job.materialization_state == "failed"
            ):
                return {"job_id": int(job_id), "outcome": "closed", "inserted": 0}
            self._validate_case_rows(case_rows)
            rows_by_task: dict[str, list[dict[str, object]]] = defaultdict(list)
            for row in case_rows:
                case_task_id = str(row.get("task_id") or "")
                if case_task_id:
                    rows_by_task[case_task_id].append(row)
            for case_task_id, requested_rows in rows_by_task.items():
                existing_job_id = self._job_id_by_task.get(case_task_id)
                if existing_job_id is not None and existing_job_id != job.job_id:
                    raise RuntimeError("judgehost task cases already belong to another job")
                if existing_job_id is None:
                    continue
                existing_rows = [
                    self._case_row(self._cases[case_id])
                    for case_id in self._case_ids_by_task[case_task_id]
                ]
                requested = sorted((self._case_identity(row) for row in requested_rows), key=repr)
                existing = sorted((self._case_identity(row) for row in existing_rows), key=repr)
                if requested != existing:
                    raise RuntimeError("judgehost task case set is immutable")
            inserted = 0
            for row in case_rows:
                case_task_id = str(row.get("task_id") or "")
                case_run_id = str(row.get("run_id") or "")
                test_name = str(row.get("test_name") or "")
                pair = (case_task_id, test_name)
                if not case_task_id or not case_run_id or not test_name:
                    continue
                existing_case_id = self._latest_case_id_by_task_test.get(pair)
                if existing_case_id is not None:
                    if self._cases[existing_case_id].job_id != job.job_id:
                        raise RuntimeError("judgehost task cases already belong to another job")
                    continue
                self._insert_case_locked(
                    job_id=job.job_id,
                    task_id=case_task_id,
                    run_id=case_run_id,
                    test_name=test_name,
                    ordinal=int(row["ordinal"]),
                    scope_sequence=int(row.get("scope_sequence") or 1),
                    source=row,
                    status="staged",
                    created_at=now_text,
                    compiler_error=job.compile_state == "failed",
                )
                inserted += 1
            return {
                "job_id": job.job_id,
                "outcome": "appended" if inserted else "duplicate",
                "inserted": inserted,
            }

    def activate_task_cases(self, task_id: str, *, now_text: str) -> bool:
        with self._lock.write_lock():
            case_ids = tuple(self._case_ids_by_task.get(task_id, ()))
            if not case_ids:
                return False
            affected_job_ids: set[int] = set()
            for case_id in case_ids:
                case = self._cases[case_id]
                if case.status == "staged":
                    self._transition_case_locked(
                        case,
                        "cache-pending",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_job=False,
                    )
                    affected_job_ids.add(case.job_id)
            self._refresh_jobs_locked(affected_job_ids)
            return True

    def cancel_staged_task_cases(self, task_id: str, *, now_text: str) -> None:
        with self._lock.write_lock():
            affected_job_ids: set[int] = set()
            for case_id in tuple(self._case_ids_by_task.get(task_id, ())):
                case = self._cases[case_id]
                if case.status == "staged":
                    self._transition_case_locked(
                        case,
                        "cancelled",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_job=False,
                    )
                    affected_job_ids.add(case.job_id)
            self._refresh_jobs_locked(affected_job_ids)

    def apply_cached_case_results(
        self,
        *,
        cached_rows: list[dict[str, object]],
        lease_owner: str,
        now_text: str,
    ) -> list[int]:
        applied_case_ids: list[int] = []
        with self._lock.write_lock():
            affected_job_ids: set[int] = set()
            for cached in cached_rows:
                case = self._cases.get(int(cached["case_id"]))
                if case is None or case.status != "cache-pending":
                    continue
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=lease_owner,
                    updated_at=now_text,
                    refresh_job=False,
                )
                for field in (
                    "runresult", "runtime_sec", "cpu_sec", "wall_sec", "memory_kb",
                    "output_run_rel", "output_error_rel", "output_system_rel", "output_diff_rel",
                    "metadata_rel", "compare_metadata_rel", "team_message_rel", "score_text",
                ):
                    setattr(case, field, cached[field])
                applied_case_ids.append(case.id)
                affected_job_ids.add(case.job_id)
            self._refresh_jobs_locked(affected_job_ids)
        return applied_case_ids

    def cache_pending_cases(self, job_id: int) -> list[JudgehostCaseRow]:
        rows: list[JudgehostCaseRow] = []
        with self._lock.write_lock():
            heap = self._cache_heaps_by_job[int(job_id)]
            live_entries: list[tuple[int, int, int, int]] = []
            while (case := self._peek_case_heap_locked(int(job_id), status="cache-pending")) is not None:
                live_entries.append(heapq.heappop(heap))
                rows.append(self._case_row(case))
            heap[:] = live_entries
            heapq.heapify(heap)
        return rows

    def mark_cache_misses(self, case_ids: list[int], *, now_text: str) -> int:
        changed = 0
        with self._lock.write_lock():
            affected_job_ids: set[int] = set()
            for case_id in dict.fromkeys(int(value) for value in case_ids):
                case = self._cases.get(case_id)
                if case is None or case.status != "cache-pending":
                    continue
                self._transition_case_locked(
                    case,
                    "pending",
                    lease_owner=None,
                    updated_at=now_text,
                    refresh_job=False,
                )
                changed += 1
                affected_job_ids.add(case.job_id)
            self._refresh_jobs_locked(affected_job_ids)
        return changed

    def lease_cases(
        self,
        job_id: int,
        *,
        hostname: str,
        limit: int,
        now_text: str,
    ) -> list[JudgehostCaseRow]:
        cap = max(1, min(256, int(limit)))
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if (
                job is None
                or job.status != "open"
                or job.materialization_state != "ready"
                or job.compile_state == "failed"
                or (job.compile_state == "unknown" and job.compile_owner not in {None, hostname})
                or (job.compile_state == "unknown" and job.compile_owner is not None and self._job_counts[job.job_id].leased)
            ):
                return []
            first = self._peek_case_heap_locked(job.job_id, status="pending")
            if first is None:
                return []
            if job.compile_state == "unknown":
                cap = 1
                job.compile_owner = hostname
                job.updated_at = now_text
            leased: list[JudgehostCaseRow] = []
            while len(leased) < cap:
                case = self._peek_case_heap_locked(job.job_id, status="pending")
                if case is None:
                    break
                heapq.heappop(self._runnable_heaps_by_job[job.job_id])
                self._transition_case_locked(
                    case,
                    "leased",
                    lease_owner=hostname,
                    updated_at=now_text,
                    refresh_job=False,
                )
                leased.append(self._case_row(case))
            self._refresh_jobs_locked({job.job_id})
            return leased
