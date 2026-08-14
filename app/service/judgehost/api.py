import secrets
import threading
from typing import BinaryIO, TypeVar

from app.db import now_iso
from app.config import ConfigValues
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.repository.workspace import WorkspaceService

from app.service.judgehost.maintenance.terminal_cleanup import JudgehostTerminalCleanup
from app.service.judgehost.maintenance.service import JudgehostMaintenance
from app.service.judgehost.ports.execution_port import JudgehostExecutionPort
from app.service.judgehost.configuration import JudgehostConfiguration
from app.service.judgehost.host.registry import JudgehostHostRegistry
from app.service.judgehost.host.status import JudgehostHostStatus
from app.service.judgehost.host.toolchain_versions import ToolchainTelemetryHandler
from app.service.judgehost.host.version_callback import JudgehostVersionCallback
from app.service.judgehost.cache.case_result import CaseResultCache
from app.service.judgehost.cache.executable import ExecutableCache
from app.service.judgehost.dispatch.materializer import BatchPayloadMaterializer
from app.service.judgehost.dispatch.service import JudgehostDispatch
from app.service.judgehost.task.admission import JudgehostTaskAdmission
from app.service.judgehost.task.batch_admission import TaskBatchAdmission
from app.service.judgehost.task.preparation import JudgehostPayloadPreparation
from app.service.judgehost.finalization.service import JudgehostBatchFinalizer
from app.service.judgehost.finalization.publication import (
    JudgehostCaseCompletionPublisher,
    JudgehostCaseDiagnosticPublisher,
)
from app.service.judgehost.host.public_status import (
    PublicJudgehostStatus,
    PublicJudgehostStatusCache,
)
from app.service.judgehost.callback.result import JudgehostCallbackIngestion
from app.service.judgehost.callback.model import CallbackOutcome
from app.service.judgehost.domjudge.file_stream import DomjudgeDownloadFile
from app.service.judgehost.domjudge.files import DomjudgeFileService
from app.service.judgehost.ports.completion import CaseTerminalReport
from app.service.judgehost.task.query import JudgehostTaskQuery, TaskPollResult
from app.service.judgehost.task.registry import JudgehostTaskRegistry, JudgehostTaskRow
from app.service.judgehost.finalization.terminalization import JudgehostTaskTerminalization
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.batch.model import ExecutionBatchRow, JudgehostCaseRow
from app.service.judgehost.domjudge.wire import DomjudgeWireProjector
from app.service.judgehost.domjudge.scripts import DomjudgeScriptCatalog

Acknowledgement = TypeVar("Acknowledgement")


class Judgehost:
    def __init__(
        self,
        workspace_service: WorkspaceService,
        config_values: ConfigValues,
        *,
        execution_port: JudgehostExecutionPort,
        runtime_blob_store: RuntimeBlobStore,
        runtime_cache_index: RuntimeCacheIndex,
    ) -> None:
        self._workspace_service = workspace_service
        self._runtime_blob_store = runtime_blob_store
        self._execution_port = execution_port
        self._tasks = JudgehostTaskRegistry()
        self._batch_runtime = JudgehostBatchRuntime()
        self._configuration = JudgehostConfiguration(config_values)
        self._hosts = JudgehostHostRegistry()
        self._wire = DomjudgeWireProjector()
        self._scripts = DomjudgeScriptCatalog()
        self._case_result_cache = CaseResultCache(
            runtime_cache_index, runtime_blob_store
        )
        self._executable_cache = ExecutableCache(runtime_cache_index)
        self._materializer = BatchPayloadMaterializer(
            runtime_blob_store,
            self._executable_cache,
        )
        self._files = DomjudgeFileService(
            self._batch_runtime,
            self._runtime_blob_store,
            self._executable_cache,
        )
        self._version_callback = JudgehostVersionCallback(
            self._batch_runtime,
            ToolchainTelemetryHandler(
                self._batch_runtime,
                self._configuration,
                self._hosts,
            ),
        )
        self._task_query = JudgehostTaskQuery(
            self._tasks,
            self._batch_runtime,
            self._configuration,
        )
        self._task_terminalization = JudgehostTaskTerminalization(self._tasks)
        self._host_status = JudgehostHostStatus(
            self._hosts,
            self._tasks,
            self._batch_runtime,
        )
        self._maintenance = JudgehostMaintenance(
            self._tasks,
            self._batch_runtime,
            self._hosts,
            self._configuration,
        )
        self._terminal_cleanup = JudgehostTerminalCleanup(
            self._tasks,
            self._batch_runtime,
            self._execution_port,
        )
        diagnostic_publisher = JudgehostCaseDiagnosticPublisher(
            self._batch_runtime,
            self._execution_port,
        )
        completion_publisher = JudgehostCaseCompletionPublisher(
            self._batch_runtime,
            self._execution_port,
            self._tasks,
            diagnostic_publisher,
        )
        self._batch_finalizer = JudgehostBatchFinalizer(
            self._batch_runtime,
            self._tasks,
            self._configuration,
            self._task_terminalization,
            completion_publisher,
        )
        self._result = JudgehostCallbackIngestion(
            self._batch_runtime,
            self._tasks,
            self._configuration,
            self._runtime_blob_store,
            self._scripts,
            case_result_cache=self._case_result_cache,
        )
        self._dispatch = JudgehostDispatch(
            self._batch_runtime,
            self._tasks,
            self._execution_port,
            self._scripts,
            self._case_result_cache,
            self._materializer,
            self._configuration,
            self._hosts,
            5.0,
        )
        self._task_batch_admission = TaskBatchAdmission(
            self._batch_runtime,
            self._tasks,
        )
        self._payload_preparation = JudgehostPayloadPreparation(
            self._workspace_service,
            self._runtime_blob_store,
            self._execution_port,
            self._scripts,
            self._configuration,
        )
        self._enqueue = JudgehostTaskAdmission(
            self._payload_preparation,
            self._execution_port,
            self._batch_runtime,
            self._tasks,
            self._task_batch_admission,
        )
        self._public_status = PublicJudgehostStatusCache(
            self._public_status_sources,
        )
        self._admission_gate: MaintenanceAdmissionGate | None = None
        self._callback_count_lock = threading.Lock()
        self._active_callbacks = 0

    # Public API delegation.
    def enabled(self) -> bool:
        return self._configuration.snapshot().enabled

    def auth_token_configured(self) -> bool:
        return bool(self._configuration.snapshot().api_token)

    def check_api_token(self, token: str) -> bool:
        expected = self._configuration.snapshot().api_token
        provided = str(token or "").strip()
        return bool(
            expected and provided and secrets.compare_digest(expected, provided)
        )

    def api_username(self) -> str:
        return self._configuration.snapshot().api_username or "judgehost"

    def check_api_basic(self, username: str, password: str) -> bool:
        settings = self._configuration.snapshot()
        provided_user = str(username or "").strip()
        provided_password = str(password or "").strip()
        return bool(
            provided_user
            and provided_password
            and provided_user == (settings.api_username or "judgehost")
            and settings.api_token
            and secrets.compare_digest(settings.api_token, provided_password)
        )

    def prepare_enqueue_payload(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_file: PayloadFile | None = None,
        upload_filename: str | None,
        run_id: str,
        selected_tests: list[str] | None,
        verification_id: str,
        verification_task_id: str = "",
        verification_program_id: str,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
        verification_payload_override: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._payload_preparation.prepare_enqueue_payload(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_file=upload_file,
            upload_filename=upload_filename,
            run_id=run_id,
            selected_tests=selected_tests,
            verification_id=verification_id,
            verification_task_id=verification_task_id,
            verification_program_id=verification_program_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            bypass_case_result_cache=bypass_case_result_cache,
            compile_only=compile_only,
            verification_payload_override=verification_payload_override,
        )

    def prepare_execution_template(
        self,
        *,
        mode: str,
        upload_file: PayloadFile,
        upload_filename: str,
        verification_payload: dict[str, object],
        expected_behavior: str,
        verification_source: str,
        task_kind: str,
        extra_source_files: dict[str, PayloadFile] | None = None,
        manual_validate_only: bool = False,
        compile_only: bool = False,
    ) -> dict[str, object]:
        return self._payload_preparation.prepare_execution_template(
            mode=mode,
            upload_file=upload_file,
            upload_filename=upload_filename,
            verification_payload=verification_payload,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            extra_source_files=extra_source_files,
            manual_validate_only=manual_validate_only,
            compile_only=compile_only,
        )

    def enqueue_task(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_file: PayloadFile | None = None,
        upload_filename: str | None,
        run_id: str | None = None,
        selected_tests: list[str] | None,
        verification_id: str,
        verification_task_id: str = "",
        verification_program_id: str,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
        persist_verification_run: bool = False,
        prepared_payload: dict[str, object] | None = None,
        execution_template: dict[str, object] | None = None,
        service_class: str = "background",
    ) -> str:
        return self._enqueue.enqueue_task(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_file=upload_file,
            upload_filename=upload_filename,
            run_id=run_id,
            selected_tests=selected_tests,
            verification_id=verification_id,
            verification_task_id=verification_task_id,
            verification_program_id=verification_program_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            bypass_case_result_cache=bypass_case_result_cache,
            compile_only=compile_only,
            persist_verification_run=persist_verification_run,
            prepared_payload=prepared_payload,
            execution_template=execution_template,
            service_class=service_class,
            admission_gate=self._admission_gate,
        )

    def enqueue_compile_only_task(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        upload_content: bytes,
        upload_filename: str,
        run_id: str,
        verification_id: str,
        verification_program_id: str,
        expected_behavior: str = "compile",
        verification_source: str = "compile.only",
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        return self._enqueue.enqueue_compile_only_task(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            upload_content=upload_content,
            upload_filename=upload_filename,
            run_id=run_id,
            verification_id=verification_id,
            verification_program_id=verification_program_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            prepared_payload=prepared_payload,
            admission_gate=self._admission_gate,
        )

    def set_admission_gate(self, gate: MaintenanceAdmissionGate | None) -> None:
        self._admission_gate = gate

    def enter_callback(self) -> bool:
        gate = self._admission_gate
        if gate is None:
            with self._callback_count_lock:
                self._active_callbacks += 1
            return True
        with gate.locked():
            if not gate.allows_runtime_work_locked():
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

    def busy_counts(self) -> dict[str, int]:
        counts = self._tasks.maintenance_counts()
        attempts = self._batch_runtime.activity_counts()
        with self._callback_count_lock:
            active_callbacks = self._active_callbacks
        return {
            "queued": int(counts.get("queued", 0)),
            "leased": max(int(counts.get("leased", 0)), attempts["leases"]),
            "reporting": max(
                int(counts.get("reporting", 0)),
                attempts["reporting"],
            ),
            "callbacks": max(active_callbacks, attempts["callbacks"]),
            "cache_probes": attempts["cache_probes"],
            "materializations": attempts["materializations"],
            "finalizations": attempts["finalizations"],
        }

    def wait_for_task_result(
        self,
        task_id: str,
        timeout_sec: float | None = None,
    ) -> TaskPollResult:
        return self._task_query.wait_for_task_result(task_id, timeout_sec)

    def poll_task_result(self, task_id: str) -> TaskPollResult | None:
        return self._task_query.poll_task_result(task_id)

    def wait_for_task_case_result(
        self,
        task_id: str,
        test_name: str,
        timeout_sec: float | None = None,
    ) -> CaseTerminalReport:
        return self._task_query.wait_for_task_case_result(
            task_id,
            test_name,
            timeout_sec,
        )

    def poll_task_case_result(
        self,
        task_id: str,
        test_name: str,
    ) -> CaseTerminalReport | None:
        return self._task_query.poll_task_case_result(task_id, test_name)

    def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
        return self._task_query.wait_for_task(task_id, timeout_sec)

    def record_host_peer_addr(self, hostname: str, peer_addr: str) -> None:
        self._host_status.record_peer(hostname, peer_addr)

    def set_host_enabled(self, hostname: str, enabled: bool) -> dict[str, int]:
        release = self._host_status.set_enabled(hostname, enabled)
        self._batch_finalizer.finalize_host_lease_release(release)
        return {
            "released_tasks": len(release.terminal_task_ids),
            "released_batches": release.affinity_count,
            "released_cases": release.lease_count,
        }

    def status(self) -> dict[str, object]:
        return self._host_status.status(self._configuration.snapshot())

    def _public_status_sources(
        self,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        settings = self._configuration.snapshot()
        return (
            self._host_status.status(settings),
            self._scripts.public_compile_specs(settings),
        )

    def public_status(self) -> PublicJudgehostStatus:
        return self._public_status.snapshot()

    def request_verification_cancel(
        self, verification_id: str, reason: str
    ) -> dict[str, int]:
        if not verification_id:
            raise RuntimeError("verification id is required")
        if not reason:
            raise RuntimeError("judgehost cancellation reason is required")
        cancellation = self._batch_runtime.request_verification_cancel(
            verification_id,
            now_text=now_iso(),
        )
        unbatched_count = self._maintenance.cancel_unbatched_verification_tasks(
            verification_id,
            reason=reason,
        )
        for task_id in cancellation.task_ids:
            batch_row = self._batch_runtime.batch_for_task(task_id)
            if batch_row is not None:
                self._batch_finalizer.finalize_task_if_ready(
                    task_id,
                    batch_row=batch_row,
                )
        for batch_id in cancellation.batch_ids:
            self._batch_finalizer.finalize_batch_if_ready(batch_id)
        return {
            "cancelled_cases": cancellation.cancelled_case_count,
            "awaiting_receipts": cancellation.awaiting_receipt_count,
            "affected_tasks": len(cancellation.task_ids) + unbatched_count,
            "affected_batches": len(cancellation.batch_ids),
        }

    def startup_cancel_inflight_tasks(self, *, reason: str) -> list[dict[str, str]]:
        return self._maintenance.startup_cancel_inflight_tasks(reason=reason)

    def forget_problem_tasks(self, problem_slug: str) -> int:
        return self._maintenance.forget_problem_tasks(problem_slug)

    def problem_run_ids(self, problem_slug: str) -> list[str]:
        return sorted(
            {
                row["run_id"]
                for row in self._tasks.snapshots()
                if row["problem_slug"] == problem_slug and row["run_id"]
            }
        )

    def case_snapshot(self, case_id: int) -> JudgehostCaseRow | None:
        return self._batch_runtime.fetch_case(case_id)

    def batch_snapshot(self, batch_id: int) -> ExecutionBatchRow | None:
        return self._batch_runtime.fetch_batch(batch_id)

    def run_case_snapshots(self, run_id: str) -> list[JudgehostCaseRow]:
        return self._batch_runtime.cases_for_run(run_id)

    def task_snapshot_for_run(self, run_id: str) -> JudgehostTaskRow | None:
        return self._tasks.get_for_run(run_id)

    def run_summary(self, run_id: str, verification_id: str = "") -> dict[str, object]:
        return self._task_query.load_run_summary(run_id, verification_id)

    def cancel_all_batches(self) -> int:
        batch_ids = self._maintenance.cancel_all_batches()
        for batch_id in batch_ids:
            self._batch_finalizer.finalize_batch_if_ready(batch_id)
        return len(batch_ids)

    def forget_domjudge_runs(self, run_ids: list[str]) -> int:
        return self._maintenance.forget_runs(run_ids)

    def schedule_verification_cleanup(self, verification_id: str) -> None:
        ready_batch_ids = self._batch_runtime.finish_verification_execution(
            verification_id,
            now_text=now_iso(),
        )
        for batch_id in ready_batch_ids:
            self._batch_finalizer.finalize_batch_if_ready(batch_id)
        self._terminal_cleanup.schedule(verification_id)

    def close_programs(
        self,
        verification_id: str,
        verification_program_ids: list[str],
    ) -> None:
        ready_batch_ids = self._batch_runtime.finish_programs(
            verification_id,
            verification_program_ids,
            now_text=now_iso(),
        )
        for batch_id in ready_batch_ids:
            self._batch_finalizer.finalize_batch_if_ready(batch_id)

    def reconcile_expired_verification_leases(self, verification_id: str) -> list[str]:
        outcome = self._maintenance.reconcile_expired_leases(verification_id)
        for release in outcome.releases:
            self._batch_finalizer.finalize_host_lease_release(release)
        return list(outcome.released_task_ids)

    def touch_verification_runtime(self, verification_id: str) -> None:
        self._terminal_cleanup.touch(verification_id)

    def resolve_artifact_blob(self, token: str) -> bytes | None:
        descriptor = self._runtime_blob_store.descriptor(token)
        if descriptor is None:
            return None
        return self._runtime_blob_store.read(descriptor)

    def domjudge_config(self) -> dict[str, object]:
        return self._wire.configuration(self._configuration.snapshot())

    def domjudge_languages(self) -> list[dict[str, object]]:
        return self._wire.languages()

    def domjudge_list_hosts(self) -> list[dict[str, object]]:
        return self._wire.hosts(self._hosts.host_rows())

    def domjudge_register_host(self, hostname: str) -> list[dict[str, object]]:
        outcome = self._dispatch.domjudge_register_host(hostname)
        self._finalize_batches(outcome.terminal_batch_ids)
        return list(outcome.workdirs)

    def domjudge_fetch_work(
        self,
        hostname: str,
        max_batchsize: int | None = None,
    ) -> list[dict[str, object]]:
        gate = self._admission_gate
        if gate is None:
            outcome = self._dispatch.domjudge_fetch_work(hostname, max_batchsize)
        else:
            outcome = self._dispatch.domjudge_fetch_work(
                hostname,
                max_batchsize,
                admission_gate=gate,
            )
        self._finalize_batches(outcome.terminal_batch_ids)
        return list(outcome.work)

    def probe_task_case_cache(self, task_ids: list[str]) -> set[str]:
        outcome = self._dispatch.probe_task_case_cache(task_ids)
        self._finalize_batches(outcome.terminal_batch_ids)
        return set(outcome.pending_task_ids)

    def _finalize_batches(
        self,
        batch_ids: tuple[int, ...],
        *,
        require_completion_ack: bool = False,
    ) -> None:
        self._batch_finalizer.retry_due_finalizations(limit=1)
        for batch_id in batch_ids:
            self._batch_finalizer.finalize_batch_if_ready(
                batch_id,
                require_completion_ack=require_completion_ack,
            )

    def _complete_callback(
        self,
        outcome: CallbackOutcome[Acknowledgement],
    ) -> Acknowledgement:
        if outcome.terminal_batch_ids:
            self._finalize_batches(
                outcome.terminal_batch_ids,
                require_completion_ack=True,
            )
        for event in outcome.host_events:
            self._hosts.record_event(
                hostname=event.hostname,
                action=event.action,
                task_id=event.task_id,
                run_id=event.run_id,
            )
        for verification_id in outcome.touched_verification_ids:
            self._terminal_cleanup.touch(verification_id)
        return outcome.acknowledgement

    def domjudge_get_source_files(
        self,
        submit_id: str,
        contest_id: str | None = None,
    ) -> list[DomjudgeDownloadFile]:
        return self._files.source_files(submit_id, contest_id)

    def domjudge_get_testcase_files(
        self,
        testcase_id: int,
    ) -> list[DomjudgeDownloadFile]:
        return self._files.testcase_files(testcase_id)

    def domjudge_get_executable_files(
        self,
        kind: str,
        script_id: object,
        *,
        hostname: str = "",
    ) -> list[DomjudgeDownloadFile]:
        outcome = self._files.executable_files(
            kind,
            script_id,
            hostname=hostname,
        )
        self._finalize_batches(outcome.terminal_batch_ids)
        if outcome.error:
            raise RuntimeError(outcome.error)
        return list(outcome.files)

    def domjudge_get_version_commands(self, judgetask_id: int) -> dict[str, object]:
        return self._version_callback.commands(judgetask_id)

    def domjudge_check_versions(
        self,
        judgetask_id: int,
        *,
        hostname: str,
        compiler: str = "",
        runner: str = "",
    ) -> dict[str, object]:
        outcome = self._version_callback.report(
            judgetask_id,
            hostname=hostname,
            compiler=compiler,
            runner=runner,
        )
        return self._complete_callback(outcome)

    def domjudge_update_judging(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
    ) -> None:
        outcome = self._result.domjudge_update_judging(
            hostname,
            judgetask_id,
            payload,
        )
        return self._complete_callback(outcome)

    def domjudge_add_judging_run(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
    ) -> int:
        outcome = self._result.domjudge_add_judging_run(
            hostname,
            judgetask_id,
            payload,
        )
        return self._complete_callback(outcome)

    def domjudge_internal_error(
        self,
        *,
        description: str,
        hostname: str = "",
        judgetask_id: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> int:
        outcome = self._result.domjudge_internal_error(
            description=description,
            hostname=hostname,
            judgetask_id=judgetask_id,
            payload=payload,
        )
        return self._complete_callback(outcome)

    def domjudge_add_debug_info(
        self,
        *,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object] | None = None,
    ) -> None:
        outcome = self._result.domjudge_add_debug_info(
            hostname=hostname,
            judgetask_id=judgetask_id,
            payload=payload,
        )
        return self._complete_callback(outcome)

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
        upload_stream: BinaryIO | None = None,
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        verification_id: str,
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
            prepared_payload=(
                None if prepared_payload is None else dict(prepared_payload)
            ),
            service_class="foreground",
        )
        task = self._tasks.get(task_id)
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
        verification_id: str,
        verification_program_id: str,
        verification_source: str = "compile.only",
        expected_behavior: str = "compile",
        prepared_payload: dict[str, object] | None = None,
    ) -> TaskPollResult:
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
            prepared_payload=(
                None if prepared_payload is None else dict(prepared_payload)
            ),
        )
        task = self._tasks.get(task_id)
        runtime_verification_id = "" if task is None else str(task["verification_id"])
        try:
            return self.wait_for_task_result(task_id, timeout_sec=None)
        finally:
            self.schedule_verification_cleanup(runtime_verification_id)

    def case_output_for_task(self, task_id: str, test_name: str) -> tuple[str, int]:
        row = self._batch_runtime.case_output_for_task(task_id, test_name)
        if row is None:
            return ("", 0)
        case_id = row["id"]
        output_ref = row["output_run_ref"]
        if isinstance(case_id, bool) or not isinstance(case_id, int):
            raise RuntimeError("judgehost case output has invalid case id")
        if not isinstance(output_ref, str):
            raise RuntimeError("judgehost case output has invalid artifact reference")
        return (output_ref, case_id)

    def case_feedback_blob_for_task(self, task_id: str, test_name: str) -> bytes | None:
        row = self._batch_runtime.case_for_task(task_id, test_name)
        if row is None:
            return None
        output_diff_ref = str(row["output_diff_ref"] or "")
        if not output_diff_ref:
            return None
        return self.resolve_artifact_blob(output_diff_ref)

    def reset_runtime_state(self) -> None:
        self._terminal_cleanup.reset()
        self._tasks.reset()
        self._hosts.clear()
        self._batch_runtime.reset()
