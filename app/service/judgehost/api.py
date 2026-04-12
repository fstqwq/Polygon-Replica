from __future__ import annotations

from pathlib import Path

from app.db import DB
from app.runtime_value import RuntimeValues
from app.service.disk.verification_store import VerificationStore
from app.service.platform.fs.layout import FsManager
from app.service.platform.judge_fs_index import JudgeFsIndexService
from app.service.repository.workspace import WorkspaceService
from app.setting import Settings

from .core import JudgehostCore
from .dispatch import DispatchHandler
from .enqueue import TaskEnqueue
from .result import ResultProcessor
from .state import JudgehostState
from .task_queue import TaskQueue
from .toolkit import DomjudgeToolkit


class Judgehost:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    CASE_CACHE_KIND = JudgeFsIndexService.KIND_CASE

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        fs_manager: FsManager,
        settings: Settings,
        constants: RuntimeValues,
        judge_fs_index_service: JudgeFsIndexService | None = None,
    ) -> None:
        _ = settings
        self.db = db
        self._state = JudgehostState(
            db=db,
            workspace_service=workspace_service,
            fs_manager=fs_manager,
            constants=constants,
            judge_fs_index_service=judge_fs_index_service,
            verification_store=VerificationStore(db),
        )
        self._toolkit = DomjudgeToolkit(self._state)
        self._core = JudgehostCore(self._state)
        self._queue = TaskQueue(self._state, self._core, self._toolkit)
        self._result = ResultProcessor(self._state, self._core, self._queue, self._toolkit)
        self._dispatch = DispatchHandler(self._state, self._core, self._queue, self._result, self._toolkit)
        self._enqueue = TaskEnqueue(self._state, self._core, self._dispatch, self._result, self._toolkit, self)
        self.apply_runtime_values(constants)

    @property
    def state(self) -> JudgehostState:
        return self._state

    # Compatibility state proxies for tests during transition.
    @property
    def _enabled(self) -> bool:
        return self._state.enabled

    @_enabled.setter
    def _enabled(self, value: bool) -> None:
        self._state.enabled = value

    @property
    def _api_token(self) -> str:
        return self._state.api_token

    @_api_token.setter
    def _api_token(self, value: str) -> None:
        self._state.api_token = value

    @property
    def _api_username(self) -> str:
        return self._state.api_username

    @_api_username.setter
    def _api_username(self, value: str) -> None:
        self._state.api_username = value

    @property
    def _constants(self) -> RuntimeValues:
        return self._state.constants

    @_constants.setter
    def _constants(self, value: RuntimeValues) -> None:
        self._state.constants = value

    @property
    def _include_build_payload(self) -> bool:
        return self._state.include_build_payload

    @_include_build_payload.setter
    def _include_build_payload(self, value: bool) -> None:
        self._state.include_build_payload = value

    @property
    def _max_tests_per_task(self) -> int:
        return self._state.max_tests_per_task

    @_max_tests_per_task.setter
    def _max_tests_per_task(self, value: int) -> None:
        self._state.max_tests_per_task = value

    @property
    def _state_lock(self):
        return self._state.state_lock

    @property
    def _tasks_by_id(self) -> dict[str, dict[str, object]]:
        return self._state.tasks_by_id

    @property
    def _judgehost_state_store(self):
        return self._state.judgehost_state_store

    # Compatibility helper forwards for tests during transition.
    def _task_by_id(self, task_id: str) -> dict[str, object]:
        return self._core.task_by_id(task_id)

    def _load_run_summary(self, run_id: str, verification_id: str = "") -> dict[str, object]:
        return self._queue.load_run_summary(run_id, verification_id)

    def _domjudge_work_root(self, task_id: str) -> Path:
        return self._toolkit.work_root(task_id)

    def _domjudge_b64_decode(self, text: str | bytes | bytearray | memoryview | None) -> bytes:
        return self._toolkit.b64_decode(text)

    def _domjudge_compile_script(self, *args, **kwargs) -> bytes:
        return self._toolkit.compile_script(*args, **kwargs)

    def _domjudge_compare_script(self, *args, **kwargs) -> bytes:
        return self._toolkit.compare_script(*args, **kwargs)

    def _domjudge_run_script(self, *args, **kwargs) -> bytes:
        return self._toolkit.run_script(*args, **kwargs)

    def _domjudge_cpp_executable_build_script(self, *args, **kwargs) -> bytes:
        return self._toolkit.cpp_executable_build_script(*args, **kwargs)

    def _domjudge_payload_blob_bytes(self, value: str | bytes | bytearray | memoryview | None) -> bytes:
        return self._toolkit.payload_blob_bytes(value)

    def _domjudge_precomputed_fields_from_payload(self, payload: dict[str, object]) -> dict[str, object]:
        return self._enqueue._domjudge_precomputed_fields_from_payload(payload)

    def _domjudge_prepare_job(self, hostname: str, task_row: dict[str, object]) -> int:
        return self._dispatch._domjudge_prepare_job(hostname, task_row)

    def _domjudge_release_prepared_job_for_queue(self, *args, **kwargs):
        return self._dispatch._domjudge_release_prepared_job_for_queue(*args, **kwargs)

    def _domjudge_lease_cases(self, *args, **kwargs):
        return self._dispatch._domjudge_lease_cases(*args, **kwargs)

    def _domjudge_try_prequeue_cache_finalize(self, *args, **kwargs):
        return self._dispatch._domjudge_try_prequeue_cache_finalize(*args, **kwargs)

    def _domjudge_strip_protocol_trace(self, text: str) -> str:
        return self._toolkit.strip_protocol_trace(text)

    # Public API delegation.
    def apply_runtime_values(self, constants: RuntimeValues) -> None:
        return self._core.apply_runtime_values(constants)

    def enabled(self) -> bool:
        return self._core.enabled()

    def auth_token_configured(self) -> bool:
        return self._core.auth_token_configured()

    def check_api_token(self, token: str) -> bool:
        return self._core.check_api_token(token)

    def api_username(self) -> str:
        return self._core.api_username()

    def check_api_basic(self, username: str, password: str) -> bool:
        return self._core.check_api_basic(username, password)

    def bind_request_peer_hostname(self, peer_addr: str, hostname: str) -> None:
        return self._core.bind_request_peer_hostname(peer_addr, hostname)

    def hostname_for_request_peer(self, peer_addr: str) -> str:
        return self._core.hostname_for_request_peer(peer_addr)

    def prepare_enqueue_payload(self, **kwargs) -> dict[str, object]:
        return self._enqueue.prepare_enqueue_payload(**kwargs)

    def enqueue_task(self, **kwargs) -> str:
        return self._enqueue.enqueue_task(**kwargs)

    def enqueue_compile_only_task(self, **kwargs) -> str:
        return self._enqueue.enqueue_compile_only_task(**kwargs)

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

    def resolve_artifact_blob(self, *args, **kwargs):
        return self._toolkit.resolve_artifact_blob(*args, **kwargs)

    def clear_testcase_registry(self, *args, **kwargs):
        return self._toolkit.clear_testcase_registry(*args, **kwargs)

    def domjudge_config(self, *args, **kwargs):
        return self._toolkit.config(*args, **kwargs)

    def domjudge_languages(self, *args, **kwargs):
        return self._toolkit.languages(*args, **kwargs)

    def domjudge_list_hosts(self, *args, **kwargs):
        return self._toolkit.list_hosts(*args, **kwargs)

    def domjudge_register_host(self, *args, **kwargs):
        return self._dispatch.domjudge_register_host(*args, **kwargs)

    def domjudge_fetch_work(self, *args, **kwargs):
        return self._dispatch.domjudge_fetch_work(*args, **kwargs)

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

    def domjudge_update_judging(self, *args, **kwargs):
        return self._result.domjudge_update_judging(*args, **kwargs)

    def domjudge_add_judging_run(self, *args, **kwargs):
        return self._result.domjudge_add_judging_run(*args, **kwargs)

    def domjudge_internal_error(self, *args, **kwargs):
        return self._result.domjudge_internal_error(*args, **kwargs)

    def domjudge_add_debug_info(self, *args, **kwargs):
        return self._result.domjudge_add_debug_info(*args, **kwargs)

    def run_submission(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
        upload_stream=None,
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        verification_id: str = "",
        verification_run_ids: list[str] | None = None,
        verification_source: str = "run.execute",
        expected_behavior: str | None = None,
        task_kind: str = "",
        force_recompile: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        if not self.enabled():
            raise RuntimeError("judgehost backend is disabled")
        if not self.auth_token_configured():
            raise RuntimeError("judgehost backend token is missing")
        if upload_stream is not None:
            raise RuntimeError("judgehost backend does not support upload_stream")
        task_id = self.enqueue_task(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_filename=upload_filename,
            run_id=run_id,
            selected_tests=selected_tests,
            verification_id=str(verification_id or ""),
            verification_run_ids=list(verification_run_ids or []),
            expected_behavior=str(expected_behavior or "unknown"),
            verification_source=str(verification_source or "run.execute"),
            task_kind=str(task_kind or ""),
            force_recompile=bool(force_recompile),
            prepared_payload=None if prepared_payload is None else dict(prepared_payload),
        )
        return self.wait_for_task(task_id, timeout_sec=None)

    def compile_only_submission(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        upload_content: bytes,
        upload_filename: str,
        run_id: str | None = None,
        verification_id: str = "",
        verification_run_ids: list[str] | None = None,
        verification_source: str = "compile.only",
        expected_behavior: str = "compile",
        prepared_payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not self.enabled():
            raise RuntimeError("judgehost backend is disabled")
        if not self.auth_token_configured():
            raise RuntimeError("judgehost backend token is missing")
        task_id = self.enqueue_compile_only_task(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            upload_content=bytes(upload_content),
            upload_filename=str(upload_filename or "submission.cpp"),
            run_id=str(run_id or ""),
            verification_id=str(verification_id or ""),
            verification_run_ids=list(verification_run_ids or []),
            expected_behavior=str(expected_behavior or "compile"),
            verification_source=str(verification_source or "compile.only"),
            prepared_payload=None if prepared_payload is None else dict(prepared_payload),
        )
        return self.wait_for_task_result(task_id, timeout_sec=None)

    def domjudge_case_output_for_task(self, task_id: str, test_name: str) -> tuple[str, Path | None, int]:
        row = self._state.judgehost_state_store.case_output_for_task(task_id, test_name)
        if row is None:
            return ("", None, 0)
        work_root = str(row["work_root"])
        case_id = int(row["id"])
        output_ref = str(row["output_run_rel"])
        if not work_root:
            return (output_ref, None, case_id)
        return (output_ref, Path(work_root).resolve(), case_id)

    def reset_runtime_state(self) -> None:
        with self._state.state_lock:
            self._state.tasks_by_id.clear()
            self._state.task_id_by_run.clear()
            self._state.hosts_state.clear()
            self._state.peer_hostname_by_client_addr.clear()
            self._state.host_judged_case_events.clear()
            self._state.host_last_judging.clear()
        with self._state.testcase_registry_lock:
            self._state.testcase_registry_by_hash.clear()
        self._state.judgehost_state_store.reset()
