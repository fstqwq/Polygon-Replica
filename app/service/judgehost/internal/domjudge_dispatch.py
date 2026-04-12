from __future__ import annotations


class JudgehostDomjudgeDispatchMixin:
    # Delegation layer; logic lives in app.service.judgehost.dispatch.DispatchHandler
    def _domjudge_visible_testcase_id(self, *args, **kwargs):
        return self._dispatch._domjudge_visible_testcase_id(*args, **kwargs)

    def _domjudge_case_rows(self, *args, **kwargs):
        return self._dispatch._domjudge_case_rows(*args, **kwargs)

    def domjudge_register_host(self, *args, **kwargs):
        return self._dispatch.domjudge_register_host(*args, **kwargs)

    def _domjudge_task_payload(self, *args, **kwargs):
        return self._dispatch._domjudge_task_payload(*args, **kwargs)

    def _domjudge_config_object(self, *args, **kwargs):
        return self._dispatch._domjudge_config_object(*args, **kwargs)

    def _domjudge_precomputed_bundle(self, *args, **kwargs):
        return self._dispatch._domjudge_precomputed_bundle(*args, **kwargs)

    def _domjudge_prepare_payload(self, *args, **kwargs):
        return self._dispatch._domjudge_prepare_payload(*args, **kwargs)

    def _domjudge_cache_entry(self, *args, **kwargs):
        return self._dispatch._domjudge_cache_entry(*args, **kwargs)

    def _domjudge_cached_case_result(self, *args, **kwargs):
        return self._dispatch._domjudge_cached_case_result(*args, **kwargs)

    def _domjudge_prepare_job(self, *args, **kwargs):
        return self._dispatch._domjudge_prepare_job(*args, **kwargs)

    def _domjudge_append_grouped_task(self, *args, **kwargs):
        return self._dispatch._domjudge_append_grouped_task(*args, **kwargs)

    def _domjudge_absorb_grouped_tasks(self, *args, **kwargs):
        return self._dispatch._domjudge_absorb_grouped_tasks(*args, **kwargs)

    def _domjudge_try_cache_shortcut(self, *args, **kwargs):
        return self._dispatch._domjudge_try_cache_shortcut(*args, **kwargs)

    def _domjudge_release_prepared_job_for_queue(self, *args, **kwargs):
        return self._dispatch._domjudge_release_prepared_job_for_queue(*args, **kwargs)

    def _domjudge_apply_cache_shortcuts_for_job(self, *args, **kwargs):
        return self._dispatch._domjudge_apply_cache_shortcuts_for_job(*args, **kwargs)

    def _domjudge_try_prequeue_cache_finalize(self, *args, **kwargs):
        return self._dispatch._domjudge_try_prequeue_cache_finalize(*args, **kwargs)

    def _domjudge_lease_cases(self, *args, **kwargs):
        return self._dispatch._domjudge_lease_cases(*args, **kwargs)

    def domjudge_fetch_work(self, *args, **kwargs):
        return self._dispatch.domjudge_fetch_work(*args, **kwargs)
