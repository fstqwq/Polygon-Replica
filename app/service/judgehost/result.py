from __future__ import annotations

import base64
import logging
import json
import re
import time
from pathlib import Path
from typing import cast

from app.service.judgehost.shared import (
    domjudge_text,
    domjudge_lower_text,
)
from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_json_hash
from app.service.judgehost.domjudge.client import domjudge_parse_script_id, domjudge_script_hash_field, domjudge_script_id
from app.service.judgehost.limits import truncate_stored_log_bytes, run_output_kb
from app.service.judgehost.file_stream import DomjudgeDownloadFile
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.judgehost.runtime import (
    domjudge_bool,
    domjudge_feedback_text_from_text,
    domjudge_parse_float,
    domjudge_parse_int,
)
from app.service.judgehost.core import JudgehostCore
from app.service.judgehost.batch_scheduler_models import (
    CaseCallbackReceipt,
    CaseClaimBusy,
    CaseReportTelemetry,
)
from app.service.judgehost.finalization import BatchFinalizationPort
from app.service.judgehost.publication import (
    JudgehostCaseCompletionPublisher,
    JudgehostCaseDiagnosticPublisher,
)
from app.service.judgehost.result_normalizer import (
    CapturedCaseArtifact,
    CapturedJudgehostCase,
    normalize_captured_case,
    pass_cache_file_name,
)
from app.service.judgehost.state import JudgehostState
from app.service.judgehost.task_queue import TaskQueue
from app.service.judgehost.toolchain_versions import ToolchainVersionCollector
from app.service.judgehost.toolkit import DomjudgeToolkit
from app.service.judgehost.pass_bundle import (
    InvalidPassBundle,
    PassBundle,
    parse_pass_bundle,
    split_pass_feedback,
)
from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    execution_result_json,
)

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
    _TASK_KIND_MAIN_CORRECT = "main-correct"
    _TASK_KIND_SOLUTION_RUN = "solution-run"

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
        self._toolchain_versions = ToolchainVersionCollector(state)
        self._diagnostic_publisher = diagnostic_publisher
        self._completion_publisher = completion_publisher
        self._batch_finalizer = batch_finalizer

    def _touch_task_verification(self, task_id: str) -> None:
        task = self._s.task_registry.get(task_id)
        if task is not None:
            self._s.touch_verification_runtime(domjudge_text(task.get("verification_id")))

    def _release_case_callback_receipt(
        self,
        receipt: CaseCallbackReceipt,
    ) -> None:
        self._s.batch_scheduler.release_case_callback_receipt(receipt.receipt_id)
        # A cleanup deadline measures continuous quiet time. Releasing the
        # receipt is the callback's linearization end, so a callback that ran
        # near the old deadline receives a fresh full quiet interval.
        self._s.touch_verification_runtime(receipt.verification_id)

    def _domjudge_task_accepts_case_updates(self, task_id: str) -> bool:
        task_row = self._core.task_by_id(task_id)
        if task_row is None:
            return False
        task_status = domjudge_lower_text(task_row["status"])
        return task_status in {
            self.STATUS_ENQUEUING,
            self.STATUS_QUEUED,
            self.STATUS_LEASED,
            self.STATUS_REPORTING,
        }

    def domjudge_get_source_files(
        self,
        submit_id: str,
        contest_id: str | None = None,
    ) -> list[DomjudgeDownloadFile]:
        safe_submit = domjudge_text(submit_id)
        if not safe_submit:
            raise RuntimeError("source files not found")
        safe_contest = None if contest_id is None else self._toolkit.contest_id(contest_id)
        submission = self._s.batch_scheduler.source_submission(safe_submit, contest_id=safe_contest)
        if submission is None:
            raise RuntimeError("source files not found")
        source_files = (
            (submission.source_name, submission.source_file),
            *submission.extra_source_items,
        )
        return [
            DomjudgeDownloadFile(filename, payload)
            for filename, payload in source_files
        ]

    def domjudge_get_testcase_files(
        self,
        testcase_id: int,
    ) -> list[DomjudgeDownloadFile]:
        token = int(testcase_id)
        row, resolution_source = self._s.batch_scheduler.testcase_refs(token)
        if row is None:
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s resolved=missing",
                token,
            )
            raise RuntimeError("testcase files not found")
        input_ref = domjudge_text(row["input_ref"])
        answer_ref = domjudge_text(row["answer_ref"])
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

    def _domjudge_executable_rows(
        self,
        *,
        kind: str,
        executable_hash: str,
    ) -> list[DomjudgeDownloadFile]:
        cached_rows = self._toolkit.read_executable_cache(kind=kind, executable_hash=executable_hash)
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

    def _domjudge_active_batch_script_hash(
        self,
        *,
        hostname: str,
        kind: str,
        requested_id: int,
    ) -> tuple[int, str] | None:
        safe_host = self._core.normalize_hostname(hostname)
        if not safe_host:
            return None
        leased_match = self._s.batch_scheduler.leased_script_hash_for_host(
            safe_host,
            kind=kind,
            script_id=requested_id,
        )
        if leased_match is not None:
            return leased_match
        field = domjudge_script_hash_field(kind)
        matching: dict[int, str] = {}
        for batch_row in self._s.batch_scheduler.host_context_batches(safe_host):
            script_hash = domjudge_lower_text(batch_row[field])
            if script_hash and domjudge_script_id(script_hash) == requested_id:
                matching[int(batch_row["batch_id"])] = script_hash
        matching_hashes = set(matching.values())
        if len(matching_hashes) != 1:
            return None
        script_hash = next(iter(matching_hashes))
        batch_id = next(batch_id for batch_id, value in matching.items() if value == script_hash)
        return (batch_id, script_hash)

    def _domjudge_shared_script_hash(self, *, kind: str, requested_id: int) -> str:
        matching_hashes = self._s.batch_scheduler.active_script_hashes(kind, requested_id)
        if not matching_hashes:
            raise RuntimeError("script files not found")
        if len(matching_hashes) > 1:
            raise RuntimeError("ambiguous script id")
        return next(iter(matching_hashes))

    def _domjudge_fail_batch_executable_lookup(self, *, batch_id: int, error_text: str) -> None:
        safe_error = domjudge_text(error_text)
        if not safe_error:
            safe_error = "judgehost executable cache missing"
        now_text = now_iso()
        self._s.batch_scheduler.append_debug_text(
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
        requested_id = domjudge_parse_script_id(script_id)
        token = domjudge_lower_text(kind)
        _ = domjudge_script_hash_field(token)
        active_match = None if not hostname else self._domjudge_active_batch_script_hash(
            hostname=hostname,
            kind=token,
            requested_id=requested_id,
        )
        if active_match is not None:
            batch_id, executable_hash = active_match
            try:
                return self._domjudge_executable_rows(kind=token, executable_hash=executable_hash)
            except RuntimeError:
                self._domjudge_fail_batch_executable_lookup(
                    batch_id=batch_id,
                    error_text=f"judgehost executable cache missing: {token}/{requested_id}",
                )
                raise
        executable_hash = self._domjudge_shared_script_hash(kind=token, requested_id=requested_id)
        return self._domjudge_executable_rows(kind=token, executable_hash=executable_hash)

    def domjudge_get_version_commands(self, judgetask_id: int) -> dict[str, object]:
        try:
            return self._toolchain_versions.version_commands(int(judgetask_id))
        except Exception:
            logger.exception(
                "failed to prepare judgehost toolchain version commands judgetask_id=%s",
                judgetask_id,
            )
            return {}

    def domjudge_check_versions(
        self,
        judgetask_id: int,
        *,
        hostname: str,
        compiler: str = "",
        runner: str = "",
    ) -> dict[str, object]:
        receipt = self._s.batch_scheduler.acquire_case_callback_receipt(
            int(judgetask_id)
        )
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
                raise RuntimeError(
                    "judgehost case is not in a version callback state"
                )
            expected_hostname = (
                receipt.lease_owner or receipt.last_callback_hostname
            )
            if not expected_hostname or expected_hostname != safe_host:
                return {}
            try:
                recorded = self._toolchain_versions.record_report(
                    int(judgetask_id),
                    hostname=safe_host,
                    compiler=compiler,
                    runner=runner,
                )
            except Exception:
                logger.exception(
                    "failed to record judgehost toolchain versions "
                    "judgetask_id=%s hostname=%s",
                    judgetask_id,
                    hostname,
                )
                recorded = False
            if recorded:
                self._queue._record_host_event_conn(
                    hostname=safe_host,
                    action="versions",
                    task_id=receipt.task_id,
                    run_id=receipt.run_id,
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

        case = self._s.batch_scheduler.fetch_case(int(case_id))
        if case is None:
            return True
        if domjudge_lower_text(case["status"]) not in {"reported", "cancelled"}:
            return False
        if bool(case["completion_acknowledged"]):
            self._diagnostic_publisher.flush_pending(int(case_id))
            return True
        batch = self._s.batch_scheduler.fetch_batch(int(batch_id))
        if batch is not None and domjudge_text(batch["failure_runresult"]):
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
        row: dict[str, object],
        task_payload: dict[str, object],
        task_kind: str,
        reported_at: str,
        reported_monotonic: float,
    ) -> CaseReportTelemetry:
        return CaseReportTelemetry(
            hostname=hostname,
            reported_at=reported_at,
            reported_monotonic=reported_monotonic,
            verification_id=domjudge_text(task_payload.get("verification_id")),
            problem_slug=domjudge_text(task_payload.get("problem")),
            task_kind=task_kind,
            source_label=Path(
                domjudge_text(
                    task_payload.get("source_label"),
                    default=domjudge_text(row.get("source_name")),
                ).replace("\\", "/")
            ).name,
            test_name=domjudge_text(row["test_name"]),
        )

    def _complete_cancelled_case_receipt(
        self,
        *,
        case_id: int,
        generation: int,
        row: dict[str, object],
        report: CaseReportTelemetry,
    ) -> bool:
        accepted = self._s.batch_scheduler.commit_cancelled_receipt(
            case_id,
            generation=generation,
            updated_at=report.reported_at,
            report_telemetry=report,
        )
        if not accepted:
            return False
        task_id = domjudge_text(row["task_id"])
        batch_id = int(row["batch_id"])
        self._completion_publisher.acknowledge_terminal_case(
            int(case_id),
            reason="judgehost task cancelled",
        )
        batch_row = self._s.batch_scheduler.batch_finalize_row(batch_id)
        if batch_row is not None:
            self._batch_finalizer.finalize_task_if_ready(
                task_id,
                batch_row=dict(batch_row),
            )
        self._batch_finalizer.finalize_batch_if_ready(
            batch_id,
            require_completion_ack=True,
        )
        return True

    def domjudge_update_judging(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> None:
        receipt = self._s.batch_scheduler.acquire_case_callback_receipt(int(judgetask_id))
        if receipt is None:
            logger.info("ignoring update for unknown judging run id: %s", int(judgetask_id))
            return
        try:
            self._touch_task_verification(receipt.task_id)
            self._process_domjudge_judging_update(
                hostname,
                judgetask_id,
                payload,
                receipt_generation=receipt.claim_generation,
            )
        finally:
            self._release_case_callback_receipt(receipt)

    def _process_domjudge_judging_update(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
        *,
        receipt_generation: int,
    ) -> None:
        safe_host = self._core.normalize_hostname(hostname)
        case_id = int(judgetask_id)
        case_row = self._s.batch_scheduler.case_execution_row(case_id)
        if case_row is None:
            # judgedaemon may still report progress for a case that was already
            # dropped by server-side cancellation/startup cleanup. Treat as
            # idempotent no-op so daemon can continue without fatal retries.
            logger.info("ignoring update for unknown judging run id: %s", case_id)
            return
        batch_status = domjudge_lower_text(case_row["batch_status"])
        case_status = domjudge_lower_text(case_row["case_status"])
        lease_owner = domjudge_text(case_row["case_lease_owner"])
        expected_hostname = lease_owner or domjudge_text(
            case_row["last_callback_hostname"]
        )
        if not expected_hostname or expected_hostname != safe_host:
            raise RuntimeError("judgehost does not own judging run")
        batch_id = int(case_row["batch_id"])
        compile_success = None
        if "compile_success" in payload:
            compile_success = (
                1
                if domjudge_bool(
                    payload.get("compile_success"),
                    default=False,
                )
                else 0
            )

        def _payload_blob_as_b64(value: object) -> str:
            raw = self._toolkit.payload_blob_bytes(value)
            if raw:
                return base64.b64encode(
                    truncate_stored_log_bytes(
                        raw,
                        self._s.config_values.snapshot(),
                    )
                ).decode("ascii")
            return domjudge_text(value)

        compile_output = ""
        compile_meta = ""
        compile_updated_at = ""
        compile_success_recorded: bool | None = None
        if compile_success == 1:
            compile_output = _payload_blob_as_b64(payload.get("output_compile"))
            compile_meta = _payload_blob_as_b64(payload.get("compile_metadata"))
            compile_updated_at = now_iso()
            compile_success_recorded = (
                self._s.batch_scheduler.record_compile_success(
                    case_id,
                    hostname=safe_host,
                    receipt_generation=receipt_generation,
                    compile_output_b64=compile_output,
                    compile_metadata_b64=compile_meta,
                    updated_at=compile_updated_at,
                )
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
        safe_task_id = domjudge_text(case_row["task_id"])
        if not self._domjudge_task_accepts_case_updates(safe_task_id):
            logger.info("ignoring update for cancelled DOMjudge task case id: %s", case_id)
            return
        self._queue._record_host_event_conn(
            hostname=safe_host,
            action="update",
            task_id=safe_task_id,
            run_id=domjudge_text(case_row["run_id"]),
        )
        if compile_success is not None:
            updated_at = compile_updated_at or now_iso()
            if compile_success == 0:
                compile_output = _payload_blob_as_b64(
                    payload.get("output_compile")
                )
                compile_meta = _payload_blob_as_b64(
                    payload.get("compile_metadata")
                )
                compile_blob = self._toolkit.b64_decode(compile_output)
                compile_log = compile_blob.decode("utf-8", errors="replace").strip()
                failure_text = (
                    domjudge_feedback_text_from_text(
                        compile_log
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
                claim = self._s.batch_scheduler.claim_compile_failure(
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
                    raise RuntimeError(
                        "judgehost case lease changed before compile failure claim"
                    )
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
                raise RuntimeError(
                    "judgehost batch closed before compile success update"
                )

    def domjudge_add_judging_run(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> int:
        receipt = self._s.batch_scheduler.acquire_case_callback_receipt(int(judgetask_id))
        if receipt is None:
            logger.info(
                "ignoring add_judging_run for unknown judging run id: %s",
                int(judgetask_id),
            )
            return 1
        try:
            return self._domjudge_add_judging_run_with_receipt(
                receipt,
                hostname,
                judgetask_id,
                payload,
            )
        finally:
            self._release_case_callback_receipt(receipt)

    def _domjudge_add_judging_run_with_receipt(
        self,
        receipt: CaseCallbackReceipt,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
    ) -> int:
        reported_monotonic = time.monotonic()
        reported_at = now_iso()
        safe_host = self._core.normalize_hostname(hostname)
        case_row = self._s.batch_scheduler.fetch_case(int(judgetask_id))
        if case_row is None:
            raise RuntimeError("pinned judgehost case disappeared")
        case_status = domjudge_lower_text(case_row["status"])
        expected_hostname = domjudge_text(
            case_row["lease_owner"]
        ) or domjudge_text(case_row["last_callback_hostname"])
        if not expected_hostname or expected_hostname != safe_host:
            raise RuntimeError("judgehost does not own judging run")
        if case_status in {"reported", "cancelled"}:
            self._complete_terminal_callback_case(
                int(judgetask_id),
                int(case_row["batch_id"]),
            )
            logger.info("ignoring stale add_judging_run result for case id: %s", int(judgetask_id))
            return 1
        if case_status == "reporting":
            raise CaseClaimBusy("judgehost case result is already being processed")
        if case_status != "leased":
            raise RuntimeError("judgehost case is not leased for this result")
        self._touch_task_verification(domjudge_text(case_row["task_id"]))
        claim = self._s.batch_scheduler.claim_case_reporting(
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
            task_id=domjudge_text(case_row["task_id"]),
            run_id=domjudge_text(case_row["run_id"]),
        )
        self._s.batch_scheduler.observe_compile_success_from_case_claim(
            claim.case_id,
            generation=claim.generation,
            lease_owner=safe_host,
            updated_at=reported_at,
        )
        task_payload = self._core.task_payload(claim.task_id)
        verification_source = task_payload.get("verification_source", "")
        task_kind = self._toolkit.task_kind(
            task_payload,
            verification_source=verification_source,
        )
        report_telemetry = self._case_report_telemetry(
            hostname=safe_host,
            row=dict(case_row),
            task_payload=task_payload,
            task_kind=task_kind,
            reported_at=reported_at,
            reported_monotonic=reported_monotonic,
        )
        if claim.cancel_requested:
            if not self._complete_cancelled_case_receipt(
                case_id=claim.case_id,
                generation=claim.generation,
                row=dict(case_row),
                report=report_telemetry,
            ):
                raise RuntimeError("judgehost cancellation receipt claim was lost")
            return 1

        def _abort_unfinished_claim() -> None:
            if self._s.batch_scheduler.abort_case_claim(
                claim.case_id,
                generation=claim.generation,
                updated_at=now_iso(),
            ):
                self._batch_finalizer.finalize_batch_if_ready(claim.batch_id)

        try:
            result_id = self._process_domjudge_judging_run(
                safe_host,
                judgetask_id,
                payload,
                claim_generation=claim.generation,
                report_telemetry=report_telemetry,
            )
        except Exception:
            _abort_unfinished_claim()
            raise
        refreshed = self._s.batch_scheduler.fetch_case(claim.case_id)
        if refreshed is not None and domjudge_lower_text(refreshed["status"]) == "reporting":
            _abort_unfinished_claim()
        return result_id

    def _process_domjudge_judging_run(
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
        row = self._s.batch_scheduler.case_execution_row(case_id)
        if row is None:
            raise RuntimeError("pinned judgehost case disappeared")
        batch_status = domjudge_lower_text(row["batch_status"])
        if batch_status != "open":
            raise RuntimeError("judgehost batch closed during result processing")
        if domjudge_lower_text(row["case_status"]) != "reporting":
            raise RuntimeError("judgehost case result claim changed")
        if domjudge_text(row["case_lease_owner"]) != safe_host:
            raise RuntimeError("judgehost does not own judging run")
        batch_id = int(row["batch_id"])
        safe_task_id = domjudge_text(row["task_id"])
        task_payload = self._core.task_payload(safe_task_id) if safe_task_id else {}
        verification_source = task_payload.get("verification_source", "")
        task_kind = self._toolkit.task_kind(task_payload, verification_source=verification_source)
        compile_only = task_kind == self._TASK_KIND_COMPILE_ONLY

        def _load_json_object(raw: object) -> dict[str, object]:
            text = domjudge_text(raw)
            if not text:
                return {}
            try:
                return cast(dict[str, object], json.loads(text))
            except Exception:
                return {}

        payload_files: dict[str, bytes] = {}

        def _capture_payload_file(
            name: str,
            value: object,
            *,
            allow_empty: bool = False,
        ) -> bytes:
            if value is None:
                if allow_empty:
                    payload_files[name] = b""
                return b""
            raw = self._toolkit.payload_blob_bytes(value)
            if (not raw) and (not allow_empty):
                return b""
            payload_files[name] = raw
            return raw

        if not compile_only:
            # Keep program.out intact. For generate-input tasks this is
            # semantic data consumed by downstream verification tasks.
            _capture_payload_file(
                "program.out",
                payload.get("output_run"),
                allow_empty=True,
            )
        _capture_payload_file("program.err", payload.get("output_error"), allow_empty=True)
        _capture_payload_file("system.out", payload.get("output_system"), allow_empty=True)
        _capture_payload_file("judgemessage.txt", payload.get("output_diff"), allow_empty=True)
        _capture_payload_file(
            "program.meta",
            payload.get("metadata"),
            allow_empty=True,
        )
        _capture_payload_file(
            "compare.meta",
            payload.get("compare_metadata"),
            allow_empty=True,
        )
        team_message_blob = self._toolkit.payload_blob_bytes(payload.get("team_message"))

        interactive = domjudge_lower_text(row["mode"]) == "interactive"
        run_cfg_for_capture = _load_json_object(row["run_config_json"])
        pass_limit = max(1, domjudge_parse_int(run_cfg_for_capture.get("pass_limit"), 1))
        capture_expected = (
            task_kind in {
                self._TASK_KIND_MAIN_CORRECT,
                self._TASK_KIND_SOLUTION_RUN,
            }
            and (interactive or pass_limit > 1)
        )
        bundle_limit_bytes = min(
            8 * 1024 * 1024,
            max(
                1024,
                int(
                    run_output_kb(self._s.config_values.snapshot())
                    * 1024
                    * 3
                    // 4
                ),
            ),
        )
        callback_pass = max(0, domjudge_parse_int(payload.get("pass"), 0))
        pass_bundle: PassBundle | None = None
        capture_warning = ""
        rejected_bundle = False
        if capture_expected:
            try:
                pass_bundle = parse_pass_bundle(
                    team_message_blob,
                    max_bundle_bytes=bundle_limit_bytes,
                    max_member_bytes=bundle_limit_bytes,
                )
                if (
                    pass_bundle is not None
                    and callback_pass > 0
                    and callback_pass != pass_bundle.final_pass_number
                ):
                    raise InvalidPassBundle(
                        "callback pass does not match final-pass-number"
                    )
            except InvalidPassBundle as exc:
                pass_bundle = None
                rejected_bundle = True
                capture_warning = (
                    "historical pass artifact capture was incomplete: " + str(exc)
                )
        if pass_bundle is None:
            payload_files["teammessage.txt"] = (
                b"" if rejected_bundle else team_message_blob
            )
            if capture_expected and not capture_warning:
                capture_warning = "historical pass artifact capture was incomplete: bundle missing"
        else:
            historical_feedback: dict[int, bytes] = {}
            final_feedback = payload_files["judgemessage.txt"]
            try:
                historical_feedback, final_feedback = split_pass_feedback(
                    pass_bundle,
                    final_feedback,
                )
            except InvalidPassBundle as exc:
                capture_warning = (
                    "historical pass artifact capture was incomplete: " + str(exc)
                )
            for bundled_pass in pass_bundle.passes:
                for name, content in bundled_pass.files.items():
                    stored_content = historical_feedback.get(
                        bundled_pass.number,
                        content,
                    ) if name == "judgemessage.txt" else content
                    cache_name = pass_cache_file_name(bundled_pass.number, name)
                    payload_files[cache_name] = stored_content
            final_files = pass_bundle.pass_files(pass_bundle.final_pass_number)
            payload_files["teammessage.txt"] = final_files["teammessage.txt"]
            payload_files["judgemessage.txt"] = final_feedback
            reduced: dict[str, list[int]] = {}
            for bundled_pass in pass_bundle.passes[:-1]:
                if bundled_pass.capture_status != CAPTURE_COMPLETE:
                    reduced.setdefault(bundled_pass.capture_status, []).append(
                        bundled_pass.number
                    )
            if reduced:
                groups = [
                    f"passes {', '.join(str(number) for number in numbers)} {status}"
                    for status, numbers in reduced.items()
                ]
                reduced_warning = (
                    "historical pass artifacts were reduced: " + "; ".join(groups)
                )
                capture_warning = (
                    f"{capture_warning}; {reduced_warning}"
                    if capture_warning
                    else reduced_warning
                )

        source_name = domjudge_text(row["source_name"])
        source_hash = domjudge_lower_text(row["source_hash"])
        if re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            raise RuntimeError(f"missing source_hash for DOMjudge case {case_id}")

        testcase_hash = domjudge_lower_text(row["testcase_hash"])
        testcase_input_hash = domjudge_lower_text(row["testcase_input_hash"])
        testcase_answer_hash = domjudge_lower_text(row["testcase_answer_hash"])
        if re.fullmatch(r"[0-9a-f]{64}", testcase_hash) is None:
            raise RuntimeError(f"missing testcase_hash for DOMjudge case {case_id}")
        if re.fullmatch(r"[0-9a-f]{64}", testcase_input_hash) is None:
            raise RuntimeError(f"missing testcase_input_hash for DOMjudge case {case_id}")
        if re.fullmatch(r"[0-9a-f]{64}", testcase_answer_hash) is None:
            raise RuntimeError(f"missing testcase_answer_hash for DOMjudge case {case_id}")

        compile_hash = domjudge_lower_text(row["compile_hash"])
        run_hash = domjudge_lower_text(row["run_hash"])
        compare_hash = domjudge_lower_text(row["compare_hash"])
        compile_cfg = _load_json_object(row["compile_config_json"])
        run_cfg = run_cfg_for_capture
        compare_cfg = _load_json_object(row["compare_config_json"])
        compile_config_hash = domjudge_json_hash(compile_cfg)
        run_config_hash = domjudge_json_hash(run_cfg)
        compare_config_hash = domjudge_json_hash(compare_cfg)
        toolchain_cmd_digest = domjudge_lower_text(
            compile_cfg.get("toolchain_cmd_digest")
        )
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._toolkit.toolchain_cmd_digest(
                source_name
            )

        current_case = self._s.batch_scheduler.fetch_case(case_id)
        if current_case is not None and bool(current_case["cancel_requested"]):
            self._complete_cancelled_case_receipt(
                case_id=case_id,
                generation=claim_generation,
                row=dict(row),
                report=report_telemetry,
            )
            return 1

        cache_files: dict[str, bytes] = dict(payload_files)
        cached_payloads = {
            name: self._s.runtime_blob_store.put_bytes(content)
            for name, content in cache_files.items()
        }
        captured_artifacts = {
            name: CapturedCaseArtifact(
                content=cache_files[name],
                blob_ref=payload_file.blob_ref or "",
            )
            for name, payload_file in cached_payloads.items()
        }
        debug_context = self._s.batch_scheduler.case_debug_context(case_id)
        debug_text = ""
        if debug_context is not None:
            debug_text = domjudge_text(debug_context["case_debug_text"])
            if not debug_text:
                debug_text = domjudge_text(debug_context["batch_debug_text"])
        normalized = normalize_captured_case(
            CapturedJudgehostCase(
                test_name=domjudge_text(row["test_name"]),
                input_ref=domjudge_text(row["input_ref"]),
                interactive=interactive,
                raw_runresult=domjudge_text(
                    payload.get("runresult"),
                    default="internal-error",
                ),
                runtime_fallback_sec=domjudge_parse_float(
                    payload.get("runtime"),
                    0.0,
                ),
                score_text=domjudge_text(payload.get("score")),
                run_config=run_cfg,
                artifacts=captured_artifacts,
                pass_bundle=pass_bundle,
                capture_warning=capture_warning,
                debug_text=debug_text,
            )
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
            files=cached_payloads,
            shortcut_eligible=shortcut_eligible,
        )
        now_text = now_iso()
        outcome = self._s.batch_scheduler.commit_case_result(
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
            batch_row = self._s.batch_scheduler.batch_finalize_row(batch_id)
            if batch_row is not None:
                self._batch_finalizer.finalize_task_if_ready(
                    safe_task_id,
                    batch_row=dict(batch_row),
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
        current_batch = self._s.batch_scheduler.fetch_batch(batch_id)
        if (
            current_batch is not None
            and domjudge_text(current_batch["failure_runresult"])
        ):
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
        batch_row = self._s.batch_scheduler.batch_finalize_row(batch_id)
        if batch_row is not None:
            try:
                self._batch_finalizer.finalize_task_if_ready(
                    safe_task_id,
                    batch_row=dict(batch_row),
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
        safe_desc = domjudge_text(description, default="judgehost internal error")
        if judgetask_id is None:
            return 0
        case_id = int(judgetask_id)
        receipt = self._s.batch_scheduler.acquire_case_callback_receipt(case_id)
        if receipt is None:
            return 0
        try:
            if receipt.status not in {
                "leased",
                "reporting",
                "reported",
                "cancelled",
            }:
                raise RuntimeError(
                    "judgehost case is not in a diagnostic callback state"
                )
            safe_host = (
                self._core.normalize_hostname(hostname)
                if hostname
                else receipt.lease_owner or receipt.last_callback_hostname
            )
            expected_hostname = (
                receipt.lease_owner or receipt.last_callback_hostname
            )
            if not expected_hostname or safe_host != expected_hostname:
                raise RuntimeError("judgehost does not own judging run")
            self._touch_task_verification(receipt.task_id)
            payload_text = self._domjudge_debug_payload_text(
                {} if payload is None else payload
            )
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
                raise RuntimeError(
                    "judgehost case lease changed before internal-error claim"
                )
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

    def _domjudge_debug_payload_text(self, payload: dict[str, object]) -> str:
        if not payload:
            return ""
        interesting_markers = (
            "fail",
            "error",
            "exception",
            "trace",
            "crash",
            "compare",
            "expected",
            "unexpected",
        )
        handled_keys = {
            "judgehostlog",
            "description",
            "message",
            "error",
            "detail",
            "details",
            "stderr",
            "stdout",
            "output_error",
            "output_system",
            "output_diff",
            "compare_output",
            "compare_error",
            "judgemessage",
            "team_message",
        }

        def _decode_maybe_b64(text: str) -> str:
            if not text:
                return ""
            compact = "".join(text.split())
            if compact and (len(compact) % 4 == 0) and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
                try:
                    blob = self._toolkit.b64_decode(compact)
                except RuntimeError:
                    blob = b""
                if blob:
                    decoded = blob.decode("utf-8", errors="replace").strip()
                    if decoded:
                        printable = sum((ch.isprintable() or ch in {"\n", "\r", "\t"}) for ch in decoded)
                        if printable >= int(len(decoded) * 0.9):
                            return decoded
            return text

        def _looks_like_raw_b64(text: str) -> bool:
            compact = "".join(text.split())
            return bool(compact) and len(compact) >= 64 and (len(compact) % 4 == 0) and bool(re.fullmatch(r"[A-Za-z0-9+/=]+", compact))

        lines: list[str] = []
        seen: set[str] = set()

        def _append_text(text: str) -> None:
            decoded = _decode_maybe_b64(text)
            if not decoded:
                return
            for raw_line in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                line = domjudge_text(raw_line)
                if not line:
                    continue
                token = line.lower()
                if token in seen:
                    continue
                seen.add(token)
                lines.append(line)
                if len(lines) >= 16:
                    return

        def _append_judgehost_log(text: str) -> None:
            decoded = _decode_maybe_b64(text)
            if not decoded:
                return
            raw_lines = [domjudge_text(item) for item in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
            raw_lines = [item for item in raw_lines if item]
            if not raw_lines:
                return
            interesting: list[str] = []
            for idx, line in enumerate(raw_lines):
                low = line.lower()
                if any(
                    marker in low
                    for marker in (
                        "comparing failed",
                        "compare script output",
                        "expected one of 42/43",
                        "testcase_run.sh",
                        "fail ",
                        "fail:",
                        "internal error",
                    )
                ):
                    for near in raw_lines[max(0, idx - 1) : min(len(raw_lines), idx + 2)]:
                        if near:
                            interesting.append(near)
            if not interesting:
                interesting = raw_lines[-8:]
            for line in interesting:
                _append_text(line)
                if len(lines) >= 16:
                    return

        def _walk_scalars(value: object, *, key_name: str="") -> list[str]:
            out: list[str] = []
            key_token = domjudge_lower_text(key_name)
            if key_token in handled_keys or key_token == "disabled":
                return out
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    out.extend(_walk_scalars(sub_value, key_name=domjudge_text(sub_key)))
                    if len(out) >= 32:
                        break
                return out
            if isinstance(value, list):
                for item in value:
                    out.extend(_walk_scalars(item, key_name=key_name))
                    if len(out) >= 32:
                        break
                return out
            decoded = _decode_maybe_b64("" if value is None else str(value))
            if not decoded or _looks_like_raw_b64(decoded):
                return out
            for raw_line in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                text = domjudge_text(raw_line)
                if not text:
                    continue
                low = text.lower()
                if any(marker in low for marker in interesting_markers):
                    out.append(text)
                if len(out) >= 32:
                    break
            return out

        for key in handled_keys:
            if key not in payload:
                continue
            value = payload[key]
            text = "" if value is None else str(value)
            if key == "judgehostlog":
                _append_judgehost_log(text)
            else:
                _append_text(text)
            if len(lines) >= 16:
                break
        if len(lines) < 16:
            for text in _walk_scalars(payload):
                _append_text(text)
                if len(lines) >= 16:
                    break
        if not lines:
            return ""
        compact = "\n".join(lines)
        if len(compact) > 4000:
            compact = compact[:4000].rstrip()
        return compact


    def domjudge_add_debug_info(self, *, hostname: str, judgetask_id: int, payload: dict[str, object] | None = None) -> None:
        safe_host = self._core.normalize_hostname(hostname)
        case_id = int(judgetask_id)
        receipt = self._s.batch_scheduler.acquire_case_callback_receipt(case_id)
        if receipt is None:
            return
        try:
            if receipt.status not in {
                "leased",
                "reporting",
                "reported",
                "cancelled",
            }:
                raise RuntimeError(
                    "judgehost case is not in a diagnostic callback state"
                )
            expected_hostname = (
                receipt.lease_owner or receipt.last_callback_hostname
            )
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
            debug_text = self._domjudge_debug_payload_text(debug_payload)
            if debug_text:
                disposition = self._diagnostic_publisher.record_debug_info(
                    case_id,
                    hostname=safe_host,
                    text=debug_text,
                    receipt_generation=receipt.claim_generation,
                )
                if disposition == "rejected":
                    raise RuntimeError(
                        "judgehost case lease changed before diagnostic claim"
                    )
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
