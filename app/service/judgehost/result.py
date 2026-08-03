from __future__ import annotations

import base64
import logging
import json
import re
import shutil
import time
from pathlib import Path
from typing import cast

from app.service.judgehost.shared import (
    domjudge_text,
    domjudge_lower_text,
)
from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_json_hash, domjudge_source_hash
from app.service.judgehost.domjudge.client import domjudge_parse_script_id, domjudge_script_hash_field, domjudge_script_id
from app.service.judgehost.limits import truncate_stored_log_bytes
from app.service.judgehost.runtime import (
    domjudge_bool,
    domjudge_feedback_text_and_files,
    domjudge_feedback_text_from_text,
    domjudge_parse_float,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_rewrite_untrusted_runresult,
    domjudge_verdict_from_runresult,
)
from app.service.verification.task_result_finalize import finalize_verification_task_result
from app.service.verification.task_scheduler import notify_verification_case_reported

from .case_result import build_case_result, decode_case_test_row
from .core import JudgehostCore
from .batch_scheduler_models import CaseResult
from .state import JudgehostState
from .task_queue import TaskQueue
from .toolkit import DomjudgeToolkit

logger = logging.getLogger(__name__)

_diag_logger = logging.getLogger("uvicorn.error")


def _answer_correct_from_compare_exit_code(compare_exit_code: int) -> bool:
    return int(compare_exit_code) == 42


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

    def __init__(self, state: JudgehostState, core: JudgehostCore, queue: TaskQueue, toolkit: DomjudgeToolkit) -> None:
        self._s = state
        self._core = core
        self._queue = queue
        self._toolkit = toolkit

    def _touch_task_verification(self, task_id: str) -> None:
        task = self._s.task_registry.get(task_id)
        if task is not None:
            self._s.touch_verification_runtime(domjudge_text(task.get("verification_id")))

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

    def _domjudge_feedback_text_and_files(
        self,
        *,
        work_root: Path,
        runresult: str,
        output_error_rel: str,
        output_diff_rel: str,
        team_message_rel: str,
    ) -> tuple[str, list[str]]:
        return domjudge_feedback_text_and_files(
            read_blob=lambda token: self._toolkit.read_artifact_blob(work_root, token),
            runresult=runresult,
            output_error_rel=output_error_rel,
            output_diff_rel=output_diff_rel,
            team_message_rel=team_message_rel,
        )

    def domjudge_get_source_files(self, submit_id: str, contest_id: str | None = None) -> list[dict[str, object]]:
        safe_submit = domjudge_text(submit_id)
        if not safe_submit:
            raise RuntimeError("source files not found")
        safe_contest = None if contest_id is None else self._toolkit.contest_id(contest_id)
        submission = self._s.batch_scheduler.source_submission(safe_submit, contest_id=safe_contest)
        if submission is None:
            raise RuntimeError("source files not found")
        source_files = (
            (submission.source_name, submission.source_bytes),
            *submission.extra_source_items,
        )
        return [
            {
                "filename": filename,
                "content": base64.b64encode(content).decode("ascii"),
            }
            for filename, content in source_files
        ]

    def domjudge_get_testcase_files(self, testcase_id: int, *, hostname: str) -> list[dict[str, object]]:
        token = int(testcase_id)
        safe_host = self._core.normalize_hostname(hostname)
        row, resolution_source = self._s.batch_scheduler.testcase_refs(token, hostname=safe_host)
        if row is None:
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s host=%s resolved=missing",
                token,
                safe_host,
            )
            raise RuntimeError("testcase files not found")
        input_ref = domjudge_text(row["input_ref"])
        answer_ref = domjudge_text(row["answer_ref"])
        input_blob = self._toolkit.resolve_artifact_blob(input_ref)
        answer_blob = self._toolkit.resolve_artifact_blob(answer_ref)
        if input_blob is None or answer_blob is None:
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s host=%s resolved=%s exists=%s input=%s answer=%s",
                token,
                safe_host,
                resolution_source,
                False,
                input_ref,
                answer_ref,
            )
            raise RuntimeError("testcase files not found")
        logger.debug(
            "judgehost.get_testcase_files testcase_id=%s host=%s resolved=%s exists=%s input=%s answer=%s",
            token,
            safe_host,
            resolution_source,
            True,
            input_ref,
            answer_ref,
        )
        return [
            {"filename": "input", "content": base64.b64encode(input_blob).decode("ascii")},
            {"filename": "output", "content": base64.b64encode(answer_blob).decode("ascii")},
        ]

    def _domjudge_executable_rows(
        self,
        *,
        kind: str,
        executable_hash: str,
    ) -> list[dict[str, object]]:
        cached_rows = self._toolkit.read_executable_cache(kind=kind, executable_hash=executable_hash)
        if not cached_rows:
            raise RuntimeError("script files not found")
        return [
            {
                "filename": str(row["filename"]),
                "content": base64.b64encode(bytes(row["content"])).decode("ascii"),
                "is_executable": bool(row["is_executable"]),
            }
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
        batch_row = self._s.batch_scheduler.active_batch_for_host(safe_host)
        if batch_row is None:
            return None
        field = domjudge_script_hash_field(kind)
        script_hash = domjudge_lower_text(batch_row[field])
        if not script_hash:
            return None
        if domjudge_script_id(script_hash) != requested_id:
            return None
        return (int(batch_row["batch_id"]), script_hash)

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
        self._domjudge_finalize_batch_if_ready(int(batch_id), force_failed=True, error_text=safe_error)

    def domjudge_get_executable_files(
        self,
        kind: str,
        script_id: object,
        *,
        hostname: str = "",
    ) -> list[dict[str, object]]:
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
        _ = int(judgetask_id)
        return {}

    def domjudge_check_versions(
        self,
        judgetask_id: int,
        *,
        hostname: str,
        compiler: str = "",
        runner: str = "",
    ) -> dict[str, object]:
        _ = int(judgetask_id)
        _ = domjudge_text(hostname)
        _ = domjudge_text(compiler)
        _ = domjudge_text(runner)
        return {}

    def _domjudge_publish_reported_case(self, *, task_id: str, test_name: str) -> bool:
        safe_task_id = domjudge_text(task_id)
        safe_test_name = domjudge_text(test_name)
        if (not safe_task_id) or (not safe_test_name):
            return False
        case_result = self._queue.poll_task_case_result(safe_task_id, safe_test_name)
        if case_result is None:
            return False
        published = self._publish_verification_case_result(
            task_id=safe_task_id,
            test_name=safe_test_name,
            case_result=case_result,
        )
        if published:
            self._s.batch_scheduler.mark_case_verification_published(
                safe_task_id,
                safe_test_name,
            )
        return published

    def _publish_verification_case_result(
        self,
        *,
        task_id: str,
        test_name: str,
        case_result: dict[str, object],
        verification_id: str = "",
    ) -> bool:
        verification_task_store = self._s.verification_task_store
        safe_verification_id = domjudge_text(verification_id)
        if not safe_verification_id:
            judgehost_task_row = self._s.task_registry.get(task_id)
            if judgehost_task_row is not None:
                safe_verification_id = domjudge_text(judgehost_task_row["verification_id"])
        if safe_verification_id and notify_verification_case_reported(
            safe_verification_id,
            task_id,
            test_name,
            case_result,
        ):
            return True
        verification_task_row = verification_task_store.find_runtime_row_by_judgehost_case(
            task_id,
            test_name,
        )
        if verification_task_row is None:
            return True
        final_result = finalize_verification_task_result(verification_task_row, result=case_result)
        verification_task_store.save_task_result(
            final_result.task_id,
            status=final_result.status,
            verdict=final_result.verdict,
            run_id=final_result.run_id,
            judgehost_task_id=final_result.judgehost_task_id,
            runtime_sec=final_result.runtime_sec,
            cpu_sec=final_result.cpu_sec,
            wall_sec=final_result.wall_sec,
            memory_kb=final_result.memory_kb,
            compile_log=final_result.compile_log,
            diagnostics_json=final_result.diagnostics_json,
            error_text=final_result.error_text,
            feedback_text=final_result.feedback_text,
            output_ref=final_result.output_ref,
            answer_correct=final_result.answer_correct,
        )
        if final_result.fail_flag_reason:
            verification_task_store.set_fail_flag(
                str(verification_task_row["verification_id"] or ""),
                reason=final_result.fail_flag_reason,
            )
        return True

    def _domjudge_case_result_from_test_row(
        self,
        *,
        task_id: str,
        task_row: dict[str, object],
        test_row: dict[str, object],
    ) -> dict[str, object]:
        safe_task_id = domjudge_text(task_id)
        payload = cast(dict[str, object], task_row["payload"])
        task_kind = domjudge_text(payload.get("task_kind"))
        summary = dict(cast(dict[str, object], task_row.get("summary") or {}))
        runresult = domjudge_text(test_row.get("runresult"))
        verdict = domjudge_text(test_row.get("verdict"), default="FL")
        feedback_text = domjudge_text(test_row.get("message"))
        summary_error = domjudge_text(summary.get("error"))
        if (
            not summary_error
            and feedback_text
            and runresult in {"checker-fail", "compare-error", "internal-error"}
        ):
            summary_error = feedback_text
        if (
            not summary_error
            and feedback_text
            and task_kind == self._TASK_KIND_MAIN_CORRECT
            and verdict != "OK"
        ):
            summary_error = feedback_text
        case_summary = {
            "source": summary.get("source") or "",
            "compile_diagnostics": list(
                cast(list[object], summary.get("compile_diagnostics") or [])
            ),
            "error": summary_error,
            "tests": [dict(test_row)],
        }
        if task_kind == self._TASK_KIND_MAIN_CORRECT:
            run_status = "ok" if verdict == "OK" else "failed"
        elif runresult in {
            "compiler-error",
            "checker-fail",
            "compare-error",
            "internal-error",
        }:
            run_status = "failed"
        else:
            run_status = "ok"
        return {
            "task_id": safe_task_id,
            "verification_id": domjudge_text(task_row["verification_id"]),
            "run_id": domjudge_text(task_row["run_id"]),
            "artifact_path": "",
            "status": run_status,
            "task_status": task_row["status"],
            "error": summary_error,
            "summary": case_summary,
        }

    def _domjudge_task_result_payload(
        self,
        *,
        task_id: str,
        batch_row: dict[str, object],
        case_results: list[tuple[dict[str, object], CaseResult | None]],
        force_failed: bool,
        error_text: str,
    ) -> dict[str, object]:
        task_row = self._core.task_by_id(task_id)
        if task_row is None:
            raise RuntimeError("judgehost task not found")
        task_payload = self._core.task_payload(task_id)
        task_kind = self._toolkit.task_kind(task_payload)
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
            if domjudge_lower_text(row["status"]) == "cancelled":
                cancelled_cases += 1
                continue
            test_name = domjudge_text(row["test_name"], default=f"{int(row['ordinal']):03}.in")
            if case_result is None:
                internal_failure_error = (
                    internal_failure_error
                    or f"{test_name}: judgehost case result missing"
                )
                continue
            test_row = decode_case_test_row(case_result)
            runresult = case_result.runresult
            verdict = case_result.verdict
            cpu_ms = max(0, int(round(case_result.cpu_sec * 1000.0)))
            wall_ms = max(0, int(round(case_result.wall_sec * 1000.0)))
            memory_kb = case_result.memory_kb
            feedback_text = case_result.feedback_text
            runresult_token = domjudge_lower_text(runresult)
            usage_time_user += cpu_ms
            usage_time_wall += wall_ms
            usage_mem_peak = max(usage_mem_peak, memory_kb)
            tests.append(test_row)
            if (
                compile_success != 0
                and (not internal_failure_error)
                and runresult_token in {"checker-fail", "compare-error", "internal-error"}
            ):
                detail = domjudge_text(feedback_text)
                if not detail:
                    detail = runresult_token.replace("-", " ")
                internal_failure_error = f"{test_name}: {detail}" if test_name else detail
            if (
                compile_success != 0
                and (not internal_failure_error)
                and task_kind == "main-correct"
                and verdict != "OK"
            ):
                detail = domjudge_text(feedback_text) or f"main correct failed on {test_name}"
                internal_failure_error = detail
        if cancelled_cases > 0 and (not internal_failure_error):
            internal_failure_error = "judgehost task cancelled"

        compile_log = ""
        compile_diag: list[dict[str, object]] = []
        compile_text = self._toolkit.b64_decode(batch_row["compile_output_b64"]).decode("utf-8", errors="replace")
        compile_error_summary = ""
        compile_error_task = ""
        if compile_success == 0:
            compile_log = "compile.log"
            message = "compilation failed"
            if compile_text.strip():
                message = compile_text.strip()
            compile_error_summary = message
            compile_error_task = domjudge_feedback_text_from_text(message) or "compilation failed"
            compile_diag.append(
                {
                    "level": "error",
                    "message": message,
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "can_link": False,
                }
            )

        run_status = "failed" if (force_failed or compile_success == 0 or cancelled_cases) else "ok"
        if (not force_failed) and internal_failure_error:
            run_status = "failed"
        summary = self._queue.load_run_summary(domjudge_text(task_row["run_id"]))
        summary["tests"] = tests
        summary["compile_log"] = compile_log
        summary["compile_diagnostics"] = compile_diag
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
                "compile": domjudge_lower_text(batch_row["compile_hash"]),
                "run": domjudge_lower_text(batch_row["run_hash"]),
                "compare": domjudge_lower_text(batch_row["compare_hash"]),
            }
        }
        if force_failed and error_text:
            summary["error"] = error_text
        elif compile_error_summary:
            summary["error"] = compile_error_summary
        elif internal_failure_error:
            summary["error"] = internal_failure_error
        result_payload: dict[str, object] = {"run_status": run_status, "summary": summary}
        if force_failed and error_text:
            result_payload["error"] = error_text
        elif compile_error_task:
            result_payload["error"] = compile_error_task
        elif internal_failure_error:
            result_payload["error"] = internal_failure_error
        return result_payload

    def _domjudge_finalize_task_if_ready(
        self,
        task_id: str,
        *,
        batch_row: dict[str, object],
        force_failed: bool = False,
        error_text: str = "",
    ) -> bool:
        safe_task_id = domjudge_text(task_id)
        if not self._s.batch_scheduler.task_cases_terminal(safe_task_id):
            return False
        case_results = self._s.batch_scheduler.task_case_results(safe_task_id)
        cases = [row for row, _result in case_results]
        if any(
            domjudge_lower_text(case["status"]) == "reported"
            and not bool(case["verification_published"])
            for case in cases
        ):
            return False
        task_row = self._s.task_registry.get(safe_task_id)
        if task_row is None:
            return False
        task_status = domjudge_lower_text(task_row["status"])
        if task_status in {"completed", "failed"}:
            return True
        if task_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
            return False
        payload = self._domjudge_task_result_payload(
            task_id=safe_task_id,
            batch_row=batch_row,
            case_results=case_results,
            force_failed=force_failed,
            error_text=error_text,
        )
        cancelled_case_result = {
            "task_id": safe_task_id,
            "verification_id": domjudge_text(task_row["verification_id"]),
            "run_id": domjudge_text(task_row["run_id"]),
            "artifact_path": "",
            "status": "failed",
            "task_status": self.STATUS_FAILED,
            "missing_case_result": True,
            "error": domjudge_text(payload.get("error"), default="judgehost task cancelled"),
            "summary": dict(cast(dict[str, object], payload["summary"])),
        }
        for case_row in cases:
            if domjudge_lower_text(case_row["status"]) != "cancelled":
                continue
            if bool(case_row["verification_published"]):
                continue
            published = self._publish_verification_case_result(
                task_id=safe_task_id,
                test_name=domjudge_text(case_row["test_name"]),
                case_result=cancelled_case_result,
            )
            if published:
                self._s.batch_scheduler.mark_case_verification_published(
                    safe_task_id,
                    domjudge_text(case_row["test_name"]),
                )
        self._queue.finalize_domjudge_task(task_id=safe_task_id, payload=payload)
        return True

    def _domjudge_publish_and_finalize_ready_tasks(
        self,
        *,
        batch_id: int,
        batch_row: dict[str, object],
        cases: list[dict[str, object]],
        force_failed: bool,
        error_text: str,
    ) -> bool:
        for row in cases:
            if domjudge_lower_text(row["status"]) != "reported":
                continue
            if bool(row["verification_published"]):
                continue
            try:
                self._domjudge_publish_reported_case(
                    task_id=domjudge_text(row["task_id"]),
                    test_name=domjudge_text(row["test_name"]),
                )
            except Exception:
                logger.exception(
                    "failed to publish terminal DOMjudge case batch_id=%s case_id=%s",
                    int(batch_id),
                    int(row["id"]),
                )
                return False

        task_ids = list(dict.fromkeys(
            task_id
            for row in cases
            if (task_id := domjudge_text(row["task_id"]))
        ))
        for task_id in task_ids:
            try:
                self._domjudge_finalize_task_if_ready(
                    task_id,
                    batch_row=batch_row,
                    force_failed=force_failed,
                    error_text=error_text,
                )
            except Exception:
                logger.exception(
                    "failed to finalize DOMjudge task task_id=%s batch_id=%s",
                    task_id,
                    int(batch_id),
                )
                return False
        return True

    def _request_batch_failure(self, batch_id: int, *, runresult: str, error_text: str) -> None:
        batch_row = self._s.batch_scheduler.fetch_batch(int(batch_id))
        if batch_row is None:
            return
        feedback = domjudge_text(error_text)
        if runresult == "compiler-error" and not feedback:
            compile_blob = self._toolkit.b64_decode(batch_row["compile_output_b64"])
            feedback = domjudge_feedback_text_from_text(
                compile_blob.decode("utf-8", errors="replace")
            ) or "compilation failed"
        if not feedback:
            feedback = runresult.replace("-", " ")
        verdict = domjudge_verdict_from_runresult(runresult)
        results: dict[int, CaseResult] = {}
        for row in self._s.batch_scheduler.cases_for_batch(int(batch_id)):
            if domjudge_lower_text(row["status"]) in {"reported", "cancelled"}:
                continue
            results[int(row["id"])] = build_case_result(
                test_name=domjudge_text(row["test_name"], default=f"{int(row['ordinal']):03}.in"),
                runresult=runresult,
                verdict=verdict,
                runtime_sec=0.0,
                cpu_sec=0.0,
                wall_sec=0.0,
                memory_kb=0,
                score_text="",
                output_run_rel="",
                output_error_rel="",
                output_system_rel="",
                output_diff_rel="",
                metadata_rel="",
                compare_metadata_rel="",
                team_message_rel="",
                feedback_text=feedback,
                feedback_files=[],
                answer_correct=False,
            )
        if results:
            self._s.batch_scheduler.request_batch_case_results(
                int(batch_id),
                results=results,
                updated_at=now_iso(),
            )

    def _schedule_finalization_retry(self, batch_id: int, *, delay_sec: float = 0.25) -> None:
        self._s.batch_scheduler.abort_batch_finalization(
            int(batch_id),
            now_text=now_iso(),
            delay_sec=delay_sec,
        )

    def retry_due_finalizations(self, *, limit: int = 1) -> None:
        for batch_id in self._s.batch_scheduler.due_batch_finalizations(limit=limit):
            self._domjudge_finalize_batch_if_ready(batch_id)

    def _clear_finalization_retry(self, batch_id: int) -> None:
        self._s.batch_scheduler.clear_batch_finalization_retry(int(batch_id))

    def _domjudge_finalize_batch_if_ready(
        self,
        batch_id: int,
        *,
        force_failed: bool = False,
        error_text: str = "",
    ) -> None:
        current = self._s.batch_scheduler.fetch_batch(int(batch_id))
        if current is None:
            return
        compile_failed = (
            current["compile_success"] is not None
            and int(current["compile_success"]) == 0
        )
        if force_failed:
            self._request_batch_failure(
                int(batch_id),
                runresult="internal-error",
                error_text=error_text,
            )
        elif compile_failed:
            self._request_batch_failure(
                int(batch_id),
                runresult="compiler-error",
                error_text="",
            )
        claim = self._s.batch_scheduler.claim_batch_finalization(
            int(batch_id),
            now_text=now_iso(),
        )
        if claim is None:
            return
        try:
            batch_row = dict(claim["batch"])
            cases = [dict(row) for row in claim["cases"]]
            task_ids = list(dict.fromkeys(
                task_id
                for row in cases
                if (task_id := domjudge_text(row["task_id"]))
            ))
            if not self._domjudge_publish_and_finalize_ready_tasks(
                batch_id=int(batch_id),
                batch_row=batch_row,
                cases=cases,
                force_failed=force_failed,
                error_text=error_text,
            ):
                self._schedule_finalization_retry(int(batch_id))
                return
            task_rows = {task_id: self._s.task_registry.get(task_id) for task_id in task_ids}
            unfinished_task_ids = [
                task_id
                for task_id, task_row in task_rows.items()
                if task_row is None
                or domjudge_lower_text(task_row["status"]) not in {"completed", "failed"}
            ]
            if unfinished_task_ids:
                unfinished_statuses = {
                    task_id: (
                        "<missing>"
                        if task_rows[task_id] is None
                        else domjudge_lower_text(
                            cast(dict[str, object], task_rows[task_id])["status"]
                        )
                    )
                    for task_id in unfinished_task_ids
                }
                transient_statuses = set(unfinished_statuses.values()) <= {
                    self.STATUS_ENQUEUING,
                    self.STATUS_REPORTING,
                }
                log = logger.debug if transient_statuses else logger.error
                log(
                    "DOMjudge batch remains finalizing because tasks are not terminal "
                    "batch_id=%s task_statuses=%s",
                    int(batch_id),
                    unfinished_statuses,
                )
                self._schedule_finalization_retry(int(batch_id))
                return
            compile_success = batch_row["compile_success"]
            compile_failed = compile_success is not None and int(compile_success) == 0
            has_cancelled_cases = any(
                domjudge_lower_text(row["status"]) == "cancelled"
                for row in cases
            )
            has_failed_tasks = any(
                domjudge_lower_text(cast(dict[str, object], task_rows[task_id])["status"])
                == self.STATUS_FAILED
                for task_id in task_ids
            )
            finished_at = now_iso()
            terminal_status = (
                "failed"
                if force_failed or compile_failed or has_cancelled_cases or has_failed_tasks
                else "completed"
            )
            work_root_text = domjudge_text(batch_row["work_root"])
            if work_root_text:
                shutil.rmtree(Path(work_root_text).resolve(), ignore_errors=True)
            updated = self._s.batch_scheduler.set_batch_terminal_status(
                int(batch_id),
                status=terminal_status,
                completed_at=finished_at,
                updated_at=finished_at,
            )
            if not updated:
                logger.error("DOMjudge batch finalization claim disappeared batch_id=%s", int(batch_id))
                self._schedule_finalization_retry(int(batch_id))
                return
            self._s.host_telemetry.record_batch_terminal(int(batch_id))
            self._clear_finalization_retry(int(batch_id))
        except Exception:
            self._schedule_finalization_retry(int(batch_id))
            raise

    def domjudge_update_judging(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> None:
        case_row = self._s.batch_scheduler.fetch_case(int(judgetask_id))
        if case_row is None:
            logger.info("ignoring update for unknown judging run id: %s", int(judgetask_id))
            return
        self._touch_task_verification(domjudge_text(case_row["task_id"]))
        self._process_domjudge_judging_update(hostname, judgetask_id, payload)

    def _process_domjudge_judging_update(
        self,
        hostname: str,
        judgetask_id: int,
        payload: dict[str, object],
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
        if batch_status == "finalizing":
            self._domjudge_finalize_batch_if_ready(int(case_row["batch_id"]))
            return
        if batch_status != "open":
            logger.info("ignoring update for terminal DOMjudge batch case id: %s", case_id)
            return
        if domjudge_lower_text(case_row["case_status"]) != "leased":
            logger.info("ignoring update for non-leased DOMjudge case id: %s", case_id)
            return
        if domjudge_text(case_row["case_lease_owner"]) != safe_host:
            logger.info("ignoring update from non-owner DOMjudge host for case id: %s", case_id)
            return
        safe_task_id = domjudge_text(case_row["task_id"])
        if not self._domjudge_task_accepts_case_updates(safe_task_id):
            logger.info("ignoring update for cancelled DOMjudge task case id: %s", case_id)
            return
        batch_id = int(case_row["batch_id"])
        compile_success = None
        if "compile_success" in payload:
            compile_success = 1 if domjudge_bool(payload.get("compile_success"), default=False) else 0

        def _payload_blob_as_b64(value: object) -> str:
            raw = self._toolkit.payload_blob_bytes(value)
            if raw:
                return base64.b64encode(truncate_stored_log_bytes(raw, self._s.constants)).decode("ascii")
            return domjudge_text(value)

        compile_output = _payload_blob_as_b64(payload.get("output_compile"))
        compile_meta = _payload_blob_as_b64(payload.get("compile_metadata"))
        if compile_success is not None:
            updated_at = now_iso()
            updated = self._s.batch_scheduler.record_compile_result(
                batch_id,
                compile_success=compile_success,
                compile_output_b64=compile_output,
                compile_metadata_b64=compile_meta,
                lease_owner=safe_host,
                updated_at=updated_at,
            )
            if not updated:
                logger.info("ignoring compile update for terminal DOMjudge batch case id: %s", case_id)
                return
            if compile_success == 0:
                self._domjudge_finalize_batch_if_ready(batch_id)

    def domjudge_add_judging_run(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> int:
        reported_monotonic = time.monotonic()
        reported_at = now_iso()
        safe_host = self._core.normalize_hostname(hostname)
        case_row = self._s.batch_scheduler.fetch_case(int(judgetask_id))
        if case_row is None:
            logger.info(
                "ignoring add_judging_run for unknown judging run id: %s",
                int(judgetask_id),
            )
            return int(judgetask_id)
        self._touch_task_verification(domjudge_text(case_row["task_id"]))
        claim = self._s.batch_scheduler.claim_case_reporting(
            int(judgetask_id),
            hostname=safe_host,
            now_text=reported_at,
        )
        if claim is None:
            logger.info("ignoring stale add_judging_run result for case id: %s", int(judgetask_id))
            return int(judgetask_id)
        self._s.batch_scheduler.observe_compile_success_from_case_claim(
            claim.case_id,
            generation=claim.generation,
            lease_owner=safe_host,
            updated_at=reported_at,
        )

        def _abort_unfinished_claim() -> None:
            if self._s.batch_scheduler.abort_case_claim(
                claim.case_id,
                generation=claim.generation,
                updated_at=now_iso(),
            ):
                self._domjudge_finalize_batch_if_ready(claim.batch_id)

        try:
            result_id = self._process_domjudge_judging_run(
                safe_host,
                judgetask_id,
                payload,
                claim_generation=claim.generation,
                reported_at=reported_at,
                reported_monotonic=reported_monotonic,
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
        reported_at: str,
        reported_monotonic: float,
    ) -> int:
        safe_host = self._core.normalize_hostname(hostname)
        case_id = int(judgetask_id)
        row = self._s.batch_scheduler.case_execution_row(case_id)
        if row is None:
            # Same stale-callback case as domjudge_update_judging: acknowledge
            # gracefully to avoid hard-failing judgedaemon retries.
            logger.info("ignoring add_judging_run for unknown judging run id: %s", case_id)
            return case_id
        batch_status = domjudge_lower_text(row["batch_status"])
        if batch_status == "finalizing":
            self._domjudge_finalize_batch_if_ready(int(row["batch_id"]))
            return case_id
        if batch_status != "open":
            logger.info("ignoring add_judging_run for terminal DOMjudge batch id: %s", case_id)
            return case_id
        if domjudge_lower_text(row["case_status"]) != "reporting":
            logger.info("ignoring add_judging_run for non-reporting DOMjudge case id: %s", case_id)
            return case_id
        if domjudge_text(row["case_lease_owner"]) != safe_host:
            logger.info(
                "ignoring add_judging_run from non-owner DOMjudge host for case id: %s",
                case_id,
            )
            return case_id
        batch_id = int(row["batch_id"])
        safe_task_id = domjudge_text(row["task_id"])
        work_root = Path(domjudge_text(row["work_root"])).resolve()
        task_payload = self._core.task_payload(safe_task_id) if safe_task_id else {}
        verification_source = task_payload.get("verification_source", "")
        task_kind = self._toolkit.task_kind(task_payload, verification_source=verification_source)
        compile_only = task_kind == self._TASK_KIND_COMPILE_ONLY

        payload_files: dict[str, bytes] = {}

        def _capture_payload_file(
            name: str,
            value: object,
            *,
            allow_empty: bool = False,
        ) -> bytes:
            if value is None:
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
        _capture_payload_file("program.err", payload.get("output_error"))
        _capture_payload_file("system.out", payload.get("output_system"))
        _capture_payload_file("judgemessage.txt", payload.get("output_diff"))
        metadata_blob = _capture_payload_file("program.meta", payload.get("metadata"))
        compare_meta_blob = _capture_payload_file("compare.meta", payload.get("compare_metadata"))
        _capture_payload_file("teammessage.txt", payload.get("team_message"))

        runtime_sec = domjudge_parse_float(payload.get("runtime"), 0.0)
        cpu_sec = runtime_sec
        wall_sec = runtime_sec
        memory_kb = 0
        compare_exit_code = -1
        program_meta: dict[str, str] = {}
        if metadata_blob:
            program_meta = domjudge_parse_meta_text(metadata_blob.decode("utf-8", errors="replace"))
            cpu_total_sec = domjudge_parse_float(program_meta.get("cpu-time"), runtime_sec)
            wall_sec = domjudge_parse_float(program_meta.get("wall-time"), cpu_total_sec)
            runtime_sec = cpu_sec = cpu_total_sec
            mem_bytes = domjudge_parse_int(program_meta.get("memory-bytes"), 0)
            memory_kb = max(0, int(mem_bytes // 1024))
        if compare_meta_blob:
            compare_meta = domjudge_parse_meta_text(
                compare_meta_blob.decode("utf-8", errors="replace")
            )
            compare_exit_code = domjudge_parse_int(compare_meta.get("exitcode"), -1)
        answer_correct = _answer_correct_from_compare_exit_code(compare_exit_code)

        score_text = domjudge_text(payload.get("score"))

        def _load_json_object(raw: object) -> dict[str, object]:
            text = domjudge_text(raw)
            if not text:
                return {}
            try:
                return cast(dict[str, object], json.loads(text))
            except Exception:
                return {}

        source_name = domjudge_text(row["source_name"])
        source_hash = domjudge_lower_text(row["source_hash"])
        # Reuse the enqueue-time source hash directly so cache keys stay stable
        # when payload contains extra sources (for example testlib.h).
        if re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            source_bytes = b""
            source_path = Path(domjudge_text(row["source_path"])).resolve()
            try:
                if source_path.exists() and source_path.is_file() and (not source_path.is_symlink()):
                    source_bytes = source_path.read_bytes()
            except OSError:
                source_bytes = b""
            source_hash = domjudge_source_hash(source_name, source_bytes)

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
        run_cfg = _load_json_object(row["run_config_json"])
        compare_cfg = _load_json_object(row["compare_config_json"])
        runresult = domjudge_lower_text(payload.get("runresult"), default="internal-error")
        runresult = domjudge_rewrite_untrusted_runresult(
            runresult,
            cpu_sec=cpu_sec,
            run_cfg_obj=run_cfg,
        )
        if runresult in {"compare-error", "run-error", "internal-error"} and compare_exit_code < 0:
            time_result = domjudge_lower_text(program_meta.get("time-result"))
            signal_num = domjudge_parse_int(program_meta.get("signal"), 0)
            output_limit_kb = domjudge_parse_int(run_cfg.get("output_limit"), 0)
            output_limit_bytes = max(0, int(output_limit_kb) * 1024)
            stdout_bytes = domjudge_parse_int(program_meta.get("stdout-bytes"), 0)
            output_truncated = domjudge_lower_text(program_meta.get("output-truncated"))
            timed_out = ("timelimit" in time_result) or signal_num == 14
            output_limited = False
            if output_limit_bytes > 0 and stdout_bytes >= output_limit_bytes:
                output_limited = True
            elif output_truncated in {"1", "true", "yes", "on"} and stdout_bytes > 0:
                output_limited = True
            if timed_out:
                runresult = "timelimit"
            elif output_limited:
                runresult = "output-limit"
        if runresult in {"compare-error", "run-error"} and compare_exit_code == 3:
            runresult = "checker-fail"
        verdict = domjudge_verdict_from_runresult(runresult)
        compile_config_hash = domjudge_json_hash(compile_cfg)
        run_config_hash = domjudge_json_hash(run_cfg)
        compare_config_hash = domjudge_json_hash(compare_cfg)
        toolchain_cmd_digest = domjudge_lower_text(compile_cfg.get("toolchain_cmd_digest"))
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._toolkit.toolchain_cmd_digest(source_name)

        cache_files: dict[str, bytes] = dict(payload_files)

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
            files=cache_files,
            shortcut_eligible=shortcut_eligible,
        )

        use_case_cache_tokens = bool(self._s.judge_fs_index_service is not None)

        def _case_blob_token(blob_name: str) -> str:
            if (not use_case_cache_tokens) or (blob_name not in cache_files):
                return ""
            return self._toolkit.cache_blob_ref(
                kind=self.CASE_CACHE_KIND,
                key_hash=case_key_hash,
                signature=case_signature,
                name=blob_name,
            )

        output_run_token = _case_blob_token("program.out")
        output_err_token = _case_blob_token("program.err")
        output_sys_token = _case_blob_token("system.out")
        output_diff_token = _case_blob_token("judgemessage.txt")
        metadata_token = _case_blob_token("program.meta")
        compare_meta_token = _case_blob_token("compare.meta")
        team_message_token = _case_blob_token("teammessage.txt")
        feedback_text, feedback_files = self._domjudge_feedback_text_and_files(
            work_root=work_root,
            runresult=runresult,
            output_error_rel=output_err_token,
            output_diff_rel=output_diff_token,
            team_message_rel=team_message_token,
        )
        # Always persist result artifacts as cache refs so product and judgehost
        # readers do not depend on work_root/results materialization.

        if not feedback_text:
            debug_context = self._s.batch_scheduler.case_debug_context(case_id)
            if debug_context is not None:
                debug_text = domjudge_text(debug_context["case_debug_text"])
                if not debug_text:
                    debug_text = domjudge_text(debug_context["batch_debug_text"])
                feedback_text = domjudge_feedback_text_from_text(debug_text)
        case_result = build_case_result(
            test_name=domjudge_text(row["test_name"]),
            runresult=runresult,
            verdict=verdict,
            runtime_sec=runtime_sec,
            cpu_sec=cpu_sec,
            wall_sec=wall_sec,
            memory_kb=memory_kb,
            score_text=score_text,
            output_run_rel=output_run_token,
            output_error_rel=output_err_token,
            output_system_rel=output_sys_token,
            output_diff_rel=output_diff_token,
            metadata_rel=metadata_token,
            compare_metadata_rel=compare_meta_token,
            team_message_rel=team_message_token,
            feedback_text=feedback_text,
            feedback_files=feedback_files,
            answer_correct=answer_correct,
        )
        now_text = now_iso()
        outcome = self._s.batch_scheduler.commit_case_result(
            case_id,
            generation=claim_generation,
            result=case_result,
            updated_at=now_text,
        )
        if outcome != "reported":
            logger.info("ignoring stale add_judging_run result for case id: %s", case_id)
            return case_id
        logger.debug(
            "domjudge add_judging_run host=%s batch_id=%s case_id=%s runresult=%s",
            safe_host,
            batch_id,
            case_id,
            runresult,
        )
        self._s.host_telemetry.record_case_reported(
            safe_host,
            batch_id,
            case_id,
            reported_at=reported_at,
            reported_monotonic=reported_monotonic,
        )
        try:
            self._domjudge_publish_reported_case(
                task_id=safe_task_id,
                test_name=domjudge_text(row["test_name"]),
            )
        except Exception:
            logger.exception(
                "failed to publish verification case result task_id=%s case_id=%s",
                safe_task_id,
                case_id,
            )
        batch_row = self._s.batch_scheduler.batch_finalize_row(batch_id)
        if batch_row is not None:
            try:
                self._domjudge_finalize_task_if_ready(
                    safe_task_id,
                    batch_row=dict(batch_row),
                )
            except Exception:
                logger.exception(
                    "failed to finalize DOMjudge task task_id=%s batch_id=%s",
                    safe_task_id,
                    batch_id,
                )
        self._domjudge_finalize_batch_if_ready(batch_id)
        return 1

    def domjudge_internal_error(
        self,
        *,
        description: str,
        judgetask_id: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> int:
        safe_desc = domjudge_text(description, default="judgehost internal error")
        if judgetask_id is None:
            return 0
        case_id = int(judgetask_id)
        target_case_id: int | None = None
        row = self._s.batch_scheduler.case_debug_context(case_id)
        if row is not None:
            batch_id = int(row["batch_id"])
            case_debug = domjudge_text(row["case_debug_text"])
            batch_debug = domjudge_text(row["batch_debug_text"])
            debug_text = case_debug
            if batch_debug and batch_debug not in debug_text:
                debug_text = batch_debug if not debug_text else f"{debug_text}\n{batch_debug}"
            result_id = case_id
            target_case_id = case_id
            case_identity = self._s.batch_scheduler.fetch_case(case_id)
            if case_identity is not None:
                self._touch_task_verification(domjudge_text(case_identity["task_id"]))
        else:
            batch_row = self._s.batch_scheduler.batch_debug_context(case_id)
            if batch_row is None:
                return 0
            batch_id = int(batch_row["batch_id"])
            debug_text = domjudge_text(batch_row["debug_text"])
            result_id = batch_id
            batch_identity = self._s.batch_scheduler.fetch_batch(batch_id)
            if batch_identity is not None:
                self._touch_task_verification(domjudge_text(batch_identity["task_id"]))
        payload_text = self._domjudge_debug_payload_text({} if payload is None else payload)
        if payload_text:
            debug_text = payload_text if not debug_text else f"{debug_text}\n{payload_text}"
            if len(debug_text) > 4000:
                debug_text = debug_text[-4000:]
        persisted_debug_text = debug_text or safe_desc
        if persisted_debug_text:
            self._s.batch_scheduler.append_debug_text(
                case_id=target_case_id,
                batch_id=batch_id,
                debug_text=persisted_debug_text,
                now_text=now_iso(),
            )
        if debug_text:
            if debug_text.lower() not in safe_desc.lower():
                safe_desc = f"{safe_desc}\n\n{debug_text}"
        self._domjudge_finalize_batch_if_ready(batch_id, force_failed=True, error_text=safe_desc)
        return result_id

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
        case_row = self._s.batch_scheduler.fetch_case(case_id)
        batch_row = self._s.batch_scheduler.fetch_batch(case_id)
        safe_task_id = ""
        safe_run_id = ""
        target_case_id: int | None = None
        target_batch_id: int | None = None
        if case_row is not None:
            safe_task_id = domjudge_text(case_row["task_id"])
            safe_run_id = domjudge_text(case_row["run_id"])
            target_case_id = int(case_row["id"])
            target_batch_id = int(case_row["batch_id"])
        elif batch_row is not None:
            safe_task_id = domjudge_text(batch_row["task_id"])
            safe_run_id = domjudge_text(batch_row["run_id"])
            target_batch_id = int(batch_row["batch_id"])
        self._touch_task_verification(safe_task_id)
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
            self._s.batch_scheduler.append_debug_text(
                case_id=target_case_id,
                batch_id=target_batch_id,
                debug_text=debug_text,
                now_text=now_iso(),
            )
            if case_row is not None:
                safe_test_name = domjudge_text(case_row["test_name"])
                if safe_task_id and safe_test_name and str(case_row["status"] or "") == "reported":
                    case_result = self._queue.poll_task_case_result(safe_task_id, safe_test_name)
                    if case_result is not None:
                        case_summary = dict(case_result["summary"])
                        test_rows = [
                            dict(row)
                            for row in cast(list[dict[str, object]], case_summary["tests"])
                        ]
                        for test_row in test_rows:
                            if domjudge_text(test_row["test"]) == safe_test_name:
                                test_row["message"] = debug_text
                        case_summary["error"] = debug_text
                        case_summary["tests"] = test_rows
                        case_result = {
                            **case_result,
                            "error": debug_text,
                            "summary": case_summary,
                        }
                        verification_task_store = self._s.verification_task_store
                        verification_task_row = verification_task_store.find_runtime_row_by_judgehost_case(
                            safe_task_id,
                            safe_test_name,
                        )
                        if verification_task_row is not None:
                            final_result = finalize_verification_task_result(verification_task_row, result=case_result)
                            verification_task_store.overwrite_task_result(
                                final_result.task_id,
                                status=final_result.status,
                                verdict=final_result.verdict,
                                run_id=final_result.run_id,
                                judgehost_task_id=final_result.judgehost_task_id,
                                runtime_sec=final_result.runtime_sec,
                                cpu_sec=final_result.cpu_sec,
                                wall_sec=final_result.wall_sec,
                                memory_kb=final_result.memory_kb,
                                compile_log=final_result.compile_log,
                                diagnostics_json=final_result.diagnostics_json,
                                error_text=final_result.error_text,
                                feedback_text=final_result.feedback_text,
                                output_ref=final_result.output_ref,
                                answer_correct=final_result.answer_correct,
                            )
                            verification_id = str(verification_task_row["verification_id"] or "")
                            if verification_id and final_result.fail_flag_reason:
                                verification_task_store.overwrite_fail_reason(
                                    verification_id,
                                    reason=final_result.fail_flag_reason,
                                )
        self._queue._record_host_event_conn(
            hostname=safe_host,
            action="debug",
            task_id=safe_task_id,
            run_id=safe_run_id,
        )
