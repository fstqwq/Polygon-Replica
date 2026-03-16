from __future__ import annotations

import base64
import logging
import json
import re
from pathlib import Path
from typing import cast

from app.service.judgehost.internal.shared import (
    domjudge_task_lease_owner,
    domjudge_text,
    domjudge_lower_text,
)
from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_json_hash, domjudge_source_hash
from app.service.judgehost.domjudge.client import domjudge_parse_script_id
from app.service.judgehost.runtime import (
    domjudge_bool,
    domjudge_feedback_line_from_bytes,
    domjudge_feedback_line_from_text,
    domjudge_parse_float,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_rewrite_untrusted_runresult,
)
from app.service.platform.hashing import sha256_hex_bytes as domjudge_sha256_bytes
from app.service.verification.test_rows import (
    build_verification_test_pass_row,
    build_verification_test_row,
    upsert_verification_test_row,
)

logger = logging.getLogger(__name__)

_diag_logger = logging.getLogger("uvicorn.error")


class JudgehostDomjudgeResultsMixin:
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
                feedback_text = domjudge_feedback_line_from_bytes(blob)
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
        payload = task_row["payload"]
        verification_id = payload.get("artifact_verification_id") or payload.get("verification_id") or task_row["artifact_verification_id"]
        target_run_id = payload.get("verification_target_run_id") or task_row["run_id"] or payload.get("run_id")
        if (not verification_id) or (not target_run_id):
            return
        summary = self._load_run_summary(target_run_id, verification_id)
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
        summary["artifact_verification_id"] = verification_id
        self._ensure_verification_result(
            row=task_row,
            verification_id=verification_id,
            run_id=target_run_id,
            run_status=run_status,
            summary_obj=summary,
            error_text="",
        )
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

    def domjudge_get_testcase_files(self, testcase_id: int) -> list[dict[str, object]]:
        token = int(testcase_id)
        row, resolution_source = self._judgehost_state_store.testcase_paths(token)
        if row is None:
            with self._testcase_registry_lock:
                record = self._testcase_registry_by_id.get(token)
                if record is not None:
                    resolution_source = "registry"
                    row = {
                        "input_path": record["input_path"],
                        "answer_path": record["answer_path"],
                    }
        if row is None:
            _diag_logger.warning("judgehost.get_testcase_files testcase_id=%s resolved=missing", token)
            raise RuntimeError("testcase files not found")
        in_path = Path(row["input_path"]).resolve()
        ans_path = Path(row["answer_path"]).resolve()
        if not in_path.exists() or not ans_path.exists():
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s resolved=%s exists=%s input=%s answer=%s",
                token,
                resolution_source,
                False,
                in_path,
                ans_path,
            )
            raise RuntimeError("testcase files not found")
        _diag_logger.warning(
            "judgehost.get_testcase_files testcase_id=%s resolved=%s exists=%s input=%s answer=%s",
            token,
            resolution_source,
            True,
            in_path,
            ans_path,
        )
        return [
            {"filename": "input", "content": base64.b64encode(in_path.read_bytes()).decode("ascii")},
            {"filename": "output", "content": base64.b64encode(ans_path.read_bytes()).decode("ascii")},
        ]

    def domjudge_get_executable_files(self, kind: str, script_id: object) -> list[dict[str, object]]:
        job_id, offset = domjudge_parse_script_id(script_id)
        expected = {"compile": 1, "run": 2, "compare": 3}
        token = domjudge_lower_text(kind)
        if token not in expected or expected[token] != offset:
            raise RuntimeError("script id/type mismatch")
        work_root_text = self._judgehost_state_store.work_root_for_job(job_id)
        if not work_root_text:
            raise RuntimeError("script files not found")
        base = (Path(work_root_text).resolve() / "scripts" / token).resolve()
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

    @staticmethod
    def _domjudge_verdict_from_runresult(raw: str) -> str:
        token = domjudge_lower_text(raw)
        mapping = {
            "correct": "OK",
            "compiler-error": "CE",
            "timelimit": "TL",
            "run-error": "RE",
            "wrong-answer": "WA",
            "no-output": "WA",
            "checker-fail": "FL",
            "output-limit": "FL",
            "compare-error": "FL",
            "internal-error": "FL",
        }
        return mapping.get(token, "FL")

    def _domjudge_task_lease_owner(self, task_id: str) -> str:
        return domjudge_task_lease_owner(self._task_by_id(task_id), default="judgehost")

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
            ready = all(domjudge_lower_text(row["status"]) == "reported" for row in cases)
        if not ready:
            return

        tests: list[dict[str, object]] = []
        internal_failure_error = ""
        usage_time_user = 0
        usage_time_wall = 0
        usage_mem_peak = 0
        work_root = Path(domjudge_text(job_row["work_root"])).resolve()

        for row in cases:
            test_name = domjudge_text(row["test_name"], default=f"{int(row['ordinal']):03}.in")
            runresult = domjudge_text(row["runresult"])
            runresult_token = domjudge_lower_text(runresult)
            verdict = self._domjudge_verdict_from_runresult(runresult)
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
            compile_error_task = domjudge_feedback_line_from_text(message, max_chars=320) or "compilation failed"
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
            status="failed" if force_failed else "completed",
            completed_at=finished_at,
            updated_at=finished_at,
        )

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
        safe_task_id = domjudge_text(row["task_id"])
        if not self._domjudge_task_accepts_case_updates(safe_task_id):
            logger.info("ignoring add_judging_run for cancelled DOMjudge task id: %s", case_id)
            return case_id
        work_root = Path(domjudge_text(row["work_root"])).resolve()
        result_root = (work_root / "results" / f"{case_id}").resolve()
        result_root.mkdir(parents=True, exist_ok=True)
        task_payload = self._task_payload(safe_task_id) if safe_task_id else {}
        verification_source = task_payload.get("verification_source", "")
        task_kind = self._domjudge_task_kind(task_payload, verification_source=verification_source)
        compile_only = task_kind == self._TASK_KIND_COMPILE_ONLY

        def _store_payload_file(
            name: str,
            value: object,
            *,
            allow_empty: bool = False,
        ) -> str:
            if value is None:
                return ""
            raw = self._domjudge_payload_blob_bytes(value)
            if (not raw) and (not allow_empty):
                return ""
            target = (result_root / name).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            return target.relative_to(work_root).as_posix()

        output_run_rel = ""
        if not compile_only:
            output_run_rel = _store_payload_file(
                "program.out",
                payload.get("output_run"),
                allow_empty=True,
            )
        output_err_rel = _store_payload_file("program.err", payload.get("output_error"))
        output_sys_rel = _store_payload_file("system.out", payload.get("output_system"))
        output_diff_rel = _store_payload_file("judgemessage.txt", payload.get("output_diff"))
        metadata_rel = _store_payload_file("program.meta", payload.get("metadata"))
        compare_meta_rel = _store_payload_file("compare.meta", payload.get("compare_metadata"))
        team_message_rel = _store_payload_file("teammessage.txt", payload.get("team_message"))

        runtime_sec = domjudge_parse_float(payload.get("runtime"), 0.0)
        cpu_sec = runtime_sec
        wall_sec = runtime_sec
        memory_kb = 0
        compare_exit_code = -1
        program_meta: dict[str, str] = {}
        if metadata_rel:
            meta_path = (work_root / metadata_rel).resolve()
            if meta_path.exists() and meta_path.is_file():
                program_meta = domjudge_parse_meta_text(meta_path.read_text(encoding="utf-8", errors="replace"))
                cpu_total_sec = domjudge_parse_float(program_meta.get("cpu-time"), runtime_sec)
                wall_sec = domjudge_parse_float(program_meta.get("wall-time"), cpu_total_sec)
                runtime_sec = cpu_sec = cpu_total_sec
                mem_bytes = domjudge_parse_int(program_meta.get("memory-bytes"), 0)
                memory_kb = max(0, int(mem_bytes // 1024))
        if compare_meta_rel:
            compare_meta_path = (work_root / compare_meta_rel).resolve()
            if compare_meta_path.exists() and compare_meta_path.is_file():
                compare_meta = domjudge_parse_meta_text(
                    compare_meta_path.read_text(encoding="utf-8", errors="replace")
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

        input_bytes = b""
        input_path = Path(domjudge_text(row["input_path"])).resolve()
        try:
            if input_path.exists() and input_path.is_file() and (not input_path.is_symlink()):
                input_bytes = input_path.read_bytes()
        except OSError:
            input_bytes = b""

        answer_bytes = b""
        answer_path = Path(domjudge_text(row["answer_path"])).resolve()
        try:
            if answer_path.exists() and answer_path.is_file() and (not answer_path.is_symlink()):
                answer_bytes = answer_path.read_bytes()
        except OSError:
            answer_bytes = b""

        testcase_hash = domjudge_lower_text(row["testcase_hash"])
        testcase_input_hash = domjudge_sha256_bytes(input_bytes)
        testcase_answer_hash = domjudge_sha256_bytes(answer_bytes)
        if not re.fullmatch(r"[0-9a-f]{64}", testcase_hash):
            if verification_source in {"verification.solve-main", "solve.main"}:
                testcase_hash = testcase_input_hash
            else:
                testcase_hash = self._domjudge_set_hash_from_blobs([input_bytes, answer_bytes])

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
        verdict = self._domjudge_verdict_from_runresult(runresult)
        feedback_text, feedback_files = self._domjudge_feedback_text_and_files(
            work_root=work_root,
            runresult=runresult,
            output_error_rel=output_err_rel,
            output_diff_rel=output_diff_rel,
            team_message_rel=team_message_rel,
        )
        if (not compile_only) and verdict == "OK" and (not output_run_rel):
            target = (result_root / "program.out").resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
            output_run_rel = target.relative_to(work_root).as_posix()
        compile_config_hash = domjudge_json_hash(compile_cfg)
        run_config_hash = domjudge_json_hash(run_cfg)
        compare_config_hash = domjudge_json_hash(compare_cfg)
        toolchain_cmd_digest = domjudge_lower_text(compile_cfg.get("toolchain_cmd_digest"))
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(source_name)

        cache_files: dict[str, bytes] = {}

        def _read_rel_blob(rel_path: str) -> bytes | None:
            return self._domjudge_read_artifact_blob(work_root, rel_path)

        for rel, blob_name in (
            (output_run_rel, "program.out"),
            (output_err_rel, "program.err"),
            (output_sys_rel, "system.out"),
            (output_diff_rel, "judgemessage.txt"),
            (metadata_rel, "program.meta"),
            (compare_meta_rel, "compare.meta"),
            (team_message_rel, "teammessage.txt"),
        ):
            blob = _read_rel_blob(rel)
            if blob is not None:
                cache_files[blob_name] = blob

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
        should_store_case_cache = verdict != "FL"
        if compile_only and verdict != "OK":
            should_store_case_cache = False
        if should_store_case_cache:
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
            )

        use_case_cache_tokens = bool(should_store_case_cache and self._judge_fs_index_service is not None)

        def _case_blob_token(blob_name: str, fallback_rel: str) -> str:
            if (not use_case_cache_tokens) or (blob_name not in cache_files):
                return fallback_rel
            return self._domjudge_cache_blob_ref(
                kind=self.CASE_CACHE_KIND,
                key_hash=case_key_hash,
                signature=case_signature,
                name=blob_name,
            )

        output_run_token = _case_blob_token("program.out", output_run_rel)
        output_err_token = _case_blob_token("program.err", output_err_rel)
        output_sys_token = _case_blob_token("system.out", output_sys_rel)
        output_diff_token = _case_blob_token("judgemessage.txt", output_diff_rel)
        metadata_token = _case_blob_token("program.meta", metadata_rel)
        compare_meta_token = _case_blob_token("compare.meta", compare_meta_rel)
        team_message_token = _case_blob_token("teammessage.txt", team_message_rel)
        # Prefer cache refs for summary output_ref so build/run consumers can still
        # resolve artifacts after judgehost temp work directories are cleaned.

        if verification_source in {"verification.solve-main", "solve.main"} and runresult == "correct":
            solve_key_hash, solve_signature = self._domjudge_solve_output_cache_ref(
                source_hash=source_hash,
                compile_hash=compile_hash,
                run_hash=run_hash,
                compile_config_hash=compile_config_hash,
                run_config_hash=run_config_hash,
                toolchain_cmd_digest=toolchain_cmd_digest,
                testcase_input_hash=testcase_input_hash,
            )
            output_hash = domjudge_sha256_bytes(cache_files.get("program.out", b""))
            self._domjudge_store_solve_output_cache(
                key_parts={"key_hash": solve_key_hash, "signature": solve_signature},
                tags={
                    "source_hash": source_hash,
                    "testcase_input_hash": testcase_input_hash,
                    "testcase_answer_hash": testcase_answer_hash,
                },
                output_hash=output_hash,
                runtime_sec=runtime_sec,
                cpu_sec=cpu_sec,
                wall_sec=wall_sec,
                memory_kb=memory_kb,
                files=cache_files,
            )

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

        def _walk_scalars(value: object) -> list[str]:
            compact = json.dumps(value, ensure_ascii=False, default=str)
            out: list[str] = []
            for raw_line in compact.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                text = domjudge_text(raw_line)
                if not text:
                    continue
                low = text.lower()
                if any(
                    marker in low
                    for marker in (
                        "fail",
                        "error",
                        "exception",
                        "trace",
                        "crash",
                        "compare",
                        "expected",
                        "unexpected",
                    )
                ):
                    out.append(text)
                if len(out) >= 32:
                    break
            return out

        for key in (
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
        ):
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
