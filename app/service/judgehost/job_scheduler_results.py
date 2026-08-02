from __future__ import annotations

from .job_scheduler_models import JudgehostJobFinalizationClaim


class JobSchedulerResultMixin:
    """Apply callback, publication, cancellation, and terminal transitions."""

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

    def claim_job_finalization(
        self,
        job_id: int,
        *,
        now_text: str,
        force_runresult: str = "",
    ) -> JudgehostJobFinalizationClaim | None:
        with self._lock.write_lock():
            job = self._jobs.get(int(job_id))
            if job is None or job.status not in {"open", "finalizing"}:
                return None
            if job.status != "finalizing":
                terminal_runresult = force_runresult
                if job.compile_success == 0:
                    terminal_runresult = "compiler-error"
                if terminal_runresult:
                    for case in self._sorted_cases_locked(self._case_ids_by_job[job.job_id]):
                        if case.status not in {"staged", "cache-pending", "pending", "leased"}:
                            continue
                        self._transition_case_locked(
                            case,
                            "reported",
                            lease_owner=case.lease_owner,
                            updated_at=now_text,
                            refresh_job=False,
                        )
                        case.runresult = terminal_runresult
                        case.runtime_sec = 0.0
                        case.cpu_sec = 0.0
                        case.wall_sec = 0.0
                        case.memory_kb = 0
                    self._refresh_jobs_locked({job.job_id})
            counts = self._job_counts[job.job_id]
            if counts.total == 0 or counts.terminal != counts.total:
                return None
            if job.status != "finalizing":
                self._index_job_scripts_locked(job, -1)
                if job.group_key:
                    if self._appendable_job_id_by_group.get(job.group_key) == job.job_id:
                        self._appendable_job_id_by_group.pop(job.group_key, None)
                self._mutate_job_locked(
                    job,
                    status="finalizing",
                    updated_at=now_text,
                )
            cases = [
                self._case_row(row)
                for row in self._sorted_cases_locked(self._case_ids_by_job[job.job_id])
            ]
            return {"job": self._job_row(job), "cases": cases}

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

    def mark_case_verification_published(self, task_id: str, test_name: str) -> bool:
        with self._lock.write_lock():
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
        with self._lock.write_lock():
            for case_id in dict.fromkeys(int(raw_case_id) for raw_case_id in case_ids):
                case = self._cases.get(case_id)
                if case is None or case.status not in self._TERMINAL_CASE_STATUSES:
                    continue
                if not case.verification_published:
                    case.verification_published = True
                    marked += 1
        return marked

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
                    if case.status in {"staged", "cache-pending", "pending", "leased"}:
                        self._transition_case_locked(
                            case,
                            "cancelled",
                            lease_owner=None,
                            updated_at=now_text,
                            refresh_job=False,
                        )
            self._refresh_jobs_locked(set(job_ids), updated_at=now_text)
            return job_ids

    def release_host_leases(self, hostname: str, *, now_text: str) -> tuple[int, int]:
        with self._lock.write_lock():
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
            if case.status == "leased" and case.lease_owner:
                leased_ids = self._leased_case_ids_by_host[case.lease_owner]
                leased_ids.discard(case.id)
                if not leased_ids:
                    self._leased_case_ids_by_host.pop(case.lease_owner, None)
            self._adjust_counts(self._job_counts[case.job_id], case.status, -1)
            self._adjust_counts(self._run_counts[case.run_id], case.status, -1)
            if case.testcase_id is not None:
                testcase_cases = self._case_ids_by_testcase[case.testcase_id]
                testcase_cases.discard(case.id)
                if not testcase_cases:
                    self._case_ids_by_testcase.pop(case.testcase_id, None)

        for job_id in affected_job_ids:
            retained = [
                case_id
                for case_id in self._case_ids_by_job[job_id]
                if case_id not in case_ids
            ]
            self._case_ids_by_job[job_id] = retained
            if retained:
                self._empty_job_ids.discard(job_id)
            else:
                self._empty_job_ids.add(job_id)

        for task_id in affected_task_ids:
            retained = [
                case_id
                for case_id in self._case_ids_by_task[task_id]
                if case_id not in case_ids
            ]
            if retained:
                self._case_ids_by_task[task_id] = retained
                self._job_id_by_task[task_id] = self._cases[retained[0]].job_id
            else:
                self._case_ids_by_task.pop(task_id, None)
                self._job_id_by_task.pop(task_id, None)

        for run_id in affected_run_ids:
            retained = [
                case_id
                for case_id in self._case_ids_by_run[run_id]
                if case_id not in case_ids
            ]
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
        with self._lock.write_lock():
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
        with self._lock.write_lock():
            job_ids = sorted(job_id for job_id, job in self._jobs.items() if job.status == "open")
            for job_id in job_ids:
                for case_id in tuple(self._case_ids_by_job[job_id]):
                    case = self._cases[case_id]
                    if case.status in {"staged", "cache-pending", "pending", "leased"}:
                        self._transition_case_locked(
                            case,
                            "cancelled",
                            lease_owner=None,
                            updated_at=now_text,
                            refresh_job=False,
                        )
            self._refresh_jobs_locked(set(job_ids), updated_at=now_text)
            return job_ids
