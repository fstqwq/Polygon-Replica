from __future__ import annotations

import heapq
import time

from .job_scheduler_models import (
    CaseClaim,
    CaseClaimBusy,
    CaseResult,
    JudgehostCaseRow,
    JudgehostJobFinalizationClaim,
)


class JobSchedulerResultMixin:
    """Apply callback, publication, cancellation, and terminal transitions."""

    def job_finalize_row(self, job_id: int) -> dict[str, object] | None:
        fields = (
            "task_id", "run_id", "group_key", "status", "compile_success",
            "compile_output_b64", "compile_metadata_b64", "debug_text", "work_root",
            "run_config_json", "source_path", "source_name", "compile_hash", "run_hash",
            "compare_hash", "materialization_state",
        )
        with self._lock:
            job = self._jobs.get(int(job_id))
            return None if job is None else {field: getattr(job, field) for field in fields}

    def claim_job_finalization(
        self,
        job_id: int,
        *,
        now_text: str,
    ) -> JudgehostJobFinalizationClaim | None:
        with self._lock:
            job = self._jobs.get(int(job_id))
            if job is None:
                return None
            counts = self._job_counts[job.job_id]
            if (
                job.status == "open"
                and counts.total > 0
                and counts.terminal == counts.total
                and job.materialization_state != "materializing"
            ):
                self._close_job_locked(job, updated_at=now_text)
            if (
                job.status != "finalize-pending"
                or counts.total == 0
                or counts.terminal != counts.total
                or job.materialization_state == "materializing"
            ):
                return None
            self._mutate_job_locked(job, status="finalizing", updated_at=now_text)
            cases = [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_job[job.job_id])
            ]
            return {"job": self._job_row(job), "cases": cases}

    def abort_job_finalization(
        self,
        job_id: int,
        *,
        now_text: str,
        delay_sec: float = 0.25,
    ) -> bool:
        with self._lock:
            job = self._jobs.get(int(job_id))
            if job is None or job.status != "finalizing":
                return False
            self._mutate_job_locked(job, status="finalize-pending", updated_at=now_text)
            deadline = time.monotonic() + max(0.0, float(delay_sec))
            current = self._finalization_retry_deadlines.get(job.job_id)
            if current is None or deadline < current:
                self._finalization_retry_deadlines[job.job_id] = deadline
                heapq.heappush(self._finalization_retry_heap, (deadline, job.job_id))
            return True

    def due_job_finalizations(self, *, limit: int) -> list[int]:
        due: list[int] = []
        now = time.monotonic()
        with self._lock:
            while self._finalization_retry_heap and len(due) < max(0, int(limit)):
                deadline, job_id = self._finalization_retry_heap[0]
                if deadline > now:
                    break
                heapq.heappop(self._finalization_retry_heap)
                if self._finalization_retry_deadlines.get(job_id) != deadline:
                    continue
                self._finalization_retry_deadlines.pop(job_id, None)
                job = self._jobs.get(job_id)
                if job is not None and job.status == "finalize-pending":
                    due.append(job_id)
        return due

    def clear_job_finalization_retry(self, job_id: int) -> None:
        with self._lock:
            self._finalization_retry_deadlines.pop(int(job_id), None)

    def set_job_terminal_status(
        self,
        job_id: int,
        *,
        status: str,
        completed_at: str,
        updated_at: str,
    ) -> bool:
        with self._lock:
            job = self._jobs.get(int(job_id))
            if job is None or job.status != "finalizing":
                return False
            self._mutate_job_locked(
                job,
                status=status,
                completed_at=completed_at,
                updated_at=updated_at,
            )
            self._finalization_retry_deadlines.pop(job.job_id, None)
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
        with self._lock:
            job = self._jobs.get(int(job_id))
            if job is None or job.status != "open":
                return False
            if job.compile_owner not in {None, lease_owner}:
                return False
            if compile_success != 1 and job.group_key:
                if self._appendable_job_id_by_group.get(job.group_key) == job.job_id:
                    self._appendable_job_id_by_group.pop(job.group_key, None)
            self._mutate_job_locked(
                job,
                compile_success=compile_success,
                compile_state="succeeded" if compile_success == 1 else "failed",
                compile_output_b64=compile_output_b64,
                compile_metadata_b64=compile_metadata_b64,
                compile_owner=None if compile_success == 1 else lease_owner,
                updated_at=updated_at,
            )
            return True

    def case_execution_row(self, case_id: int) -> dict[str, object] | None:
        with self._lock:
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
                "run_id": case.run_id,
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
        with self._lock:
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            case = self._cases[case_id]
            output_ref = "" if case.result is None else case.result.output_run_rel
            return {
                "id": case.id,
                "output_run_rel": output_ref,
                "work_root": self._jobs[case.job_id].work_root,
            }

    def case_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        with self._lock:
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

    def case_result_for_task(self, task_id: str, test_name: str) -> CaseResult | None:
        with self._lock:
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return None
            return self._cases[case_id].result

    def claim_case_reporting(
        self,
        case_id: int,
        *,
        hostname: str,
        now_text: str,
    ) -> CaseClaim | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            if case is None or case.status in self._TERMINAL_CASE_STATUSES:
                return None
            if case.status == "reporting":
                raise CaseClaimBusy("judgehost case result is already being processed")
            if case.status != "leased" or case.lease_owner != hostname:
                return None
            case.claim_generation += 1
            self._transition_case_locked(
                case,
                "reporting",
                lease_owner=hostname,
                updated_at=now_text,
            )
            return CaseClaim(
                case_id=case.id,
                generation=case.claim_generation,
                job_id=case.job_id,
                task_id=case.task_id,
                test_name=case.test_name,
            )

    def claim_cache_cases(
        self,
        job_id: int,
        *,
        hostname: str,
        limit: int,
        now_text: str,
    ) -> list[tuple[CaseClaim, JudgehostCaseRow]]:
        claimed: list[tuple[CaseClaim, JudgehostCaseRow]] = []
        with self._lock:
            job = self._jobs.get(int(job_id))
            if job is None or job.status != "open":
                return claimed
            while len(claimed) < max(0, int(limit)):
                case = self._peek_case_heap_locked(job.job_id, status="cache-pending")
                if case is None:
                    break
                heapq.heappop(self._cache_heaps_by_job[job.job_id])
                case.claim_generation += 1
                self._transition_case_locked(
                    case,
                    "cache-probing",
                    lease_owner=hostname,
                    updated_at=now_text,
                    refresh_job=False,
                )
                claim = CaseClaim(
                    case_id=case.id,
                    generation=case.claim_generation,
                    job_id=case.job_id,
                    task_id=case.task_id,
                    test_name=case.test_name,
                )
                claimed.append((claim, self._case_row(case)))
            self._refresh_jobs_locked({job.job_id}, updated_at=now_text)
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
            self._transition_case_locked(case, "cancelled", lease_owner=None, updated_at=updated_at)
            return "cancelled"
        case.result = terminal_result or result
        result_owner = case.lease_owner if case.status == "reporting" else None
        self._transition_case_locked(
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
    ) -> str | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            if (
                case is None
                or case.status not in {"reporting", "cache-probing"}
                or case.claim_generation != int(generation)
            ):
                return None
            return self._finish_claim_locked(case, result=result, updated_at=updated_at)

    def finish_cache_miss(
        self,
        case_id: int,
        *,
        generation: int,
        updated_at: str,
    ) -> bool:
        with self._lock:
            case = self._cases.get(int(case_id))
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
                self._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif terminal_result is not None:
                case.result = terminal_result
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            else:
                self._transition_case_locked(
                    case,
                    "pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            return True

    def abort_case_claim(
        self,
        case_id: int,
        *,
        generation: int,
        updated_at: str,
    ) -> bool:
        with self._lock:
            case = self._cases.get(int(case_id))
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
                self._transition_case_locked(
                    case,
                    "cancelled",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif terminal_result is not None:
                case.result = terminal_result
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif case.status == "cache-probing":
                self._transition_case_locked(
                    case,
                    "cache-pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            elif case.requeue_on_abort:
                self._transition_case_locked(
                    case,
                    "pending",
                    lease_owner=None,
                    updated_at=updated_at,
                )
            else:
                self._transition_case_locked(
                    case,
                    "leased",
                    lease_owner=case.lease_owner,
                    updated_at=updated_at,
                )
            case.requeue_on_abort = False
            return True

    def request_job_case_results(
        self,
        job_id: int,
        *,
        results: dict[int, CaseResult],
        updated_at: str,
    ) -> set[str]:
        affected_tasks: set[str] = set()
        with self._lock:
            job = self._jobs.get(int(job_id))
            if job is None:
                return affected_tasks
            if job.group_key and self._appendable_job_id_by_group.get(job.group_key) == job.job_id:
                self._appendable_job_id_by_group.pop(job.group_key, None)
            affected = False
            for case_id, result in results.items():
                case = self._cases.get(int(case_id))
                if (
                    case is None
                    or case.job_id != job.job_id
                    or case.status in self._TERMINAL_CASE_STATUSES
                ):
                    continue
                affected_tasks.add(case.task_id)
                affected = True
                if case.status in {"reporting", "cache-probing"}:
                    if not case.cancel_requested:
                        case.terminal_result = result
                    continue
                case.result = result
                self._transition_case_locked(
                    case,
                    "reported",
                    lease_owner=case.lease_owner,
                    updated_at=updated_at,
                    refresh_job=False,
                )
            if affected:
                self._refresh_jobs_locked({job.job_id}, updated_at=updated_at)
        return affected_tasks

    def mark_case_verification_published(self, task_id: str, test_name: str) -> bool:
        with self._lock:
            case_id = self._latest_case_id_by_task_test.get((task_id, test_name))
            if case_id is None:
                return False
            case = self._cases[case_id]
            if case.status not in self._TERMINAL_CASE_STATUSES:
                return False
            case.verification_published = True
            return True

    def mark_cases_verification_published(self, case_ids: list[int]) -> int:
        marked = 0
        with self._lock:
            for case_id in dict.fromkeys(int(raw_case_id) for raw_case_id in case_ids):
                case = self._cases.get(case_id)
                if case is None or case.status not in self._TERMINAL_CASE_STATUSES:
                    continue
                if not case.verification_published:
                    case.verification_published = True
                    marked += 1
        return marked

    def case_debug_context(self, case_id: int) -> dict[str, object] | None:
        with self._lock:
            case = self._cases.get(int(case_id))
            if case is None:
                return None
            return {
                "job_id": case.job_id,
                "case_debug_text": case.debug_text,
                "job_debug_text": self._jobs[case.job_id].debug_text,
            }

    def job_debug_context(self, job_id: int) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(int(job_id))
            return None if job is None else {"job_id": job.job_id, "debug_text": job.debug_text}

    def append_debug_text(
        self,
        *,
        case_id: int | None,
        job_id: int | None,
        debug_text: str,
        now_text: str,
    ) -> None:
        with self._lock:
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
        with self._lock:
            result: dict[str, dict[str, int]] = {}
            for run_id in (run_id for run_id in run_ids if run_id):
                counts = self._run_counts.get(run_id)
                if counts is None or counts.total == 0:
                    continue
                result[run_id] = {
                    "total": counts.total,
                    "reported": counts.reported,
                    "leased": counts.leased + counts.reporting,
                }
            return result

    def _cancel_case_locked(self, case, *, now_text: str) -> None:
        if case.status in self._TERMINAL_CASE_STATUSES:
            return
        if case.status in {"reporting", "cache-probing"}:
            case.cancel_requested = True
            case.terminal_result = None
            return
        case.result = None
        self._transition_case_locked(
            case,
            "cancelled",
            lease_owner=None,
            updated_at=now_text,
            refresh_job=False,
        )

    def cancel_jobs_for_runs(self, run_ids: list[str], *, now_text: str) -> list[int]:
        safe_run_ids = {run_id for run_id in run_ids if run_id}
        if not safe_run_ids:
            return []
        with self._lock:
            job_ids = sorted({
                job_id
                for run_id in safe_run_ids
                for job_id in self._job_ids_by_run.get(run_id, ())
                if self._jobs[job_id].status in {"open", "finalize-pending", "finalizing"}
            })
            for run_id in safe_run_ids:
                for case_id in tuple(self._case_ids_by_run.get(run_id, ())):
                    self._cancel_case_locked(self._cases[case_id], now_text=now_text)
            self._refresh_jobs_locked(set(job_ids), updated_at=now_text)
            return job_ids

    def release_host_leases(self, hostname: str, *, now_text: str) -> tuple[int, int]:
        with self._lock:
            active_job_id = self._active_job_by_host.pop(hostname, None)
            job_ids = [] if active_job_id is None else [active_job_id]
            affected_job_ids = set(job_ids)
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if job is not None and job.status == "open" and job.compile_owner == hostname:
                    job.compile_owner = None
            case_ids = list(self._leased_case_ids_by_host.get(hostname, ()))
            for case_id in case_ids:
                case = self._cases[case_id]
                if case.status == "reporting":
                    case.requeue_on_abort = True
                elif case.status == "leased":
                    self._transition_case_locked(
                        case,
                        "pending",
                        lease_owner=None,
                        updated_at=now_text,
                        refresh_job=False,
                    )
                affected_job_ids.add(case.job_id)
            self._refresh_jobs_locked(affected_job_ids, updated_at=now_text)
            return len(job_ids), len(case_ids)

    def _remove_cases_locked(self, case_ids: set[int]) -> None:
        cases = [self._cases[case_id] for case_id in case_ids if case_id in self._cases]
        if not cases:
            return
        affected_job_ids = {case.job_id for case in cases}
        affected_task_ids = {case.task_id for case in cases}
        affected_run_ids = {case.run_id for case in cases}
        affected_pairs = {(case.task_id, case.test_name) for case in cases}

        for case in cases:
            if case.status in {"leased", "reporting"} and case.lease_owner:
                leased_ids = self._leased_case_ids_by_host[case.lease_owner]
                leased_ids.discard(case.id)
                if not leased_ids:
                    self._leased_case_ids_by_host.pop(case.lease_owner, None)
            self._adjust_counts(self._job_counts[case.job_id], case.status, -1)
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

        for job_id in affected_job_ids:
            retained = self._case_ids_by_job[job_id].difference(case_ids)
            self._case_ids_by_job[job_id] = retained
            if retained:
                self._empty_job_ids.discard(job_id)
            else:
                self._empty_job_ids.add(job_id)

        for task_id in affected_task_ids:
            retained = self._case_ids_by_task[task_id].difference(case_ids)
            if retained:
                self._case_ids_by_task[task_id] = retained
                self._job_id_by_task[task_id] = self._cases[next(iter(retained))].job_id
            else:
                self._case_ids_by_task.pop(task_id, None)
                self._job_id_by_task.pop(task_id, None)
                self._task_case_counts.pop(task_id, None)

        for run_id in affected_run_ids:
            retained = self._case_ids_by_run[run_id].difference(case_ids)
            if retained:
                self._case_ids_by_run[run_id] = retained
                self._job_ids_by_run[run_id] = {
                    self._cases[case_id].job_id
                    for case_id in retained
                }
            else:
                self._case_ids_by_run.pop(run_id, None)
                self._job_ids_by_run.pop(run_id, None)
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

    def _remove_job_locked(self, job_id: int) -> None:
        job = self._jobs.pop(job_id)
        if job.status == "open":
            self._index_job_scripts_locked(job, -1)
        self._ready_job_ids.discard(job_id)
        self._finalization_retry_deadlines.pop(job_id, None)
        for hostname, active_job_id in tuple(self._active_job_by_host.items()):
            if active_job_id == job_id:
                self._active_job_by_host.pop(hostname, None)
        if job.group_key:
            group_jobs = self._job_ids_by_group[job.group_key]
            group_jobs.discard(job_id)
            if not group_jobs:
                self._job_ids_by_group.pop(job.group_key, None)
            if self._appendable_job_id_by_group.get(job.group_key) == job_id:
                self._appendable_job_id_by_group.pop(job.group_key, None)
        self._case_ids_by_job.pop(job_id, None)
        self._job_counts.pop(job_id, None)
        self._job_specs.pop(job_id, None)
        self._cache_heaps_by_job.pop(job_id, None)
        self._runnable_heaps_by_job.pop(job_id, None)
        self._empty_job_ids.discard(job_id)

    def forget_runs(self, run_ids: list[str]) -> int:
        safe_run_ids = {run_id for run_id in run_ids if run_id}
        if not safe_run_ids:
            return 0
        with self._lock:
            affected_jobs = {
                job_id
                for run_id in safe_run_ids
                for job_id in self._job_ids_by_run.get(run_id, ())
            }
            case_ids = {
                case_id
                for run_id in safe_run_ids
                for case_id in self._case_ids_by_run.get(run_id, ())
            }
            self._remove_cases_locked(case_ids)
            for job_id in affected_jobs:
                if job_id in self._jobs:
                    self._touch_job_locked(self._jobs[job_id])
            empty_jobs = tuple(job_id for job_id in affected_jobs if job_id in self._empty_job_ids)
            for job_id in empty_jobs:
                self._remove_job_locked(job_id)
            return len(empty_jobs)

    def cancel_all_inflight(self, *, now_text: str) -> list[int]:
        with self._lock:
            job_ids = sorted(
                job_id
                for job_id, job in self._jobs.items()
                if job.status in {"open", "finalize-pending", "finalizing"}
            )
            for job_id in job_ids:
                for case_id in tuple(self._case_ids_by_job[job_id]):
                    self._cancel_case_locked(self._cases[case_id], now_text=now_text)
            self._refresh_jobs_locked(set(job_ids), updated_at=now_text)
            return job_ids
