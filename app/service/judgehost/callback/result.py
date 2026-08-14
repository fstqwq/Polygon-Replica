import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import cast

from app.db import now_iso
from app.service.judgehost.callback.artifact_capture import (
    CaseArtifactCapture,
    CaseArtifactRequest,
    decode_callback_blob,
)
from app.service.judgehost.batch.model import (
    CaseCallbackReceipt,
    CaseClaimBusy,
    CaseReportTelemetry,
)
from app.service.judgehost.core import JudgehostCore
from app.service.judgehost.callback.diagnostic_payload import parse_diagnostic_payload
from app.service.judgehost.domjudge.identity import (
    parse_script_id,
    script_hash_field,
    script_id,
)
from app.service.judgehost.domjudge.file_stream import DomjudgeDownloadFile
from app.service.judgehost.work.finalization import BatchFinalizationPort
from app.service.judgehost.domjudge.limits import (
    run_output_kb,
    truncate_stored_log_bytes,
)
from app.service.judgehost.work.publication import (
    JudgehostCaseCompletionPublisher,
    JudgehostCaseDiagnosticPublisher,
)
from app.service.judgehost.callback.result_normalizer import (
    CapturedJudgehostCase,
    normalize_captured_case,
)
from app.service.judgehost.domjudge.result import (
    parse_bool,
    bounded_feedback_text,
    parse_nonnegative_float,
    parse_int,
)
from app.service.judgehost.domjudge.codec import decode_text
from app.service.judgehost.state import JudgehostState
from app.service.judgehost.work.task_queue import TaskQueue
from app.service.judgehost.telemetry.toolchain_versions import (
    ToolchainTelemetryHandler,
    ToolchainVersionReport,
)
from app.service.judgehost.domjudge.toolkit import DomjudgeToolkit
from app.service.platform.error_text import aux_display_text_limit_bytes
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.execution.codec import execution_result_json

logger = logging.getLogger(__name__)

_diag_logger = logging.getLogger("uvicorn.error")


class ResultProcessor:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    CASE_CACHE_KIND = DomjudgeToolkit.CASE_CACHE_KIND
    _TASK_KIND_COMPILE_ONLY = "compile-only"

    def __init__(
        self,
        state: JudgehostState,
        core: JudgehostCore,
        queue: TaskQueue,
        toolkit: DomjudgeToolkit,
        *,
        diagnostic_publisher: JudgehostCaseDiagnosticPublisher,
        completion_publisher: JudgehostCaseCompletionPublisher,
        batch_finalizer: BatchFinalizationPort,
    ) -> None:
        self._s = state
        self._core = core
        self._queue = queue
        self._toolkit = toolkit
        self._artifact_capture = CaseArtifactCapture(state.runtime_blob_store)
        self._toolchain_telemetry = ToolchainTelemetryHandler(
            state,
            queue._record_host_event_conn,
        )
        self._diagnostic_publisher = diagnostic_publisher
        self._completion_publisher = completion_publisher
        self._batch_finalizer = batch_finalizer

    def _display_text_limit_bytes(self) -> int:
        return aux_display_text_limit_bytes(self._s.config_values.snapshot())

    def _touch_task_verification(self, task_id: str) -> None:
        task = self._s.task_registry.get(task_id)
        if task is not None:
            self._s.touch_verification_runtime(decode_text(raw=task.get("verification_id")))

    def _release_case_callback_receipt(
        self,
        receipt: CaseCallbackReceipt,
    ) -> None:
        self._s.batch_runtime.release_case_callback_receipt(receipt.receipt_id)
        # A cleanup deadline measures continuous quiet time. Releasing the
        # receipt is the callback's linearization end, so a callback that ran
        # near the old deadline receives a fresh full quiet interval.
        self._s.touch_verification_runtime(receipt.verification_id)

    def _task_accepts_case_updates(self, task_id: str) -> bool:
        task_row = self._core.task_by_id(task_id)
        if task_row is None:
            return False
        task_status = decode_text(lower=True, raw=task_row["status"])
        return task_status in {
            self.STATUS_ENQUEUING,
            self.STATUS_QUEUED,
            self.STATUS_LEASED,
            self.STATUS_REPORTING,
        }

    @staticmethod
    def _verification_source(task_payload: dict[str, object]) -> str | None:
        value = task_payload.get("verification_source")
        if value is None or isinstance(value, str):
            return value
        raise RuntimeError("judgehost task verification_source must be a string")

    def domjudge_get_source_files(
        self,
        submit_id: str,
        contest_id: str | None = None,
    ) -> list[DomjudgeDownloadFile]:
        safe_submit = decode_text(raw=submit_id)
        if not safe_submit:
            raise RuntimeError("source files not found")
        safe_contest = None if contest_id is None else self._toolkit.contest_id(contest_id)
        submission = self._s.batch_runtime.source_submission(safe_submit, contest_id=safe_contest)
        if submission is None:
            raise RuntimeError("source files not found")
        source_files = (
            (submission.source_name, submission.source_file),
            *submission.extra_source_items,
        )
        return [DomjudgeDownloadFile(filename, payload) for filename, payload in source_files]

    def domjudge_get_testcase_files(
        self,
        testcase_id: int,
    ) -> list[DomjudgeDownloadFile]:
        token = int(testcase_id)
        row, resolution_source = self._s.batch_runtime.testcase_refs(token)
        if row is None:
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s resolved=missing",
                token,
            )
            raise RuntimeError("testcase files not found")
        input_ref = decode_text(raw=row["input_ref"])
        answer_ref = decode_text(raw=row["answer_ref"])
        input_file = self._s.runtime_blob_store.descriptor(input_ref)
        answer_file = self._s.runtime_blob_store.descriptor(answer_ref)
        if input_file is None or answer_file is None:
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s resolved=%s exists=%s input=%s answer=%s",
                token,
                resolution_source,
                False,
                input_ref,
                answer_ref,
            )
            raise RuntimeError("testcase files not found")
        logger.debug(
            "judgehost.get_testcase_files testcase_id=%s resolved=%s exists=%s input=%s answer=%s",
            token,
            resolution_source,
            True,
            input_ref,
            answer_ref,
        )
        return [
            DomjudgeDownloadFile("input", input_file),
            DomjudgeDownloadFile("output", answer_file),
        ]

    def _executable_rows(
        self,
        *,
        kind: str,
        executable_hash: str,
    ) -> list[DomjudgeDownloadFile]:
        cached_rows = self._toolkit.read_executable_cache(
            kind=kind, executable_hash=executable_hash
        )
        if not cached_rows:
            raise RuntimeError("script files not found")
        return [
            DomjudgeDownloadFile(
                str(row["filename"]),
                cast(PayloadFile, row["payload"]),
                bool(row["is_executable"]),
            )
            for row in cached_rows
        ]

    def _active_batch_script_hash(
        self,
        *,
        hostname: str,
        kind: str,
        requested_id: int,
    ) -> tuple[int, str] | None:
        safe_host = self._core.normalize_hostname(hostname)
        if not safe_host:
            return None
        leased_match = self._s.batch_runtime.leased_script_hash_for_host(
            safe_host,
            kind=kind,
            script_id=requested_id,
        )
        if leased_match is not None:
            return leased_match
        matching: dict[int, str] = {}
        for batch_row in self._s.batch_runtime.host_context_batches(safe_host):
            if kind == "compile":
                script_hash = batch_row["compile_hash"]
            elif kind == "run":
                script_hash = batch_row["run_hash"]
            elif kind == "compare":
                script_hash = batch_row["compare_hash"]
            else:
                raise RuntimeError("invalid script kind")
            if script_hash and script_id(script_hash) == requested_id:
                matching[int(batch_row["batch_id"])] = script_hash
        matching_hashes = set(matching.values())
        if len(matching_hashes) != 1:
            return None
        script_hash = next(iter(matching_hashes))
        batch_id = next(batch_id for batch_id, value in matching.items() if value == script_hash)
        return (batch_id, script_hash)

    def _shared_script_hash(self, *, kind: str, requested_id: int) -> str:
        matching_hashes = self._s.batch_runtime.active_script_hashes(kind, requested_id)
        if not matching_hashes:
            raise RuntimeError("script files not found")
        if len(matching_hashes) > 1:
            raise RuntimeError("ambiguous script id")
        return next(iter(matching_hashes))

    def _fail_batch_executable_lookup(self, *, batch_id: int, error_text: str) -> None:
        safe_error = decode_text(raw=error_text)
        if not safe_error:
            safe_error = "judgehost executable cache missing"
        now_text = now_iso()
        self._s.batch_runtime.append_debug_text(
            case_id=None,
            batch_id=int(batch_id),
            debug_text=safe_error,
            now_text=now_text,
        )
        self._batch_finalizer.finalize_batch_if_ready(
            int(batch_id),
            force_failed=True,
            error_text=safe_error,
        )

    def domjudge_get_executable_files(
        self,
        kind: str,
        script_id: object,
        *,
        hostname: str = "",
    ) -> list[DomjudgeDownloadFile]:
        requested_id = parse_script_id(script_id)
        token = decode_text(lower=True, raw=kind)
        script_hash_field(token)
        active_match = (
            None
            if not hostname
            else self._active_batch_script_hash(
                hostname=hostname,
                kind=token,
                requested_id=requested_id,
            )
        )
        if active_match is not None:
            batch_id, executable_hash = active_match
            try:
                return self._executable_rows(kind=token, executable_hash=executable_hash)
            except RuntimeError:
                self._fail_batch_executable_lookup(
                    batch_id=batch_id,
                    error_text=f"judgehost executable cache missing: {token}/{requested_id}",
                )
                raise
        executable_hash = self._shared_script_hash(kind=token, requested_id=requested_id)
        return self._executable_rows(kind=token, executable_hash=executable_hash)

    def domjudge_get_version_commands(self, judgetask_id: int) -> dict[str, object]:
        return self._toolchain_telemetry.version_commands(int(judgetask_id))

    def domjudge_check_versions(
        self,
        judgetask_id: int,
        *,
        hostname: str,
        compiler: str = "",
        runner: str = "",
    ) -> dict[str, object]:
        receipt = self._s.batch_runtime.acquire_case_callback_receipt(int(judgetask_id))
        if receipt is None:
            return {}
        safe_host = self._core.normalize_hostname(hostname)
        try:
            if receipt.status not in {
                "leased",
                "reporting",
                "reported",
                "cancelled",
            }:
                raise RuntimeError("judgehost case is not in a version callback state")
            expected_hostname = receipt.lease_owner or receipt.last_callback_hostname
            if not expected_hostname or expected_hostname != safe_host:
                return {}
            self._toolchain_telemetry.record_report(
                ToolchainVersionReport(
                    judgetask_id=int(judgetask_id),
                    hostname=safe_host,
                    compiler=compiler,
                    runner=runner,
                    task_id=receipt.task_id,
                    run_id=receipt.run_id,
                )
            )
            return {}
        finally:
            self._release_case_callback_receipt(receipt)

    def _complete_terminal_callback_case(
        self,
        case_id: int,
        batch_id: int,
        *,
        reason: str = "",
    ) -> bool:
        """Acknowledge one retry without splitting a program-failure commit."""

        case = self._s.batch_runtime.fetch_case(int(case_id))
        if case is None:
            return True
        if case["status"] not in {"reported", "cancelled"}:
            return False
        if bool(case["completion_acknowledged"]):
            self._diagnostic_publisher.flush_pending(int(case_id))
            return True
        batch = self._s.batch_runtime.fetch_batch(int(batch_id))
        if batch is not None and batch["failure_runresult"]:
            self._batch_finalizer.finalize_batch_if_ready(
                int(batch_id),
                require_completion_ack=True,
            )
            return True
        if not self._completion_publisher.acknowledge_terminal_case(
            int(case_id),
            reason=reason,
        ):
            return False
        self._batch_finalizer.finalize_batch_if_ready(
            int(batch_id),
            require_completion_ack=True,
        )
        return True

    def _case_report_telemetry(
        self,
        *,
        hostname: str,
        test_name: str,
        task_payload: dict[str, object],
        task_kind: str,
        reported_at: str,
        reported_monotonic: float,
    ) -> CaseReportTelemetry:
        raw_source_name = task_payload.get("source_name")
        source_name = raw_source_name if isinstance(raw_source_name, str) else ""
        return CaseReportTelemetry(
            hostname=hostname,
            reported_at=reported_at,
            reported_monotonic=reported_monotonic,
            verification_id=decode_text(raw=task_payload.get("verification_id")),
            problem_slug=decode_text(raw=task_payload.get("problem")),
            task_kind=task_kind,
            source_label=Path(
                decode_text(
                    raw=task_payload.get("source_label"),
                    default=source_name,
                ).replace("\\", "/")
            ).name,
            test_name=test_name,
        )

    def _complete_cancelled_case_receipt(
        self,
        *,
        case_id: int,
        generation: int,
        task_id: str,
        batch_id: int,
        report: CaseReportTelemetry,
    ) -> bool:
        accepted = self._s.batch_runtime.commit_cancelled_receipt(
            case_id,
            generation=generation,
            updated_at=report.reported_at,
            report_telemetry=report,
        )
        if not accepted:
            return False
        self._completion_publisher.acknowledge_terminal_case(
            int(case_id),
            reason="judgehost task cancelled",
        )
        batch_row = self._s.batch_runtime.batch_for_task(task_id)
        if batch_row is not None:
            self._batch_finalizer.finalize_task_if_ready(
                task_id,
                batch_row=batch_row,
            )
        self._batch_finalizer.finalize_batch_if_ready(
            batch_id,
            require_completion_ack=True,
        )
        return True

    def domjudge_update_judging(
        self, hostname: str, judgetask_id: int, payload: dict[str, object]
    ) -> None:
        receipt = self._s.batch_runtime.acquire_case_callback_receipt(int(judgetask_id))
        if receipt is None:
            logger.info("ignoring update for unknown judging run id: %s", int(judgetask_id))
            return
        try:
            self._touch_task_verification(receipt.task_id)
            self._process_judging_update(
                hostname,
                judgetask_id,
                payload,
                receipt_generation=receipt.claim_generation,
            )
        finally:
            self._release_case_callback_receipt(receipt)

    def _process_judging_update(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
        *,
        receipt_generation: int,
    ) -> None:
        safe_host = self._core.normalize_hostname(hostname)
        case_id = int(judgetask_id)
        case_row = self._s.batch_runtime.case_execution_row(case_id)
        if case_row is None:
            # judgedaemon may still report progress for a case that was already
            # dropped by server-side cancellation/startup cleanup. Treat as
            # idempotent no-op so daemon can continue without fatal retries.
            logger.info("ignoring update for unknown judging run id: %s", case_id)
            return
        batch_status = case_row["batch_status"]
        case_status = case_row["case_status"]
        lease_owner = case_row["case_lease_owner"]
        expected_hostname = lease_owner or case_row["last_callback_hostname"]
        if not expected_hostname or expected_hostname != safe_host:
            raise RuntimeError("judgehost does not own judging run")
        batch_id = int(case_row["batch_id"])
        compile_success = None
        if "compile_success" in payload:
            compile_success = (
                1
                if parse_bool(
                    payload.get("compile_success"),
                    default=False,
                )
                else 0
            )

        def _payload_blob_as_b64(value: object) -> str:
            raw = decode_callback_blob(value)
            if raw:
                return base64.b64encode(
                    truncate_stored_log_bytes(
                        raw,
                        self._s.config_values.snapshot(),
                    )
                ).decode("ascii")
            return ""

        compile_output = ""
        compile_meta = ""
        compile_updated_at = ""
        compile_success_recorded: bool | None = None
        if compile_success == 1:
            compile_output = _payload_blob_as_b64(payload.get("output_compile"))
            compile_meta = _payload_blob_as_b64(payload.get("compile_metadata"))
            compile_updated_at = now_iso()
            compile_success_recorded = self._s.batch_runtime.record_compile_success(
                case_id,
                hostname=safe_host,
                receipt_generation=receipt_generation,
                compile_output_b64=compile_output,
                compile_metadata_b64=compile_meta,
                updated_at=compile_updated_at,
            )
        if batch_status in {"finalize-pending", "finalizing"}:
            self._batch_finalizer.finalize_batch_if_ready(
                batch_id,
                require_completion_ack=True,
            )
            return
        if batch_status != "open":
            logger.info("ignoring update for terminal DOMjudge batch case id: %s", case_id)
            return
        if case_status in {"reported", "cancelled"}:
            self._complete_terminal_callback_case(
                case_id,
                batch_id,
            )
            return
        if case_status not in {"leased", "reporting"}:
            raise RuntimeError("judgehost case does not accept this update")
        safe_task_id = case_row["task_id"]
        if not self._task_accepts_case_updates(safe_task_id):
            logger.info("ignoring update for cancelled DOMjudge task case id: %s", case_id)
            return
        if compile_success == 0:
            compile_output = _payload_blob_as_b64(payload.get("output_compile"))
            compile_meta = _payload_blob_as_b64(payload.get("compile_metadata"))
        self._queue._record_host_event_conn(
            hostname=safe_host,
            action="update",
            task_id=safe_task_id,
            run_id=case_row["run_id"],
        )
        if compile_success is not None:
            updated_at = compile_updated_at or now_iso()
            if compile_success == 0:
                compile_blob = self._toolkit.b64_decode(compile_output)
                compile_log = compile_blob.decode("utf-8", errors="replace").strip()
                failure_text = (
                    bounded_feedback_text(
                        compile_log,
                        limit_bytes=self._display_text_limit_bytes(),
                    )
                    or "compilation failed"
                )
                compile_diagnostics = (
                    {
                        "level": "error",
                        "message": compile_log or failure_text,
                        "file": "",
                        "line": 0,
                        "column": 0,
                        "can_link": False,
                    },
                )
                claim = self._s.batch_runtime.claim_compile_failure(
                    case_id,
                    hostname=safe_host,
                    receipt_generation=receipt_generation,
                    compile_output_b64=compile_output,
                    compile_metadata_b64=compile_meta,
                    failure_text=failure_text,
                    compile_log=compile_log,
                    compile_diagnostics=compile_diagnostics,
                    updated_at=updated_at,
                )
                if claim.outcome == "rejected":
                    raise RuntimeError("judgehost case lease changed before compile failure claim")
                if claim.outcome in {"late", "idempotent"}:
                    self._complete_terminal_callback_case(
                        case_id,
                        claim.batch_id,
                    )
                    return
                if claim.outcome == "cancelled":
                    self._completion_publisher.acknowledge_terminal_case(
                        case_id,
                        reason="judgehost task cancelled",
                    )
                    self._batch_finalizer.finalize_batch_if_ready(
                        claim.batch_id,
                        require_completion_ack=True,
                    )
                    return
                self._batch_finalizer.finalize_batch_if_ready(
                    claim.batch_id,
                    require_completion_ack=True,
                )
                return
            if not compile_success_recorded:
                if self._complete_terminal_callback_case(case_id, batch_id):
                    return
                raise RuntimeError("judgehost batch closed before compile success update")

    def domjudge_add_judging_run(
        self, hostname: str, judgetask_id: int, payload: dict[str, object]
    ) -> int:
        receipt = self._s.batch_runtime.acquire_case_callback_receipt(int(judgetask_id))
        if receipt is None:
            logger.info(
                "ignoring add_judging_run for unknown judging run id: %s",
                int(judgetask_id),
            )
            return 1
        try:
            return self._add_judging_run_with_receipt(
                receipt,
                hostname,
                judgetask_id,
                payload,
            )
        finally:
            self._release_case_callback_receipt(receipt)

    def _add_judging_run_with_receipt(
        self,
        receipt: CaseCallbackReceipt,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
    ) -> int:
        reported_monotonic = time.monotonic()
        reported_at = now_iso()
        safe_host = self._core.normalize_hostname(hostname)
        case_row = self._s.batch_runtime.fetch_case(int(judgetask_id))
        if case_row is None:
            raise RuntimeError("pinned judgehost case disappeared")
        case_status = case_row["status"]
        expected_hostname = case_row["lease_owner"] or case_row["last_callback_hostname"]
        if not expected_hostname or expected_hostname != safe_host:
            raise RuntimeError("judgehost does not own judging run")
        if case_status in {"reported", "cancelled"}:
            self._complete_terminal_callback_case(
                int(judgetask_id),
                int(case_row["batch_id"]),
            )
            logger.info(
                "ignoring stale add_judging_run result for case id: %s",
                int(judgetask_id),
            )
            return 1
        if case_status == "reporting":
            raise CaseClaimBusy("judgehost case result is already being processed")
        if case_status != "leased":
            raise RuntimeError("judgehost case is not leased for this result")
        self._touch_task_verification(case_row["task_id"])
        claim = self._s.batch_runtime.claim_case_reporting(
            int(judgetask_id),
            hostname=safe_host,
            receipt_generation=receipt.claim_generation,
            now_text=reported_at,
        )
        if claim is None:
            raise RuntimeError("judgehost case lease changed before result claim")
        self._queue._record_host_event_conn(
            hostname=safe_host,
            action="report",
            task_id=case_row["task_id"],
            run_id=case_row["run_id"],
        )
        self._s.batch_runtime.observe_compile_success_from_case_claim(
            claim.case_id,
            generation=claim.generation,
            lease_owner=safe_host,
            updated_at=reported_at,
        )
        task_payload = self._core.task_payload(claim.task_id)
        verification_source = self._verification_source(task_payload)
        task_kind = self._toolkit.task_kind(
            task_payload,
            verification_source=verification_source,
        )
        report_telemetry = self._case_report_telemetry(
            hostname=safe_host,
            test_name=case_row["test_name"],
            task_payload=task_payload,
            task_kind=task_kind,
            reported_at=reported_at,
            reported_monotonic=reported_monotonic,
        )
        if claim.cancel_requested:
            if not self._complete_cancelled_case_receipt(
                case_id=claim.case_id,
                generation=claim.generation,
                task_id=case_row["task_id"],
                batch_id=case_row["batch_id"],
                report=report_telemetry,
            ):
                raise RuntimeError("judgehost cancellation receipt claim was lost")
            return 1

        def _abort_unfinished_claim() -> None:
            if self._s.batch_runtime.abort_case_claim(
                claim.case_id,
                generation=claim.generation,
                updated_at=now_iso(),
            ):
                self._batch_finalizer.finalize_batch_if_ready(claim.batch_id)

        try:
            result_id = self._process_judging_run(
                safe_host,
                judgetask_id,
                payload,
                claim_generation=claim.generation,
                report_telemetry=report_telemetry,
            )
        except Exception:
            _abort_unfinished_claim()
            raise
        refreshed = self._s.batch_runtime.fetch_case(claim.case_id)
        if refreshed is not None and refreshed["status"] == "reporting":
            _abort_unfinished_claim()
        return result_id

    def _process_judging_run(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
        *,
        claim_generation: int,
        report_telemetry: CaseReportTelemetry,
    ) -> int:
        safe_host = self._core.normalize_hostname(hostname)
        case_id = int(judgetask_id)
        row = self._s.batch_runtime.case_execution_row(case_id)
        if row is None:
            raise RuntimeError("pinned judgehost case disappeared")
        batch_status = row["batch_status"]
        if batch_status != "open":
            raise RuntimeError("judgehost batch closed during result processing")
        if row["case_status"] != "reporting":
            raise RuntimeError("judgehost case result claim changed")
        if row["case_lease_owner"] != safe_host:
            raise RuntimeError("judgehost does not own judging run")
        batch_id = int(row["batch_id"])
        safe_task_id = row["task_id"]
        task_payload = self._core.task_payload(safe_task_id) if safe_task_id else {}
        verification_source = self._verification_source(task_payload)
        task_kind = self._toolkit.task_kind(task_payload, verification_source=verification_source)
        compile_only = task_kind == self._TASK_KIND_COMPILE_ONLY

        def _load_json_object(raw: object) -> dict[str, object]:
            text = decode_text(raw=raw)
            if not text:
                return {}
            try:
                return cast(dict[str, object], json.loads(text))
            except Exception:
                return {}

        interactive = row["mode"] == "interactive"
        run_cfg_for_capture = _load_json_object(row["run_config_json"])
        pass_limit = max(1, parse_int(run_cfg_for_capture.get("pass_limit"), 1))
        bundle_limit_bytes = min(
            8 * 1024 * 1024,
            max(
                1024,
                int(run_output_kb(self._s.config_values.snapshot()) * 1024 * 3 // 4),
            ),
        )
        callback_pass = max(0, parse_int(payload.get("pass"), 0))
        prepared_artifacts = self._artifact_capture.prepare(
            CaseArtifactRequest(
                payload=payload,
                task_kind=task_kind,
                interactive=interactive,
                pass_limit=pass_limit,
                callback_pass=callback_pass,
                bundle_limit_bytes=bundle_limit_bytes,
            )
        )

        source_name = row["source_name"]
        source_hash = row["source_hash"]
        if re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            raise RuntimeError(f"missing source_hash for DOMjudge case {case_id}")

        testcase_hash = row["testcase_hash"]
        testcase_input_hash = row["testcase_input_hash"]
        testcase_answer_hash = row["testcase_answer_hash"]
        if re.fullmatch(r"[0-9a-f]{64}", testcase_hash) is None:
            raise RuntimeError(f"missing testcase_hash for DOMjudge case {case_id}")
        if re.fullmatch(r"[0-9a-f]{64}", testcase_input_hash) is None:
            raise RuntimeError(f"missing testcase_input_hash for DOMjudge case {case_id}")
        if re.fullmatch(r"[0-9a-f]{64}", testcase_answer_hash) is None:
            raise RuntimeError(f"missing testcase_answer_hash for DOMjudge case {case_id}")

        compile_hash = row["compile_hash"]
        run_hash = row["run_hash"]
        compare_hash = row["compare_hash"]
        compile_cfg = _load_json_object(row["compile_config_json"])
        run_cfg = run_cfg_for_capture
        compare_cfg = _load_json_object(row["compare_config_json"])
        compile_config_hash = RuntimeCacheIndex.signature(compile_cfg)
        run_config_hash = RuntimeCacheIndex.signature(run_cfg)
        compare_config_hash = RuntimeCacheIndex.signature(compare_cfg)
        toolchain_cmd_digest = decode_text(lower=True, raw=compile_cfg.get("toolchain_cmd_digest"))
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._toolkit.toolchain_cmd_digest(source_name)

        current_case = self._s.batch_runtime.fetch_case(case_id)
        if current_case is not None and bool(current_case["cancel_requested"]):
            self._complete_cancelled_case_receipt(
                case_id=case_id,
                generation=claim_generation,
                task_id=row["task_id"],
                batch_id=row["batch_id"],
                report=report_telemetry,
            )
            return 1

        captured_artifacts = self._artifact_capture.capture(prepared_artifacts)
        debug_context = self._s.batch_runtime.case_debug_context(case_id)
        debug_text = ""
        if debug_context is not None:
            debug_text = decode_text(raw=debug_context["case_debug_text"])
            if not debug_text:
                debug_text = decode_text(raw=debug_context["batch_debug_text"])
        normalized = normalize_captured_case(
            CapturedJudgehostCase(
                test_name=row["test_name"],
                input_ref=row["input_ref"],
                interactive=interactive,
                raw_runresult=decode_text(
                    raw=payload.get("runresult"),
                    default="internal-error",
                ),
                runtime_fallback_sec=parse_nonnegative_float(
                    payload.get("runtime"),
                    0.0,
                ),
                score_text=decode_text(raw=payload.get("score")),
                run_config=run_cfg,
                artifacts=captured_artifacts.artifacts,
                pass_bundle=captured_artifacts.pass_bundle,
                capture_warning=captured_artifacts.warning,
                debug_text=debug_text,
            ),
            limit_bytes=self._display_text_limit_bytes(),
        )
        case_result = normalized.result
        runresult = normalized.runresult
        verdict = normalized.verdict
        runtime_sec = normalized.runtime_sec
        cpu_sec = normalized.cpu_sec
        wall_sec = normalized.wall_sec
        memory_kb = normalized.memory_kb
        score_text = normalized.score_text

        case_key_hash, case_signature = self._toolkit.case_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            compare_config_hash=compare_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_hash=testcase_hash,
        )
        shortcut_eligible = verdict != "FL"
        if compile_only and verdict != "OK":
            shortcut_eligible = False

        self._toolkit.store_case_cache(
            key_parts={"key_hash": case_key_hash, "signature": case_signature},
            tags={
                "source_hash": source_hash,
                "testcase_hash": testcase_hash,
                "verification_source": verification_source,
                "task_kind": task_kind,
            },
            runresult=runresult,
            runtime_sec=runtime_sec,
            cpu_sec=cpu_sec,
            wall_sec=wall_sec,
            memory_kb=memory_kb,
            score_text=score_text,
            result_json=execution_result_json(case_result),
            files=captured_artifacts.payloads,
            shortcut_eligible=shortcut_eligible,
        )
        now_text = now_iso()
        outcome = self._s.batch_runtime.commit_case_result(
            case_id,
            generation=claim_generation,
            result=case_result,
            updated_at=now_text,
            report_telemetry=report_telemetry,
        )
        if outcome == "cancelled":
            self._completion_publisher.acknowledge_terminal_case(
                case_id,
                reason="judgehost task cancelled",
            )
            batch_row = self._s.batch_runtime.batch_for_task(safe_task_id)
            if batch_row is not None:
                self._batch_finalizer.finalize_task_if_ready(
                    safe_task_id,
                    batch_row=batch_row,
                )
            self._batch_finalizer.finalize_batch_if_ready(
                batch_id,
                require_completion_ack=True,
            )
            return 1
        if outcome != "reported":
            if self._complete_terminal_callback_case(case_id, batch_id):
                logger.info("ignoring stale add_judging_run result for case id: %s", case_id)
                return 1
            raise RuntimeError("judgehost case result lost its completion claim")
        logger.debug(
            "domjudge add_judging_run host=%s batch_id=%s case_id=%s runresult=%s",
            safe_host,
            batch_id,
            case_id,
            runresult,
        )
        current_batch = self._s.batch_runtime.fetch_batch(batch_id)
        if current_batch is not None and current_batch["failure_runresult"]:
            # A compile/internal batch failure owns every still-open Case.
            # Do not publish the Case that happened to leave `reporting`
            # first: finalization waits for the whole batch and sends one
            # reported_many transaction for all affected verification tasks.
            self._batch_finalizer.finalize_batch_if_ready(
                batch_id,
                require_completion_ack=True,
            )
            return 1
        if not self._completion_publisher.acknowledge_terminal_case(case_id):
            raise RuntimeError("verification task completion was not acknowledged")
        batch_row = self._s.batch_runtime.batch_for_task(safe_task_id)
        if batch_row is not None:
            try:
                self._batch_finalizer.finalize_task_if_ready(
                    safe_task_id,
                    batch_row=batch_row,
                )
            except Exception:
                logger.exception(
                    "failed to finalize DOMjudge task task_id=%s batch_id=%s",
                    safe_task_id,
                    batch_id,
                )
        self._batch_finalizer.finalize_batch_if_ready(
            batch_id,
            require_completion_ack=True,
        )
        return 1

    def domjudge_internal_error(
        self,
        *,
        description: str,
        hostname: str = "",
        judgetask_id: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> int:
        safe_desc = decode_text(raw=description, default="judgehost internal error")
        if judgetask_id is None:
            return 0
        case_id = int(judgetask_id)
        receipt = self._s.batch_runtime.acquire_case_callback_receipt(case_id)
        if receipt is None:
            return 0
        try:
            if receipt.status not in {
                "leased",
                "reporting",
                "reported",
                "cancelled",
            }:
                raise RuntimeError("judgehost case is not in a diagnostic callback state")
            safe_host = (
                self._core.normalize_hostname(hostname)
                if hostname
                else receipt.lease_owner or receipt.last_callback_hostname
            )
            expected_hostname = receipt.lease_owner or receipt.last_callback_hostname
            if not expected_hostname or safe_host != expected_hostname:
                raise RuntimeError("judgehost does not own judging run")
            self._touch_task_verification(receipt.task_id)
            payload_text = parse_diagnostic_payload({} if payload is None else payload).text
            diagnostic_text = safe_desc
            if payload_text:
                if safe_desc.lower() in payload_text.lower():
                    diagnostic_text = payload_text
                elif payload_text.lower() not in safe_desc.lower():
                    diagnostic_text = f"{safe_desc}\n\n{payload_text}"
            failure_text = diagnostic_text
            claim = self._diagnostic_publisher.claim_internal_error(
                case_id,
                hostname=safe_host,
                failure_text=failure_text,
                diagnostic_text=diagnostic_text,
                receipt_generation=receipt.claim_generation,
            )
            if claim.outcome == "rejected":
                raise RuntimeError("judgehost case lease changed before internal-error claim")
            if safe_host:
                self._queue._record_host_event_conn(
                    hostname=safe_host,
                    action="internal-error",
                    task_id=receipt.task_id,
                    run_id=receipt.run_id,
                )
            if claim.outcome in {"late", "idempotent"}:
                # A reporting Case owns its canonical decision. The pending
                # diagnostic is flushed by that completion; a terminal Case
                # can flush it immediately.
                self._complete_terminal_callback_case(
                    case_id,
                    claim.batch_id,
                )
                return case_id
            if claim.outcome == "cancelled":
                self._completion_publisher.acknowledge_terminal_case(
                    case_id,
                    reason="judgehost task cancelled",
                )
                self._batch_finalizer.finalize_batch_if_ready(
                    claim.batch_id,
                    require_completion_ack=True,
                )
                return case_id
            self._batch_finalizer.finalize_batch_if_ready(
                claim.batch_id,
                require_completion_ack=True,
            )
            return case_id
        finally:
            self._release_case_callback_receipt(receipt)

    def domjudge_add_debug_info(
        self,
        *,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object] | None = None,
    ) -> None:
        safe_host = self._core.normalize_hostname(hostname)
        case_id = int(judgetask_id)
        receipt = self._s.batch_runtime.acquire_case_callback_receipt(case_id)
        if receipt is None:
            return
        try:
            if receipt.status not in {
                "leased",
                "reporting",
                "reported",
                "cancelled",
            }:
                raise RuntimeError("judgehost case is not in a diagnostic callback state")
            expected_hostname = receipt.lease_owner or receipt.last_callback_hostname
            if not expected_hostname or expected_hostname != safe_host:
                raise RuntimeError("judgehost does not own judging run")
            self._touch_task_verification(receipt.task_id)
            debug_payload = {} if payload is None else payload
            if debug_payload:
                logger.debug(
                    "domjudge debug info host=%s judgetask_id=%s payload_keys=%s",
                    safe_host,
                    case_id,
                    sorted(debug_payload.keys()),
                )
            debug_text = parse_diagnostic_payload(debug_payload).text
            if debug_text:
                disposition = self._diagnostic_publisher.record_debug_info(
                    case_id,
                    hostname=safe_host,
                    text=debug_text,
                    receipt_generation=receipt.claim_generation,
                )
                if disposition == "rejected":
                    raise RuntimeError("judgehost case lease changed before diagnostic claim")
                if disposition == "pending":
                    # See internal-error above: the immutable receipt protects
                    # cleanup, while current Case state decides whether this
                    # callback must perform the post-completion flush itself.
                    self._complete_terminal_callback_case(
                        case_id,
                        receipt.batch_id,
                    )
            self._queue._record_host_event_conn(
                hostname=safe_host,
                action="debug",
                task_id=receipt.task_id,
                run_id=receipt.run_id,
            )
        finally:
            self._release_case_callback_receipt(receipt)
