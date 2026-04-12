from __future__ import annotations

from pathlib import Path
from app.db import DB
from app.service.disk.verification_store import VerificationStore
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
from .core import JudgehostCore
from .state import JudgehostState
from .toolkit import DomjudgeToolkit


class Judgehost(
    JudgehostCoreMixin,
    JudgehostEnqueueMixin,
    JudgehostQueueMixin,
    JudgehostDomjudgeUtilsMixin,
    JudgehostDomjudgeDispatchMixin,
    JudgehostDomjudgeResultsMixin,
):
    _STATE_ATTR_MAP = {
        "_workspace_service": "workspace_service",
        "_fs_manager": "fs_manager",
        "_constants": "constants",
        "_lock": "lock",
        "_enabled": "enabled",
        "_api_token": "api_token",
        "_api_username": "api_username",
        "_fetch_batch_size": "fetch_batch_size",
        "_lease_sec": "lease_sec",
        "_wait_timeout_sec": "wait_timeout_sec",
        "_wait_poll_sec": "wait_poll_sec",
        "_online_window_sec": "online_window_sec",
        "_max_source_bytes": "max_source_bytes",
        "_max_tests_per_task": "max_tests_per_task",
        "_include_build_payload": "include_build_payload",
        "_max_binary_payload_bytes": "max_binary_payload_bytes",
        "_lease_requeue_lock": "lease_requeue_lock",
        "_lease_requeue_next_ts": "lease_requeue_next_ts",
        "_testcase_registry_lock": "testcase_registry_lock",
        "_testcase_registry_by_hash": "testcase_registry_by_hash",
        "_state_lock": "state_lock",
        "_tasks_by_id": "tasks_by_id",
        "_task_id_by_run": "task_id_by_run",
        "_hosts_state": "hosts_state",
        "_peer_hostname_by_client_addr": "peer_hostname_by_client_addr",
        "_host_judged_case_events": "host_judged_case_events",
        "_host_last_judging": "host_last_judging",
        "_judgehost_state_store": "judgehost_state_store",
        "_verification_store": "verification_store",
        "_judge_fs_index_service": "judge_fs_index_service",
    }

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
        object.__setattr__(self, "db", db)
        object.__setattr__(
            self,
            "_state",
            JudgehostState(
                db=db,
                workspace_service=workspace_service,
                fs_manager=fs_manager,
                constants=constants,
                judge_fs_index_service=judge_fs_index_service,
                verification_store=VerificationStore(db),
            ),
        )
        object.__setattr__(self, "_toolkit", DomjudgeToolkit(self._state))
        object.__setattr__(self, "_core", JudgehostCore(self._state))
        self.apply_runtime_values(constants)

    def __getattribute__(self, name: str):
        state_attr_map = object.__getattribute__(self, "_STATE_ATTR_MAP")
        if name in state_attr_map:
            instance_dict = object.__getattribute__(self, "__dict__")
            state = instance_dict.get("_state")
            if state is not None:
                return getattr(state, state_attr_map[name])
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value) -> None:
        state_attr_map = type(self)._STATE_ATTR_MAP
        if name in state_attr_map and "_state" in self.__dict__:
            setattr(self._state, state_attr_map[name], value)
            return
        object.__setattr__(self, name, value)

    @property
    def state(self) -> JudgehostState:
        return self._state

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
