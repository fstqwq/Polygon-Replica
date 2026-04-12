from __future__ import annotations


class JudgehostDomjudgeResultsMixin:
    # Delegation layer; logic lives in app.service.judgehost.result.ResultProcessor
    def _domjudge_task_accepts_case_updates(self, *args, **kwargs):
        return self._result._domjudge_task_accepts_case_updates(*args, **kwargs)

    def _domjudge_feedback_token_order(self, *args, **kwargs):
        return self._result._domjudge_feedback_token_order(*args, **kwargs)

    def _domjudge_feedback_text_and_files(self, *args, **kwargs):
        return self._result._domjudge_feedback_text_and_files(*args, **kwargs)

    def _domjudge_update_verification_run_case_progress(self, *args, **kwargs):
        return self._result._domjudge_update_verification_run_case_progress(*args, **kwargs)

    def domjudge_get_source_files(self, *args, **kwargs):
        return self._result.domjudge_get_source_files(*args, **kwargs)

    def domjudge_get_testcase_files(self, *args, **kwargs):
        return self._result.domjudge_get_testcase_files(*args, **kwargs)

    def domjudge_get_executable_files(self, *args, **kwargs):
        return self._result.domjudge_get_executable_files(*args, **kwargs)

    def domjudge_get_version_commands(self, *args, **kwargs):
        return self._result.domjudge_get_version_commands(*args, **kwargs)

    def domjudge_check_versions(self, *args, **kwargs):
        return self._result.domjudge_check_versions(*args, **kwargs)

    def _domjudge_verdict_from_runresult(self, *args, **kwargs):
        return self._result._domjudge_verdict_from_runresult(*args, **kwargs)

    def _domjudge_task_lease_owner(self, *args, **kwargs):
        return self._result._domjudge_task_lease_owner(*args, **kwargs)

    def _domjudge_finalize_case_task(self, *args, **kwargs):
        return self._result._domjudge_finalize_case_task(*args, **kwargs)

    def _domjudge_finalize_if_ready(self, *args, **kwargs):
        return self._result._domjudge_finalize_if_ready(*args, **kwargs)

    def domjudge_update_judging(self, *args, **kwargs):
        return self._result.domjudge_update_judging(*args, **kwargs)

    def domjudge_add_judging_run(self, *args, **kwargs):
        return self._result.domjudge_add_judging_run(*args, **kwargs)

    def domjudge_internal_error(self, *args, **kwargs):
        return self._result.domjudge_internal_error(*args, **kwargs)

    def _domjudge_debug_payload_text(self, *args, **kwargs):
        return self._result._domjudge_debug_payload_text(*args, **kwargs)

    def domjudge_add_debug_info(self, *args, **kwargs):
        return self._result.domjudge_add_debug_info(*args, **kwargs)
