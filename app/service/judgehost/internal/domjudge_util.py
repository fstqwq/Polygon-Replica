from __future__ import annotations

from app.service.judgehost.toolkit import DomjudgeToolkit


class JudgehostDomjudgeUtilsMixin:
    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _TASK_KIND_GENERATE_INPUT = "generate-input"
    _TASK_KIND_MAIN_CORRECT = "main-correct"
    _TASK_KIND_SOLUTION_RUN = "solution-run"
    _TASK_KIND_SET = {
        _TASK_KIND_COMPILE_ONLY,
        _TASK_KIND_GENERATE_INPUT,
        _TASK_KIND_MAIN_CORRECT,
        _TASK_KIND_SOLUTION_RUN,
    }

    @staticmethod
    def _domjudge_contest_id(*args, **kwargs):
        return DomjudgeToolkit._domjudge_contest_id(*args, **kwargs)

    def _domjudge_work_root(self, *args, **kwargs):
        return self._toolkit.work_root(*args, **kwargs)

    @staticmethod
    def _domjudge_b64_decode(*args, **kwargs):
        return DomjudgeToolkit._domjudge_b64_decode(*args, **kwargs)

    @staticmethod
    def _domjudge_payload_blob_bytes(*args, **kwargs):
        return DomjudgeToolkit._domjudge_payload_blob_bytes(*args, **kwargs)

    def _domjudge_manifest_from_files(self, *args, **kwargs):
        return self._toolkit.manifest_from_files(*args, **kwargs)

    def _domjudge_validate_cache_entry(self, *args, **kwargs):
        return self._toolkit.validate_cache_entry(*args, **kwargs)

    def _domjudge_case_cache_ref(self, *args, **kwargs):
        return self._toolkit.case_cache_ref(*args, **kwargs)

    def _domjudge_cache_get(self, *args, **kwargs):
        return self._toolkit.cache_get(*args, **kwargs)

    def _domjudge_cache_put(self, *args, **kwargs):
        return self._toolkit.cache_put(*args, **kwargs)

    def _domjudge_cache_delete(self, *args, **kwargs):
        return self._toolkit.cache_delete(*args, **kwargs)

    def _domjudge_cache_read_blob(self, *args, **kwargs):
        return self._toolkit.cache_read_blob(*args, **kwargs)

    @staticmethod
    def _domjudge_cache_blob_ref(*args, **kwargs):
        return DomjudgeToolkit._domjudge_cache_blob_ref(*args, **kwargs)

    def _domjudge_build_cached_case(self, *args, **kwargs):
        return self._toolkit.build_cached_case(*args, **kwargs)

    def _domjudge_store_case_cache(self, *args, **kwargs):
        return self._toolkit.store_case_cache(*args, **kwargs)

    def _domjudge_set_hash_from_blobs(self, *args, **kwargs):
        return self._toolkit.set_hash_from_blobs(*args, **kwargs)

    def _domjudge_read_artifact_blob(self, *args, **kwargs):
        return self._toolkit.read_artifact_blob(*args, **kwargs)

    def resolve_artifact_blob(self, *args, **kwargs):
        return self._toolkit.resolve_artifact_blob(*args, **kwargs)

    @staticmethod
    def _domjudge_strip_protocol_trace(*args, **kwargs):
        return DomjudgeToolkit._domjudge_strip_protocol_trace(*args, **kwargs)

    @staticmethod
    def _domjudge_force_cpp_define(*args, **kwargs):
        return DomjudgeToolkit._domjudge_force_cpp_define(*args, **kwargs)

    @staticmethod
    def _domjudge_ensure_bytes_file(*args, **kwargs):
        return DomjudgeToolkit._domjudge_ensure_bytes_file(*args, **kwargs)

    def _domjudge_testcase_blob_ref(self, *args, **kwargs):
        return self._toolkit.testcase_blob_ref(*args, **kwargs)

    def clear_testcase_registry(self, *args, **kwargs):
        return self._toolkit.clear_testcase_registry(*args, **kwargs)

    def _domjudge_register_cached_testcase(self, *args, **kwargs):
        return self._toolkit.register_cached_testcase(*args, **kwargs)

    @staticmethod
    def _domjudge_language_extensions(*args, **kwargs):
        return DomjudgeToolkit._domjudge_language_extensions(*args, **kwargs)

    @staticmethod
    def _domjudge_shell_words(*args, **kwargs):
        return DomjudgeToolkit._domjudge_shell_words(*args, **kwargs)

    @staticmethod
    def _domjudge_shell_tokens(*args, **kwargs):
        return DomjudgeToolkit._domjudge_shell_tokens(*args, **kwargs)

    def _domjudge_toolchain_cmd_digest(self, *args, **kwargs):
        return self._toolkit.toolchain_cmd_digest(*args, **kwargs)

    def _domjudge_load_script_asset(self, *args, **kwargs):
        return self._toolkit.load_script_asset(*args, **kwargs)

    @staticmethod
    def _domjudge_render_script_template(*args, **kwargs):
        return DomjudgeToolkit._domjudge_render_script_template(*args, **kwargs)

    def _domjudge_compile_script(self, *args, **kwargs):
        return self._toolkit.compile_script(*args, **kwargs)

    def _domjudge_cpp_executable_build_script(self, *args, **kwargs):
        return self._toolkit.cpp_executable_build_script(*args, **kwargs)

    def _domjudge_task_kind(self, *args, **kwargs):
        return self._toolkit.task_kind(*args, **kwargs)

    def _domjudge_group_key(self, *args, **kwargs):
        return self._toolkit.group_key(*args, **kwargs)

    def _domjudge_is_grouped_verification_task(self, *args, **kwargs):
        return self._toolkit.is_grouped_verification_task(*args, **kwargs)

    def _domjudge_execution_modes(self, *args, **kwargs):
        return self._toolkit.execution_modes(*args, **kwargs)

    def _domjudge_run_script(self, *args, **kwargs):
        return self._toolkit.run_script(*args, **kwargs)

    def _domjudge_compare_script(self, *args, **kwargs):
        return self._toolkit.compare_script(*args, **kwargs)

    def domjudge_config(self, *args, **kwargs):
        return self._toolkit.config(*args, **kwargs)

    @staticmethod
    def domjudge_languages(*args, **kwargs):
        return DomjudgeToolkit.domjudge_languages(*args, **kwargs)

    def domjudge_list_hosts(self, *args, **kwargs):
        return self._toolkit.list_hosts(*args, **kwargs)
