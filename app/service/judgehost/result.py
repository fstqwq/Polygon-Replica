from __future__ import annotations

import base64
import logging
import json
import re
import shutil
from pathlib import Path
from typing import cast

from app.service.judgehost.shared import (
    domjudge_task_lease_owner,
    domjudge_text,
    domjudge_lower_text,
)
from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_json_hash, domjudge_source_hash
from app.service.judgehost.domjudge.client import domjudge_parse_script_id, domjudge_script_hash_field, domjudge_script_id
from app.service.judgehost.runtime import (
    domjudge_bool,
    domjudge_feedback_text_from_bytes,
    domjudge_feedback_text_from_text,
    domjudge_parse_float,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_rewrite_untrusted_runresult,
    domjudge_verdict_from_runresult,
)
from app.service.verification.test_rows import (
    build_verification_test_pass_row,
    build_verification_test_row,
    upsert_verification_test_row,
)
from app.service.verification.task_result_finalize import finalize_verification_task_result
from app.service.verification.task_scheduler import notify_verification_case_reported
from app.service.verification.task_store import VerificationTaskStore

from .core import JudgehostCore
from .state import JudgehostState
from .task_queue import TaskQueue
from .toolkit import DomjudgeToolkit

logger = logging.getLogger(__name__)

_diag_logger = logging.getLogger("uvicorn.error")


class ResultProcessor:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    CASE_CACHE_KIND = DomjudgeToolkit.CASE_CACHE_KIND
    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _STATE_ATTR_MAP = {
        "_state_lock": "state_lock",
        "_tasks_by_id": "tasks_by_id",
        "_judgehost_state_store": "judgehost_state_store",
        "_judge_fs_index_service": "judge_fs_index_service",
        "db": "db",
    }

    def __init__(self, state: JudgehostState, core: JudgehostCore, queue: TaskQueue, toolkit: DomjudgeToolkit) -> None:
        self._s = state
        self._core = core
        self._queue = queue
        self._toolkit = toolkit

    def __getattribute__(self, name: str):
        state_attr_map = object.__getattribute__(self, "_STATE_ATTR_MAP")
        if name in state_attr_map:
            state = object.__getattribute__(self, "_s")
            return getattr(state, state_attr_map[name])
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value) -> None:
        state_attr_map = type(self)._STATE_ATTR_MAP
        if name in state_attr_map and "_s" in self.__dict__:
            setattr(self._s, state_attr_map[name], value)
            return
        object.__setattr__(self, name, value)

    def __getattr__(self, name: str):
        core_aliases = {"_normalize_hostname", "_task_by_id", "_task_payload", "_record_host_judging"}
        if name in core_aliases:
            return getattr(object.__getattribute__(self, "_core"), name[1:])
        queue_aliases = {"_load_run_summary", "_record_host_event_conn", "report_result", "poll_task_case_result"}
        if name in queue_aliases:
            target = object.__getattribute__(self, "_queue")
            if name == "_record_host_event_conn":
                return object.__getattribute__(target, name)
            alias = name[1:] if name.startswith("_") else name
            return getattr(target, alias)
        toolkit_alias_map = {
            "_domjudge_b64_decode": "b64_decode",
            "_domjudge_cache_blob_ref": "cache_blob_ref",
            "_domjudge_case_cache_ref": "case_cache_ref",
            "_domjudge_contest_id": "contest_id",
            "_domjudge_payload_blob_bytes": "payload_blob_bytes",
            "_domjudge_read_artifact_blob": "read_artifact_blob",
            "_domjudge_store_case_cache": "store_case_cache",
            "_domjudge_task_kind": "task_kind",
            "_domjudge_toolchain_cmd_digest": "toolchain_cmd_digest",
            "resolve_artifact_blob": "resolve_artifact_blob",
        }
        alias = toolkit_alias_map.get(name)
        if alias is not None:
            return getattr(object.__getattribute__(self, "_toolkit"), alias)
        raise AttributeError(name)
    def _domjudge_task_accepts_case_updates(self, task_id: str) -> bool:
        task_row = self._task_by_id(task_id)
        if task_row is None:
            return False
        task_status = domjudge_lower_text(task_row["status"])
        return task_status in {
            self.STATUS_ENQUEUING,
            self.STATUS_QUEUED,
            self.STATUS_LEASED,
            self.STATUS_REPORTING,
        }

    @staticmethod
    def _domjudge_feedback_token_order(
        *,
        runresult: str,
        output_error_rel: str,
        output_diff_rel: str,
        team_message_rel: str,
    ) -> list[str]:
        runresult_token = domjudge_lower_text(runresult)
        if runresult_token in {"run-error", "internal-error"}:
            ordered = [output_error_rel, output_diff_rel, team_message_rel]
        else:
            ordered = [output_diff_rel, team_message_rel, output_error_rel]
        feedback_tokens: list[str] = []
        for token in ordered:
            feedback_token = domjudge_text(token)
            if feedback_token:
                feedback_tokens.append(feedback_token)
        return feedback_tokens

    def _domjudge_feedback_text_and_files(
        self,
        *,
        work_root: Path,
        runresult: str,
        output_error_rel: str,
        output_diff_rel: str,
        team_message_rel: str,
    ) -> tuple[str, list[str]]:
        feedback_files = self._domjudge_feedback_token_order(
            runresult=runresult,
            output_error_rel=output_error_rel,
            output_diff_rel=output_diff_rel,
            team_message_rel=team_message_rel,
        )
        feedback_text = ""
        for token in feedback_files:
            if feedback_text:
                break
            blob = self._domjudge_read_artifact_blob(work_root, token)
            if blob is not None:
                feedback_text = domjudge_feedback_text_from_bytes(blob)
        return feedback_text, feedback_files

    def _domjudge_update_verification_run_case_progress(
        self,
        *,
        task_id: str,
        source_path: str,
        test_name: str,
        verdict: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        feedback_text: str,
        output_ref: str,
        run_status: str = "running",
        runresult: str = "",
        feedback_files: list[str] | None = None,
    ) -> None:
        if not task_id:
            return
        task_row = self._task_by_id(task_id)
        if task_row is None:
            return
        payload = cast(dict[str, object], task_row["payload"])
        summary = dict(cast(dict[str, object], task_row.get("summary") or {}))
        verdict_token = verdict.upper() or "FL"
        time_ms = max(0, int(round(runtime_sec * 1000.0)))
        time_user_ms = max(0, int(round((cpu_sec if cpu_sec > 0.0 else runtime_sec) * 1000.0)))
        time_wall_ms = max(0, int(round((wall_sec if wall_sec > 0.0 else (cpu_sec if cpu_sec > 0.0 else runtime_sec)) * 1000.0)))
        memory_kb_int = max(0, memory_kb)
        test_row = build_verification_test_row(
            test_name=test_name,
            verdict=verdict_token,
            time_ms=time_ms,
            time_user_ms=time_user_ms,
            time_wall_ms=time_wall_ms,
            memory_kb=memory_kb_int,
            message=feedback_text,
            output_ref=output_ref,
            feedback_files=list(feedback_files or []),
            passes=[
                build_verification_test_pass_row(
                    verdict=verdict_token,
                    time_ms=time_ms,
                    time_user_ms=time_user_ms,
                    time_wall_ms=time_wall_ms,
                    memory_kb=memory_kb_int,
                    feedback=feedback_text,
                    output_ref=output_ref,
                    runresult=runresult,
                )
            ],
            runresult=runresult,
        )
        upsert_verification_test_row(
            summary,
            test_row=test_row,
            selected_tests_count=len(payload.get("selected_tests", [])),
        )
        summary["source"] = summary.get("source") or source_path
        summary["verification_source"] = payload.get("verification_source") or summary.get("verification_source") or "verification.start"
        summary["status"] = run_status
        judgehost_block = dict(cast(dict[str, object], summary.get("judgehost") or {}))
        judgehost_block["task_id"] = task_id
        summary["judgehost"] = judgehost_block
        with self._state_lock:
            row = self._tasks_by_id.get(task_id)
            if row is not None:
                row["summary"] = summary
                row["updated_at"] = now_iso()
    def domjudge_get_source_files(self, submit_id: str, contest_id: str | None = None) -> list[dict[str, object]]:
        safe_submit = domjudge_text(submit_id)
        if not safe_submit:
            raise RuntimeError("source files not found")
        safe_contest = None if contest_id is None else self._domjudge_contest_id(contest_id)
        row = self._judgehost_state_store.source_file_job(safe_submit, contest_id=safe_contest)
        if row is None:
            raise RuntimeError("source files not found")
        source_path = Path(domjudge_text(row["source_path"])).resolve()
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError("source files not found")
        source_name = domjudge_text(row["source_name"], default=source_path.name)
        files: list[Path] = []
        try:
            source_dir = source_path.parent.resolve()
            if source_dir.exists() and source_dir.is_dir() and (not source_dir.is_symlink()):
                candidates = sorted(
                    [entry for entry in source_dir.iterdir() if entry.exists() and entry.is_file() and (not entry.is_symlink())],
                    key=lambda entry: (0 if entry == source_path else 1, entry.name.lower(), entry.name),
                )
                files.extend(candidates)
        except Exception:
            files = []
        if not files:
            files = [source_path]

        out: list[dict[str, object]] = []
        seen_names: set[str] = set()
        for file_path in files:
            filename = domjudge_text(file_path.name)
            if not filename or filename in seen_names:
                continue
            seen_names.add(filename)
            out.append({"filename": filename, "content": base64.b64encode(file_path.read_bytes()).decode("ascii")})
        if not out:
            out = [{"filename": source_name, "content": base64.b64encode(source_path.read_bytes()).decode("ascii")}]
        return out

    def domjudge_get_testcase_files(self, testcase_id: int, *, hostname: str) -> list[dict[str, object]]:
        token = int(testcase_id)
        safe_host = self._normalize_hostname(hostname)
        row, resolution_source = self._judgehost_state_store.testcase_refs(token, hostname=safe_host)
        if row is None:
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s host=%s resolved=missing",
                token,
                safe_host,
            )
            raise RuntimeError("testcase files not found")
        input_ref = domjudge_text(row["input_ref"])
        answer_ref = domjudge_text(row["answer_ref"])
        input_blob = self.resolve_artifact_blob(input_ref)
        answer_blob = self.resolve_artifact_blob(answer_ref)
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
        _diag_logger.warning(
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

    def domjudge_get_executable_files(self, kind: str, script_id: object) -> list[dict[str, object]]:
        requested_id = domjudge_parse_script_id(script_id)
        token = domjudge_lower_text(kind)
        _ = domjudge_script_hash_field(token)
        candidates = self._judgehost_state_store.jobs_for_script_kind(token)
        matching_roots: list[Path] = []
        matching_hashes: set[str] = set()
        for row in candidates:
            script_hash = domjudge_lower_text(row["script_hash"])
            if not script_hash:
                continue
            if domjudge_script_id(script_hash) != requested_id:
                continue
            matching_hashes.add(script_hash)
            work_root_text = domjudge_text(row["work_root"])
            if not work_root_text:
                continue
            matching_roots.append(Path(work_root_text).resolve())
        if not matching_roots:
            raise RuntimeError("script files not found")
        unique_roots = {str(root) for root in matching_roots}
        if len(unique_roots) > 1 and len(matching_hashes) > 1:
            raise RuntimeError("ambiguous script id")
        base = (matching_roots[0] / "scripts" / token).resolve()
        if not base.exists() or not base.is_dir():
            raise RuntimeError("script files not found")
        rows: list[dict[str, object]] = []
        for file in sorted(base.iterdir(), key=lambda item: item.name):
            if not file.is_file():
                continue
            st_mode = int(file.stat().st_mode)
            rows.append(
                {
                    "filename": file.name,
                    "content": base64.b64encode(file.read_bytes()).decode("ascii"),
                    "is_executable": bool(st_mode & 0o111),
                }
            )
        if not rows:
            raise RuntimeError("script files not found")
        return rows

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

    def _domjudge_task_lease_owner(self, task_id: str) -> str:
        return domjudge_task_lease_owner(self._task_by_id(task_id), default="judgehost")

    def _domjudge_finalize_case_task(self, *, task_id: str, test_name: str, hostname: str) -> None:
        safe_task_id = domjudge_text(task_id)
        safe_test_name = domjudge_text(test_name)
        if (not safe_task_id) or (not safe_test_name):
            return
        task_row = self._task_by_id(safe_task_id)
        if task_row is None:
            return
        task_status = domjudge_lower_text(task_row["status"])
        if task_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
            return
        case_result = self.poll_task_case_result(safe_task_id, safe_test_name)
        if case_result is None:
            return
        self.report_result(
            task_id=safe_task_id,
            hostname=self._normalize_hostname(hostname),
            payload={
                "run_status": str(case_result.get("status") or "failed"),
                "error": str(case_result.get("error") or ""),
                "summary": dict(case_result.get("summary") or {}),
            },
        )
        verification_task_store = VerificationTaskStore(self.db)
        verification_task_row = verification_task_store.find_runtime_row_by_judgehost_case(
            safe_task_id,
            safe_test_name,
        )
        if verification_task_row is None:
            return
        final_result = finalize_verification_task_result(verification_task_row, result=case_result)
        notify_verification_case_reported(
            str(verification_task_row["verification_id"] or ""),
            safe_task_id,
            safe_test_name,
            final_result,
        )

    def _domjudge_finalize_if_ready(self, job_id: int, *, force_failed: bool = False, error_text: str = "") -> None:
        job_row = self._judgehost_state_store.job_finalize_row(int(job_id))
        if job_row is None:
            return
        current_status = domjudge_lower_text(job_row["status"])
        if current_status in {"completed", "failed"}:
            return
        cases = self._judgehost_state_store.cases_for_job(int(job_id))
        if not cases:
            return
        task_id = domjudge_text(job_row["task_id"])
        task_payload = self._task_payload(task_id) if task_id else {}
        task_kind = self._domjudge_task_kind(task_payload)
        compile_only = task_kind == self._TASK_KIND_COMPILE_ONLY
        compile_success_raw = job_row["compile_success"]
        compile_success = None
        if compile_success_raw is not None:
            try:
                compile_success = int(compile_success_raw)
            except Exception:
                compile_success = None
        ready = force_failed or compile_success == 0
        if not ready:
            ready = all(domjudge_lower_text(row["status"]) in {"reported", "cancelled"} for row in cases)
        if not ready:
            return
        grouped_job = bool(domjudge_text(job_row["group_key"]))
        if grouped_job:
            if force_failed and compile_success != 0:
                forced_at = now_iso()
                self._judgehost_state_store.mark_job_cases_reported(
                    int(job_id),
                    runresult="internal-error",
                    updated_at=forced_at,
                )
                cases = self._judgehost_state_store.cases_for_job(int(job_id))
            has_cancelled_cases = False
            for row in cases:
                if domjudge_lower_text(row["status"]) == "cancelled":
                    has_cancelled_cases = True
                    continue
                self._domjudge_finalize_case_task(
                    task_id=domjudge_text(row["task_id"]),
                    test_name=domjudge_text(row["test_name"]),
                    hostname=self._domjudge_task_lease_owner(domjudge_text(row["task_id"])),
                )
            finished_at = now_iso()
            self._judgehost_state_store.set_job_terminal_status(
                int(job_id),
                status="failed" if (force_failed or compile_success == 0 or has_cancelled_cases) else "completed",
                completed_at=finished_at,
                updated_at=finished_at,
            )
            shutil.rmtree(Path(domjudge_text(job_row["work_root"])).resolve(), ignore_errors=True)
            return

        tests: list[dict[str, object]] = []
        internal_failure_error = ""
        cancelled_cases = 0
        usage_time_user = 0
        usage_time_wall = 0
        usage_mem_peak = 0
        work_root = Path(domjudge_text(job_row["work_root"])).resolve()

        for row in cases:
            if domjudge_lower_text(row["status"]) == "cancelled":
                cancelled_cases += 1
                continue
            test_name = domjudge_text(row["test_name"], default=f"{int(row['ordinal']):03}.in")
            runresult = domjudge_text(row["runresult"])
            runresult_token = domjudge_lower_text(runresult)
            verdict = domjudge_verdict_from_runresult(runresult)
            if compile_success == 0:
                verdict = "CE"
            cpu_sec = domjudge_parse_float(row["cpu_sec"], domjudge_parse_float(row["runtime_sec"], 0.0))
            wall_sec = domjudge_parse_float(row["wall_sec"], cpu_sec)
            memory_kb = max(0, domjudge_parse_int(row["memory_kb"], 0))
            cpu_ms = max(0, int(round(cpu_sec * 1000)))
            wall_ms = max(0, int(round(wall_sec * 1000)))
            usage_time_user += cpu_ms
            usage_time_wall += wall_ms
            usage_mem_peak = max(usage_mem_peak, memory_kb)
            feedback_text, feedback_files = self._domjudge_feedback_text_and_files(
                work_root=work_root,
                runresult=runresult,
                output_error_rel=row["output_error_rel"],
                output_diff_rel=row["output_diff_rel"],
                team_message_rel=row["team_message_rel"],
            )
            output_ref = domjudge_text(row["output_run_rel"])
            tests.append(
                build_verification_test_row(
                    test_name=test_name,
                    verdict=verdict,
                    time_ms=cpu_ms,
                    time_user_ms=cpu_ms,
                    time_wall_ms=wall_ms,
                    memory_kb=memory_kb,
                    message=feedback_text,
                    output_ref=output_ref,
                    feedback_files=feedback_files,
                    passes=[
                        build_verification_test_pass_row(
                            verdict=verdict,
                            time_ms=cpu_ms,
                            time_user_ms=cpu_ms,
                            time_wall_ms=wall_ms,
                            memory_kb=memory_kb,
                            feedback=feedback_text,
                            output_ref=output_ref,
                            runresult=runresult,
                        )
                    ],
                    runresult=runresult,
                )
            )
            if (
                compile_success != 0
                and (not internal_failure_error)
                and runresult_token in {"checker-fail", "compare-error", "internal-error"}
            ):
                detail = domjudge_text(feedback_text)
                if not detail:
                    detail = runresult_token.replace("-", " ")
                internal_failure_error = f"{test_name}: {detail}" if test_name else detail
        if cancelled_cases > 0 and (not internal_failure_error):
            internal_failure_error = "judgehost task cancelled"

        compile_log = ""
        compile_diag: list[dict[str, object]] = []
        compile_text = self._domjudge_b64_decode(job_row["compile_output_b64"]).decode("utf-8", errors="replace")
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

        run_status = "failed" if (force_failed or compile_success == 0) else "ok"
        if (not force_failed) and internal_failure_error:
            run_status = "failed"
        summary = self._load_run_summary(domjudge_text(job_row["run_id"]))
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
                "compile": domjudge_lower_text(job_row["compile_hash"]),
                "run": domjudge_lower_text(job_row["run_hash"]),
                "compare": domjudge_lower_text(job_row["compare_hash"]),
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
        try:
            self.report_result(
                task_id=task_id,
                hostname=self._domjudge_task_lease_owner(task_id),
                payload=result_payload,
            )
        except RuntimeError as exc:
            logger.warning("failed to finalize DOMjudge job %s via report_result: %s", int(job_id), exc)
        finished_at = now_iso()
        self._judgehost_state_store.set_job_terminal_status(
            int(job_id),
            status="failed" if (force_failed or cancelled_cases > 0) else "completed",
            completed_at=finished_at,
            updated_at=finished_at,
        )
        shutil.rmtree(work_root, ignore_errors=True)

    def domjudge_update_judging(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> None:
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        case_row = self._judgehost_state_store.case_execution_row(case_id)
        if case_row is None:
            # judgedaemon may still report progress for a case that was already
            # dropped by server-side cancellation/startup cleanup. Treat as
            # idempotent no-op so daemon can continue without fatal retries.
            logger.info("ignoring update for unknown judging run id: %s", case_id)
            return
        if domjudge_lower_text(case_row["job_status"]) not in {"queued", "leased"}:
            logger.info("ignoring update for terminal DOMjudge job case id: %s", case_id)
            return
        if domjudge_lower_text(case_row["case_status"]) != "leased":
            logger.info("ignoring update for non-leased DOMjudge case id: %s", case_id)
            return
        safe_task_id = domjudge_text(case_row["task_id"])
        if not self._domjudge_task_accepts_case_updates(safe_task_id):
            logger.info("ignoring update for cancelled DOMjudge task case id: %s", case_id)
            return
        job_id = int(case_row["job_id"])
        compile_success = None
        if "compile_success" in payload:
            compile_success = 1 if domjudge_bool(payload.get("compile_success"), default=False) else 0

        def _payload_blob_as_b64(value: object) -> str:
            raw = self._domjudge_payload_blob_bytes(value)
            if raw:
                return base64.b64encode(raw).decode("ascii")
            return domjudge_text(value)

        compile_output = _payload_blob_as_b64(payload.get("output_compile"))
        compile_meta = _payload_blob_as_b64(payload.get("compile_metadata"))
        if compile_success is not None:
            updated_at = now_iso()
            updated = self._judgehost_state_store.record_compile_result(
                job_id,
                compile_success=compile_success,
                compile_output_b64=compile_output,
                compile_metadata_b64=compile_meta,
                lease_owner=safe_host,
                updated_at=updated_at,
            )
            if not updated:
                logger.info("ignoring compile update for terminal DOMjudge job case id: %s", case_id)
                return
            if compile_success == 0:
                self._judgehost_state_store.mark_compile_failed_cases(job_id, updated_at=updated_at)
                self._domjudge_finalize_if_ready(job_id)

    def domjudge_add_judging_run(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> int:
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        row = self._judgehost_state_store.case_execution_row(case_id)
        if row is None:
            # Same stale-callback case as domjudge_update_judging: acknowledge
            # gracefully to avoid hard-failing judgedaemon retries.
            logger.info("ignoring add_judging_run for unknown judging run id: %s", case_id)
            return case_id
        if domjudge_lower_text(row["job_status"]) not in {"queued", "leased"}:
            logger.info("ignoring add_judging_run for terminal DOMjudge job id: %s", case_id)
            return case_id
        if domjudge_lower_text(row["case_status"]) != "leased":
            logger.info("ignoring add_judging_run for non-leased DOMjudge case id: %s", case_id)
            return case_id
        job_id = int(row["job_id"])
        grouped_job = bool(domjudge_text(row["group_key"]))
        safe_task_id = domjudge_text(row["task_id"])
        if not self._domjudge_task_accepts_case_updates(safe_task_id):
            logger.info("ignoring add_judging_run for cancelled DOMjudge task id: %s", case_id)
            return case_id
        work_root = Path(domjudge_text(row["work_root"])).resolve()
        task_payload = self._task_payload(safe_task_id) if safe_task_id else {}
        verification_source = task_payload.get("verification_source", "")
        task_kind = self._domjudge_task_kind(task_payload, verification_source=verification_source)
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
            raw = self._domjudge_payload_blob_bytes(value)
            if (not raw) and (not allow_empty):
                return b""
            payload_files[name] = raw
            return raw

        if not compile_only:
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
            toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(source_name)

        cache_files: dict[str, bytes] = dict(payload_files)

        case_key_hash, case_signature = self._domjudge_case_cache_ref(
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
        self._domjudge_store_case_cache(
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

        use_case_cache_tokens = bool(self._judge_fs_index_service is not None)

        def _case_blob_token(blob_name: str) -> str:
            if (not use_case_cache_tokens) or (blob_name not in cache_files):
                return ""
            return self._domjudge_cache_blob_ref(
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

        now_text = now_iso()
        updated_case = self._judgehost_state_store.report_case_result(
            case_id,
            lease_owner=safe_host,
            runresult=runresult,
            runtime_sec=runtime_sec,
            cpu_sec=cpu_sec,
            wall_sec=wall_sec,
            memory_kb=memory_kb,
            output_run_rel=output_run_token,
            output_error_rel=output_err_token,
            output_system_rel=output_sys_token,
            output_diff_rel=output_diff_token,
            metadata_rel=metadata_token,
            compare_metadata_rel=compare_meta_token,
            team_message_rel=team_message_token,
            score_text=score_text,
            updated_at=now_text,
        )
        if not updated_case:
            logger.info("ignoring stale add_judging_run result for case id: %s", case_id)
            return case_id
        logger.warning(
            "domjudge add_judging_run host=%s job_id=%s case_id=%s runresult=%s",
            safe_host,
            job_id,
            case_id,
            runresult,
        )
        self._record_host_judging(safe_host, label=f"j{job_id}", updated_at=now_text)
        if not compile_only:
            try:
                self._domjudge_update_verification_run_case_progress(
                    task_id=safe_task_id,
                    source_path=row["source_path"],
                    test_name=row["test_name"],
                    verdict=verdict,
                    runtime_sec=runtime_sec,
                    cpu_sec=cpu_sec,
                    wall_sec=wall_sec,
                    memory_kb=memory_kb,
                    feedback_text=feedback_text,
                    output_ref=output_run_token,
                    run_status="running",
                    runresult=runresult,
                    feedback_files=feedback_files,
                )
            except Exception:
                logger.exception(
                    "failed to persist incremental verification case progress task_id=%s case_id=%s",
                    safe_task_id,
                    case_id,
                )
        if grouped_job:
            try:
                self._domjudge_finalize_case_task(
                    task_id=safe_task_id,
                    test_name=str(row["test_name"] or ""),
                    hostname=safe_host,
                )
            except Exception:
                logger.exception(
                    "failed to finalize grouped DOMjudge case task_id=%s case_id=%s",
                    safe_task_id,
                    case_id,
                )
        else:
            verification_task_store = VerificationTaskStore(self.db)
            verification_task_row = verification_task_store.find_runtime_row_by_judgehost_case(
                safe_task_id,
                str(row["test_name"] or ""),
            )
            if verification_task_row is not None:
                try:
                    case_result = self.poll_task_case_result(safe_task_id, str(row["test_name"] or ""))
                    if case_result is not None:
                        final_result = finalize_verification_task_result(verification_task_row, result=case_result)
                        notify_verification_case_reported(
                            str(verification_task_row["verification_id"] or ""),
                            safe_task_id,
                            str(row["test_name"] or ""),
                            final_result,
                        )
                except Exception:
                    logger.exception(
                        "failed to publish verification task result event task_id=%s case_id=%s",
                        safe_task_id,
                        case_id,
                    )

        self._domjudge_finalize_if_ready(job_id)
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
        row = self._judgehost_state_store.case_debug_context(case_id)
        if row is not None:
            job_id = int(row["job_id"])
            case_debug = domjudge_text(row["case_debug_text"])
            job_debug = domjudge_text(row["job_debug_text"])
            debug_text = case_debug
            if job_debug and job_debug not in debug_text:
                debug_text = job_debug if not debug_text else f"{debug_text}\n{job_debug}"
            result_id = case_id
        else:
            job_row = self._judgehost_state_store.job_debug_context(case_id)
            if job_row is None:
                return 0
            job_id = int(job_row["job_id"])
            debug_text = domjudge_text(job_row["debug_text"])
            result_id = job_id
        payload_text = self._domjudge_debug_payload_text({} if payload is None else payload)
        if payload_text:
            debug_text = payload_text if not debug_text else f"{debug_text}\n{payload_text}"
            if len(debug_text) > 4000:
                debug_text = debug_text[-4000:]
        if debug_text:
            if debug_text.lower() not in safe_desc.lower():
                safe_desc = f"{safe_desc}\n\n{debug_text}"
        self._domjudge_finalize_if_ready(job_id, force_failed=True, error_text=safe_desc)
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
                    blob = self._domjudge_b64_decode(compact)
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
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        case_row = self._judgehost_state_store.fetch_case(case_id)
        job_row = self._judgehost_state_store.fetch_job(case_id)
        safe_task_id = ""
        safe_run_id = ""
        target_case_id: int | None = None
        target_job_id: int | None = None
        if case_row is not None:
            safe_task_id = domjudge_text(case_row["task_id"])
            safe_run_id = domjudge_text(case_row["run_id"])
            target_case_id = int(case_row["id"])
            target_job_id = int(case_row["job_id"])
        elif job_row is not None:
            safe_task_id = domjudge_text(job_row["task_id"])
            safe_run_id = domjudge_text(job_row["run_id"])
            target_job_id = int(job_row["job_id"])
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
            self._judgehost_state_store.append_debug_text(
                case_id=target_case_id,
                job_id=target_job_id,
                debug_text=debug_text,
                now_text=now_iso(),
            )
        self._record_host_event_conn(
            hostname=safe_host,
            action="debug",
            task_id=safe_task_id,
            run_id=safe_run_id,
        )

