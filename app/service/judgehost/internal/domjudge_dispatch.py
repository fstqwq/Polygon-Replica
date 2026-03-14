from __future__ import annotations

from .shared import (
    Path,
    RUN_TEST_NAME_RE,
    domjudge_active_job_for_host,
    domjudge_cases_for_job,
    domjudge_executable_hash,
    domjudge_shared_pending_job,
    json,
    logger,
    now_iso,
    re,
    sqlite3,
    time,
)

class JudgehostDomjudgeDispatchMixin:
    def domjudge_register_host(self, hostname: str) -> list[dict[str, object]]:
        safe_host = self._normalize_hostname(hostname)
        now_text = now_iso()
        with self._domdb_conn() as conn:
            unfinished: list[dict[str, object]] = []
            self._requeue_expired_leases(conn, force=True)
            self._record_host_event_conn(conn, hostname=safe_host, action="register")
            affected = conn.execute(
                """
                SELECT job_id,submit_id
                FROM judgehost_domjudge_jobs
                WHERE lease_owner=? AND status IN ('leased','queued')
                ORDER BY job_id ASC
                """,
                [safe_host],
            ).fetchall()
            remap_submit_ids: list[tuple[int, str]] = []
            remap_seed = int(time.time() * 1000)
            remap_step = 0
            for row in affected:
                job_id = int(row["job_id"])
                # Re-registration after transient disconnect can make judgedaemon retry
                # unfinished runs in an existing working directory. Allocate a fresh
                # numeric submitid to force a clean judgedaemon working path.
                remap_step += 1
                new_submitid = str(remap_seed + remap_step)
                remap_submit_ids.append((job_id, new_submitid))
                unfinished.append({"jobid": job_id, "submitid": new_submitid})
            for job_id, new_submitid in remap_submit_ids:
                conn.execute(
                    "UPDATE judgehost_domjudge_jobs SET submit_id=? WHERE job_id=?",
                    [new_submitid, job_id],
                )
            if unfinished:
                logger.warning(
                    "domjudge register_host host=%s unfinished_jobs=%s",
                    safe_host,
                    unfinished,
                )
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=NULL, status='queued', updated_at=?
                WHERE lease_owner=? AND status IN ('leased','queued')
                """,
                [now_text, safe_host],
            )
            conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='pending', lease_owner=NULL, updated_at=?
                WHERE lease_owner=? AND status='leased'
                """,
                [now_text, safe_host],
            )
            with self._state_lock:
                for task in self._tasks_by_id.values():
                    if self._domjudge_text(task.get("lease_owner")) != safe_host:
                        continue
                    if self._domjudge_lower_text(task.get("status")) != self.STATUS_LEASED:
                        continue
                    task["status"] = self.STATUS_QUEUED
                    task["lease_owner"] = ""
                    task["lease_expires_at"] = ""
                    task["updated_at"] = now_text
            return unfinished

    def _domjudge_active_job_for_host(self, hostname: str) -> sqlite3.Row | None:
        return domjudge_active_job_for_host(hostname, fetch_all=self._db_fetch_all)

    def _domjudge_shared_pending_job(self, hostname: str) -> sqlite3.Row | None:
        return domjudge_shared_pending_job(hostname, fetch_all=self._db_fetch_all)

    def _domjudge_cases_for_job(self, job_id: int, status: str | None = None) -> list[sqlite3.Row]:
        return domjudge_cases_for_job(job_id, status=status, fetch_all=self._db_fetch_all)

    def _domjudge_prepare_job(self, hostname: str, task: dict[str, object]) -> int:
        task_id = self._domjudge_text(task.get("task_id"))
        if not task_id:
            raise RuntimeError("missing task_id for DOMjudge compatibility")
        run_id = self._domjudge_text(task.get("run_id"))
        payload = task.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("judgehost task payload is missing")
        existing = self._db_fetch_one("SELECT job_id FROM judgehost_domjudge_jobs WHERE task_id=?", [task_id])
        if existing is not None:
            job_id = int(existing["job_id"])
            self._db_execute(
                "UPDATE judgehost_domjudge_jobs SET lease_owner=?, status='leased', updated_at=? WHERE job_id=?",
                [hostname, now_iso(), job_id],
            )
            return job_id

        source_name = self._domjudge_path_name(payload.get("source_name"), default="submission.cpp")
        source_bytes = self._domjudge_b64_decode(payload.get("source_b64"))
        if not source_bytes:
            raise RuntimeError("submission source payload is empty")
        extra_sources_raw = payload.get("extra_sources_b64")
        extra_sources_obj = extra_sources_raw if isinstance(extra_sources_raw, dict) else {}
        extra_source_items: list[tuple[str, bytes]] = []
        for raw_name, raw_blob in sorted(extra_sources_obj.items(), key=lambda item: str(item[0] or "")):
            safe_name = self._domjudge_path_name(raw_name)
            if (not safe_name) or safe_name == source_name:
                continue
            blob = self._domjudge_b64_decode(raw_blob)
            if not blob:
                continue
            extra_source_items.append((safe_name, blob))
        verification_payload = payload.get("verification_payload")
        if not isinstance(verification_payload, dict):
            raise RuntimeError("verification payload is required for DOMjudge compatibility")
        manual_validate_only = self._domjudge_bool(payload.get("manual_validate_only"), default=False)
        compile_only, generate_mode, solve_mode = self._domjudge_execution_modes(payload)
        tests_payload = verification_payload.get("tests")
        tests_rows = [row for row in (tests_payload if isinstance(tests_payload, list) else []) if isinstance(row, dict)]
        if compile_only:
            # compile_only is a virtual compile task and must not fan out over real tests.
            tests_rows = [
                {
                    "name": "compile-only.in",
                    "input_b64": "",
                    "answer_name": "compile-only.ans",
                    "answer_b64": "",
                }
            ]
        if not tests_rows:
            raise RuntimeError("no tests in judgehost payload")
        run_cfg_obj: dict[str, object] = {}
        run_cfg_raw = self._domjudge_text(verification_payload.get("run_config_json"))
        if run_cfg_raw:
            try:
                parsed = json.loads(run_cfg_raw)
                if isinstance(parsed, dict):
                    run_cfg_obj = parsed
            except Exception:
                run_cfg_obj = {}
        problem_limits_obj = verification_payload.get("problem_limits")
        if not isinstance(problem_limits_obj, dict):
            problem_limits_obj = {}
        checker_args_raw = run_cfg_obj.get("checker_args")
        checker_args: list[str] = []
        if isinstance(checker_args_raw, list):
            for item in checker_args_raw:
                token = self._domjudge_text(item)
                if token:
                    checker_args.append(token)
        mode = self._domjudge_lower_text(payload.get("mode"), default="pass-fail")
        configured_max_passes = max(
            1,
            self._domjudge_parse_int(
                run_cfg_obj.get("max_passes"),
                self._domjudge_parse_int(problem_limits_obj.get("max_passes"), 16),
            ),
        )
        max_passes = configured_max_passes if mode == "multi-pass" else 1
        verification_source = self._domjudge_lower_text(payload.get("verification_source"))
        expected_behavior = self._domjudge_lower_text(payload.get("expected_behavior"))
        force_recompile = self._domjudge_bool(payload.get("force_recompile"), default=False)
        contest_id = self._domjudge_contest_id(payload.get("problem"))
        submit_id = self._domjudge_submit_id_from_run_id(run_id)
        language_id, language_exts = self._domjudge_language_extensions(source_name)
        source_hash = self._domjudge_source_hash(source_name, source_bytes)
        if extra_source_items:
            hash_blobs: list[bytes] = [f"{source_name}\0".encode("utf-8") + source_bytes]
            hash_blobs.extend(f"{name}\0".encode("utf-8") + blob for name, blob in extra_source_items)
            source_hash = self._domjudge_set_hash_from_blobs(hash_blobs)

        compile_timeout = max(1, int(getattr(self._constants, "TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120) or 120))
        compile_mem_mb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048))
        compile_output_kb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536))
        run_output_kb = max(64, int(getattr(self._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536))
        run_process_limit = max(1, int(getattr(self._constants, "RUN_EXEC_PROCESS_LIMIT", 64) or 64))
        default_cfg = getattr(self._constants, "GENERAL_CONFIG_DEFAULTS", {}) or {}
        run_tl_ms = self._domjudge_parse_int(
            run_cfg_obj.get("time_limit_ms"),
            self._domjudge_parse_int(
                problem_limits_obj.get("time_limit_ms"),
                self._domjudge_parse_int(default_cfg.get("time_limit_ms", 2000), 2000),
            ),
        )
        run_mem_mb = self._domjudge_parse_int(
            run_cfg_obj.get("memory_limit_mb"),
            self._domjudge_parse_int(
                problem_limits_obj.get("memory_limit_mb"),
                self._domjudge_parse_int(default_cfg.get("memory_limit_mb", 1024), 1024),
            ),
        )
        run_tl_ms = max(100, run_tl_ms)
        run_mem_mb = max(16, run_mem_mb)
        run_tl_sec = max(0.1, float(run_tl_ms) / 1000.0)
        # DOMjudge already applies global timelimit_overshoot to derive
        # hard CPU time. Keep per-task overshoot at zero so all modes use
        # the same max(TL * 2, TL + 1s) policy.
        run_overshoot_sec = 0.0
        run_mem_kb = max(16 * 1024, int(run_mem_mb * 1024))

        binaries_b64 = verification_payload.get("binaries_b64")
        binaries_obj = binaries_b64 if isinstance(binaries_b64, dict) else {}
        checker_bytes = self._domjudge_b64_decode(binaries_obj.get("checker"))
        validator_bytes = self._domjudge_b64_decode(binaries_obj.get("validator"))
        interactor_bytes = self._domjudge_b64_decode(binaries_obj.get("interactor"))
        sources_b64 = verification_payload.get("sources_b64")
        sources_obj = sources_b64 if isinstance(sources_b64, dict) else {}
        checker_source_bytes = self._domjudge_b64_decode(sources_obj.get("checker.cpp"))
        validator_source_bytes = self._domjudge_b64_decode(sources_obj.get("validator.cpp"))
        interactor_source_bytes = self._domjudge_b64_decode(sources_obj.get("interactor.cpp"))
        testlib_header_bytes = self._domjudge_b64_decode(sources_obj.get("testlib.h"))
        if checker_source_bytes:
            checker_source_bytes = self._domjudge_force_cpp_define(checker_source_bytes)
        if validator_source_bytes:
            validator_source_bytes = self._domjudge_force_cpp_define(validator_source_bytes)
        if interactor_source_bytes:
            interactor_source_bytes = self._domjudge_force_cpp_define(interactor_source_bytes)
        # Prefer source payloads over host-built binaries to avoid libc/libstdc++ ABI
        # mismatch between producer and judgehost runtime.
        if checker_source_bytes:
            checker_bytes = b""
        if validator_source_bytes:
            validator_bytes = b""
        if interactor_source_bytes:
            interactor_bytes = b""
        has_interactor_payload = bool(interactor_bytes or interactor_source_bytes)
        interactive = (
            (not compile_only)
            and (not generate_mode)
            and (mode == "interactive" or (mode == "multi-pass" and has_interactor_payload))
        )
        if (not compile_only) and (not generate_mode) and mode == "interactive" and not has_interactor_payload:
            raise RuntimeError("interactive mode requires interactor payload")

        compile_files: list[tuple[str, bytes, bool]] = [
            (
                "run",
                self._domjudge_compile_script(
                    source_name,
                    manual_validate_only=manual_validate_only,
                    compile_only=compile_only,
                ),
                True,
            )
        ]
        run_files: list[tuple[str, bytes, bool]] = []
        compare_files: list[tuple[str, bytes, bool]] = []
        if interactive:
            # DOMjudge combined run/compare wraps the provided run executable
            # itself (renames run->runjury and writes run-interactive.sh).
            # Therefore we must provide jury program as "run" here.
            if interactor_bytes:
                run_files.append(("run", interactor_bytes, True))
            elif interactor_source_bytes:
                run_files.append(
                    ("build", self._domjudge_cpp_executable_build_script("interactor.cpp", role="interactor"), True)
                )
                run_files.append(("interactor.cpp", interactor_source_bytes, False))
                if testlib_header_bytes:
                    run_files.append(("testlib.h", testlib_header_bytes, False))
            else:
                raise RuntimeError("interactive mode requires interactor payload")
            # combined_run_compare=true means compare executable is not fetched.
            compare_files.append(("run", self._domjudge_compare_script(solve_mode=solve_mode), True))
        else:
            run_files.append(
                (
                    "run",
                    self._domjudge_run_script(
                        False,
                        solve_mode=solve_mode,
                        compile_only=compile_only,
                        generate_mode=generate_mode,
                        manual_validate_only=manual_validate_only,
                    ),
                    True,
                )
            )
            if compile_only or solve_mode:
                # compile_only/verification.solve-main must accept without checker/answer semantics.
                compare_files.append(("run", self._domjudge_compare_script(solve_mode=True), True))
            elif generate_mode:
                compare_files.append(("run", self._domjudge_compare_script(generate_mode=True), True))
                if validator_source_bytes:
                    compare_files.append(("validator.cpp", validator_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))
                elif validator_bytes:
                    compare_files.append(("validator", validator_bytes, True))
            else:
                compare_files.append(("run", self._domjudge_compare_script(solve_mode=False), True))
                if checker_source_bytes:
                    compare_files.append(("checker.cpp", checker_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))
                elif checker_bytes:
                    compare_files.append(("checker", checker_bytes, True))

        precomputed_raw = payload.get("domjudge_precomputed")
        precomputed = precomputed_raw if isinstance(precomputed_raw, dict) else {}
        precompile_hash = self._domjudge_lower_text(precomputed.get("compile_hash"))
        prerun_hash = self._domjudge_lower_text(precomputed.get("run_hash"))
        precompare_hash = self._domjudge_lower_text(precomputed.get("compare_hash"))
        presource_hash = self._domjudge_lower_text(precomputed.get("source_hash"))
        precompile_config = precomputed.get("compile_config")
        prerun_config = precomputed.get("run_config")
        precompare_config = precomputed.get("compare_config")
        use_precomputed = (
            bool(re.fullmatch(r"[0-9a-f]{32}", precompile_hash))
            and bool(re.fullmatch(r"[0-9a-f]{32}", prerun_hash))
            and bool(re.fullmatch(r"[0-9a-f]{32}", precompare_hash))
            and bool(re.fullmatch(r"[0-9a-f]{64}", presource_hash))
            and isinstance(precompile_config, dict)
            and isinstance(prerun_config, dict)
            and isinstance(precompare_config, dict)
        )
        if use_precomputed:
            source_hash = presource_hash
            compile_hash = precompile_hash
            run_hash = prerun_hash
            compare_hash = precompare_hash
            compile_config = dict(precompile_config)
            run_config = dict(prerun_config)
            compare_config = dict(precompare_config)
        else:
            compile_hash = domjudge_executable_hash(compile_files)
            run_hash = domjudge_executable_hash(run_files)
            compare_hash = domjudge_executable_hash(compare_files)
            toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(
                source_name,
                manual_validate_only=manual_validate_only,
            )
            compare_script_timelimit = max(1, int(run_tl_sec))
            if checker_source_bytes or validator_source_bytes:
                # compare script may need one-time local checker rebuild when host binary
                # is ABI-incompatible with judgehost runtime; reserve enough wall time.
                compare_script_timelimit = max(compare_script_timelimit, min(compile_timeout, 120))

            compile_config = {
                "hash": compile_hash,
                "toolchain_cmd_digest": toolchain_cmd_digest,
                "filter_compiler_files": False,
                "language_extensions": list(language_exts),
                "script_timelimit": compile_timeout,
                "script_memory_limit": int(compile_mem_mb * 1024),
                "script_filesize_limit": int(compile_output_kb),
            }
            run_config = {
                "hash": run_hash,
                "time_limit": run_tl_sec,
                "overshoot": run_overshoot_sec,
                "memory_limit": run_mem_kb,
                "output_limit": int(run_output_kb),
                "process_limit": run_process_limit,
                "entry_point": None,
                "pass_limit": max_passes,
                "language_id": language_id,
            }
            compare_config = {
                "hash": compare_hash,
                "combined_run_compare": bool(interactive),
                "compare_args": " ".join(
                    [*(['--validate-input'] if manual_validate_only else []), *checker_args]
                ),
                "script_timelimit": int(compare_script_timelimit),
                "script_memory_limit": run_mem_kb,
                "script_filesize_limit": int(run_output_kb),
            }
        compile_config_hash = self._domjudge_json_hash(compile_config)
        run_config_hash = self._domjudge_json_hash(run_config)
        compare_config_hash = self._domjudge_json_hash(compare_config)

        work_key = self._domjudge_json_hash(
            {
                "schema": "v1",
                "source_hash": source_hash,
                "source_name": source_name,
                "compile_hash": compile_hash,
                "run_hash": run_hash,
                "compare_hash": compare_hash,
                "compile_config_hash": compile_config_hash,
                "run_config_hash": run_config_hash,
                "compare_config_hash": compare_config_hash,
            }
        )
        work_root = self._domjudge_work_root(f"job-{work_key[:32]}")
        source_dir = (work_root / "source").resolve()
        scripts_compile_dir = (work_root / "scripts" / "compile").resolve()
        scripts_run_dir = (work_root / "scripts" / "run").resolve()
        scripts_compare_dir = (work_root / "scripts" / "compare").resolve()
        for directory in (source_dir, scripts_compile_dir, scripts_run_dir, scripts_compare_dir):
            directory.mkdir(parents=True, exist_ok=True)
        source_path = (source_dir / source_name).resolve()
        self._domjudge_ensure_bytes_file(source_path, source_bytes, executable=False)
        for name, blob in extra_source_items:
            target = (source_dir / name).resolve()
            if target == source_path:
                continue
            self._domjudge_ensure_bytes_file(target, blob, executable=False)
        for name, content, is_exec in compile_files:
            self._domjudge_ensure_bytes_file(scripts_compile_dir / name, content, executable=is_exec)
        for name, content, is_exec in run_files:
            self._domjudge_ensure_bytes_file(scripts_run_dir / name, content, executable=is_exec)
        for name, content, is_exec in compare_files:
            self._domjudge_ensure_bytes_file(scripts_compare_dir / name, content, executable=is_exec)

        now_text = now_iso()
        with self._domdb_conn() as conn:
            conn.execute(
                """
                INSERT INTO judgehost_domjudge_jobs(
                    task_id,run_id,submit_id,contest_id,mode,source_name,source_path,work_root,
                    compile_hash,run_hash,compare_hash,source_hash,compile_config_json,run_config_json,compare_config_json,
                    expected_behavior,verification_source,force_recompile,
                    lease_owner,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    task_id,
                    run_id,
                    submit_id,
                    contest_id,
                    mode,
                    source_name,
                    str(source_path),
                    str(work_root),
                    compile_hash,
                    run_hash,
                    compare_hash,
                    source_hash,
                    json.dumps(compile_config, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(run_config, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(compare_config, ensure_ascii=False, separators=(",", ":")),
                    expected_behavior,
                    verification_source,
                    1 if force_recompile else 0,
                    hostname,
                    "leased",
                    now_text,
                    now_text,
                ],
            )
            job_row = conn.execute("SELECT job_id FROM judgehost_domjudge_jobs WHERE task_id=?", [task_id]).fetchone()
            if job_row is None:
                raise RuntimeError("failed to allocate DOMjudge compatibility job")
            job_id = int(job_row["job_id"])
            # Official judgedaemon validates submitid as integer.
            submit_id = str(job_id)
            conn.execute(
                "UPDATE judgehost_domjudge_jobs SET submit_id=? WHERE job_id=?",
                [submit_id, job_id],
            )
            ordinal = 0
            for entry in tests_rows:
                ordinal += 1
                raw_name = self._domjudge_text(entry.get("name"))
                test_name = raw_name if RUN_TEST_NAME_RE.fullmatch(raw_name) else f"{ordinal:03}.in"
                in_bytes = self._domjudge_b64_decode(entry.get("input_b64"))
                ans_bytes = self._domjudge_b64_decode(entry.get("answer_b64"))
                testcase_input_hash = self._domjudge_sha256_bytes(in_bytes)
                testcase_answer_hash = self._domjudge_sha256_bytes(ans_bytes)
                # verification.solve-main must not depend on pre-existing answers:
                # use input hash as testcase key so cache identity is
                # (main_correct/source signature + input_hash).
                testcase_hash = (
                    str(testcase_input_hash)
                    if solve_mode
                    else self._domjudge_set_hash_from_blobs([in_bytes, ans_bytes])
                )
                testcase_id, in_path_text, ans_path_text = self._domjudge_register_cached_testcase(
                    conn,
                    testcase_hash=testcase_hash,
                    in_bytes=in_bytes,
                    ans_bytes=ans_bytes,
                )
                conn.execute(
                    """
                    INSERT INTO judgehost_domjudge_cases(
                        job_id,task_id,run_id,test_name,ordinal,testcase_id,testcase_hash,testcase_input_hash,testcase_answer_hash,input_path,answer_path,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        job_id,
                        task_id,
                        run_id,
                        test_name,
                        ordinal,
                        testcase_id,
                        testcase_hash,
                        testcase_input_hash,
                        testcase_answer_hash,
                        str(in_path_text),
                        str(ans_path_text),
                        "pending",
                        now_text,
                        now_text,
                    ],
                )
            conn.commit()
        return job_id

    def _domjudge_try_cache_shortcut(
        self,
        *,
        hostname: str,
        job_row: sqlite3.Row,
        case_row: sqlite3.Row,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
    ) -> dict[str, object] | None:
        source_hash = self._domjudge_lower_text(job_row["source_hash"])
        compile_hash = self._domjudge_lower_text(job_row["compile_hash"])
        run_hash = self._domjudge_lower_text(job_row["run_hash"])
        compare_hash = self._domjudge_lower_text(job_row["compare_hash"])
        testcase_hash = self._domjudge_lower_text(case_row["testcase_hash"])
        testcase_input_hash = self._domjudge_lower_text(case_row["testcase_input_hash"])
        testcase_answer_hash = self._domjudge_lower_text(case_row["testcase_answer_hash"])
        answer_path = Path(self._domjudge_text(case_row["answer_path"])).resolve()
        input_path = Path(self._domjudge_text(case_row["input_path"])).resolve()
        if (not testcase_input_hash) and input_path.exists() and input_path.is_file():
            testcase_input_hash = self._domjudge_sha256_bytes(input_path.read_bytes())
        if (not testcase_answer_hash) and answer_path.exists() and answer_path.is_file():
            testcase_answer_hash = self._domjudge_sha256_bytes(answer_path.read_bytes())

        force_recompile = bool(int(job_row["force_recompile"] or 0))
        expected_behavior = self._domjudge_lower_text(job_row["expected_behavior"], default="unknown")
        verification_source = self._domjudge_lower_text(job_row["verification_source"])
        solve_mode = verification_source in {"verification.solve-main", "solve.main"}
        compile_only = expected_behavior == "compile"

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
        solve_key_hash, solve_signature = self._domjudge_solve_output_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_input_hash=testcase_input_hash,
        )
        if force_recompile:
            self._domjudge_cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
            self._domjudge_cache_delete(self.SOLVE_OUTPUT_CACHE_KIND, solve_key_hash, solve_signature)
            return None
        run_cfg_obj: dict[str, object] = {}
        try:
            parsed_run_cfg = json.loads(str(job_row["run_config_json"] or "{}"))
            if isinstance(parsed_run_cfg, dict):
                run_cfg_obj = parsed_run_cfg
        except Exception:
            run_cfg_obj = {}

        cached_exact = self._domjudge_cache_get(self.CASE_CACHE_KIND, case_key_hash, case_signature)
        if isinstance(cached_exact, dict):
            cached_value = cached_exact.get("value")
            cached_obj = cached_value if isinstance(cached_value, dict) else {}
            cached_runresult = self._domjudge_lower_text(cached_obj.get("runresult"))
            cached_runresult = self._domjudge_rewrite_untrusted_runresult(
                cached_runresult,
                cpu_sec=self._domjudge_parse_float(cached_obj.get("cpu_sec"), self._domjudge_parse_float(cached_obj.get("runtime_sec"), 0.0)),
                run_cfg_obj=run_cfg_obj,
            )
            cached_verdict = self._domjudge_verdict_from_runresult(cached_runresult)
            if cached_verdict == "FL":
                self._domjudge_cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            # Build answer generation, expected accepted runs, and compile-only tasks
            # must not reuse non-OK cached outcomes; otherwise transient failures can
            # poison later requests.
            if (solve_mode or expected_behavior in {"accepted", "compile"}) and cached_verdict != "OK":
                if expected_behavior == "compile":
                    self._domjudge_cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            materialized = self._domjudge_materialize_cached_case(
                cache_kind=self.CASE_CACHE_KIND,
                cache_key_hash=case_key_hash,
                cache_signature=case_signature,
                cache_value=dict(cached_obj),
                cache_files=dict(cached_exact.get("files") or {}),
            )
            output_run_rel = self._domjudge_text(materialized.get("output_run_rel"))
            if cached_verdict == "OK" and (not compile_only):
                # Cached OK result must carry a resolvable output artifact.
                if not output_run_rel:
                    self._domjudge_cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                    return None
            return {
                "lease_owner": hostname,
                "runresult": cached_runresult,
                "runtime_sec": float(materialized.get("runtime_sec") or 0.0),
                "cpu_sec": float(materialized.get("cpu_sec") or 0.0),
                "wall_sec": float(materialized.get("wall_sec") or 0.0),
                "memory_kb": int(materialized.get("memory_kb") or 0),
                "output_run_rel": output_run_rel,
                "output_error_rel": str(materialized.get("output_error_rel") or ""),
                "output_system_rel": str(materialized.get("output_system_rel") or ""),
                "output_diff_rel": str(materialized.get("output_diff_rel") or ""),
                "metadata_rel": str(materialized.get("metadata_rel") or ""),
                "compare_metadata_rel": str(materialized.get("compare_metadata_rel") or ""),
                "team_message_rel": str(materialized.get("team_message_rel") or ""),
                "score_text": str(materialized.get("score_text") or ""),
            }

        if solve_mode or expected_behavior != "accepted":
            return None
        cached_solve = self._domjudge_cache_get(self.SOLVE_OUTPUT_CACHE_KIND, solve_key_hash, solve_signature)
        if not isinstance(cached_solve, dict):
            return None
        solve_value = cached_solve.get("value")
        solve_obj = solve_value if isinstance(solve_value, dict) else {}
        output_hash = self._domjudge_lower_text(solve_obj.get("output_hash"))
        if (not output_hash) or (not testcase_answer_hash) or output_hash != testcase_answer_hash:
            return None
        materialized = self._domjudge_materialize_cached_case(
            cache_kind=self.SOLVE_OUTPUT_CACHE_KIND,
            cache_key_hash=solve_key_hash,
            cache_signature=solve_signature,
            cache_value=dict(solve_obj),
            cache_files=dict(cached_solve.get("files") or {}),
        )
        output_run_rel = self._domjudge_text(materialized.get("output_run_rel"))
        if not output_run_rel:
            self._domjudge_cache_delete(self.SOLVE_OUTPUT_CACHE_KIND, solve_key_hash, solve_signature)
            return None
        return {
            "lease_owner": hostname,
            "runresult": "correct",
            "runtime_sec": float(materialized.get("runtime_sec") or 0.0),
            "cpu_sec": float(materialized.get("cpu_sec") or 0.0),
            "wall_sec": float(materialized.get("wall_sec") or 0.0),
            "memory_kb": int(materialized.get("memory_kb") or 0),
            "output_run_rel": output_run_rel,
            "output_error_rel": str(materialized.get("output_error_rel") or ""),
            "output_system_rel": str(materialized.get("output_system_rel") or ""),
            "output_diff_rel": str(materialized.get("output_diff_rel") or ""),
            "metadata_rel": str(materialized.get("metadata_rel") or ""),
            "compare_metadata_rel": str(materialized.get("compare_metadata_rel") or ""),
            "team_message_rel": str(materialized.get("team_message_rel") or ""),
            "score_text": str(materialized.get("score_text") or ""),
        }

    def _domjudge_release_prepared_job_for_queue(self, job_id: int) -> None:
        now_text = now_iso()
        prequeue_host = self._normalize_hostname("prequeue-cache")
        with self._domdb_conn() as conn:
            leased_case_rows = conn.execute(
                """
                SELECT id,ordinal,test_name,lease_owner
                FROM judgehost_domjudge_cases
                WHERE job_id=? AND status='leased' AND lease_owner=?
                ORDER BY ordinal ASC, id ASC
                """,
                [int(job_id), prequeue_host],
            ).fetchall()
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=NULL, status='queued', updated_at=?
                WHERE job_id=? AND lease_owner=? AND status IN ('leased','queued')
                """,
                [now_text, int(job_id), prequeue_host],
            )
            conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='pending', lease_owner=NULL, updated_at=?
                WHERE job_id=? AND status='leased' AND lease_owner=?
                """,
                [now_text, int(job_id), prequeue_host],
            )
    def _domjudge_try_prequeue_cache_finalize(self, *, task_id: str, run_id: str, payload: dict[str, object]) -> None:
        safe_task_id = self._domjudge_text(task_id)
        if not safe_task_id:
            return
        task_payload = dict(payload or {})
        compile_only = self._domjudge_task_kind(task_payload) == self._TASK_KIND_COMPILE_ONLY
        verification_payload = task_payload.get("verification_payload")
        if not isinstance(verification_payload, dict):
            return
        tests_payload = verification_payload.get("tests")
        if not isinstance(tests_payload, list):
            if not compile_only:
                return
        elif not any(isinstance(row, dict) for row in tests_payload):
            if not compile_only:
                return

        prequeue_host = self._normalize_hostname("prequeue-cache")
        job_id = 0
        try:
            job_id = int(
                self._domjudge_prepare_job(
                    prequeue_host,
                    {
                        "task_id": safe_task_id,
                        "run_id": self._domjudge_text(run_id),
                        "payload": task_payload,
                    },
                )
            )
            job_row = self._db_fetch_one(
                """
                SELECT submit_id,contest_id,task_id,source_name,compile_config_json,run_config_json,compare_config_json,
                       compile_hash,run_hash,compare_hash,source_hash,expected_behavior,verification_source,force_recompile,work_root,run_id
                FROM judgehost_domjudge_jobs
                WHERE job_id=?
                """,
                [int(job_id)],
            )
            if job_row is None:
                return
            rows = self._db_fetch_all(
                """
                SELECT *
                FROM judgehost_domjudge_cases
                WHERE job_id=? AND status='pending'
                ORDER BY ordinal ASC, id ASC
                """,
                [int(job_id)],
            )
            if not rows:
                self._domjudge_finalize_if_ready(int(job_id))
                return

            compile_cfg: dict[str, object] = {}
            run_cfg: dict[str, object] = {}
            compare_cfg: dict[str, object] = {}
            try:
                parsed = json.loads(str(job_row["compile_config_json"] or "{}"))
                if isinstance(parsed, dict):
                    compile_cfg = parsed
            except Exception:
                compile_cfg = {}
            try:
                parsed = json.loads(str(job_row["run_config_json"] or "{}"))
                if isinstance(parsed, dict):
                    run_cfg = parsed
            except Exception:
                run_cfg = {}
            try:
                parsed = json.loads(str(job_row["compare_config_json"] or "{}"))
                if isinstance(parsed, dict):
                    compare_cfg = parsed
            except Exception:
                compare_cfg = {}
            compile_config_hash = self._domjudge_json_hash(compile_cfg)
            run_config_hash = self._domjudge_json_hash(run_cfg)
            compare_config_hash = self._domjudge_json_hash(compare_cfg)
            toolchain_cmd_digest = self._domjudge_lower_text(compile_cfg.get("toolchain_cmd_digest"))
            if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
                toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(str(job_row["source_name"] or ""))

            now_text = now_iso()
            cached_rows: list[tuple[sqlite3.Row, dict[str, object]]] = []
            pending_rows = 0
            for row in rows:
                shortcut = self._domjudge_try_cache_shortcut(
                    hostname=prequeue_host,
                    job_row=job_row,
                    case_row=row,
                    compile_config_hash=compile_config_hash,
                    run_config_hash=run_config_hash,
                    compare_config_hash=compare_config_hash,
                    toolchain_cmd_digest=toolchain_cmd_digest,
                )
                if isinstance(shortcut, dict):
                    cached_rows.append((row, dict(shortcut)))
                else:
                    pending_rows += 1

            if cached_rows:
                with self._domdb_conn() as conn:
                    for row, cached in cached_rows:
                        case_id = int(row["id"])
                        conn.execute(
                            """
                            UPDATE judgehost_domjudge_cases
                            SET status='reported', lease_owner=?, runresult=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?,
                                output_run_rel=?, output_error_rel=?, output_system_rel=?, output_diff_rel=?, metadata_rel=?, compare_metadata_rel=?, team_message_rel=?, score_text=?, updated_at=?
                            WHERE id=? AND status='pending'
                            """,
                            [
                                str(cached.get("lease_owner") or prequeue_host),
                                str(cached.get("runresult") or ""),
                                cached.get("runtime_sec"),
                                cached.get("cpu_sec"),
                                cached.get("wall_sec"),
                                cached.get("memory_kb"),
                                str(cached.get("output_run_rel") or ""),
                                str(cached.get("output_error_rel") or ""),
                                str(cached.get("output_system_rel") or ""),
                                str(cached.get("output_diff_rel") or ""),
                                str(cached.get("metadata_rel") or ""),
                                str(cached.get("compare_metadata_rel") or ""),
                                str(cached.get("team_message_rel") or ""),
                                str(cached.get("score_text") or ""),
                                now_text,
                                int(case_id),
                            ],
                        )
            if pending_rows > 0:
                self._domjudge_release_prepared_job_for_queue(int(job_id))
                return

            with self._state_lock:
                row = self._tasks_by_id.get(safe_task_id)
                if row is not None and self._domjudge_lower_text(row.get("status")) == self.STATUS_ENQUEUING:
                    row["status"] = self.STATUS_QUEUED
                    row["updated_at"] = now_iso()
            self._domjudge_finalize_if_ready(int(job_id))
        except Exception as exc:
            if job_id > 0:
                try:
                    self._domjudge_release_prepared_job_for_queue(int(job_id))
                except Exception:
                    pass
            logger.warning("prequeue cache consumption failed task_id=%s: %s", safe_task_id, exc)

    def _domjudge_lease_cases(self, job_id: int, hostname: str, max_batchsize: int) -> list[dict[str, object]]:
        cap = max(1, min(256, int(max_batchsize)))
        now_text = now_iso()
        job_row = self._db_fetch_one(
            """
            SELECT submit_id,contest_id,task_id,compile_config_json,run_config_json,compare_config_json,compile_hash,run_hash,compare_hash
            FROM judgehost_domjudge_jobs
            WHERE job_id=?
            """,
            [int(job_id)],
        )
        if job_row is None:
            return []
        rows = self._db_fetch_all(
            """
            SELECT *
            FROM judgehost_domjudge_cases
            WHERE job_id=? AND status='pending'
            ORDER BY ordinal ASC, id ASC
            LIMIT ?
            """,
            [int(job_id), int(cap)],
        )
        if not rows:
            return []
        compile_provider_job_id = self._domjudge_script_provider_job_id(
            kind="compile",
            script_hash=str(job_row["compile_hash"] or ""),
            default_job_id=int(job_id),
        )
        run_provider_job_id = self._domjudge_script_provider_job_id(
            kind="run",
            script_hash=str(job_row["run_hash"] or ""),
            default_job_id=int(job_id),
        )
        compare_provider_job_id = self._domjudge_script_provider_job_id(
            kind="compare",
            script_hash=str(job_row["compare_hash"] or ""),
            default_job_id=int(job_id),
        )
        compile_id = int(self._domjudge_script_ids(compile_provider_job_id)[0])
        run_id_num = int(self._domjudge_script_ids(run_provider_job_id)[1])
        compare_id = int(self._domjudge_script_ids(compare_provider_job_id)[2])
        raw_submit_id = self._domjudge_text(job_row["submit_id"])
        safe_submit_id = str(int(raw_submit_id))
        safe_task_id = str(job_row["task_id"] or "")
        out: list[dict[str, object]] = []
        with self._domdb_conn() as conn:
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=?, status='leased', updated_at=?
                WHERE job_id=?
                """,
                [hostname, now_text, int(job_id)],
            )
            for row in rows:
                case_id = int(row["id"])
                # DOMjudge working directories are keyed by testcase id. Reusing a
                # shared testcase id across multiple case rows in one submit can make
                # judgedaemon treat distinct cases as the same work item. Always
                # expose the case id as testcase id at API boundary.
                testcase_id = case_id
                updated = conn.execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='leased', lease_owner=?, updated_at=?
                    WHERE id=? AND status='pending'
                    """,
                    [hostname, now_text, case_id],
                )
                if int(updated.rowcount or 0) <= 0:
                    continue
                out.append(
                    {
                        "type": "judging_run",
                        "judgetaskid": case_id,
                        "jobid": int(job_id),
                        "uuid": safe_task_id,
                        "submitid": safe_submit_id,
                        "contestid": str(job_row["contest_id"] or "local"),
                        "compile_script_id": str(int(compile_id)),
                        "run_script_id": str(int(run_id_num)),
                        "compare_script_id": str(int(compare_id)),
                        "testcase_id": str(int(testcase_id)),
                        "testcase_hash": str(row["testcase_hash"] or ""),
                        "compile_config": str(job_row["compile_config_json"] or "{}"),
                        "run_config": str(job_row["run_config_json"] or "{}"),
                        "compare_config": str(job_row["compare_config_json"] or "{}"),
                    }
                )
        self.renew_lease(safe_task_id, hostname)
        return out

    def domjudge_fetch_work(self, hostname: str, max_batchsize: int | None = None) -> list[dict[str, object]]:
        safe_host = self._normalize_hostname(hostname)
        if not self._host_enabled_conn(hostname=safe_host):
            self._record_host_event_conn(hostname=safe_host, action="disabled")
            return []
        cap = self._fetch_batch_size if max_batchsize is None else max(1, min(256, int(max_batchsize)))
        max_attempts = max(1, min(32, cap * 4))

        for _ in range(max_attempts):
            active = self._domjudge_active_job_for_host(safe_host)
            if active is not None:
                active_job_id = int(active["job_id"])
                leased_cases = self._domjudge_lease_cases(active_job_id, safe_host, cap)
                if leased_cases:
                    return leased_cases
                # No pending cases for the active job; attempt finalization and retry.
                self._domjudge_finalize_if_ready(active_job_id)
                refreshed = self._domjudge_active_job_for_host(safe_host)
                if refreshed is not None and int(refreshed["job_id"]) == active_job_id:
                    return []
                continue

            leased = self.fetch_work(safe_host, limit=1)
            if not leased:
                shared_job = self._domjudge_shared_pending_job(safe_host)
                if shared_job is not None:
                    shared_job_id = int(shared_job["job_id"])
                    leased_cases = self._domjudge_lease_cases(shared_job_id, safe_host, cap)
                    if leased_cases:
                        return leased_cases
                    self._domjudge_finalize_if_ready(shared_job_id)
                return []
            leased_task = leased[0] if isinstance(leased[0], dict) else {}
            task_id = self._domjudge_text(leased_task.get("task_id"))
            try:
                active_job_id = self._domjudge_prepare_job(safe_host, leased_task)
            except Exception as exc:
                error_text = str(exc).strip() or "invalid judgehost task payload"
                logger.warning("invalid judgehost task dropped task_id=%s host=%s: %s", task_id, safe_host, error_text)
                if task_id:
                    try:
                        self.report_result(
                            task_id=task_id,
                            hostname=safe_host,
                            payload={
                                "run_status": "failed",
                                "error": error_text,
                                "summary": {"error": error_text},
                            },
                        )
                    except Exception as report_exc:
                        logger.warning("failed to mark invalid judgehost task as failed task_id=%s: %s", task_id, report_exc)
                continue

            leased_cases = self._domjudge_lease_cases(active_job_id, safe_host, cap)
            if leased_cases:
                return leased_cases
            self._domjudge_finalize_if_ready(active_job_id)

        return []

