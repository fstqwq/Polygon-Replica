from __future__ import annotations

import sqlite3
import threading

from app.db import DB
from app.runtime_value import RuntimeValues
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

JUDGEHOST_BACKEND_NAME = "domjudge-judgehost"


class Judgehost(
    JudgehostCoreMixin,
    JudgehostEnqueueMixin,
    JudgehostQueueMixin,
    JudgehostDomjudgeUtilsMixin,
    JudgehostDomjudgeDispatchMixin,
    JudgehostDomjudgeResultsMixin,
):
    BACKEND_NAME = JUDGEHOST_BACKEND_NAME
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    CASE_CACHE_KIND = JudgeFsIndexService.KIND_CASE
    SOLVE_OUTPUT_CACHE_KIND = JudgeFsIndexService.KIND_SOLVE_OUTPUT

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
        self._testcase_registry_next_id = 1
        self._testcase_registry_by_hash: dict[str, dict[str, object]] = {}
        self._testcase_registry_by_id: dict[int, dict[str, object]] = {}
        self._state_lock = threading.RLock()
        self._tasks_by_id: dict[str, dict[str, object]] = {}
        self._task_id_by_run: dict[str, str] = {}
        self._hosts_state: dict[str, dict[str, object]] = {}
        self._host_judged_case_events: dict[str, list[float]] = {}
        self._host_last_judging: dict[str, dict[str, str]] = {}
        self._domdb_lock = threading.RLock()
        self._domdb = sqlite3.connect(":memory:", check_same_thread=False)
        self._domdb.row_factory = sqlite3.Row
        self._init_domdb_schema()
        self._judge_fs_index_service = judge_fs_index_service
        self.apply_runtime_values(constants)

    @classmethod
    def backend_name(cls) -> str:
        return cls.BACKEND_NAME

    def backend_status(self) -> dict[str, object]:
        status = self.status()
        queue = status.get("queue") if isinstance(status, dict) else {}
        queue_text = ""
        if isinstance(queue, dict):
            queue_text = f"queue queued={int(queue.get('queued', 0))}, leased={int(queue.get('leased', 0))}"
        ready = bool(self.enabled() and self.auth_token_configured())
        if not self.enabled():
            detail = "judgehost service disabled"
        elif not self.auth_token_configured():
            detail = "set JUDGEHOST_API_TOKEN in system config"
        else:
            detail = f"judgehost queue ready ({queue_text})".strip()
        return {
            "configured": self.BACKEND_NAME,
            "active": self.BACKEND_NAME,
            "available": [
                {
                    "name": self.BACKEND_NAME,
                    "ready": ready,
                    "detail": detail,
                }
            ],
        }

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
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
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
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )
        return self.wait_for_task_result(task_id, timeout_sec=None)

