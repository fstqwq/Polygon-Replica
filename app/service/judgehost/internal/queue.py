from __future__ import annotations


class JudgehostQueueMixin:
    # Delegation layer; logic lives in app.service.judgehost.task_queue.TaskQueue
    def domjudge_runs_with_leased_cases(self, *args, **kwargs):
        return self._queue.domjudge_runs_with_leased_cases(*args, **kwargs)

    def fetch_work(self, *args, **kwargs):
        return self._queue.fetch_work(*args, **kwargs)

    def renew_lease(self, *args, **kwargs):
        return self._queue.renew_lease(*args, **kwargs)

    def report_result(self, *args, **kwargs):
        return self._queue.report_result(*args, **kwargs)

    def wait_for_task_result(self, *args, **kwargs):
        return self._queue.wait_for_task_result(*args, **kwargs)

    def poll_task_result(self, *args, **kwargs):
        return self._queue.poll_task_result(*args, **kwargs)

    def wait_for_task_case_result(self, *args, **kwargs):
        return self._queue.wait_for_task_case_result(*args, **kwargs)

    def poll_task_case_result(self, *args, **kwargs):
        return self._queue.poll_task_case_result(*args, **kwargs)

    def wait_for_task(self, *args, **kwargs):
        return self._queue.wait_for_task(*args, **kwargs)

    def set_host_enabled(self, *args, **kwargs):
        return self._queue.set_host_enabled(*args, **kwargs)

    def status(self, *args, **kwargs):
        return self._queue.status(*args, **kwargs)

    def cancel_tasks_for_runs(self, *args, **kwargs):
        return self._queue.cancel_tasks_for_runs(*args, **kwargs)

    def startup_cancel_inflight_tasks(self, *args, **kwargs):
        return self._queue.startup_cancel_inflight_tasks(*args, **kwargs)

    def forget_problem_tasks(self, *args, **kwargs):
        return self._queue.forget_problem_tasks(*args, **kwargs)

    def cancel_domjudge_jobs_for_runs(self, *args, **kwargs):
        return self._queue.cancel_domjudge_jobs_for_runs(*args, **kwargs)

    def cancel_all_domjudge_inflight(self, *args, **kwargs):
        return self._queue.cancel_all_domjudge_inflight(*args, **kwargs)

    def forget_domjudge_runs(self, *args, **kwargs):
        return self._queue.forget_domjudge_runs(*args, **kwargs)

    def _run_ids_with_leased_cases(self, *args, **kwargs):
        return self._queue._run_ids_with_leased_cases(*args, **kwargs)

    def _lease_matching_group_task(self, *args, **kwargs):
        return self._queue._lease_matching_group_task(*args, **kwargs)

    def _claim_lease_requeue_slot(self, *args, **kwargs):
        return self._queue._claim_lease_requeue_slot(*args, **kwargs)

    def _requeue_expired_leases(self, *args, **kwargs):
        return self._queue._requeue_expired_leases(*args, **kwargs)

    def _record_host_event_conn(self, *args, **kwargs):
        return self._queue._record_host_event_conn(*args, **kwargs)

    def _host_enabled_conn(self, *args, **kwargs):
        return self._queue._host_enabled_conn(*args, **kwargs)

    def _load_run_summary(self, *args, **kwargs):
        return self._queue.load_run_summary(*args, **kwargs)

    def _summary_error_text(self, *args, **kwargs):
        return self._queue._summary_error_text(*args, **kwargs)

    def _host_status_rows(self, *args, **kwargs):
        return self._queue._host_status_rows(*args, **kwargs)
