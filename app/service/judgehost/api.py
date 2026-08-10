from __future__ import annotations

from datetime import datetime, timezone
import threading

from app.db import DB, now_iso
from app.runtime_value import RuntimeValues
from app.service.platform.admission import MaintenanceAdmissionGate
from app.service.platform.fs.layout import FsManager
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.repository.workspace import WorkspaceService
from app.service.verification.task_store import VerificationTaskStore
from app.setting import Settings

from app.service.judgehost.cleanup import JudgehostTerminalCleanup
from app.service.judgehost.completion import CaseCompletionSink, CaseDiagnosticSink
from app.service.judgehost.core import JudgehostCore
from app.service.judgehost.dispatch import DispatchHandler
from app.service.judgehost.enqueue import TaskEnqueue
from app.service.judgehost.public_status import PublicJudgehostStatus, PublicJudgehostStatusCache
from app.service.judgehost.result import ResultProcessor
from app.service.judgehost.runtime import parse_iso_utc
from app.service.judgehost.state import JudgehostState
from app.service.judgehost.task_queue import TaskQueue
from app.service.judgehost.toolkit import DomjudgeToolkit


class Judgehost:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    CASE_CACHE_KIND = RuntimeCacheIndex.RESULT

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        fs_manager: FsManager,
        settings: Settings,
        constants: RuntimeValues,
        *,
        case_completion_sink: CaseCompletionSink,
        case_diagnostic_sink: CaseDiagnosticSink,
        verification_task_store: VerificationTaskStore,
        runtime_blob_store: RuntimeBlobStore,
        runtime_cache_index: RuntimeCacheIndex,
    ) -> None:
        _ = settings
        self.db = db
        self._state = JudgehostState(
            db=db,
            workspace_service=workspace_service,
            fs_manager=fs_manager,
            constants=constants,
            runtime_blob_store=runtime_blob_store,
            runtime_cache_index=runtime_cache_index,
            verification_task_store=verification_task_store,
            case_completion_sink=case_completion_sink,
            case_diagnostic_sink=case_diagnostic_sink,
        )
        self._toolkit = DomjudgeToolkit(self._state)
        self._core = JudgehostCore(self._state)
        self._queue = TaskQueue(self._state, self._core)
        self._result = ResultProcessor(self._state, self._core, self._queue, self._toolkit)
        self._dispatch = DispatchHandler(self._state, self._core, self._queue, self._result, self._toolkit)
        self._enqueue = TaskEnqueue(self._state, self._core, self._dispatch, self._toolkit)
        self._terminal_cleanup = JudgehostTerminalCleanup(
            self._state.task_registry,
            self._state.batch_scheduler,
            self._state.verification_task_store,
        )
        self._public_status = PublicJudgehostStatusCache(
            lambda: self.status(),
            self._toolkit.public_compile_specs,
        )
        self._state.touch_verification_runtime = self._terminal_cleanup.touch
        self._admission_gate: MaintenanceAdmissionGate | None = None
        self._callback_count_lock = threading.Lock()
        self._active_callbacks = 0
        self.apply_runtime_values(constants)

    @property
    def state(self) -> JudgehostState:
        return self._state

    @property
    def toolkit(self) -> DomjudgeToolkit:
        return self._toolkit

    @property
    def core(self) -> JudgehostCore:
        return self._core

    @property
    def queue(self) -> TaskQueue:
        return self._queue

    @property
    def result(self) -> ResultProcessor:
        return self._result

    @property
    def dispatch(self) -> DispatchHandler:
        return self._dispatch

    @property
    def enqueue(self) -> TaskEnqueue:
        return self._enqueue

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

    def prepare_enqueue_payload(self, **kwargs) -> dict[str, object]:
        return self._enqueue.prepare_enqueue_payload(**kwargs)

    def prepare_execution_template(self, **kwargs) -> dict[str, object]:
        return self._enqueue.prepare_execution_template(**kwargs)

    def enqueue_task(self, **kwargs) -> str:
        gate = self._admission_gate
        if gate is None:
            return self._enqueue.enqueue_task(**kwargs)
        with gate.locked():
            self._require_admission_locked(gate)
            return self._enqueue.enqueue_task(**kwargs)

    def enqueue_compile_only_task(self, **kwargs) -> str:
        gate = self._admission_gate
        if gate is None:
            return self._enqueue.enqueue_compile_only_task(**kwargs)
        with gate.locked():
            self._require_admission_locked(gate)
            return self._enqueue.enqueue_compile_only_task(**kwargs)

    def set_admission_gate(self, gate: MaintenanceAdmissionGate | None) -> None:
        self._admission_gate = gate

    def enter_callback(self) -> bool:
        gate = self._admission_gate
        if gate is None:
            with self._callback_count_lock:
                self._active_callbacks += 1
            return True
        with gate.locked():
            if not gate.is_open_locked():
                return False
            with self._callback_count_lock:
                self._active_callbacks += 1
            return True

    def leave_callback(self) -> None:
        gate = self._admission_gate
        if gate is None:
            with self._callback_count_lock:
                if self._active_callbacks <= 0:
                    raise RuntimeError("judgehost callback counter underflow")
                self._active_callbacks -= 1
            return
        with gate.locked():
            with self._callback_count_lock:
                if self._active_callbacks <= 0:
                    raise RuntimeError("judgehost callback counter underflow")
                self._active_callbacks -= 1

    @staticmethod
    def _require_admission_locked(gate: MaintenanceAdmissionGate) -> None:
        if not gate.is_open_locked():
            raise RuntimeError("maintenance in progress: judgehost admission is closed")

    def busy_counts(self) -> dict[str, int]:
        counts = self._state.task_registry.maintenance_counts()
        with self._callback_count_lock:
            active_callbacks = self._active_callbacks
        return {
            "queued": int(counts.get("queued", 0)),
            "leased": int(counts.get("leased", 0)),
            "reporting": int(counts.get("reporting", 0)),
            "callbacks": active_callbacks,
        }

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

    def record_host_peer_addr(self, hostname: str, peer_addr: str) -> None:
        self._queue.record_host_peer_addr(hostname, peer_addr)

    def set_host_enabled(self, hostname: str, enabled: bool) -> dict[str, int]:
        release = self._queue.set_host_enabled(hostname, enabled)
        self._result.finalize_host_lease_release(release)
        return {
            "released_tasks": len(release.terminal_task_ids),
            "released_batches": release.affinity_count,
            "released_cases": release.lease_count,
        }

    def status(self, *args, **kwargs):
        return self._queue.status(*args, **kwargs)

    def public_status(self) -> PublicJudgehostStatus:
        return self._public_status.snapshot()

    def request_verification_cancel(self, verification_id: str, reason: str) -> dict[str, int]:
        if not verification_id:
            raise RuntimeError("verification id is required")
        if not reason:
            raise RuntimeError("judgehost cancellation reason is required")
        cancellation = self._state.batch_scheduler.request_verification_cancel(
            verification_id,
            now_text=now_iso(),
        )
        unbatched_count = self._queue.cancel_unbatched_verification_tasks(
            verification_id,
            reason=reason,
        )
        for task_id in cancellation.task_ids:
            batch_row = self._state.batch_scheduler.batch_for_task(task_id)
            if batch_row is not None:
                self._result._domjudge_finalize_task_if_ready(
                    task_id,
                    batch_row=dict(batch_row),
                )
        for batch_id in cancellation.batch_ids:
            self._result._domjudge_finalize_batch_if_ready(batch_id)
        return {
            "cancelled_cases": cancellation.cancelled_case_count,
            "awaiting_receipts": cancellation.awaiting_receipt_count,
            "affected_tasks": len(cancellation.task_ids) + unbatched_count,
            "affected_batches": len(cancellation.batch_ids),
        }

    def startup_cancel_inflight_tasks(self, *args, **kwargs):
        return self._queue.startup_cancel_inflight_tasks(*args, **kwargs)

    def forget_problem_tasks(self, *args, **kwargs):
        return self._queue.forget_problem_tasks(*args, **kwargs)

    def cancel_all_domjudge_batches(self) -> int:
        batch_ids = self._queue.cancel_all_domjudge_batches()
        for batch_id in batch_ids:
            self._result._domjudge_finalize_batch_if_ready(batch_id)
        return len(batch_ids)

    def forget_domjudge_runs(self, *args, **kwargs):
        return self._queue.forget_domjudge_runs(*args, **kwargs)

    def schedule_verification_cleanup(self, verification_id: str) -> None:
        ready_batch_ids = self._state.batch_scheduler.finish_verification_execution(
            verification_id,
            now_text=now_iso(),
        )
        for batch_id in ready_batch_ids:
            self._result._domjudge_finalize_batch_if_ready(batch_id)
        self._terminal_cleanup.schedule(verification_id)

    def close_programs(
        self,
        verification_id: str,
        verification_program_ids: list[str],
    ) -> None:
        ready_batch_ids = self._state.batch_scheduler.finish_programs(
            verification_id,
            verification_program_ids,
            now_text=now_iso(),
        )
        for batch_id in ready_batch_ids:
            self._result._domjudge_finalize_batch_if_ready(batch_id)

    def reconcile_expired_verification_leases(self, verification_id: str) -> list[str]:
        if not verification_id:
            return []
        now_dt = datetime.now(timezone.utc)
        now_text = now_iso()
        with self._state.state_lock:
            stale_hosts: list[str] = []
            for hostname, row in self._state.hosts_state.items():
                seen_at = parse_iso_utc(row.get("last_seen_at"))
                if seen_at is None:
                    continue
                if (now_dt - seen_at).total_seconds() > float(self._state.online_window_sec):
                    stale_hosts.append(str(hostname))
        released_task_ids: set[str] = set()
        for hostname in stale_hosts:
            selected_task_ids: set[str] = set()
            for case in self._state.batch_scheduler.cases_for_host(hostname):
                if str(case["status"] or "") != "leased":
                    continue
                batch = self._state.batch_scheduler.fetch_batch(int(case["batch_id"]))
                if batch is None or str(batch["verification_id"] or "") != verification_id:
                    continue
                task_id = str(case["task_id"] or "")
                if task_id:
                    selected_task_ids.add(task_id)
            if not selected_task_ids:
                continue
            release = self._state.batch_scheduler.release_host_leases(
                hostname,
                now_text=now_text,
                verification_id=verification_id,
            )
            self._result.finalize_host_lease_release(release)
            released_task_ids.update(selected_task_ids)
        return sorted(released_task_ids)

    def touch_verification_runtime(self, verification_id: str) -> None:
        self._terminal_cleanup.touch(verification_id)

    def resolve_artifact_blob(self, *args, **kwargs):
        return self._toolkit.resolve_artifact_blob(*args, **kwargs)

    def domjudge_config(self, *args, **kwargs):
        return self._toolkit.config(*args, **kwargs)

    def domjudge_languages(self, *args, **kwargs):
        return self._toolkit.languages(*args, **kwargs)

    def domjudge_list_hosts(self, *args, **kwargs):
        return self._toolkit.list_hosts(*args, **kwargs)

    def domjudge_register_host(self, *args, **kwargs):
        return self._dispatch.domjudge_register_host(*args, **kwargs)

    def domjudge_fetch_work(self, *args, **kwargs):
        gate = self._admission_gate
        if gate is None:
            return self._dispatch.domjudge_fetch_work(*args, **kwargs)
        return self._dispatch.domjudge_fetch_work(
            *args,
            admission_gate=gate,
            **kwargs,
        )

    def probe_task_case_cache(self, task_ids: list[str]) -> set[str]:
        return self._dispatch.probe_task_case_cache(task_ids)

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
        verification_program_id: str,
        verification_source: str = "run.execute",
        expected_behavior: str | None = None,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
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
            verification_program_id=verification_program_id,
            expected_behavior=str(expected_behavior or "unknown"),
            verification_source=str(verification_source or "run.execute"),
            task_kind=str(task_kind or ""),
            bypass_case_result_cache=bool(bypass_case_result_cache),
            prepared_payload=None if prepared_payload is None else dict(prepared_payload),
            service_class="foreground",
        )
        task = self._state.task_registry.get(task_id)
        runtime_verification_id = "" if task is None else str(task["verification_id"])
        try:
            return self.wait_for_task(task_id, timeout_sec=None)
        finally:
            self.schedule_verification_cleanup(runtime_verification_id)

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
        verification_program_id: str,
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
            verification_program_id=verification_program_id,
            expected_behavior=str(expected_behavior or "compile"),
            verification_source=str(verification_source or "compile.only"),
            prepared_payload=None if prepared_payload is None else dict(prepared_payload),
        )
        task = self._state.task_registry.get(task_id)
        runtime_verification_id = "" if task is None else str(task["verification_id"])
        try:
            return self.wait_for_task_result(task_id, timeout_sec=None)
        finally:
            self.schedule_verification_cleanup(runtime_verification_id)

    def domjudge_case_output_for_task(self, task_id: str, test_name: str) -> tuple[str, int]:
        row = self._state.batch_scheduler.case_output_for_task(task_id, test_name)
        if row is None:
            return ("", 0)
        case_id = int(row["id"])
        output_ref = str(row["output_run_ref"])
        return (output_ref, case_id)

    def domjudge_case_feedback_blob_for_task(self, task_id: str, test_name: str) -> bytes | None:
        row = self._state.batch_scheduler.case_for_task(task_id, test_name)
        if row is None:
            return None
        output_diff_ref = str(row["output_diff_ref"] or "")
        if not output_diff_ref:
            return None
        return self.resolve_artifact_blob(output_diff_ref)

    def reset_runtime_state(self) -> None:
        self._terminal_cleanup.reset()
        self._state.task_registry.reset()
        with self._state.state_lock:
            self._state.hosts_state.clear()
            self._state.host_toolchains.clear()
        self._state.batch_scheduler.reset()
