import logging
from typing import Protocol

from app.service.judgehost.configuration import JudgehostConfiguration
from app.db import now_iso
from app.service.judgehost.batch.model import (
    CaseResult,
    ExecutionBatchRow,
    HostLeaseRelease,
    JudgehostCaseRow,
)
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.domjudge.case_result import decode_case_test_row
from app.service.judgehost.domjudge.result import (
    bounded_feedback_text,
)
from app.service.judgehost.domjudge.codec import decode_base64, decode_text
from app.service.judgehost.domjudge import task_plan
from app.service.judgehost.finalization.terminalization import JudgehostTaskTerminalization
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.summary import load_run_summary
from app.service.platform.error_text import aux_display_text_limit_bytes

logger = logging.getLogger(__name__)


class TerminalCasePublisher(Protocol):
    def publish_reported_cases(self, case_ids: tuple[int, ...]) -> bool: ...

    def acknowledge_terminal_case(
        self,
        case_id: int,
        *,
        reason: str = "",
    ) -> bool: ...


class JudgehostBatchFinalizer:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_FAILED = "failed"
    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _TASK_KIND_MAIN_CORRECT = "main-correct"

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        tasks: JudgehostTaskRegistry,
        configuration: JudgehostConfiguration,
        task_terminalization: JudgehostTaskTerminalization,
        publisher: TerminalCasePublisher,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._tasks = tasks
        self._configuration = configuration
        self._task_terminalization = task_terminalization
        self._publisher = publisher

    def _display_text_limit_bytes(self) -> int:
        return aux_display_text_limit_bytes(self._configuration.snapshot().values)

    def _task_payload(self, task_id: str) -> dict[str, object]:
        row = self._tasks.get(task_id)
        return {} if row is None else row["payload"].copy()

    def finalize_host_lease_release(self, release: HostLeaseRelease) -> None:
        display_limit = self._display_text_limit_bytes()
        for task_id in release.terminal_task_ids:
            batch_row = self._batch_runtime.batch_for_task(task_id)
            if batch_row is not None:
                self._finalize_task_if_ready(
                    task_id,
                    batch_row=batch_row,
                    display_limit_bytes=display_limit,
                )
        for batch_id in release.terminal_batch_ids:
            self.publish_batch_results(batch_id)

    def _task_result_payload(
        self,
        *,
        task_id: str,
        batch_row: ExecutionBatchRow,
        case_results: list[tuple[JudgehostCaseRow, CaseResult | None]],
        display_limit_bytes: int,
    ) -> dict[str, object]:
        task_row = self._tasks.get(task_id)
        if task_row is None:
            raise RuntimeError("judgehost task not found")
        task_payload = self._task_payload(task_id)
        task_kind = task_plan.task_kind(task_payload)
        compile_only = task_kind == self._TASK_KIND_COMPILE_ONLY
        compile_success_raw = batch_row["compile_success"]
        compile_success = None if compile_success_raw is None else int(compile_success_raw)
        tests: list[dict[str, object]] = []
        internal_failure_error = ""
        cancelled_cases = 0
        usage_time_user = 0
        usage_time_wall = 0
        usage_mem_peak = 0

        for row, case_result in case_results:
            if row["status"] == "cancelled":
                cancelled_cases += 1
                continue
            test_name = row["test_name"] or f"{int(row['ordinal']):03}.in"
            if case_result is None:
                internal_failure_error = (
                    internal_failure_error or f"{test_name}: judgehost case result missing"
                )
                continue
            test_row = decode_case_test_row(case_result, test_name=test_name)
            runresult = case_result.runresult
            verdict = case_result.verdict
            cpu_ms = max(
                0,
                int(round((case_result.cpu_sec or 0.0) * 1000.0)),
            )
            wall_ms = max(
                0,
                int(round((case_result.wall_sec or 0.0) * 1000.0)),
            )
            memory_kb = case_result.memory_kb or 0
            feedback_text = case_result.feedback_text
            runresult_token = runresult
            usage_time_user += cpu_ms
            usage_time_wall += wall_ms
            usage_mem_peak = max(usage_mem_peak, memory_kb)
            tests.append(test_row)
            if (
                compile_success != 0
                and not internal_failure_error
                and (
                    verdict == "FL"
                    or runresult_token in {"checker-fail", "compare-error", "internal-error"}
                )
            ):
                detail = feedback_text
                if not detail:
                    detail = runresult_token.replace("-", " ")
                internal_failure_error = f"{test_name}: {detail}" if test_name else detail
            if (
                compile_success != 0
                and not internal_failure_error
                and task_kind == self._TASK_KIND_MAIN_CORRECT
                and verdict != "OK"
            ):
                internal_failure_error = feedback_text or (
                    "main solution failed without Judgehost diagnostics "
                    f"for {test_name}"
                )
        if cancelled_cases > 0 and not internal_failure_error:
            internal_failure_error = "judgehost task cancelled"

        compile_log = ""
        compile_diagnostics: list[dict[str, object]] = []
        compile_text = decode_base64(batch_row["compile_output_b64"]).decode(
            "utf-8", errors="replace"
        )
        compile_error_summary = ""
        compile_error_task = ""
        if compile_success == 0:
            compile_log = "compile.log"
            message = compile_text.strip() or "compilation failed"
            compile_error_summary = message
            compile_error_task = (
                bounded_feedback_text(
                    message,
                    limit_bytes=display_limit_bytes,
                )
                or "compilation failed"
            )
            compile_diagnostics.append(
                {
                    "level": "error",
                    "message": message,
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "can_link": False,
                }
            )

        run_status = "failed" if compile_success == 0 or cancelled_cases else "ok"
        if internal_failure_error:
            run_status = "failed"
        summary = load_run_summary(
            self._tasks,
            decode_text(raw=task_row["run_id"]),
        )
        summary["tests"] = tests
        summary["compile_log"] = compile_log
        summary["compile_diagnostics"] = compile_diagnostics
        if compile_only:
            summary["compile_only"] = True
        summary["usage"] = {
            "tests": len(tests),
            "time_ms_total": usage_time_user,
            "time_user_ms_total": usage_time_user,
            "time_wall_ms_total": usage_time_wall,
            "memory_kb_peak": usage_mem_peak,
        }
        summary["judgehost"] = {
            "script_hashes": {
                "compile": batch_row["compile_hash"],
                "run": batch_row["run_hash"],
                "compare": batch_row["compare_hash"],
            }
        }
        if compile_error_summary:
            summary["error"] = compile_error_summary
        elif internal_failure_error:
            summary["error"] = internal_failure_error
        result_payload: dict[str, object] = {
            "run_status": run_status,
            "summary": summary,
        }
        if compile_error_task:
            result_payload["error"] = compile_error_task
        elif internal_failure_error:
            result_payload["error"] = internal_failure_error
        return result_payload

    def finalize_task_if_ready(
        self,
        task_id: str,
        *,
        batch_row: ExecutionBatchRow,
    ) -> bool:
        return self._finalize_task_if_ready(
            task_id,
            batch_row=batch_row,
            display_limit_bytes=self._display_text_limit_bytes(),
        )

    def _finalize_task_if_ready(
        self,
        task_id: str,
        *,
        batch_row: ExecutionBatchRow,
        display_limit_bytes: int,
    ) -> bool:
        safe_task_id = task_id
        if self._batch_runtime.batch_verification_cancellation_requested(
            int(batch_row["batch_id"])
        ):
            return False
        task_row = self._tasks.get(safe_task_id)
        if task_row is None:
            return False
        task_status = decode_text(lower=True, raw=task_row["status"])
        if task_status in {"completed", "failed"}:
            return True
        if task_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
            return False
        if not self._batch_runtime.task_cases_terminal(safe_task_id):
            return False
        case_results = self._batch_runtime.task_case_results(safe_task_id)
        cases = [row for row, _result in case_results]
        if any(
            case["status"] == "reported" and not bool(case["completion_acknowledged"])
            for case in cases
        ):
            return False
        payload = self._task_result_payload(
            task_id=safe_task_id,
            batch_row=batch_row,
            case_results=case_results,
            display_limit_bytes=display_limit_bytes,
        )
        for case_row in cases:
            if case_row["status"] != "cancelled":
                continue
            if bool(case_row["completion_acknowledged"]):
                continue
            published = self._publisher.acknowledge_terminal_case(
                int(case_row["id"]),
                reason=decode_text(
                    raw=payload.get("error"),
                    default="judgehost task cancelled",
                ),
            )
            if not published:
                return False
        self._task_terminalization.finalize_task(
            task_id=safe_task_id,
            payload=payload,
        )
        return True

    def _publish_cases(self, cases: tuple[JudgehostCaseRow, ...]) -> None:
        unacknowledged = tuple(
            row["id"] for row in cases
            if row["status"] == "reported" and not row["completion_acknowledged"]
        )
        if unacknowledged and not self._publisher.publish_reported_cases(unacknowledged):
            raise RuntimeError("verification task completion was rejected")
        for case in cases:
            if not self._publisher.acknowledge_terminal_case(case["id"]):
                raise RuntimeError("verification task completion is not durably acknowledged")

    def retry_due_finalizations(self, *, limit: int = 1) -> None:
        for batch_id in self._batch_runtime.due_batch_finalizations(limit=limit):
            if self._batch_runtime.batch_verification_cancellation_requested(
                batch_id
            ):
                continue
            self.publish_batch_results(batch_id)
            self.finalize_batch_if_ready(batch_id)

    def retire_cancelled_batch(self, batch_id: int) -> bool:
        """Finish runtime-only cancellation without publishing Case completions."""

        current = self._batch_runtime.fetch_batch(int(batch_id))
        if current is None or current["status"] in {"completed", "failed"}:
            return True
        claim = self._batch_runtime.claim_batch_finalization(
            int(batch_id),
            now_text=now_iso(),
        )
        if claim is None:
            return False
        cases = claim.cases
        task_ids = {row["task_id"] for row in cases if row["task_id"]}
        if any(
            (task := self._tasks.get(task_id)) is None
            or task["status"] not in {"completed", "failed"}
            for task_id in task_ids
        ):
            self._batch_runtime.abort_batch_finalization(
                claim,
                now_text=now_iso(),
                delay_sec=0.0,
            )
            return False
        completed_at = now_iso()
        return self._batch_runtime.set_batch_terminal_status(
            claim,
            status="failed",
            completed_at=completed_at,
            updated_at=completed_at,
        )

    def publish_batch_results(
        self,
        batch_id: int,
        *,
        case_ids: tuple[int, ...] | None = None,
        require_completion_ack: bool = False,
        defer_task_finalization: bool = False,
    ) -> None:
        cases = self._batch_runtime.claim_case_publications(
            batch_id, case_ids=case_ids, wait_for_active=require_completion_ack,
        )
        if not cases:
            if require_completion_ack and not self._batch_runtime.publications_acknowledged(
                batch_id, case_ids=case_ids,
            ):
                raise RuntimeError("verification task completion is not durably acknowledged")
            return
        succeeded = False
        try:
            batch = self._batch_runtime.fetch_batch(batch_id)
            if batch is None or self._batch_runtime.batch_verification_cancellation_requested(batch_id):
                return
            self._publish_cases(cases)
            if not defer_task_finalization:
                display_limit = self._display_text_limit_bytes()
                for task_id in dict.fromkeys(row["task_id"] for row in cases if row["task_id"]):
                    self._finalize_task_if_ready(
                        task_id, batch_row=batch, display_limit_bytes=display_limit,
                    )
            succeeded = True
        except Exception:
            logger.exception("failed to publish judgehost cases batch_id=%s", batch_id)
            if require_completion_ack:
                raise
        finally:
            ready = self._batch_runtime.complete_case_publications(
                batch_id, cases, retry=not succeeded,
            )
            if ready:
                self.finalize_batch_if_ready(batch_id)
        if require_completion_ack and not self._batch_runtime.publications_acknowledged(
            batch_id, case_ids=case_ids,
        ):
            raise RuntimeError("verification task completion is not durably acknowledged")

    def finalize_batch_if_ready(
        self,
        batch_id: int,
    ) -> None:
        if self._batch_runtime.batch_verification_cancellation_requested(batch_id):
            return
        claim = self._batch_runtime.claim_batch_finalization(batch_id, now_text=now_iso())
        if claim is None:
            return
        try:
            batch = claim.batch
            task_ids = tuple(dict.fromkeys(row["task_id"] for row in claim.cases if row["task_id"]))
            for task_id in task_ids:
                if not self.finalize_task_if_ready(task_id, batch_row=batch):
                    self._batch_runtime.abort_batch_finalization(claim, now_text=now_iso())
                    return
            task_rows = [self._tasks.get(task_id) for task_id in task_ids]
            failed = (
                batch["compile_success"] == 0
                or any(row["status"] == "cancelled" for row in claim.cases)
                or any(row is not None and row["status"] == self.STATUS_FAILED for row in task_rows)
            )
            finished_at = now_iso()
            if not self._batch_runtime.set_batch_terminal_status(
                claim, status="failed" if failed else "completed",
                completed_at=finished_at, updated_at=finished_at,
            ):
                raise RuntimeError("judgehost batch finalization claim disappeared")
        except Exception:
            self._batch_runtime.abort_batch_finalization(claim, now_text=now_iso())
            raise
