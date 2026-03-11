from __future__ import annotations

from .shared import (
    Path,
    RUN_TEST_NAME_RE,
    _INVOCATION_ID_RE,
    _RUN_ID_RE,
    base64,
    domjudge_executable_hash,
    json,
    now_iso,
    re,
    time,
    uuid,
)
from app.service.run.summary import summary_for_db


class JudgehostEnqueueMixin:
    def _collect_build_payload(
        self,
        *,
        problem: str,
        build_id: str,
        workspace: Path,
        mode: str,
        selected_tests: list[str],
    ) -> dict[str, object]:
        if not self._include_build_payload:
            return {}
        build_row = self._db_fetch_one("SELECT build_ref FROM builds WHERE id=?", [str(build_id or "").strip()])
        build_ref = str(build_row["build_ref"] or "").strip().lower() if build_row is not None else ""
        if not build_ref:
            return {}
        artifact_root = self._run_service.fs_manager.build_paths(build_ref).root.resolve()
        if not artifact_root.exists() or (not artifact_root.is_dir()):
            return {}
        tests_dir = (artifact_root / "tests").resolve()
        ans_dir = (artifact_root / "ans").resolve()
        logs_dir = (artifact_root / "logs").resolve()
        bin_dir = (artifact_root / "bin").resolve()

        wanted_tests: list[str] = []
        if selected_tests:
            for raw in selected_tests:
                token = Path(str(raw or "").strip()).name
                if not RUN_TEST_NAME_RE.fullmatch(token):
                    continue
                if token in wanted_tests:
                    continue
                wanted_tests.append(token)
        else:
            if tests_dir.exists():
                for p in sorted(tests_dir.glob("*.in")):
                    token = p.name
                    if not RUN_TEST_NAME_RE.fullmatch(token):
                        continue
                    wanted_tests.append(token)
                    if len(wanted_tests) >= self._max_tests_per_task:
                        break

        tests_payload: list[dict[str, object]] = []
        for test_name in wanted_tests:
            test_file = (tests_dir / test_name).resolve()
            if not test_file.exists() or (not test_file.is_file()):
                continue
            test_bytes = self._safe_read_bytes(
                test_file,
                max_bytes=self._max_test_payload_bytes,
                label="test payload",
            )
            ans_name = f"{Path(test_name).stem}.ans"
            ans_file = (ans_dir / ans_name).resolve()
            ans_bytes = b""
            if ans_file.exists() and ans_file.is_file():
                ans_bytes = self._safe_read_bytes(
                    ans_file,
                    max_bytes=self._max_test_payload_bytes,
                    label="answer payload",
                )
            tests_payload.append(
                {
                    "name": test_name,
                    "input_b64": base64.b64encode(test_bytes).decode("ascii"),
                    "answer_name": ans_name,
                    "answer_b64": base64.b64encode(ans_bytes).decode("ascii"),
                }
            )

        run_config_text = ""
        run_cfg_obj: dict[str, object] = {}
        run_cfg_path = (logs_dir / "run_config.json").resolve()
        if run_cfg_path.exists() and run_cfg_path.is_file():
            run_cfg_bytes = self._safe_read_bytes(
                run_cfg_path,
                max_bytes=self._max_test_payload_bytes,
                label="run config payload",
            )
            run_config_text = run_cfg_bytes.decode("utf-8", errors="replace")
            try:
                parsed_cfg = json.loads(run_config_text)
                if isinstance(parsed_cfg, dict):
                    run_cfg_obj = parsed_cfg
            except Exception:
                run_cfg_obj = {}

        binaries: dict[str, str] = {}
        for name in ("checker", "validator", "interactor"):
            p = (bin_dir / name).resolve()
            if not p.exists() or (not p.is_file()):
                continue
            blob = self._safe_read_bytes(
                p,
                max_bytes=self._max_binary_payload_bytes,
                label=f"{name} payload",
            )
            binaries[name] = base64.b64encode(blob).decode("ascii")

        workspace_resolved = workspace.resolve()

        def _safe_workspace_rel_file(rel_path: str) -> Path | None:
            token = str(rel_path or "").strip().replace("\\", "/")
            if not token:
                return None
            candidate = (workspace_resolved / token).resolve()
            if candidate == workspace_resolved or workspace_resolved not in candidate.parents:
                return None
            if candidate.is_symlink() or (not candidate.exists()) or (not candidate.is_file()):
                return None
            return candidate

        def _first_cpp_under(rel_dir: str) -> Path | None:
            base = (workspace_resolved / rel_dir).resolve()
            if base == workspace_resolved or workspace_resolved not in base.parents:
                return None
            if (not base.exists()) or (not base.is_dir()):
                return None
            for path in sorted(base.glob("*.cpp")):
                resolved = path.resolve()
                if resolved.is_symlink() or (not resolved.is_file()):
                    continue
                return resolved
            return None

        build_cfg_obj: dict[str, object] = {}
        build_cfg_path = _safe_workspace_rel_file("config/build.json")
        if build_cfg_path is not None:
            try:
                parsed_build_cfg = json.loads(build_cfg_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(parsed_build_cfg, dict):
                    build_cfg_obj = parsed_build_cfg
            except Exception:
                build_cfg_obj = {}
        problem_cfg_obj: dict[str, object] = {}
        problem_cfg_path = _safe_workspace_rel_file("config/problem.json")
        if problem_cfg_path is not None:
            try:
                parsed_problem_cfg = json.loads(problem_cfg_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(parsed_problem_cfg, dict):
                    problem_cfg_obj = parsed_problem_cfg
            except Exception:
                problem_cfg_obj = {}
        try:
            problem_time_limit_ms = int(problem_cfg_obj.get("time_limit_ms", 0))
        except Exception:
            problem_time_limit_ms = 0
        try:
            problem_memory_limit_mb = int(problem_cfg_obj.get("memory_limit_mb", 0))
        except Exception:
            problem_memory_limit_mb = 0
        if problem_time_limit_ms < 0:
            problem_time_limit_ms = 0
        if problem_memory_limit_mb < 0:
            problem_memory_limit_mb = 0

        checker_source: Path | None = None
        checker_standard = str(run_cfg_obj.get("checker_standard") or build_cfg_obj.get("checker_standard") or "").strip()
        repo_root = Path(__file__).resolve().parents[4]
        if checker_standard:
            token = checker_standard[5:] if checker_standard.startswith("std::") else checker_standard
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", token):
                std_root = (repo_root / "third_party" / "upstream" / "testlib" / "checkers").resolve()
                source = (std_root / token).resolve()
                if source.exists() and source.is_file():
                    checker_source = source
        if checker_source is None:
            checker_source = _safe_workspace_rel_file(str(build_cfg_obj.get("checker_source") or ""))
        if checker_source is None:
            checker_source = _safe_workspace_rel_file("checkers/checker.cpp")
        if checker_source is None:
            checker_source = _first_cpp_under("checkers")

        validator_source: Path | None = _safe_workspace_rel_file(str(build_cfg_obj.get("validator_source") or ""))
        if validator_source is None:
            validator_source = _safe_workspace_rel_file("validators/validator.cpp")
        if validator_source is None:
            validator_source = _first_cpp_under("validators")

        interactive_mode = str(mode or "").strip().lower() in {"interactive", "multi-pass"}
        interactor_source: Path | None = None
        if interactive_mode:
            interactor_source = _safe_workspace_rel_file(str(build_cfg_obj.get("interactor_source") or ""))
            if interactor_source is None:
                interactor_source = _safe_workspace_rel_file("interactors/interactor.cpp")
            if interactor_source is None:
                interactor_source = _first_cpp_under("interactors")

        source_files: dict[str, Path] = {}
        if checker_source is not None:
            source_files["checker.cpp"] = checker_source
        if validator_source is not None:
            source_files["validator.cpp"] = validator_source
        if interactor_source is not None:
            source_files["interactor.cpp"] = interactor_source
        if source_files:
            testlib_source = _safe_workspace_rel_file("third_party/testlib/testlib.h")
            if testlib_source is None:
                upstream_testlib = (repo_root / "third_party" / "upstream" / "testlib" / "testlib.h").resolve()
                if upstream_testlib.exists() and upstream_testlib.is_file():
                    testlib_source = upstream_testlib
            if testlib_source is not None:
                source_files["testlib.h"] = testlib_source

        sources_payload: dict[str, str] = {}
        for name, source_path in source_files.items():
            blob = self._safe_read_bytes(
                source_path,
                max_bytes=self._max_binary_payload_bytes,
                label=f"{name} payload",
            )
            sources_payload[name] = base64.b64encode(blob).decode("ascii")

        return {
            "tests": tests_payload,
            "run_config_json": run_config_text,
            "problem_limits": {
                "time_limit_ms": int(problem_time_limit_ms),
                "memory_limit_mb": int(problem_memory_limit_mb),
            },
            "binaries_b64": binaries,
            "sources_b64": sources_payload,
        }

    def _build_task_payload(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_filename: str | None,
        selected_tests: list[str],
        invocation_id: str,
        invocation_run_ids: list[str],
        expected_behavior: str,
        invocation_source: str,
        run_id: str,
        task_kind: str = "",
        force_recompile: bool = False,
        compile_only: bool = False,
    ) -> dict[str, object]:
        ctx = self._run_service.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(str(ctx["workspace"]["path"]))

        source_bytes: bytes
        source_name: str
        source_label: str
        if isinstance(upload_content, (bytes, bytearray)):
            source_bytes = bytes(upload_content)
            source_name = str(upload_filename or "submission.cpp").strip() or "submission.cpp"
            source_label = source_name
        else:
            source_path = self._safe_workspace_source(workspace, str(submission_path or ""))
            source_bytes = self._safe_read_bytes(
                source_path,
                max_bytes=self._max_source_bytes,
                label="submission payload",
            )
            source_name = source_path.name
            source_label = str(submission_path or source_name)

        build_payload = self._collect_build_payload(
            problem=problem,
            build_id=build_id,
            workspace=workspace,
            mode=mode,
            selected_tests=selected_tests,
        )
        safe_task_kind = self._domjudge_task_kind(
            {
                "task_kind": task_kind,
                "invocation_source": invocation_source,
                "compile_only": bool(compile_only),
            }
        )
        legacy_compile_only = safe_task_kind == self._TASK_KIND_COMPILE_ONLY
        return {
            "type": "invocation.run",
            "run_id": run_id,
            "problem": problem,
            "username": username,
            "build_id": build_id,
            "mode": mode,
            "submission_path": str(submission_path or ""),
            "source_name": source_name,
            "source_label": source_label,
            "source_b64": base64.b64encode(source_bytes).decode("ascii"),
            "selected_tests": list(selected_tests),
            "invocation_id": invocation_id,
            "invocation_run_ids": list(invocation_run_ids),
            "expected_behavior": expected_behavior,
            "invocation_source": invocation_source,
            "task_kind": safe_task_kind,
            "force_recompile": bool(force_recompile),
            "compile_only": bool(legacy_compile_only),
            "build_payload": build_payload,
            "enqueued_at": now_iso(),
        }

    def _domjudge_precomputed_fields_from_payload(self, payload: dict[str, object]) -> dict[str, object]:
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
        build_payload = payload.get("build_payload")
        if not isinstance(build_payload, dict):
            raise RuntimeError("build payload is required for DOMjudge compatibility")
        run_cfg_obj: dict[str, object] = {}
        run_cfg_raw = self._domjudge_text(build_payload.get("run_config_json"))
        if run_cfg_raw:
            try:
                parsed = json.loads(run_cfg_raw)
                if isinstance(parsed, dict):
                    run_cfg_obj = parsed
            except Exception:
                run_cfg_obj = {}
        problem_limits_obj = build_payload.get("problem_limits")
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
        compile_only, generate_mode, solve_mode = self._domjudge_execution_modes(payload)
        configured_max_passes = max(
            1,
            self._domjudge_parse_int(
                run_cfg_obj.get("max_passes"),
                self._domjudge_parse_int(problem_limits_obj.get("max_passes"), 16),
            ),
        )
        max_passes = configured_max_passes if mode == "multi-pass" else 1
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
        pass_fail_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_PASS_FAIL_SEC", 1) or 1))
        multi_pass_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_MULTI_PASS_SEC", 15) or 15))
        interactive_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_INTERACTIVE_SEC", 15) or 15))
        run_overshoot_sec = pass_fail_slack
        if mode == "interactive":
            run_overshoot_sec = interactive_slack
        elif mode == "multi-pass":
            run_overshoot_sec = multi_pass_slack
        run_mem_kb = max(16 * 1024, int(run_mem_mb * 1024))
        binaries_b64 = build_payload.get("binaries_b64")
        binaries_obj = binaries_b64 if isinstance(binaries_b64, dict) else {}
        checker_bytes = self._domjudge_b64_decode(binaries_obj.get("checker"))
        validator_bytes = self._domjudge_b64_decode(binaries_obj.get("validator"))
        interactor_bytes = self._domjudge_b64_decode(binaries_obj.get("interactor"))
        sources_b64 = build_payload.get("sources_b64")
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

        compile_files: list[tuple[str, bytes, bool]] = [("run", self._domjudge_compile_script(source_name), True)]
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
                    ),
                    True,
                )
            )
            if compile_only or solve_mode:
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

        source_hash = self._domjudge_source_hash(source_name, source_bytes)
        if extra_source_items:
            hash_blobs: list[bytes] = [f"{source_name}\0".encode("utf-8") + source_bytes]
            hash_blobs.extend(f"{name}\0".encode("utf-8") + blob for name, blob in extra_source_items)
            source_hash = self._domjudge_set_hash_from_blobs(hash_blobs)
        compile_hash = domjudge_executable_hash(compile_files)
        run_hash = domjudge_executable_hash(run_files)
        compare_hash = domjudge_executable_hash(compare_files)
        toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(source_name)
        compare_script_timelimit = max(1, int(run_tl_sec))
        if checker_source_bytes or validator_source_bytes:
            compare_script_timelimit = max(compare_script_timelimit, min(compile_timeout, 120))
        compile_config = {
            "hash": compile_hash,
            "toolchain_cmd_digest": toolchain_cmd_digest,
            "filter_compiler_files": False,
            "language_extensions": list(self._domjudge_language_extensions(source_name)[1]),
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
            "language_id": self._domjudge_language_extensions(source_name)[0],
        }
        compare_config = {
            "hash": compare_hash,
            "combined_run_compare": bool(interactive),
            "compare_args": " ".join(checker_args),
            "script_timelimit": int(compare_script_timelimit),
            "script_memory_limit": run_mem_kb,
            "script_filesize_limit": int(run_output_kb),
        }
        return {
            "source_hash": source_hash,
            "compile_hash": compile_hash,
            "run_hash": run_hash,
            "compare_hash": compare_hash,
            "toolchain_cmd_digest": toolchain_cmd_digest,
            "compile_config": compile_config,
            "run_config": run_config,
            "compare_config": compare_config,
        }

    def prepare_enqueue_payload(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_filename: str | None,
        run_id: str,
        selected_tests: list[str] | None,
        invocation_id: str,
        invocation_run_ids: list[str] | None,
        expected_behavior: str,
        invocation_source: str,
        task_kind: str = "",
        force_recompile: bool = False,
        compile_only: bool = False,
    ) -> dict[str, object]:
        selected = [str(item or "").strip() for item in (selected_tests or [])]
        selected = [item for item in selected if RUN_TEST_NAME_RE.fullmatch(item)]
        selected = list(dict.fromkeys(selected))
        inv_run_ids = [str(item or "").strip() for item in (invocation_run_ids or [])]
        inv_run_ids = [item for item in inv_run_ids if _RUN_ID_RE.fullmatch(item)]
        inv_run_ids = list(dict.fromkeys(inv_run_ids))
        safe_run_id = self._normalize_run_id(run_id)
        payload = self._build_task_payload(
            problem=problem,
            username=username,
            build_id=build_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_filename=upload_filename,
            selected_tests=selected,
            invocation_id=invocation_id,
            invocation_run_ids=inv_run_ids,
            expected_behavior=expected_behavior,
            invocation_source=invocation_source,
            task_kind=task_kind,
            run_id=safe_run_id,
            force_recompile=bool(force_recompile),
            compile_only=bool(compile_only),
        )
        payload["domjudge_precomputed"] = self._domjudge_precomputed_fields_from_payload(payload)
        return payload

    def _initial_summary(
        self,
        *,
        run_id: str,
        task_id: str,
        mode: str,
        source_label: str,
        selected_tests: list[str],
        invocation_id: str,
        invocation_run_ids: list[str],
        expected_behavior: str,
        invocation_source: str,
        task_kind: str = "",
        compile_only: bool = False,
    ) -> dict[str, object]:
        safe_task_kind = self._domjudge_task_kind(
            {
                "task_kind": task_kind,
                "invocation_source": invocation_source,
                "compile_only": bool(compile_only),
            }
        )
        summary: dict[str, object] = {
            "mode": mode,
            "source": source_label,
            "selected_tests": list(selected_tests),
            "selected_tests_count": len(selected_tests),
            "invocation_source": str(invocation_source or "").strip() or "run.execute",
            "task_kind": safe_task_kind,
            "tests": [],
            "compile_log": "",
            "compile_diagnostics": [],
            "toolchain_digest": "judgehost",
            "sandbox_backend": self._run_service.sandbox.name,
            "invocation_backend": "domjudge-judgehost",
            "limits": {},
            "usage": {},
            "judgehost": {
                "task_id": task_id,
                "status": self.STATUS_QUEUED,
            },
        }
        if safe_task_kind == self._TASK_KIND_COMPILE_ONLY:
            summary["compile_only"] = True
        safe_invocation_id = str(invocation_id or "").strip()
        if _INVOCATION_ID_RE.fullmatch(safe_invocation_id):
            safe_ids: list[str] = []
            for raw in invocation_run_ids:
                token = str(raw or "").strip()
                if not _RUN_ID_RE.fullmatch(token):
                    continue
                if token in safe_ids:
                    continue
                safe_ids.append(token)
            if run_id not in safe_ids:
                safe_ids.append(run_id)
            summary["invocation"] = {
                "id": safe_invocation_id,
                "source": str(invocation_source or "run.execute").strip() or "run.execute",
                "run_ids": safe_ids,
                "expected_behavior": str(expected_behavior or "unknown").strip() or "unknown",
                "completed": False,
            }
        return summary

    def _ensure_run_row(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        run_id: str,
        mode: str,
        summary: dict[str, object],
    ) -> str:
        ctx = self._run_service.workspace_service.workspace_context(problem, username, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_row = self._db_fetch_one("SELECT build_ref FROM builds WHERE id=?", [str(build_id or "").strip()])
        build_ref = str(build_row["build_ref"] or "").strip().lower() if build_row is not None else ""
        run_root = self._run_service.fs_manager.prepare_run_root(run_id).resolve()
        now_text = now_iso()
        existing = self._db_fetch_one("SELECT id FROM runs WHERE id=?", [run_id])
        encoded = summary_for_db(
            summary,
            tests_limit=self._run_service.DB_SUMMARY_TESTS_LIMIT,
            diagnostics_limit=self._run_service.DB_SUMMARY_DIAGNOSTICS_LIMIT,
            feedback_files_limit=self._run_service.DB_SUMMARY_FEEDBACK_FILES_LIMIT,
            diagnostic_message_limit=self._run_service.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
        )
        if existing is None:
            self._db_execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    build_ref,
                    mode,
                    "running",
                    encoded,
                    str(run_root),
                    now_text,
                ],
            )
        else:
            self._db_execute(
                """
                UPDATE runs
                SET problem_id=?,workspace_id=?,build_id=?,build_ref=?,mode=?,status=?,summary_json=?,artifact_path=?,finished_at=NULL
                WHERE id=?
                """,
                [
                    problem_id,
                    workspace_id,
                    build_id,
                    build_ref,
                    mode,
                    "running",
                    encoded,
                    str(run_root),
                    run_id,
                ],
            )
        return str(run_root)

    def enqueue_task(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_filename: str | None,
        run_id: str,
        selected_tests: list[str] | None,
        invocation_id: str,
        invocation_run_ids: list[str] | None,
        expected_behavior: str,
        invocation_source: str,
        task_kind: str = "",
        force_recompile: bool = False,
        compile_only: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        safe_run_id = self._normalize_run_id(run_id)
        selected = [str(item or "").strip() for item in (selected_tests or [])]
        selected = [item for item in selected if RUN_TEST_NAME_RE.fullmatch(item)]
        selected = list(dict.fromkeys(selected))
        inv_run_ids = [str(item or "").strip() for item in (invocation_run_ids or [])]
        inv_run_ids = [item for item in inv_run_ids if _RUN_ID_RE.fullmatch(item)]
        inv_run_ids = list(dict.fromkeys(inv_run_ids))
        payload = self._build_task_payload(
            problem=problem,
            username=username,
            build_id=build_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_filename=upload_filename,
            selected_tests=selected,
            invocation_id=invocation_id,
            invocation_run_ids=inv_run_ids,
            expected_behavior=expected_behavior,
            invocation_source=invocation_source,
            task_kind=task_kind,
            run_id=safe_run_id,
            force_recompile=bool(force_recompile),
            compile_only=bool(compile_only),
        )
        if isinstance(prepared_payload, dict):
            payload.update(dict(prepared_payload))
        safe_task_kind = self._domjudge_task_kind(payload)
        payload["run_id"] = safe_run_id
        payload["problem"] = problem
        payload["username"] = username
        payload["build_id"] = build_id
        payload["mode"] = mode
        payload["submission_path"] = str(submission_path or "")
        payload["selected_tests"] = list(selected)
        payload["invocation_id"] = invocation_id
        payload["invocation_run_ids"] = list(inv_run_ids)
        payload["expected_behavior"] = expected_behavior
        payload["invocation_source"] = invocation_source
        payload["task_kind"] = safe_task_kind
        payload["force_recompile"] = bool(force_recompile)
        payload["compile_only"] = bool(safe_task_kind == self._TASK_KIND_COMPILE_ONLY)
        task_id = ""
        summary: dict[str, object] | None = None
        while True:
            with self._state_lock:
                existing_task_id = str(self._task_id_by_run.get(safe_run_id) or "").strip()
                if existing_task_id:
                    existing_task = self._tasks_by_id.get(existing_task_id)
                    if existing_task is None:
                        self._task_id_by_run.pop(safe_run_id, None)
                    else:
                        existing_status = str(existing_task.get("status") or "").strip().lower()
                        if existing_status != self.STATUS_ENQUEUING:
                            return existing_task_id
                if not existing_task_id or existing_task_id not in self._tasks_by_id:
                    task_id = f"jt-{uuid.uuid4().hex[:12]}"
                    source_label = str(payload.get("source_label") or payload.get("source_name") or "upload")
                    summary = self._initial_summary(
                        run_id=safe_run_id,
                        task_id=task_id,
                        mode=mode,
                        source_label=source_label,
                        selected_tests=selected,
                        invocation_id=invocation_id,
                        invocation_run_ids=inv_run_ids,
                        expected_behavior=expected_behavior,
                        invocation_source=invocation_source,
                        task_kind=safe_task_kind,
                        compile_only=bool(safe_task_kind == self._TASK_KIND_COMPILE_ONLY),
                    )
                    now_text = now_iso()
                    self._tasks_by_id[task_id] = {
                        "id": task_id,
                        "run_id": safe_run_id,
                        "problem_slug": str(problem),
                        "username": str(username),
                        "build_id": str(build_id),
                        "mode": str(mode),
                        "status": self.STATUS_ENQUEUING,
                        "payload": dict(payload),
                        "result": {},
                        "error_text": "",
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "created_at": now_text,
                        "updated_at": now_text,
                        "completed_at": "",
                        "attempt_count": 0,
                    }
                    self._task_id_by_run[safe_run_id] = task_id
                    break
            # Another thread is creating the same run task; wait for terminal enqueue step.
            time.sleep(0.01)

        if summary is None or not task_id:
            raise RuntimeError("failed to allocate judgehost task")

        try:
            self._ensure_run_row(
                problem=problem,
                username=username,
                build_id=build_id,
                run_id=safe_run_id,
                mode=mode,
                summary=summary,
            )
        except Exception:
            with self._state_lock:
                row = self._tasks_by_id.get(task_id)
                if row is not None and str(row.get("status") or "").strip().lower() == self.STATUS_ENQUEUING:
                    self._tasks_by_id.pop(task_id, None)
                    if self._task_id_by_run.get(safe_run_id) == task_id:
                        self._task_id_by_run.pop(safe_run_id, None)
            raise

        self._domjudge_try_prequeue_cache_finalize(
            task_id=task_id,
            run_id=safe_run_id,
            payload=dict(payload),
        )
        with self._state_lock:
            row = self._tasks_by_id.get(task_id)
            if row is not None and str(row.get("status") or "").strip().lower() == self.STATUS_ENQUEUING:
                row["status"] = self.STATUS_QUEUED
                row["updated_at"] = now_iso()
        return task_id

    def enqueue_compile_only_task(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        upload_content: bytes,
        upload_filename: str,
        run_id: str,
        invocation_id: str,
        invocation_run_ids: list[str] | None = None,
        expected_behavior: str = "compile",
        invocation_source: str = "compile.only",
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        return self.enqueue_task(
            problem=problem,
            username=username,
            build_id=build_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=bytes(upload_content),
            upload_filename=str(upload_filename or "submission.cpp"),
            run_id=run_id,
            selected_tests=[],
            invocation_id=str(invocation_id or ""),
            invocation_run_ids=list(invocation_run_ids or [run_id]),
            expected_behavior=str(expected_behavior or "compile"),
            invocation_source=str(invocation_source or "compile.only"),
            task_kind=self._TASK_KIND_COMPILE_ONLY,
            compile_only=True,
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )

