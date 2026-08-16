import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import TypeVar, cast

from app.service.judgehost.configuration import (
    JudgehostConfiguration,
    JudgehostSettings,
)
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
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.validation import normalize_judgehost_hostname
from app.service.judgehost.cache.case_result import CaseCacheLookup, CaseResultCache
from app.service.judgehost.callback.diagnostic_payload import parse_diagnostic_payload
from app.service.judgehost.callback.model import CallbackOutcome, HostEvent
from app.service.judgehost.domjudge.limits import (
    truncate_stored_log_bytes,
    upload_max_bytes,
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
from app.service.judgehost.domjudge.codec import (
    decode_base64,
    decode_text,
)
from app.service.judgehost.domjudge.scripts import DomjudgeScriptCatalog
from app.service.judgehost.domjudge.task_plan import task_kind
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.platform.error_text import aux_display_text_limit_bytes
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.execution.codec import execution_result_json

logger = logging.getLogger(__name__)

Acknowledgement = TypeVar("Acknowledgement")


class JudgehostCallbackIngestion:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    _TASK_KIND_COMPILE_ONLY = "compile-only"

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        tasks: JudgehostTaskRegistry,
        configuration: JudgehostConfiguration,
        runtime_blob_store: RuntimeBlobStore,
        scripts: DomjudgeScriptCatalog,
        *,
        case_result_cache: CaseResultCache,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._tasks = tasks
        self._configuration = configuration
        self._runtime_blob_store = runtime_blob_store
        self._scripts = scripts
        self._artifact_capture = CaseArtifactCapture(runtime_blob_store)
        self._case_result_cache = case_result_cache

    @staticmethod
    def _display_text_limit_bytes(settings: JudgehostSettings) -> int:
        return aux_display_text_limit_bytes(settings.values)

    @staticmethod
    def _outcome(
        acknowledgement: Acknowledgement,
        terminal_batch_ids: tuple[int, ...] = (),
        *,
        verification_ids: tuple[str, ...] = (),
        host_events: tuple[HostEvent, ...] = (),
    ) -> CallbackOutcome[Acknowledgement]:
        return CallbackOutcome(
            acknowledgement=acknowledgement,
            terminal_batch_ids=tuple(dict.fromkeys(terminal_batch_ids)),
            touched_verification_ids=tuple(
                dict.fromkeys(token for token in verification_ids if token)
            ),
            host_events=host_events,
        )

    def _task_payload(self, task_id: str) -> dict[str, object]:
        row = self._tasks.get(task_id)
        return {} if row is None else row["payload"].copy()

    def _release_case_callback_receipt(
        self,
        receipt: CaseCallbackReceipt,
    ) -> None:
        self._batch_runtime.release_case_callback_receipt(receipt.receipt_id)

    def _task_accepts_case_updates(self, task_id: str) -> bool:
        task_row = self._tasks.get(task_id)
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

    def _complete_terminal_callback_case(
        self,
        case_id: int,
        batch_id: int,
        *,
        reason: str = "",
    ) -> bool:
        """Acknowledge one retry without splitting a program-failure commit."""

        case = self._batch_runtime.fetch_case(int(case_id))
        if case is None:
            return True
        if case["status"] not in {"reported", "cancelled"}:
            return False
        if bool(case["completion_acknowledged"]):
            return True
        batch = self._batch_runtime.fetch_batch(int(batch_id))
        if batch is not None and batch["failure_runresult"]:
            return True
        del reason
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
        accepted = self._batch_runtime.commit_cancelled_receipt(
            case_id,
            generation=generation,
            updated_at=report.reported_at,
            report_telemetry=report,
        )
        if not accepted:
            return False
        return True

    def domjudge_update_judging(
        self, hostname: str, judgetask_id: int, payload: dict[str, object]
    ) -> CallbackOutcome[None]:
        settings = self._configuration.snapshot()
        receipt = self._batch_runtime.acquire_case_callback_receipt(int(judgetask_id))
        if receipt is None:
            logger.info(
                "ignoring update for unknown judging run id: %s", int(judgetask_id)
            )
            return self._outcome(None)
        try:
            host_event = self._process_judging_update(
                hostname,
                judgetask_id,
                payload,
                receipt_generation=receipt.claim_generation,
                settings=settings,
            )
        finally:
            self._release_case_callback_receipt(receipt)
        return self._outcome(
            None,
            (receipt.batch_id,),
            verification_ids=(receipt.verification_id,),
            host_events=() if host_event is None else (host_event,),
        )

    def _process_judging_update(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
        *,
        receipt_generation: int,
        settings: JudgehostSettings,
    ) -> HostEvent | None:
        safe_host = normalize_judgehost_hostname(hostname)
        case_id = int(judgetask_id)
        case_row = self._batch_runtime.case_execution_row(case_id)
        if case_row is None:
            # judgedaemon may still report progress for a case that was already
            # dropped by server-side cancellation/startup cleanup. Treat as
            # idempotent no-op so daemon can continue without fatal retries.
            logger.info("ignoring update for unknown judging run id: %s", case_id)
            return None
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
                        settings.values,
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
            compile_success_recorded = self._batch_runtime.record_compile_success(
                case_id,
                hostname=safe_host,
                receipt_generation=receipt_generation,
                compile_output_b64=compile_output,
                compile_metadata_b64=compile_meta,
                updated_at=compile_updated_at,
            )
        if batch_status in {"finalize-pending", "finalizing"}:
            return None
        if batch_status != "open":
            logger.info(
                "ignoring update for terminal DOMjudge batch case id: %s", case_id
            )
            return None
        if case_status in {"reported", "cancelled"}:
            self._complete_terminal_callback_case(
                case_id,
                batch_id,
            )
            return None
        if case_status not in {"leased", "reporting"}:
            raise RuntimeError("judgehost case does not accept this update")
        safe_task_id = case_row["task_id"]
        if not self._task_accepts_case_updates(safe_task_id):
            logger.info(
                "ignoring update for cancelled DOMjudge task case id: %s", case_id
            )
            return None
        if compile_success == 0:
            compile_output = _payload_blob_as_b64(payload.get("output_compile"))
            compile_meta = _payload_blob_as_b64(payload.get("compile_metadata"))
        host_event = HostEvent(
            hostname=safe_host,
            action="update",
            task_id=safe_task_id,
            run_id=case_row["run_id"],
        )
        if compile_success is not None:
            updated_at = compile_updated_at or now_iso()
            if compile_success == 0:
                compile_blob = decode_base64(compile_output)
                compile_log = compile_blob.decode("utf-8", errors="replace").strip()
                failure_text = (
                    bounded_feedback_text(
                        compile_log,
                        limit_bytes=self._display_text_limit_bytes(settings),
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
                claim = self._batch_runtime.claim_compile_failure(
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
                    return host_event
                if claim.outcome == "cancelled":
                    return host_event
                return host_event
            if not compile_success_recorded:
                if self._complete_terminal_callback_case(case_id, batch_id):
                    return host_event
                raise RuntimeError(
                    "judgehost batch closed before compile success update"
                )
        return host_event

    def domjudge_add_judging_run(
        self, hostname: str, judgetask_id: int, payload: dict[str, object]
    ) -> CallbackOutcome[int]:
        settings = self._configuration.snapshot()
        receipt = self._batch_runtime.acquire_case_callback_receipt(int(judgetask_id))
        if receipt is None:
            logger.info(
                "ignoring add_judging_run for unknown judging run id: %s",
                int(judgetask_id),
            )
            return self._outcome(1)
        try:
            acknowledgement, host_event = self._add_judging_run_with_receipt(
                receipt,
                hostname,
                judgetask_id,
                payload,
                settings=settings,
            )
        finally:
            self._release_case_callback_receipt(receipt)
        return self._outcome(
            acknowledgement,
            (receipt.batch_id,),
            verification_ids=(receipt.verification_id,),
            host_events=() if host_event is None else (host_event,),
        )

    def _add_judging_run_with_receipt(
        self,
        receipt: CaseCallbackReceipt,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
        *,
        settings: JudgehostSettings,
    ) -> tuple[int, HostEvent | None]:
        reported_monotonic = time.monotonic()
        reported_at = now_iso()
        safe_host = normalize_judgehost_hostname(hostname)
        case_row = self._batch_runtime.fetch_case(int(judgetask_id))
        if case_row is None:
            raise RuntimeError("pinned judgehost case disappeared")
        case_status = case_row["status"]
        expected_hostname = (
            case_row["lease_owner"] or case_row["last_callback_hostname"]
        )
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
            return 1, None
        if case_status == "reporting":
            raise CaseClaimBusy("judgehost case result is already being processed")
        if case_status != "leased":
            raise RuntimeError("judgehost case is not leased for this result")
        claim = self._batch_runtime.claim_case_reporting(
            int(judgetask_id),
            hostname=safe_host,
            receipt_generation=receipt.claim_generation,
            now_text=reported_at,
        )
        if claim is None:
            raise RuntimeError("judgehost case lease changed before result claim")
        host_event = HostEvent(
            hostname=safe_host,
            action="report",
            task_id=case_row["task_id"],
            run_id=case_row["run_id"],
        )
        self._batch_runtime.observe_compile_success_from_case_claim(
            claim.case_id,
            generation=claim.generation,
            lease_owner=safe_host,
            updated_at=reported_at,
        )
        task_payload = self._task_payload(claim.task_id)
        verification_source = self._verification_source(task_payload)
        canonical_task_kind = task_kind(
            task_payload,
            verification_source=verification_source,
        )
        report_telemetry = self._case_report_telemetry(
            hostname=safe_host,
            test_name=case_row["test_name"],
            task_payload=task_payload,
            task_kind=canonical_task_kind,
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
            return 1, host_event

        def _abort_unfinished_claim() -> None:
            if self._batch_runtime.abort_case_claim(
                claim.case_id,
                generation=claim.generation,
                updated_at=now_iso(),
            ):
                return

        try:
            result_id = self._process_judging_run(
                safe_host,
                judgetask_id,
                payload,
                claim_generation=claim.generation,
                report_telemetry=report_telemetry,
                settings=settings,
            )
        except Exception:
            _abort_unfinished_claim()
            raise
        refreshed = self._batch_runtime.fetch_case(claim.case_id)
        if refreshed is not None and refreshed["status"] == "reporting":
            _abort_unfinished_claim()
        return result_id, host_event

    def _process_judging_run(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
        *,
        claim_generation: int,
        report_telemetry: CaseReportTelemetry,
        settings: JudgehostSettings,
    ) -> int:
        safe_host = normalize_judgehost_hostname(hostname)
        case_id = int(judgetask_id)
        row = self._batch_runtime.case_execution_row(case_id)
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
        task_payload = self._task_payload(safe_task_id) if safe_task_id else {}
        verification_source = self._verification_source(task_payload)
        canonical_task_kind = task_kind(
            task_payload, verification_source=verification_source
        )
        compile_only = canonical_task_kind == self._TASK_KIND_COMPILE_ONLY

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
                int(upload_max_bytes(settings.values) * 3 // 4),
            ),
        )
        callback_pass = max(0, parse_int(payload.get("pass"), 0))
        prepared_artifacts = self._artifact_capture.prepare(
            CaseArtifactRequest(
                payload=payload,
                task_kind=canonical_task_kind,
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
            raise RuntimeError(
                f"missing testcase_input_hash for DOMjudge case {case_id}"
            )
        if re.fullmatch(r"[0-9a-f]{64}", testcase_answer_hash) is None:
            raise RuntimeError(
                f"missing testcase_answer_hash for DOMjudge case {case_id}"
            )

        compile_hash = row["compile_hash"]
        run_hash = row["run_hash"]
        compare_hash = row["compare_hash"]
        compile_cfg = _load_json_object(row["compile_config_json"])
        run_cfg = run_cfg_for_capture
        compare_cfg = _load_json_object(row["compare_config_json"])
        compile_config_hash = RuntimeCacheIndex.signature(compile_cfg)
        run_config_hash = RuntimeCacheIndex.signature(run_cfg)
        compare_config_hash = RuntimeCacheIndex.signature(compare_cfg)
        toolchain_cmd_digest = decode_text(
            lower=True, raw=compile_cfg.get("toolchain_cmd_digest")
        )
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._scripts.toolchain_cmd_digest(
                settings,
                source_name,
            )

        current_case = self._batch_runtime.fetch_case(case_id)
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
        debug_context = self._batch_runtime.case_debug_context(case_id)
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
            limit_bytes=self._display_text_limit_bytes(settings),
        )
        case_result = normalized.result
        runresult = normalized.runresult
        verdict = normalized.verdict
        runtime_sec = normalized.runtime_sec
        cpu_sec = normalized.cpu_sec
        wall_sec = normalized.wall_sec
        memory_kb = normalized.memory_kb
        score_text = normalized.score_text

        cache_verification_source = verification_source or ""
        case_key_hash, case_signature = self._case_result_cache.identity(
            CaseCacheLookup(
                source_hash=source_hash,
                compile_hash=compile_hash,
                run_hash=run_hash,
                compare_hash=compare_hash,
                compile_config_hash=compile_config_hash,
                run_config_hash=run_config_hash,
                compare_config_hash=compare_config_hash,
                toolchain_cmd_digest=toolchain_cmd_digest,
                testcase_hash=testcase_hash,
                run_config=run_cfg,
                expected_behavior=decode_text(
                    raw=task_payload.get("expected_behavior"), default="unknown"
                ),
                main_correct=cache_verification_source == "main-correct",
                requires_output=(
                    cache_verification_source == "main-correct"
                    or "generate-input" in cache_verification_source
                ),
                bypass=False,
            )
        )
        shortcut_eligible = verdict != "FL"
        if compile_only and verdict != "OK":
            shortcut_eligible = False

        self._case_result_cache.store(
            key_hash=case_key_hash,
            signature=case_signature,
            tags={
                "source_hash": source_hash,
                "testcase_hash": testcase_hash,
                "verification_source": cache_verification_source,
                "task_kind": canonical_task_kind,
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
        outcome = self._batch_runtime.commit_case_result(
            case_id,
            generation=claim_generation,
            result=case_result,
            updated_at=now_text,
            report_telemetry=report_telemetry,
        )
        if outcome == "cancelled":
            return 1
        if outcome != "reported":
            if self._complete_terminal_callback_case(case_id, batch_id):
                logger.info(
                    "ignoring stale add_judging_run result for case id: %s", case_id
                )
                return 1
            raise RuntimeError("judgehost case result lost its completion claim")
        logger.debug(
            "domjudge add_judging_run host=%s batch_id=%s case_id=%s runresult=%s",
            safe_host,
            batch_id,
            case_id,
            runresult,
        )
        current_batch = self._batch_runtime.fetch_batch(batch_id)
        if current_batch is not None and current_batch["failure_runresult"]:
            # A compile/internal batch failure owns every still-open Case.
            # Do not publish the Case that happened to leave `reporting`
            # first: finalization waits for the whole batch and sends one
            # reported_many transaction for all affected verification tasks.
            return 1
        return 1

    def domjudge_internal_error(
        self,
        *,
        description: str,
        hostname: str = "",
        judgetask_id: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> CallbackOutcome[int]:
        settings = self._configuration.snapshot()
        safe_desc = decode_text(raw=description, default="judgehost internal error")
        if judgetask_id is None:
            return self._outcome(0)
        case_id = int(judgetask_id)
        receipt = self._batch_runtime.acquire_case_callback_receipt(case_id)
        if receipt is None:
            return self._outcome(0)
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
                normalize_judgehost_hostname(hostname)
                if hostname
                else receipt.lease_owner or receipt.last_callback_hostname
            )
            expected_hostname = receipt.lease_owner or receipt.last_callback_hostname
            if not expected_hostname or safe_host != expected_hostname:
                raise RuntimeError("judgehost does not own judging run")
            payload_text = parse_diagnostic_payload(
                {} if payload is None else payload
            ).text
            diagnostic_text = safe_desc
            if payload_text:
                if safe_desc.lower() in payload_text.lower():
                    diagnostic_text = payload_text
                elif payload_text.lower() not in safe_desc.lower():
                    diagnostic_text = f"{safe_desc}\n\n{payload_text}"
            failure_text = diagnostic_text
            claim = self._batch_runtime.claim_internal_error(
                case_id,
                hostname=safe_host,
                failure_text=failure_text,
                diagnostic_text=diagnostic_text,
                receipt_generation=receipt.claim_generation,
                diagnostic_limit_bytes=self._display_text_limit_bytes(settings),
                updated_at=now_iso(),
            )
            if claim.outcome == "rejected":
                raise RuntimeError(
                    "judgehost case lease changed before internal-error claim"
                )
            host_events = (
                ()
                if not safe_host
                else (
                    HostEvent(
                        hostname=safe_host,
                        action="internal-error",
                        task_id=receipt.task_id,
                        run_id=receipt.run_id,
                    ),
                )
            )
            if claim.outcome in {"late", "idempotent"}:
                # A reporting Case owns its canonical decision. The pending
                # diagnostic is flushed by that completion; a terminal Case
                # can flush it immediately.
                self._complete_terminal_callback_case(
                    case_id,
                    claim.batch_id,
                )
                return self._outcome(
                    case_id,
                    (receipt.batch_id,),
                    verification_ids=(receipt.verification_id,),
                    host_events=host_events,
                )
            if claim.outcome == "cancelled":
                return self._outcome(
                    case_id,
                    (receipt.batch_id,),
                    verification_ids=(receipt.verification_id,),
                    host_events=host_events,
                )
            return self._outcome(
                case_id,
                (receipt.batch_id,),
                verification_ids=(receipt.verification_id,),
                host_events=host_events,
            )
        finally:
            self._release_case_callback_receipt(receipt)

    def domjudge_add_debug_info(
        self,
        *,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object] | None = None,
    ) -> CallbackOutcome[None]:
        settings = self._configuration.snapshot()
        safe_host = normalize_judgehost_hostname(hostname)
        case_id = int(judgetask_id)
        receipt = self._batch_runtime.acquire_case_callback_receipt(case_id)
        if receipt is None:
            return self._outcome(None)
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
            expected_hostname = receipt.lease_owner or receipt.last_callback_hostname
            if not expected_hostname or expected_hostname != safe_host:
                raise RuntimeError("judgehost does not own judging run")
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
                disposition = self._batch_runtime.record_case_diagnostic(
                    case_id,
                    kind="debug-info",
                    hostname=safe_host,
                    text=debug_text,
                    receipt_generation=receipt.claim_generation,
                    diagnostic_limit_bytes=self._display_text_limit_bytes(settings),
                    now_text=now_iso(),
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
            host_event = HostEvent(
                hostname=safe_host,
                action="debug",
                task_id=receipt.task_id,
                run_id=receipt.run_id,
            )
        finally:
            self._release_case_callback_receipt(receipt)
        return self._outcome(
            None,
            (receipt.batch_id,),
            verification_ids=(receipt.verification_id,),
            host_events=(host_event,),
        )
