from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from collections.abc import Iterator
from typing import TypedDict

from app.service.platform.rwlock import WriterPriorityRWLock


class JudgehostJobRow(TypedDict):
    job_id: int
    task_id: str
    run_id: str
    group_key: str
    submit_id: str
    contest_id: str
    mode: str
    source_name: str
    source_path: str
    work_root: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    source_hash: str
    compile_config_json: str
    run_config_json: str
    compare_config_json: str
    expected_behavior: str
    verification_source: str
    force_recompile: int
    lease_owner: str
    compile_success: int | None
    compile_output_b64: str
    compile_metadata_b64: str
    debug_text: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str


class JudgehostCaseRow(TypedDict):
    id: int
    job_id: int
    task_id: str
    run_id: str
    test_name: str
    ordinal: int
    testcase_id: int | None
    testcase_hash: str
    testcase_input_hash: str
    testcase_answer_hash: str
    input_ref: str
    answer_ref: str
    status: str
    lease_owner: str
    runresult: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    output_run_rel: str
    output_error_rel: str
    output_system_rel: str
    output_diff_rel: str
    metadata_rel: str
    compare_metadata_rel: str
    team_message_rel: str
    score_text: str
    debug_text: str
    created_at: str
    updated_at: str


class JudgehostJobAppendResult(TypedDict):
    job_id: int
    outcome: str
    inserted: int


class JudgehostJobFinalizationClaim(TypedDict):
    job: JudgehostJobRow
    cases: list[JudgehostCaseRow]


@dataclass
class _JobRecord:
    job_id: int
    task_id: str
    run_id: str
    group_key: str
    submit_id: str
    contest_id: str
    mode: str
    source_name: str
    source_path: str
    work_root: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    source_hash: str
    compile_config_json: str
    run_config_json: str
    compare_config_json: str
    expected_behavior: str
    verification_source: str
    force_recompile: int
    lease_owner: str | None
    compile_success: int | None
    compile_output_b64: str | None
    compile_metadata_b64: str | None
    debug_text: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass
class _CaseRecord:
    id: int
    job_id: int
    task_id: str
    run_id: str
    test_name: str
    ordinal: int
    testcase_id: int | None
    testcase_hash: str
    testcase_input_hash: str
    testcase_answer_hash: str
    input_ref: str
    answer_ref: str
    status: str
    lease_owner: str | None
    runresult: str | None
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    output_run_rel: str | None
    output_error_rel: str | None
    output_system_rel: str | None
    output_diff_rel: str | None
    metadata_rel: str | None
    compare_metadata_rel: str | None
    team_message_rel: str | None
    score_text: str | None
    debug_text: str
    created_at: str
    updated_at: str


@dataclass
class _StatusCounts:
    pending: int = 0
    leased: int = 0
    reported: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.leased + self.reported + self.cancelled

    @property
    def terminal(self) -> int:
        return self.reported + self.cancelled


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


class JudgehostStateStore:
    """Indexed process-local state for DOMjudge compatibility jobs and cases."""

    _ACTIVE_JOB_STATUSES = frozenset({"queued", "leased"})
    _TERMINAL_CASE_STATUSES = frozenset({"reported", "cancelled"})

    def __init__(self, lock: threading.RLock | None = None, *, id_base: int | None = None):
        self._lock = WriterPriorityRWLock() if lock is None else _ExclusiveLockAdapter(lock)
        self._id_base = max(1, int(id_base if id_base is not None else time.time() * 1000))
        self._next_job_id = self._id_base + 1
        self._next_case_id = self._id_base + 1
        self._jobs: dict[int, _JobRecord] = {}
        self._cases: dict[int, _CaseRecord] = {}
        self._case_ids_by_job: dict[int, list[int]] = defaultdict(list)
        self._case_ids_by_task: dict[str, list[int]] = defaultdict(list)
        self._case_ids_by_run: dict[str, list[int]] = defaultdict(list)
        self._case_ids_by_testcase: dict[int, set[int]] = defaultdict(set)
        self._latest_case_id_by_task_test: dict[tuple[str, str], int] = {}
        self._job_id_by_task: dict[str, int] = {}
        self._job_ids_by_run: dict[str, set[int]] = defaultdict(set)
        self._primary_job_id_by_task: dict[str, int] = {}
        self._primary_job_id_by_run: dict[str, int] = {}
        self._job_id_by_submit: dict[str, int] = {}
        self._job_ids_by_group: dict[str, set[int]] = defaultdict(set)
        self._job_ids_by_host: dict[str, set[int]] = defaultdict(set)
        self._leased_case_ids_by_host: dict[str, set[int]] = defaultdict(set)
        self._active_job_ids: set[int] = set()
        self._finalizing_job_ids: set[int] = set()
        self._empty_job_ids: set[int] = set()
        self._shared_pending_job_ids_by_priority: dict[int, set[int]] = defaultdict(set)
        self._job_counts: dict[int, _StatusCounts] = {}
        self._run_counts: dict[str, _StatusCounts] = defaultdict(_StatusCounts)

    def ensure_schema(self) -> None:
        """Retained as a no-op because this store is no longer SQLite-backed."""

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
            self._primary_job_id_by_task.clear()
            self._primary_job_id_by_run.clear()
            self._job_id_by_submit.clear()
            self._job_ids_by_group.clear()
            self._job_ids_by_host.clear()
            self._leased_case_ids_by_host.clear()
            self._active_job_ids.clear()
            self._finalizing_job_ids.clear()
            self._empty_job_ids.clear()
            self._shared_pending_job_ids_by_priority.clear()
            self._job_counts.clear()
            self._run_counts.clear()

    @staticmethod
    def _priority(job: _JobRecord) -> int:
        source = job.verification_source
        if source == "compile.only":
            return 0
        if "generate-input" in source:
            return 1
        if source == "main-correct":
            return 2
        if source.startswith("sanity-check"):
            return 3
        return 10

    @staticmethod
    def _job_row(job: _JobRecord) -> JudgehostJobRow:
        return asdict(job)  # type: ignore[return-value]

    @staticmethod
    def _case_row(case: _CaseRecord) -> JudgehostCaseRow:
        return asdict(case)  # type: ignore[return-value]

    def _shared_eligible_locked(self, job: _JobRecord) -> bool:
        if self._job_counts[job.job_id].pending <= 0:
            return False
        if job.status == "queued" and not (job.lease_owner or "").strip():
            return True
        return (
            job.status == "leased"
            and job.compile_success == 1
            and (job.lease_owner or "").strip() != "prequeue-cache"
        )

    def _remove_job_runtime_indexes_locked(self, job: _JobRecord) -> None:
        self._active_job_ids.discard(job.job_id)
        self._finalizing_job_ids.discard(job.job_id)
        if job.lease_owner:
            self._job_ids_by_host[job.lease_owner].discard(job.job_id)
        for ids in self._shared_pending_job_ids_by_priority.values():
            ids.discard(job.job_id)

    def _add_job_runtime_indexes_locked(self, job: _JobRecord) -> None:
        if job.status in self._ACTIVE_JOB_STATUSES:
            self._active_job_ids.add(job.job_id)
            if job.lease_owner:
                self._job_ids_by_host[job.lease_owner].add(job.job_id)
            if self._shared_eligible_locked(job):
                self._shared_pending_job_ids_by_priority[self._priority(job)].add(job.job_id)
        elif job.status == "finalizing":
            self._finalizing_job_ids.add(job.job_id)

    def _mutate_job_locked(self, job: _JobRecord, **changes: object) -> None:
        self._remove_job_runtime_indexes_locked(job)
        for field, value in changes.items():
            setattr(job, field, value)
        self._add_job_runtime_indexes_locked(job)

    @staticmethod
    def _adjust_counts(counts: _StatusCounts, status: str, delta: int) -> None:
        setattr(counts, status, getattr(counts, status) + delta)

    def _transition_case_locked(
        self,
        case: _CaseRecord,
        status: str,
        *,
        lease_owner: str | None,
        updated_at: str,
    ) -> None:
        old_status = case.status
        old_owner = case.lease_owner
        job = self._jobs[case.job_id]
        self._remove_job_runtime_indexes_locked(job)
        self._adjust_counts(self._job_counts[case.job_id], old_status, -1)
        self._adjust_counts(self._job_counts[case.job_id], status, 1)
        self._adjust_counts(self._run_counts[case.run_id], old_status, -1)
        self._adjust_counts(self._run_counts[case.run_id], status, 1)
        if old_status == "leased" and old_owner:
            self._leased_case_ids_by_host[old_owner].discard(case.id)
        case.status = status
        case.lease_owner = lease_owner
        case.updated_at = updated_at
        if status == "leased" and lease_owner:
            self._leased_case_ids_by_host[lease_owner].add(case.id)
        self._add_job_runtime_indexes_locked(job)

    def _insert_case_locked(
        self,
        *,
        job_id: int,
        task_id: str,
        run_id: str,
        test_name: str,
        ordinal: int,
        source: dict[str, object],
        status: str,
        created_at: str,
        compiler_error: bool = False,
    ) -> _CaseRecord:
        case_id = self._next_case_id
        self._next_case_id += 1
        case = _CaseRecord(
            id=case_id,
            job_id=job_id,
            task_id=task_id,
            run_id=run_id,
            test_name=test_name,
            ordinal=ordinal,
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
        return case

    def _sorted_cases_locked(self, case_ids: list[int]) -> list[_CaseRecord]:
        return sorted(
            (self._cases[case_id] for case_id in case_ids if case_id in self._cases),
            key=lambda row: (row.ordinal, row.id),
        )

    def active_job_for_host(self, hostname: str) -> JudgehostJobRow | None:
        with self._lock.read_lock():
            candidates = [
                self._jobs[job_id]
                for job_id in self._job_ids_by_host.get(hostname, ())
                if self._jobs[job_id].status in self._ACTIVE_JOB_STATUSES
            ]
            if not candidates:
                return None
            return self._job_row(min(candidates, key=lambda row: (self._priority(row), row.job_id)))

    def shared_pending_job(self, hostname: str) -> JudgehostJobRow | None:
        with self._lock.read_lock():
            own = [
                self._jobs[job_id]
                for job_id in self._job_ids_by_host.get(hostname, ())
                if self._jobs[job_id].status in self._ACTIVE_JOB_STATUSES
                and self._job_counts[job_id].pending > 0
            ]
            if own:
                job = min(
                    own,
                    key=lambda row: (
                        self._priority(row),
                        0 if row.status == "leased" else 1,
                        row.created_at,
                        row.job_id,
                    ),
                )
                return self._job_row(job)
            candidates = [
                self._jobs[job_id]
                for ids in self._shared_pending_job_ids_by_priority.values()
                for job_id in ids
            ]
            if not candidates:
                return None
            job = min(
                candidates,
                key=lambda row: (
                    self._priority(row),
                    0 if row.status == "leased" else 1,
                    row.created_at,
                    row.job_id,
                ),
            )
            return self._job_row(job)

    def higher_priority_pending_job_exists(self, *, exclude_job_id: int, priority_lt: int) -> bool:
        with self._lock.read_lock():
            return any(
                job_id != int(exclude_job_id)
                for priority, job_ids in self._shared_pending_job_ids_by_priority.items()
                if priority < int(priority_lt)
                for job_id in job_ids
            )

    def host_leased_case_count(self, hostname: str) -> int:
        with self._lock.read_lock():
            return len(self._leased_case_ids_by_host.get(hostname, ()))

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
            candidates = [
                self._jobs[job_id]
                for job_id in self._job_ids_by_group.get(group_key, ())
                if self._jobs[job_id].status in self._ACTIVE_JOB_STATUSES
            ]
            if not candidates:
                return None
            job = max(candidates, key=lambda row: (row.updated_at, row.job_id))
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
            job: _JobRecord | None = None
            if submit_id.isdigit():
                job = self._jobs.get(int(submit_id))
            if job is None:
                job_id = self._job_id_by_submit.get(submit_id)
                candidate = None if job_id is None else self._jobs[job_id]
                if candidate is not None and (
                    contest_id is None or candidate.contest_id == contest_id
                ):
                    job = candidate
                if job is None and candidate is not None:
                    job = candidate
            if job is None:
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

    def active_script_hashes_for_kind(self, kind: str) -> list[str]:
        field = {
            "compile": "compile_hash",
            "run": "run_hash",
            "compare": "compare_hash",
        }.get(kind)
        if field is None:
            return []
        with self._lock.read_lock():
            rows = sorted(self._active_job_ids, reverse=True)[:256]
            return [
                value
                for job_id in rows
                if (value := str(getattr(self._jobs[job_id], field) or "")).strip()
            ]

    def job_finalize_row(self, job_id: int) -> dict[str, object] | None:
        fields = (
            "task_id", "run_id", "group_key", "status", "compile_success",
            "compile_output_b64", "compile_metadata_b64", "debug_text", "work_root",
            "run_config_json", "source_path", "source_name", "compile_hash", "run_hash",
            "compare_hash",
        )
        with self._lock.read_lock():
            job = self._jobs.get(int(job_id))
            return None if job is None else {field: getattr(job, field) for field in fields}

    def finalizing_job_ids(self) -> list[int]:
        with self._lock.read_lock():
            return sorted(
                self._finalizing_job_ids,
                key=lambda job_id: (self._jobs[job_id].updated_at, job_id),
            )

    def claim_job_finalization(
        self,
        job_id: int,
        *,
        now_text: str,
        force_runresult: str = "",
    ) -> JudgehostJobFinalizationClaim | None:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if job is None or job.status not in {"queued", "leased", "finalizing"}:
                return None
            newly_claimed = job.status != "finalizing"
            if job.status != "finalizing":
                terminal_runresult = force_runresult
                if job.compile_success == 0:
                    terminal_runresult = "compiler-error"
                if terminal_runresult:
                    for case in self._sorted_cases_locked(self._case_ids_by_job[job.job_id]):
                        if case.status not in {"pending", "leased"}:
                            continue
                        self._transition_case_locked(
                            case,
                            "reported",
                            lease_owner=case.lease_owner,
                            updated_at=now_text,
                        )
                        case.runresult = terminal_runresult
                        case.runtime_sec = 0.0
                        case.cpu_sec = 0.0
                        case.wall_sec = 0.0
                        case.memory_kb = 0
            counts = self._job_counts[job.job_id]
            if counts.total == 0 or counts.terminal != counts.total:
                return None
            if job.status != "finalizing":
                self._mutate_job_locked(
                    job,
                    status="finalizing",
                    lease_owner=None,
                    updated_at=now_text,
                )
            cases = [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_job[job.job_id])
            ]
            job_row = self._job_row(job)
            if newly_claimed and job_row["lease_owner"] is None:
                job_row["lease_owner"] = ""
            return {"job": job_row, "cases": cases}

    def set_job_terminal_status(
        self,
        job_id: int,
        *,
        status: str,
        completed_at: str,
        updated_at: str,
    ) -> bool:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if job is None or job.status != "finalizing":
                return False
            self._mutate_job_locked(
                job,
                status=status,
                completed_at=completed_at,
                updated_at=updated_at,
            )
            return True

    def record_compile_result(
        self,
        job_id: int,
        *,
        compile_success: int,
        compile_output_b64: str,
        compile_metadata_b64: str,
        lease_owner: str,
        updated_at: str,
    ) -> bool:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if job is None or job.status not in self._ACTIVE_JOB_STATUSES:
                return False
            self._mutate_job_locked(
                job,
                compile_success=compile_success,
                compile_output_b64=compile_output_b64,
                compile_metadata_b64=compile_metadata_b64,
                lease_owner=lease_owner,
                updated_at=updated_at,
            )
            return True

    def case_execution_row(self, case_id: int) -> dict[str, object] | None:
        with self._lock.read_lock():
            case = self._cases.get(int(case_id))
            if case is None:
                return None
            job = self._jobs[case.job_id]
            return {
                "id": case.id,
                "job_id": case.job_id,
                "task_id": case.task_id,
                "test_name": case.test_name,
                "testcase_hash": case.testcase_hash,
                "testcase_input_hash": case.testcase_input_hash,
                "testcase_answer_hash": case.testcase_answer_hash,
                "input_ref": case.input_ref,
                "answer_ref": case.answer_ref,
                "case_status": case.status,
                "case_lease_owner": case.lease_owner,
                "run_id": job.run_id,
                "work_root": job.work_root,
                "mode": job.mode,
                "source_name": job.source_name,
                "source_path": job.source_path,
                "job_status": job.status,
                "group_key": job.group_key,
                "source_hash": job.source_hash,
                "compile_hash": job.compile_hash,
                "run_hash": job.run_hash,
                "compare_hash": job.compare_hash,
                "compile_config_json": job.compile_config_json,
                "run_config_json": job.run_config_json,
                "compare_config_json": job.compare_config_json,
                "compile_success": job.compile_success,
            }

    def case_output_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        with self._lock.read_lock():
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            case = self._cases[case_id]
            return {
                "id": case.id,
                "output_run_rel": case.output_run_rel,
                "work_root": self._jobs[case.job_id].work_root,
            }

    def case_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        with self._lock.read_lock():
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            case = self._cases[case_id]
            job = self._jobs[case.job_id]
            return {
                **self._case_row(case),
                "job_id": job.job_id,
                "job_status": job.status,
                "work_root": job.work_root,
                "compile_success": job.compile_success,
            }

    def report_case_result(
        self,
        case_id: int,
        *,
        lease_owner: str,
        runresult: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        output_run_rel: str,
        output_error_rel: str,
        output_system_rel: str,
        output_diff_rel: str,
        metadata_rel: str,
        compare_metadata_rel: str,
        team_message_rel: str,
        score_text: str,
        updated_at: str,
    ) -> bool:
        with self._lock.write_lock():
            case = self._cases.get(int(case_id))
            if case is None or case.status != "leased" or case.lease_owner != lease_owner:
                return False
            self._transition_case_locked(
                case,
                "reported",
                lease_owner=lease_owner,
                updated_at=updated_at,
            )
            case.runresult = runresult
            case.runtime_sec = runtime_sec
            case.cpu_sec = cpu_sec
            case.wall_sec = wall_sec
            case.memory_kb = memory_kb
            case.output_run_rel = output_run_rel
            case.output_error_rel = output_error_rel
            case.output_system_rel = output_system_rel
            case.output_diff_rel = output_diff_rel
            case.metadata_rel = metadata_rel
            case.compare_metadata_rel = compare_metadata_rel
            case.team_message_rel = team_message_rel
            case.score_text = score_text
            return True

    def case_debug_context(self, case_id: int) -> dict[str, object] | None:
        with self._lock.read_lock():
            case = self._cases.get(int(case_id))
            if case is None:
                return None
            return {
                "job_id": case.job_id,
                "case_debug_text": case.debug_text,
                "job_debug_text": self._jobs[case.job_id].debug_text,
            }

    def job_debug_context(self, job_id: int) -> dict[str, object] | None:
        with self._lock.read_lock():
            job = self._jobs.get(int(job_id))
            return None if job is None else {"job_id": job.job_id, "debug_text": job.debug_text}

    def register_host_requeue(
        self,
        hostname: str,
        *,
        now_text: str,
        remap_seed: int,
    ) -> list[dict[str, object]]:
        with self._lock.write_lock():
            affected = sorted(
                job_id
                for job_id in self._job_ids_by_host.get(hostname, ())
                if self._jobs[job_id].status in self._ACTIVE_JOB_STATUSES
            )
            unfinished: list[dict[str, object]] = []
            for step, job_id in enumerate(affected, start=1):
                job = self._jobs[job_id]
                self._job_id_by_submit.pop(job.submit_id, None)
                job.submit_id = str(remap_seed + step)
                self._job_id_by_submit[job.submit_id] = job_id
                unfinished.append({"jobid": job_id, "submitid": job.submit_id})
                self._mutate_job_locked(job, lease_owner=None, status="queued", updated_at=now_text)
            for case_id in tuple(self._leased_case_ids_by_host.get(hostname, ())):
                case = self._cases[case_id]
                self._transition_case_locked(case, "pending", lease_owner=None, updated_at=now_text)
            return unfinished

    def create_job_with_cases(
        self,
        *,
        task_id: str,
        run_id: str,
        group_key: str,
        submit_id: str,
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
        lease_owner: str,
        status: str,
        created_at: str,
        case_rows: list[dict[str, object]],
    ) -> int:
        with self._lock.write_lock():
            case_task_ids = {str(row.get("task_id") or task_id) for row in case_rows}
            if any(case_task_id in self._job_id_by_task for case_task_id in case_task_ids):
                raise RuntimeError("judgehost task cases already belong to another job")
            if task_id in self._primary_job_id_by_task or run_id in self._primary_job_id_by_run:
                raise RuntimeError("judgehost job identity already exists")
            if submit_id in self._job_id_by_submit:
                raise RuntimeError("judgehost submit id already exists")
            job_id = self._next_job_id
            self._next_job_id += 1
            job = _JobRecord(
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
                lease_owner=lease_owner,
                compile_success=None,
                compile_output_b64=None,
                compile_metadata_b64=None,
                debug_text="",
                status=status,
                created_at=created_at,
                updated_at=created_at,
                completed_at=None,
            )
            self._jobs[job_id] = job
            self._job_counts[job_id] = _StatusCounts()
            self._primary_job_id_by_task[task_id] = job_id
            self._primary_job_id_by_run[run_id] = job_id
            self._job_id_by_submit[job.submit_id] = job_id
            self._job_ids_by_group[job.group_key].add(job_id)
            self._empty_job_ids.add(job_id)
            for case_row in case_rows:
                self._insert_case_locked(
                    job_id=job_id,
                    task_id=str(case_row.get("task_id") or task_id),
                    run_id=str(case_row.get("run_id") or run_id),
                    test_name=str(case_row["test_name"]),
                    ordinal=int(case_row["ordinal"]),
                    source=case_row,
                    status=str(case_row["status"]),
                    created_at=created_at,
                )
            self._add_job_runtime_indexes_locked(job)
            return job_id

    @staticmethod
    def _case_identity(row: dict[str, object]) -> tuple[object, ...]:
        return (
            row.get("run_id"),
            row.get("test_name"),
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
            if job.status not in self._ACTIVE_JOB_STATUSES:
                return {"job_id": int(job_id), "outcome": "closed", "inserted": 0}
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
            existing_pairs = {
                (self._cases[case_id].task_id, self._cases[case_id].test_name)
                for case_id in self._case_ids_by_job[job.job_id]
            }
            next_ordinal = 1 + max(
                (self._cases[case_id].ordinal for case_id in self._case_ids_by_job[job.job_id]),
                default=0,
            )
            inserted = 0
            self._remove_job_runtime_indexes_locked(job)
            for row in case_rows:
                case_task_id = str(row.get("task_id") or "")
                case_run_id = str(row.get("run_id") or "")
                test_name = str(row.get("test_name") or "")
                pair = (case_task_id, test_name)
                if not case_task_id or not case_run_id or not test_name or pair in existing_pairs:
                    continue
                self._insert_case_locked(
                    job_id=job.job_id,
                    task_id=case_task_id,
                    run_id=case_run_id,
                    test_name=test_name,
                    ordinal=next_ordinal,
                    source=row,
                    status=str(row.get("status") or "pending"),
                    created_at=now_text,
                    compiler_error=job.compile_success == 0,
                )
                existing_pairs.add(pair)
                next_ordinal += 1
                inserted += 1
            self._add_job_runtime_indexes_locked(job)
            return {
                "job_id": job.job_id,
                "outcome": "appended" if inserted else "duplicate",
                "inserted": inserted,
            }

    def release_prepared_job_for_queue(
        self,
        job_id: int,
        *,
        lease_owner: str,
        now_text: str,
    ) -> None:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if (
                job is None
                or job.lease_owner != lease_owner
                or job.status not in self._ACTIVE_JOB_STATUSES
            ):
                return
            self._mutate_job_locked(job, lease_owner=None, status="queued", updated_at=now_text)
            for case_id in tuple(self._case_ids_by_job[job.job_id]):
                case = self._cases[case_id]
                if case.status == "leased" and case.lease_owner == lease_owner:
                    self._transition_case_locked(
                        case,
                        "pending",
                        lease_owner=None,
                        updated_at=now_text,
                    )

    def apply_cached_case_results(
        self,
        *,
        cached_rows: list[dict[str, object]],
        lease_owner: str,
        now_text: str,
    ) -> None:
        with self._lock.write_lock():
            for cached in cached_rows:
                case = self._cases.get(int(cached["case_id"]))
                if case is None or case.status != "pending":
                    continue
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=lease_owner,
                    updated_at=now_text,
                )
                for field in (
                    "runresult", "runtime_sec", "cpu_sec", "wall_sec", "memory_kb",
                    "output_run_rel", "output_error_rel", "output_system_rel", "output_diff_rel",
                    "metadata_rel", "compare_metadata_rel", "team_message_rel", "score_text",
                ):
                    setattr(case, field, cached[field])

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
            if job is None or job.status not in self._ACTIVE_JOB_STATUSES:
                return []
            pending = [
                row
                for row in self._sorted_cases_locked(self._case_ids_by_job[job.job_id])
                if row.status == "pending"
            ][:cap]
            if not pending:
                return []
            self._mutate_job_locked(job, lease_owner=hostname, status="leased", updated_at=now_text)
            leased: list[JudgehostCaseRow] = []
            for case in pending:
                self._transition_case_locked(
                    case,
                    "leased",
                    lease_owner=hostname,
                    updated_at=now_text,
                )
                leased.append(self._case_row(case))
            return leased

    def append_debug_text(
        self,
        *,
        case_id: int | None,
        job_id: int | None,
        debug_text: str,
        now_text: str,
    ) -> None:
        with self._lock.write_lock():
            if case_id is not None and int(case_id) in self._cases:
                case = self._cases[int(case_id)]
                case.debug_text = self._merge_debug_text(case.debug_text, debug_text)
                case.updated_at = now_text
            if job_id is not None and int(job_id) in self._jobs:
                job = self._jobs[int(job_id)]
                job.debug_text = self._merge_debug_text(job.debug_text, debug_text)
                job.updated_at = now_text

    @staticmethod
    def _merge_debug_text(current: str, incoming: str) -> str:
        merged = incoming if not current else f"{current}\n{incoming}"
        return merged[-4000:]

    def case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        with self._lock.read_lock():
            result: dict[str, dict[str, int]] = {}
            for run_id in (run_id for run_id in run_ids if run_id):
                counts = self._run_counts.get(run_id)
                if counts is None or counts.total == 0:
                    continue
                result[run_id] = {
                    "total": counts.total,
                    "reported": counts.reported,
                    "leased": counts.leased,
                }
            return result

    def cancel_jobs_for_runs(self, run_ids: list[str], *, now_text: str) -> list[int]:
        safe_run_ids = {run_id for run_id in run_ids if run_id}
        if not safe_run_ids:
            return []
        with self._lock.write_lock():
            job_ids = sorted({
                job_id
                for run_id in safe_run_ids
                for job_id in self._job_ids_by_run.get(run_id, ())
                if self._jobs[job_id].status in self._ACTIVE_JOB_STATUSES
            })
            for run_id in safe_run_ids:
                for case_id in tuple(self._case_ids_by_run.get(run_id, ())):
                    case = self._cases[case_id]
                    if case.status in {"pending", "leased"}:
                        self._transition_case_locked(
                            case,
                            "cancelled",
                            lease_owner=None,
                            updated_at=now_text,
                        )
            for job_id in job_ids:
                job = self._jobs[job_id]
                self._mutate_job_locked(job, updated_at=now_text)
            return job_ids

    def release_host_leases(self, hostname: str, *, now_text: str) -> tuple[int, int]:
        with self._lock.write_lock():
            job_ids = [
                job_id
                for job_id in self._job_ids_by_host.get(hostname, ())
                if self._jobs[job_id].status in self._ACTIVE_JOB_STATUSES
            ]
            for job_id in job_ids:
                self._mutate_job_locked(
                    self._jobs[job_id],
                    lease_owner=None,
                    status="queued",
                    updated_at=now_text,
                )
            case_ids = list(self._leased_case_ids_by_host.get(hostname, ()))
            for case_id in case_ids:
                self._transition_case_locked(
                    self._cases[case_id],
                    "pending",
                    lease_owner=None,
                    updated_at=now_text,
                )
            return len(job_ids), len(case_ids)

    def release_host_job_ownership(self, hostname: str, *, now_text: str) -> int:
        with self._lock.write_lock():
            job_ids = [
                job_id
                for job_id in self._job_ids_by_host.get(hostname, ())
                if self._jobs[job_id].status in self._ACTIVE_JOB_STATUSES
            ]
            for job_id in job_ids:
                self._mutate_job_locked(
                    self._jobs[job_id],
                    lease_owner=None,
                    status="queued",
                    updated_at=now_text,
                )
            return len(job_ids)

    def _remove_case_locked(self, case: _CaseRecord) -> None:
        if case.status == "leased" and case.lease_owner:
            self._leased_case_ids_by_host[case.lease_owner].discard(case.id)
        self._adjust_counts(self._job_counts[case.job_id], case.status, -1)
        self._adjust_counts(self._run_counts[case.run_id], case.status, -1)
        self._case_ids_by_job[case.job_id].remove(case.id)
        self._case_ids_by_task[case.task_id].remove(case.id)
        self._case_ids_by_run[case.run_id].remove(case.id)
        if case.testcase_id is not None:
            self._case_ids_by_testcase[case.testcase_id].discard(case.id)
        self._cases.pop(case.id)
        pair = (case.task_id, case.test_name)
        if self._latest_case_id_by_task_test.get(pair) == case.id:
            remaining = [
                case_id
                for case_id in self._case_ids_by_task[case.task_id]
                if self._cases[case_id].test_name == case.test_name
            ]
            if remaining:
                self._latest_case_id_by_task_test[pair] = max(remaining)
            else:
                self._latest_case_id_by_task_test.pop(pair, None)
        if not self._case_ids_by_task[case.task_id]:
            self._case_ids_by_task.pop(case.task_id, None)
            self._job_id_by_task.pop(case.task_id, None)
        if not self._case_ids_by_run[case.run_id]:
            self._case_ids_by_run.pop(case.run_id, None)
            self._job_ids_by_run.pop(case.run_id, None)
            self._run_counts.pop(case.run_id, None)
        elif not any(
            self._cases[case_id].job_id == case.job_id
            for case_id in self._case_ids_by_run[case.run_id]
        ):
            self._job_ids_by_run[case.run_id].discard(case.job_id)
        if not self._case_ids_by_job[case.job_id]:
            self._empty_job_ids.add(case.job_id)

    def _remove_job_locked(self, job_id: int) -> None:
        job = self._jobs.pop(job_id)
        self._remove_job_runtime_indexes_locked(job)
        self._primary_job_id_by_task.pop(job.task_id, None)
        self._primary_job_id_by_run.pop(job.run_id, None)
        self._job_id_by_submit.pop(job.submit_id, None)
        self._job_ids_by_group[job.group_key].discard(job_id)
        self._case_ids_by_job.pop(job_id, None)
        self._job_counts.pop(job_id, None)
        self._empty_job_ids.discard(job_id)

    def forget_runs(self, run_ids: list[str]) -> int:
        safe_run_ids = {run_id for run_id in run_ids if run_id}
        if not safe_run_ids:
            return 0
        with self._lock.write_lock():
            affected_jobs = {
                job_id
                for run_id in safe_run_ids
                for job_id in self._job_ids_by_run.get(run_id, ())
            }
            for job_id in affected_jobs:
                self._remove_job_runtime_indexes_locked(self._jobs[job_id])
            for run_id in safe_run_ids:
                for case_id in tuple(self._case_ids_by_run.get(run_id, ())):
                    self._remove_case_locked(self._cases[case_id])
            for job_id in affected_jobs:
                if job_id in self._jobs:
                    self._add_job_runtime_indexes_locked(self._jobs[job_id])
            empty_jobs = tuple(self._empty_job_ids)
            for job_id in empty_jobs:
                self._remove_job_locked(job_id)
            return len(empty_jobs)

    def cancel_all_inflight(self, *, now_text: str) -> list[int]:
        with self._lock.write_lock():
            job_ids = sorted(self._active_job_ids)
            for job_id in job_ids:
                job = self._jobs[job_id]
                self._mutate_job_locked(job, updated_at=now_text)
                for case_id in tuple(self._case_ids_by_job[job_id]):
                    case = self._cases[case_id]
                    if case.status in {"pending", "leased"}:
                        self._transition_case_locked(
                            case,
                            "cancelled",
                            lease_owner=None,
                            updated_at=now_text,
                        )
            return job_ids
