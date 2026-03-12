from __future__ import annotations

import logging

from app.service.judgehost.internal.shared import (
    Path,
    base64,
    domjudge_lower_text,
    domjudge_task_lease_owner,
    json,
    logger,
    now_iso,
    re,
)

_diag_logger = logging.getLogger("uvicorn.error")


class JudgehostDomjudgeResultsMixin:
    def domjudge_get_source_files(self, submit_id: str, contest_id: str | None = None) -> list[dict[str, object]]:
        safe_submit = self._domjudge_text(submit_id)
        if not safe_submit:
            raise RuntimeError("source files not found")
        row = None
        if safe_submit.isdigit():
            row = self._db_fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE job_id=?
                """,
                [int(safe_submit)],
            )
        if row is None and contest_id is not None:
            safe_contest = self._domjudge_contest_id(contest_id)
            row = self._db_fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE submit_id=? AND contest_id=?
                """,
                [safe_submit, safe_contest],
            )
        if row is None:
            row = self._db_fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE submit_id=?
                """,
                [safe_submit],
            )
        if row is None:
            raise RuntimeError("source files not found")
        source_path = Path(self._domjudge_text(row["source_path"])).resolve()
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError("source files not found")
        source_name = self._domjudge_text(row["source_name"], default=source_path.name)
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
            filename = self._domjudge_text(file_path.name)
            if not filename or filename in seen_names:
                continue
            seen_names.add(filename)
            content = base64.b64encode(file_path.read_bytes()).decode("ascii")
            out.append({"filename": filename, "content": content})
        if not out:
            content = base64.b64encode(source_path.read_bytes()).decode("ascii")
            out = [{"filename": source_name, "content": content}]
        return out

    def domjudge_get_testcase_files(self, testcase_id: int) -> list[dict[str, object]]:
        token = int(testcase_id)
        resolution_source = "case-id"
        # judgedaemon receives case id as testcase_id; resolve case row first.
        row = self._db_fetch_one(
            """
            SELECT input_path,answer_path
            FROM judgehost_domjudge_cases
            WHERE id=?
            LIMIT 1
            """,
            [token],
        )
        if row is None:
            resolution_source = "stored-testcase-id"
            row = self._db_fetch_one(
                """
                SELECT input_path,answer_path
                FROM judgehost_domjudge_cases
                WHERE testcase_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                [token],
            )
        if row is None:
            with self._testcase_registry_lock:
                record = self._testcase_registry_by_id.get(int(token))
                if isinstance(record, dict):
                    resolution_source = "registry"
                    row = {
                        "input_path": self._domjudge_text(record.get("input_path")),
                        "answer_path": self._domjudge_text(record.get("answer_path")),
                    }
        if row is None:
            _diag_logger.warning("judgehost.get_testcase_files testcase_id=%s resolved=missing", token)
            raise RuntimeError("testcase files not found")
        in_path = Path(self._domjudge_text(row["input_path"])).resolve()
        ans_path = Path(self._domjudge_text(row["answer_path"])).resolve()
        if not in_path.exists() or not ans_path.exists():
            _diag_logger.warning(
                "judgehost.get_testcase_files testcase_id=%s resolved=%s exists=%s input=%s answer=%s",
                token,
                resolution_source,
                False,
                str(in_path),
                str(ans_path),
            )
            raise RuntimeError("testcase files not found")
        _diag_logger.warning(
            "judgehost.get_testcase_files testcase_id=%s resolved=%s exists=%s input=%s answer=%s",
            token,
            resolution_source,
            True,
            str(in_path),
            str(ans_path),
        )
        return [
            {"filename": "input", "content": base64.b64encode(in_path.read_bytes()).decode("ascii")},
            {"filename": "output", "content": base64.b64encode(ans_path.read_bytes()).decode("ascii")},
        ]

    def domjudge_get_executable_files(self, kind: str, script_id: object) -> list[dict[str, object]]:
        job_id, offset = self._domjudge_parse_script_id(script_id)
        expected = {"compile": 1, "run": 2, "compare": 3}
        token = self._domjudge_lower_text(kind)
        if token not in expected or expected[token] != offset:
            raise RuntimeError("script id/type mismatch")
        job_row = self._db_fetch_one("SELECT work_root FROM judgehost_domjudge_jobs WHERE job_id=?", [job_id])
        if job_row is None:
            raise RuntimeError("script files not found")
        base = (Path(self._domjudge_text(job_row["work_root"])).resolve() / "scripts" / token).resolve()
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
        _ = self._domjudge_text(hostname)
        _ = self._domjudge_text(compiler)
        _ = self._domjudge_text(runner)
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
        job_row = self._db_fetch_one(
            """
            SELECT task_id,run_id,status,compile_success,compile_output_b64,compile_metadata_b64,work_root,run_config_json,
                   compile_hash,run_hash,compare_hash
            FROM judgehost_domjudge_jobs
            WHERE job_id=?
            """,
            [int(job_id)],
        )
        if job_row is None:
            return
        current_status = self._domjudge_lower_text(job_row["status"])
        if current_status in {"completed", "failed"}:
            return
        cases = self._domjudge_cases_for_job(int(job_id))
        if not cases:
            return
        task_id = self._domjudge_text(job_row["task_id"])
        task_payload_obj = self._task_payload(task_id) if task_id else {}
        task_kind = self._domjudge_task_kind(task_payload_obj)
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
            ready = all(self._domjudge_lower_text(row["status"]) == "reported" for row in cases)
        if not ready:
            return

        tests: list[dict[str, object]] = []
        internal_failure_error = ""
        usage_time_user = 0
        usage_time_wall = 0
        usage_mem_peak = 0
        work_root = Path(self._domjudge_text(job_row["work_root"])).resolve()

        for row in cases:
            test_name = self._domjudge_text(row["test_name"], default=f"{int(row['ordinal']):03}.in")
            test_stem = Path(test_name).stem
            runresult = self._domjudge_text(row["runresult"])
            runresult_token = runresult.lower()
            verdict = self._domjudge_verdict_from_runresult(runresult)
            if compile_success == 0:
                verdict = "CE"
            cpu_sec = self._domjudge_parse_float(row["cpu_sec"], self._domjudge_parse_float(row["runtime_sec"], 0.0))
            wall_sec = self._domjudge_parse_float(row["wall_sec"], cpu_sec)
            memory_kb = max(0, self._domjudge_parse_int(row["memory_kb"], 0))
            cpu_ms = max(0, int(round(cpu_sec * 1000)))
            wall_ms = max(0, int(round(wall_sec * 1000)))
            usage_time_user += cpu_ms
            usage_time_wall += wall_ms
            usage_mem_peak = max(usage_mem_peak, memory_kb)
            feedback_files: list[str] = []
            feedback_text = ""
            has_output_diff = False
            has_team_message = False
            for key in ("output_diff_rel", "team_message_rel"):
                token = self._domjudge_text(row[key])
                if token:
                    if key == "output_diff_rel":
                        has_output_diff = True
                    elif key == "team_message_rel":
                        has_team_message = True
                    if not feedback_text:
                        blob = self._domjudge_read_artifact_blob(work_root, token)
                        if blob is not None:
                            feedback_text = self._domjudge_feedback_line_from_bytes(blob)
            if test_stem:
                for filename, present in (("judgemessage.txt", has_output_diff), ("teammessage.txt", has_team_message)):
                    if not present:
                        continue
                    feedback_files.append(f"feedback_dir/{test_stem}/{filename}")
            final_pass = {
                "verdict": verdict,
                "time_ms": cpu_ms,
                "time_user_ms": cpu_ms,
                "time_wall_ms": wall_ms,
                "memory_kb": memory_kb,
                "runresult": runresult,
            }
            output_ref = self._domjudge_text(row["output_run_rel"])
            if output_ref:
                final_pass["output_ref"] = output_ref
            if feedback_text:
                final_pass["feedback"] = feedback_text
            tests.append(
                {
                    "test": test_name,
                    "passes": [final_pass],
                    "verdict": verdict,
                    "time_ms": cpu_ms,
                    "time_user_ms": cpu_ms,
                    "time_wall_ms": wall_ms,
                    "memory_kb": memory_kb,
                    "feedback_files": feedback_files,
                    "runresult": runresult,
                }
            )
            if (
                compile_success != 0
                and (not internal_failure_error)
                and runresult_token in {"checker-fail", "compare-error", "internal-error"}
            ):
                detail = self._domjudge_text(feedback_text)
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
            compile_error_task = self._domjudge_feedback_line_from_text(message, max_chars=320) or "compilation failed"
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
        summary = self._load_run_summary(self._domjudge_text(job_row["run_id"]))
        summary = dict(summary or {})
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
        judgehost_obj = summary.get("judgehost")
        if not isinstance(judgehost_obj, dict):
            judgehost_obj = {}
        judgehost_obj["script_hashes"] = {
            "compile": self._domjudge_lower_text(job_row["compile_hash"]),
            "run": self._domjudge_lower_text(job_row["run_hash"]),
            "compare": self._domjudge_lower_text(job_row["compare_hash"]),
        }
        summary["judgehost"] = judgehost_obj
        summary.setdefault("invocation_backend", "domjudge-judgehost")
        if force_failed and error_text:
            summary["error"] = str(error_text)
        elif compile_error_summary:
            summary["error"] = compile_error_summary
        elif internal_failure_error:
            summary["error"] = internal_failure_error
        result_payload: dict[str, object] = {"run_status": run_status, "summary": summary}
        if force_failed and error_text:
            result_payload["error"] = str(error_text)
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
        self._db_execute(
            "UPDATE judgehost_domjudge_jobs SET status=?, completed_at=?, updated_at=? WHERE job_id=?",
            ["failed" if force_failed else "completed", now_iso(), now_iso(), int(job_id)],
        )

    def domjudge_update_judging(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> None:
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        case_row = self._db_fetch_one("SELECT id,job_id FROM judgehost_domjudge_cases WHERE id=?", [case_id])
        if case_row is None:
            # judgedaemon may still report progress for a case that was already
            # dropped by server-side cancellation/startup cleanup. Treat as
            # idempotent no-op so daemon can continue without fatal retries.
            logger.info("ignoring update for unknown judging run id: %s", case_id)
            return
        job_id = int(case_row["job_id"])
        compile_success = None
        if "compile_success" in payload:
            compile_success = 1 if self._domjudge_bool(payload.get("compile_success"), default=False) else 0

        def _payload_blob_as_b64(value: object) -> str:
            if isinstance(value, (bytes, bytearray, memoryview)):
                raw = bytes(value)
                if not raw:
                    return ""
                return base64.b64encode(raw).decode("ascii")
            return self._domjudge_text(value)

        compile_output = _payload_blob_as_b64(payload.get("output_compile"))
        compile_meta = _payload_blob_as_b64(payload.get("compile_metadata"))
        if compile_success is not None:
            self._db_execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET compile_success=?, compile_output_b64=?, compile_metadata_b64=?, lease_owner=?, updated_at=?
                WHERE job_id=?
                """,
                [compile_success, compile_output, compile_meta, safe_host, now_iso(), job_id],
            )
            if compile_success == 0:
                self._db_execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='reported', runresult='compiler-error', runtime_sec=0, cpu_sec=0, wall_sec=0, memory_kb=0, updated_at=?
                    WHERE job_id=? AND status<>'reported'
                    """,
                    [now_iso(), job_id],
                )
                self._domjudge_finalize_if_ready(job_id)

    def domjudge_add_judging_run(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> int:
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        row = self._db_fetch_one(
            """
            SELECT
                c.id,c.job_id,c.task_id,c.test_name,c.testcase_hash,c.input_path,c.answer_path,
                j.run_id,j.work_root,j.mode,j.source_name,j.source_path,
                j.source_hash,j.compile_hash,j.run_hash,j.compare_hash,
                j.compile_config_json,j.run_config_json,j.compare_config_json,j.compile_success
            FROM judgehost_domjudge_cases c
            JOIN judgehost_domjudge_jobs j ON j.job_id=c.job_id
            WHERE c.id=?
            """,
            [case_id],
        )
        if row is None:
            # Same stale-callback case as domjudge_update_judging: acknowledge
            # gracefully to avoid hard-failing judgedaemon retries.
            logger.info("ignoring add_judging_run for unknown judging run id: %s", case_id)
            return case_id
        job_id = int(row["job_id"])
        safe_task_id = self._domjudge_text(row["task_id"])
        work_root = Path(self._domjudge_text(row["work_root"])).resolve()
        result_root = (work_root / "results" / f"{case_id}").resolve()
        result_root.mkdir(parents=True, exist_ok=True)
        task_payload_obj = self._task_payload(safe_task_id) if safe_task_id else {}
        invocation_source = self._domjudge_lower_text(task_payload_obj.get("invocation_source"))
        task_kind = self._domjudge_task_kind(task_payload_obj, invocation_source=invocation_source)
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
            return str(target.relative_to(work_root).as_posix())

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

        runtime_sec = self._domjudge_parse_float(payload.get("runtime"), 0.0)
        cpu_sec = runtime_sec
        wall_sec = runtime_sec
        memory_kb = 0
        compare_exit_code = -1
        program_meta: dict[str, str] = {}
        if metadata_rel:
            meta_path = (work_root / metadata_rel).resolve()
            if meta_path.exists() and meta_path.is_file():
                program_meta = self._domjudge_parse_meta_text(meta_path.read_text(encoding="utf-8", errors="replace"))
                cpu_total_sec = self._domjudge_parse_float(program_meta.get("cpu-time"), runtime_sec)
                wall_sec = self._domjudge_parse_float(program_meta.get("wall-time"), cpu_total_sec)
                cpu_sec = cpu_total_sec
                runtime_sec = cpu_sec
                mem_bytes = self._domjudge_parse_int(program_meta.get("memory-bytes"), 0)
                memory_kb = max(0, int(mem_bytes // 1024))
        if compare_meta_rel:
            compare_meta_path = (work_root / compare_meta_rel).resolve()
            if compare_meta_path.exists() and compare_meta_path.is_file():
                compare_meta = self._domjudge_parse_meta_text(
                    compare_meta_path.read_text(encoding="utf-8", errors="replace")
                )
                compare_exit_code = self._domjudge_parse_int(compare_meta.get("exitcode"), -1)

        score_text = self._domjudge_text(payload.get("score"))
        feedback_text = ""
        for rel in (output_diff_rel, team_message_rel):
            token = self._domjudge_text(rel)
            if (not token) or feedback_text:
                continue
            blob = self._domjudge_read_artifact_blob(work_root, token)
            if blob is not None:
                feedback_text = self._domjudge_feedback_line_from_bytes(blob)

        def _load_json_object(raw: object) -> dict[str, object]:
            text = self._domjudge_text(raw)
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        source_name = self._domjudge_text(row["source_name"])
        source_hash = self._domjudge_lower_text(row["source_hash"])
        # Reuse the enqueue-time source hash directly so cache keys stay stable
        # when payload contains extra sources (for example testlib.h).
        if re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            source_bytes = b""
            source_path = Path(self._domjudge_text(row["source_path"])).resolve()
            try:
                if source_path.exists() and source_path.is_file() and (not source_path.is_symlink()):
                    source_bytes = source_path.read_bytes()
            except OSError:
                source_bytes = b""
            source_hash = self._domjudge_source_hash(source_name, source_bytes)

        input_bytes = b""
        input_path = Path(self._domjudge_text(row["input_path"])).resolve()
        try:
            if input_path.exists() and input_path.is_file() and (not input_path.is_symlink()):
                input_bytes = input_path.read_bytes()
        except OSError:
            input_bytes = b""

        answer_bytes = b""
        answer_path = Path(self._domjudge_text(row["answer_path"])).resolve()
        try:
            if answer_path.exists() and answer_path.is_file() and (not answer_path.is_symlink()):
                answer_bytes = answer_path.read_bytes()
        except OSError:
            answer_bytes = b""

        testcase_hash = self._domjudge_lower_text(row["testcase_hash"])
        testcase_input_hash = self._domjudge_sha256_bytes(input_bytes)
        testcase_answer_hash = self._domjudge_sha256_bytes(answer_bytes)
        if not re.fullmatch(r"[0-9a-f]{64}", testcase_hash):
            if invocation_source in {"build.solve", "solve.main"}:
                testcase_hash = testcase_input_hash
            else:
                testcase_hash = self._domjudge_set_hash_from_blobs([input_bytes, answer_bytes])

        compile_hash = self._domjudge_lower_text(row["compile_hash"])
        run_hash = self._domjudge_lower_text(row["run_hash"])
        compare_hash = self._domjudge_lower_text(row["compare_hash"])
        compile_cfg_obj = _load_json_object(row["compile_config_json"])
        run_cfg_obj = _load_json_object(row["run_config_json"])
        compare_cfg_obj = _load_json_object(row["compare_config_json"])
        runresult = self._domjudge_lower_text(payload.get("runresult"), default="internal-error")
        runresult = self._domjudge_rewrite_untrusted_runresult(
            runresult,
            cpu_sec=cpu_sec,
            run_cfg_obj=run_cfg_obj,
        )
        if runresult in {"compare-error", "run-error", "internal-error"} and compare_exit_code < 0:
            time_result = self._domjudge_lower_text(program_meta.get("time-result"))
            signal_num = self._domjudge_parse_int(program_meta.get("signal"), 0)
            output_limit_kb = self._domjudge_parse_int(run_cfg_obj.get("output_limit"), 0)
            output_limit_bytes = max(0, int(output_limit_kb) * 1024)
            stdout_bytes = self._domjudge_parse_int(program_meta.get("stdout-bytes"), 0)
            output_truncated = self._domjudge_lower_text(program_meta.get("output-truncated"))
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
        if (not compile_only) and verdict == "OK" and (not self._domjudge_text(output_run_rel)):
            target = (result_root / "program.out").resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
            output_run_rel = str(target.relative_to(work_root).as_posix())
        compile_config_hash = self._domjudge_json_hash(compile_cfg_obj)
        run_config_hash = self._domjudge_json_hash(run_cfg_obj)
        compare_config_hash = self._domjudge_json_hash(compare_cfg_obj)
        toolchain_cmd_digest = self._domjudge_lower_text(compile_cfg_obj.get("toolchain_cmd_digest"))
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
            blob = _read_rel_blob(str(rel or ""))
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
                    "invocation_source": invocation_source,
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
            rel_token = self._domjudge_text(fallback_rel)
            if (not use_case_cache_tokens) or (blob_name not in cache_files):
                return rel_token
            return self._domjudge_cache_blob_ref(
                kind=self.CASE_CACHE_KIND,
                key_hash=case_key_hash,
                signature=case_signature,
                name=blob_name,
            )

        output_run_token = _case_blob_token("program.out", str(output_run_rel or ""))
        output_err_token = _case_blob_token("program.err", str(output_err_rel or ""))
        output_sys_token = _case_blob_token("system.out", str(output_sys_rel or ""))
        output_diff_token = _case_blob_token("judgemessage.txt", str(output_diff_rel or ""))
        metadata_token = _case_blob_token("program.meta", str(metadata_rel or ""))
        compare_meta_token = _case_blob_token("compare.meta", str(compare_meta_rel or ""))
        team_message_token = _case_blob_token("teammessage.txt", str(team_message_rel or ""))
        # Prefer cache refs for summary output_ref so build/run consumers can still
        # resolve artifacts after judgehost temp work directories are cleaned.

        if invocation_source in {"build.solve", "solve.main"} and runresult == "correct":
            solve_key_hash, solve_signature = self._domjudge_solve_output_cache_ref(
                source_hash=source_hash,
                compile_hash=compile_hash,
                run_hash=run_hash,
                compile_config_hash=compile_config_hash,
                run_config_hash=run_config_hash,
                toolchain_cmd_digest=toolchain_cmd_digest,
                testcase_input_hash=testcase_input_hash,
            )
            output_bytes = cache_files.get("program.out", b"")
            output_hash = self._domjudge_sha256_bytes(output_bytes)
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
        self._db_execute(
            """
            UPDATE judgehost_domjudge_cases
            SET status='reported', lease_owner=?, runresult=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?,
                output_run_rel=?, output_error_rel=?, output_system_rel=?, output_diff_rel=?, metadata_rel=?, compare_metadata_rel=?, team_message_rel=?, score_text=?, updated_at=?
            WHERE id=?
            """,
            [
                safe_host,
                runresult,
                runtime_sec,
                cpu_sec,
                wall_sec,
                memory_kb,
                output_run_token,
                output_err_token,
                output_sys_token,
                output_diff_token,
                metadata_token,
                compare_meta_token,
                team_message_token,
                score_text,
                now_text,
                case_id,
            ],
        )
        logger.warning(
            "domjudge add_judging_run host=%s job_id=%s case_id=%s runresult=%s",
            safe_host,
            job_id,
            case_id,
            runresult,
        )
        self._record_host_judging(safe_host, label=f"j{job_id}", updated_at=now_text)

        self._domjudge_finalize_if_ready(job_id)
        return 1

    def domjudge_internal_error(
        self,
        *,
        description: str,
        judgetask_id: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> int:
        safe_desc = self._domjudge_text(description, default="judgehost internal error")
        if judgetask_id is None:
            return 0
        case_id = int(judgetask_id)
        row = self._db_fetch_one(
            """
            SELECT c.job_id,c.debug_text AS case_debug_text,j.debug_text AS job_debug_text
            FROM judgehost_domjudge_cases c
            JOIN judgehost_domjudge_jobs j ON j.job_id=c.job_id
            WHERE c.id=?
            """,
            [case_id],
        )
        if row is not None:
            job_id = int(row["job_id"])
            case_debug = self._domjudge_text(row["case_debug_text"])
            job_debug = self._domjudge_text(row["job_debug_text"])
            debug_text = case_debug
            if job_debug and job_debug not in debug_text:
                debug_text = job_debug if not debug_text else f"{debug_text}\n{job_debug}"
            result_id = case_id
        else:
            job_row = self._db_fetch_one("SELECT job_id,debug_text FROM judgehost_domjudge_jobs WHERE job_id=?", [case_id])
            if job_row is None:
                return 0
            job_id = int(job_row["job_id"])
            debug_text = self._domjudge_text(job_row["debug_text"])
            result_id = job_id
        payload_text = self._domjudge_debug_payload_text(payload if isinstance(payload, dict) else {})
        if payload_text:
            debug_text = payload_text if not debug_text else f"{debug_text}\n{payload_text}"
            if len(debug_text) > 4000:
                debug_text = debug_text[-4000:]
        if debug_text:
            lowered = safe_desc.lower()
            if debug_text.lower() not in lowered:
                safe_desc = f"{safe_desc}\n\n{debug_text}"
        self._domjudge_finalize_if_ready(job_id, force_failed=True, error_text=safe_desc)
        return result_id

    def _domjudge_debug_payload_text(self, payload: dict[str, object]) -> str:
        payload_obj = payload if isinstance(payload, dict) else {}
        if not payload_obj:
            return ""

        def _decode_maybe_b64(text: str) -> str:
            raw = self._domjudge_text(text)
            if not raw:
                return ""
            compact = "".join(raw.split())
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
            return raw

        lines: list[str] = []
        seen: set[str] = set()

        def _append_text(text: str) -> None:
            decoded = _decode_maybe_b64(text)
            if not decoded:
                return
            for raw_line in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                line = self._domjudge_text(raw_line)
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
            raw_lines = [self._domjudge_text(item) for item in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
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
            out: list[str] = []

            def _walk(node: object) -> None:
                if len(out) >= 32:
                    return
                if isinstance(node, dict):
                    for key, child in node.items():
                        safe_key = self._domjudge_lower_text(key)
                        if safe_key in {
                            "judgetaskid",
                            "judgetask_id",
                            "judgingid",
                            "judging_id",
                            "runid",
                            "run_id",
                            "hostname",
                            "host",
                        }:
                            continue
                        _walk(child)
                    return
                if isinstance(node, (list, tuple, set)):
                    for child in node:
                        _walk(child)
                    return
                text = self._domjudge_text(node)
                if not text:
                    return
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

            _walk(value)
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
            if key not in payload_obj:
                continue
            if key == "judgehostlog":
                _append_judgehost_log(str(payload_obj.get(key) or ""))
            else:
                _append_text(str(payload_obj.get(key) or ""))
            if len(lines) >= 16:
                break
        if len(lines) < 16:
            for text in _walk_scalars(payload_obj):
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
        case_row = self._db_fetch_one(
            "SELECT id,job_id,task_id,run_id FROM judgehost_domjudge_cases WHERE id=?",
            [case_id],
        )
        job_row = self._db_fetch_one(
            "SELECT job_id,task_id,run_id FROM judgehost_domjudge_jobs WHERE job_id=?",
            [case_id],
        )
        safe_task_id = ""
        safe_run_id = ""
        target_case_id: int | None = None
        target_job_id: int | None = None
        if case_row is not None:
            safe_task_id = self._domjudge_text(case_row["task_id"])
            safe_run_id = self._domjudge_text(case_row["run_id"])
            target_case_id = int(case_row["id"])
            target_job_id = int(case_row["job_id"])
        elif job_row is not None:
            safe_task_id = self._domjudge_text(job_row["task_id"])
            safe_run_id = self._domjudge_text(job_row["run_id"])
            target_job_id = int(job_row["job_id"])
        debug_payload = payload if isinstance(payload, dict) else {}
        if debug_payload:
            logger.debug(
                "domjudge debug info host=%s judgetask_id=%s payload_keys=%s",
                safe_host,
                case_id,
                sorted(str(key) for key in debug_payload.keys()),
            )
        with self._domdb_conn() as conn:
            debug_text = self._domjudge_debug_payload_text(debug_payload)
            if debug_text:
                now_text = now_iso()
                if target_case_id is not None:
                    current_row = conn.execute(
                        "SELECT debug_text FROM judgehost_domjudge_cases WHERE id=?",
                        [target_case_id],
                    ).fetchone()
                    current_text = self._domjudge_text(current_row["debug_text"]) if current_row is not None else ""
                    merged_text = debug_text if not current_text else f"{current_text}\n{debug_text}"
                    if len(merged_text) > 4000:
                        merged_text = merged_text[-4000:]
                    conn.execute(
                        "UPDATE judgehost_domjudge_cases SET debug_text=?, updated_at=? WHERE id=?",
                        [merged_text, now_text, target_case_id],
                    )
                if target_job_id is not None:
                    current_row = conn.execute(
                        "SELECT debug_text FROM judgehost_domjudge_jobs WHERE job_id=?",
                        [target_job_id],
                    ).fetchone()
                    current_text = self._domjudge_text(current_row["debug_text"]) if current_row is not None else ""
                    merged_text = debug_text if not current_text else f"{current_text}\n{debug_text}"
                    if len(merged_text) > 4000:
                        merged_text = merged_text[-4000:]
                    conn.execute(
                        "UPDATE judgehost_domjudge_jobs SET debug_text=?, updated_at=? WHERE job_id=?",
                        [merged_text, now_text, target_job_id],
                    )
            self._record_host_event_conn(
                conn,
                hostname=safe_host,
                action="debug",
                task_id=safe_task_id,
                run_id=safe_run_id,
            )

