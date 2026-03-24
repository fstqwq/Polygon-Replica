from __future__ import annotations

import threading
from pathlib import Path

from app.db import DB
from app.service.disk.verification_store import VerificationStore
from app.runtime_value import RuntimeValues
from app.service.memory.judgehost_state_store import JudgehostStateStore
from app.service.platform.judge_fs_index import JudgeFsIndexService
from app.service.platform.fs.layout import FsManager
from app.service.repository.workspace import WorkspaceService
from app.setting import Settings

from .internal.core import JudgehostCoreMixin
from .internal.domjudge_dispatch import JudgehostDomjudgeDispatchMixin
from .internal.domjudge_result import JudgehostDomjudgeResultsMixin
from .internal.domjudge_util import JudgehostDomjudgeUtilsMixin
from .internal.enqueue import JudgehostEnqueueMixin
from .internal.queue import JudgehostQueueMixin


class Judgehost(
    JudgehostCoreMixin,
    JudgehostEnqueueMixin,
    JudgehostQueueMixin,
    JudgehostDomjudgeUtilsMixin,
    JudgehostDomjudgeDispatchMixin,
    JudgehostDomjudgeResultsMixin,
):
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
        self.db = db
        self._workspace_service = workspace_service
        self._fs_manager = fs_manager
        self._settings = settings
        self._constants = constants
        self._lock = threading.Lock()
        self._enabled = False
        self._api_token = ""
        self._api_username = "judgehost"
        self._fetch_batch_size = 1
        self._lease_sec = 120
        self._wait_timeout_sec = 900
        self._wait_poll_sec = 0.5
        self._online_window_sec = 120
        self._max_source_bytes = 262144
        self._max_tests_per_task = 512
        self._max_test_payload_bytes = 1048576
        self._include_build_payload = True
        self._max_binary_payload_bytes = 8388608
        self._lease_requeue_lock = threading.Lock()
        self._lease_requeue_next_ts = 0.0
        self._testcase_registry_lock = threading.RLock()
        self._testcase_registry_by_hash: dict[str, dict[str, object]] = {}
        self._state_lock = threading.RLock()
        self._tasks_by_id: dict[str, dict[str, object]] = {}
        self._task_id_by_run: dict[str, str] = {}
        self._hosts_state: dict[str, dict[str, object]] = {}
        self._peer_hostname_by_client_addr: dict[str, str] = {}
        self._host_judged_case_events: dict[str, list[float]] = {}
        self._host_last_judging: dict[str, dict[str, str]] = {}
        self._judgehost_state_store = JudgehostStateStore()
        self._verification_store = VerificationStore(self.db)
        self._judge_fs_index_service = judge_fs_index_service
        self.apply_runtime_values(constants)

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

    def domjudge_work_root_for_task(self, task_id: str) -> Path | None:
        job_row = self._judgehost_state_store.job_for_task(task_id)
        if job_row is None:
            return None
        work_root = str(job_row["work_root"])
        if not work_root:
            return None
        return Path(work_root).resolve()

    def domjudge_case_output_for_task(self, task_id: str, test_name: str) -> tuple[str, Path | None, int]:
        row = self._judgehost_state_store.case_output_for_task(task_id, test_name)
        if row is None:
            return ("", None, 0)
        work_root = str(row["work_root"])
        case_id = int(row["id"])
        output_ref = str(row["output_run_rel"])
        if not work_root:
            return (output_ref, None, case_id)
        return (output_ref, Path(work_root).resolve(), case_id)

    def reset_runtime_state(self) -> None:
        with self._state_lock:
            self._tasks_by_id.clear()
            self._task_id_by_run.clear()
            self._hosts_state.clear()
            self._peer_hostname_by_client_addr.clear()
            self._host_judged_case_events.clear()
            self._host_last_judging.clear()
        with self._testcase_registry_lock:
            self._testcase_registry_by_hash.clear()
        self._judgehost_state_store.reset()
