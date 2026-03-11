from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import IO, TYPE_CHECKING

from app.db import now_iso
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.run.runtime import RUN_TEST_NAME_RE
from app.service.run.summary import summary_for_db

if TYPE_CHECKING:
    from app.service.run.api import Run


def run_submission(
    self: "Run",
    problem: str,
    username: str,
    build_id: str,
    submission_path: str | None = None,
    mode: str = "pass-fail",
    upload_content: bytes | None = None,
    upload_filename: str | None = None,
    upload_stream: IO[bytes] | None = None,
    run_id: str | None = None,
    selected_tests: list[str] | None = None,
    invocation_id: str | None = None,
    invocation_run_ids: list[str] | None = None,
    expected_behavior: str | None = None,
    invocation_source: str = "run.execute",
    force_recompile: bool = False,
) -> str:
    _ = bool(force_recompile)
    supported_modes = {"pass-fail", "interactive", "multi-pass"}
    raw_selected_tests: list[str] = []
    if isinstance(selected_tests, str):
        raw_selected_tests.append(selected_tests)
    elif isinstance(selected_tests, list):
        raw_selected_tests.extend(str(item or "") for item in selected_tests)
    elif isinstance(selected_tests, tuple):
        raw_selected_tests.extend(str(item or "") for item in selected_tests)
    elif selected_tests is not None:
        try:
            raw_selected_tests.extend(str(item or "") for item in list(selected_tests))
        except Exception:
            raw_selected_tests.append(str(selected_tests or ""))
    selected_test_names: list[str] = []
    seen_selected_test_names: set[str] = set()
    for raw in raw_selected_tests:
        token = str(raw or "").strip()
        if not token:
            continue
        if not RUN_TEST_NAME_RE.fullmatch(token):
            raise RuntimeError(f"invalid selected test name: {token}")
        if token in seen_selected_test_names:
            continue
        seen_selected_test_names.add(token)
        selected_test_names.append(token)

    run_id = str(run_id or "").strip() or f"r-{uuid.uuid4().hex[:12]}"
    safe_invocation_id = str(invocation_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", safe_invocation_id):
        safe_invocation_id = ""
    safe_invocation_run_ids: list[str] = []
    raw_invocation_run_ids = invocation_run_ids or []
    for raw in raw_invocation_run_ids:
        token = str(raw or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", token):
            continue
        if token in safe_invocation_run_ids:
            continue
        safe_invocation_run_ids.append(token)
    if safe_invocation_id and run_id not in safe_invocation_run_ids:
        safe_invocation_run_ids.append(run_id)
    safe_expected_behavior = normalize_expected_behavior(str(expected_behavior or "unknown"))
    safe_invocation_source = str(invocation_source or "run.execute").strip() or "run.execute"

    def _attach_invocation_block(summary: dict, *, completed: bool | None = None) -> None:
        if not safe_invocation_id or not isinstance(summary, dict):
            return
        payload: dict[str, object] = {
            "id": safe_invocation_id,
            "source": safe_invocation_source,
            "run_ids": list(safe_invocation_run_ids),
            "expected_behavior": safe_expected_behavior,
        }
        if isinstance(completed, bool):
            payload["completed"] = completed
        summary["invocation"] = payload

    ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
    artifact_root: Path | None = None
    build_ref = ""
    build_row = self.db.fetch_one(
        "SELECT problem_id,workspace_id,status,summary_json,build_ref FROM builds WHERE id=?",
        [build_id],
    )
    preflight_reasons: list[str] = []
    if mode not in supported_modes:
        preflight_reasons.append(f"unsupported run mode: {mode}")
    if build_row is None:
        preflight_reasons.append("build metadata missing")
    else:
        build_ref = str(build_row["build_ref"] or "").strip().lower()
        if not build_ref:
            preflight_reasons.append("build_ref missing")
        else:
            try:
                artifact_root = self._canonical_build_artifact_root(build_ref)
            except RuntimeError as exc:
                preflight_reasons.append(str(exc))
        if build_row["problem_id"] != ctx["problem"]["id"]:
            preflight_reasons.append("build does not belong to selected problem")
        if build_row["workspace_id"] != ctx["workspace"]["id"]:
            preflight_reasons.append("build does not belong to selected workspace")
        if build_row["status"] != "ok":
            preflight_reasons.append(f"build status is {build_row['status']}")

    if artifact_root is not None:
        if not artifact_root.exists():
            preflight_reasons.append("artifact root missing")
        else:
            for required_dir in ["tests", "ans"]:
                dir_path = artifact_root / required_dir
                if not dir_path.exists() or not dir_path.is_dir():
                    preflight_reasons.append(f"artifact directory missing: {required_dir}/")
                elif not self._is_safe_dir(artifact_root, dir_path):
                    preflight_reasons.append(f"artifact directory invalid: {required_dir}/")

    preflight_ok = not preflight_reasons
    preflight_error = f"build not runnable: {build_id}"
    if preflight_reasons:
        preflight_error += " (" + "; ".join(preflight_reasons) + ")"
    run_root = self.fs_manager.prepare_run_root(run_id)
    compile_log_file = run_root / "compile.log"

    self.db.execute(
        "INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            run_id,
            ctx["problem"]["id"],
            ctx["workspace"]["id"],
            build_id,
            build_ref,
            mode,
            "running",
            str(run_root),
            now_iso(),
        ],
    )
    if safe_invocation_id:
        initial_summary = {
            "mode": mode,
            "source": submission_path or upload_filename or "upload",
            "selected_tests": selected_test_names,
            "selected_tests_count": len(selected_test_names),
            "tests": [],
            "compile_log": "compile.log",
            "compile_diagnostics": [],
            "limits": {},
            "usage": {},
        }
        _attach_invocation_block(initial_summary, completed=False)
        self.db.execute(
            "UPDATE runs SET summary_json=? WHERE id=?",
            [
                summary_for_db(
                    initial_summary,
                    tests_limit=self.DB_SUMMARY_TESTS_LIMIT,
                    diagnostics_limit=self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
                    feedback_files_limit=self.DB_SUMMARY_FEEDBACK_FILES_LIMIT,
                    diagnostic_message_limit=self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
                ),
                run_id,
            ],
        )

    if not preflight_ok:
        error = preflight_error
        compile_log_file.write_text(error + "\n", encoding="utf-8")
        failed_test, failed_reason = self._build_failure_context(build_row)
        if not failed_reason:
            failed_reason = error
        failed_tests = self._synthesize_failure_tests(
            preferred_test=failed_test,
            selected_test_names=selected_test_names,
            reason=failed_reason,
        )
        summary = {
            "error": error,
            "mode": mode,
            "source": submission_path or "upload",
            "tests": failed_tests,
            "selected_tests": selected_test_names,
            "selected_tests_count": len(selected_test_names),
            "compile_log": "compile.log",
            "compile_diagnostics": [],
            "toolchain_digest": "unknown",
            "sandbox_backend": self.sandbox.name,
            "limits": {},
            "usage": {},
        }
        _attach_invocation_block(summary, completed=True)
        (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self._finalize_run(run_id, "failed", summary)
        return run_id

    assert artifact_root is not None
    run_cfg = self._load_run_config(artifact_root, default_mode=mode)
    checker_args = [str(x) for x in run_cfg["checker_args"]]
    max_passes = int(run_cfg["max_passes"])
    run_timeout_ms = int(run_cfg.get("run_timeout_ms") or 0)
    run_timeout_sec = int(run_cfg["run_timeout_sec"])
    run_time_limit_ms = int(run_cfg.get("time_limit_ms") or self.DEFAULT_TIME_LIMIT_MS)
    run_memory_mb = int(run_cfg["run_memory_mb"])
    run_process_limit = int(run_cfg["run_process_limit"])
    run_output_kb = int(run_cfg["run_output_kb"])
    progress_persist_min_interval_ms = max(0, int(self.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS))
    progress_persist_batch_updates = max(1, int(self.RUN_PROGRESS_PERSIST_BATCH_UPDATES))
    progress_pending_updates = 0
    progress_last_persist_monotonic: float | None = None
    progress_has_persisted = False
    sandbox_limits = {
        "cpu_ms": run_time_limit_ms,
        "wall_ms": run_timeout_ms,
        "memory_mb": run_memory_mb,
        "pids": run_process_limit,
        "output_kb": run_output_kb,
    }

    def _persist_running_summary(current_tests: list[dict]) -> None:
        nonlocal progress_pending_updates
        nonlocal progress_last_persist_monotonic
        nonlocal progress_has_persisted
        tests_snapshot = [dict(row) for row in current_tests if isinstance(row, dict)]
        if not tests_snapshot:
            return
        progress_pending_updates += 1
        now_monotonic = time.monotonic()
        should_persist = False
        if not progress_has_persisted:
            # Persist the first progress snapshot eagerly so the UI can show
            # testcase-level activity as soon as it starts.
            should_persist = True
        elif progress_pending_updates >= progress_persist_batch_updates:
            should_persist = True
        elif progress_persist_min_interval_ms <= 0:
            should_persist = True
        elif progress_last_persist_monotonic is None:
            should_persist = True
        else:
            elapsed_ms = int((now_monotonic - progress_last_persist_monotonic) * 1000)
            if elapsed_ms >= progress_persist_min_interval_ms:
                should_persist = True
        if not should_persist:
            return
        summary = {
            "mode": mode,
            "source": source_label,
            "selected_tests": selected_test_names,
            "selected_tests_count": len(selected_test_names),
            "tests": tests_snapshot,
            "feedback_dir": "feedback_dir",
            "compile_diagnostics": compile_diagnostics,
            "compile_log": "compile.log",
            "toolchain_digest": toolchain_digest,
            "run_config": run_cfg,
            "sandbox_backend": self.sandbox.name,
            "limits": sandbox_limits,
            "usage": {
                "tests": len(tests_snapshot),
                "time_ms_total": sum(int(t.get("time_ms", 0) or 0) for t in tests_snapshot if isinstance(t, dict)),
                "time_user_ms_total": sum(int(t.get("time_user_ms", t.get("time_ms", 0)) or 0) for t in tests_snapshot if isinstance(t, dict)),
                "time_wall_ms_total": sum(int(t.get("time_wall_ms", 0) or 0) for t in tests_snapshot if isinstance(t, dict)),
                "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in tests_snapshot if isinstance(t, dict)] or [0]),
            },
        }
        _attach_invocation_block(summary, completed=False)
        self.db.execute(
            "UPDATE runs SET summary_json=? WHERE id=?",
            [
                summary_for_db(
                    summary,
                    tests_limit=self.DB_SUMMARY_TESTS_LIMIT,
                    diagnostics_limit=self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
                    feedback_files_limit=self.DB_SUMMARY_FEEDBACK_FILES_LIMIT,
                    diagnostic_message_limit=self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
                ),
                run_id,
            ],
        )
        progress_pending_updates = 0
        progress_last_persist_monotonic = now_monotonic
        progress_has_persisted = True

    tests_dir = artifact_root / "tests"
    ans_dir = artifact_root / "ans"
    checker = artifact_root / "bin" / "checker"
    interactor = artifact_root / "bin" / "interactor"
    feedback_dir = run_root / "feedback_dir"
    feedback_dir.mkdir(parents=True, exist_ok=True)

    workspace = Path(ctx["workspace"]["path"])
    sub_bin = run_root / "submission"
    compile_diagnostics: list[dict] = []
    source_label = submission_path or "upload"
    verdicts = []
    toolchain_digest = "unknown"
    compile_workspace: Path | None = None
    try:
        has_uploaded_source = False
        if upload_stream is not None:
            suffix = Path(upload_filename or "submission.cpp").suffix or ".cpp"
            sub_src = run_root / f"uploaded_submission{suffix}"
            sub_src.parent.mkdir(parents=True, exist_ok=True)
            with sub_src.open("wb") as out:
                shutil.copyfileobj(upload_stream, out, length=1024 * 1024)
            source_label = upload_filename or sub_src.name
            compile_workspace = None
            has_uploaded_source = True
        if not has_uploaded_source and upload_content is not None:
            suffix = Path(upload_filename or "submission.cpp").suffix or ".cpp"
            sub_src = run_root / f"uploaded_submission{suffix}"
            sub_src.write_bytes(upload_content)
            source_label = upload_filename or sub_src.name
            compile_workspace = None
            has_uploaded_source = True
        if not has_uploaded_source:
            if not submission_path:
                raise RuntimeError("submission_path is required when upload is not provided")
            source_in_workspace: Path | None = None
            with self.workspace_service.workspace_lock(workspace):
                source_in_workspace = self._resolve_submission_source(workspace, submission_path)
                sub_src = run_root / source_in_workspace.name
                shutil.copy2(source_in_workspace, sub_src)
            source_label = submission_path
            compile_workspace = None

        include_dirs: list[Path] = []
        include_dir = workspace / "third_party" / "testlib"
        if include_dir.exists():
            include_dirs.append(include_dir)

        ok, cout, cerr, toolchain_digest = self.toolchain.compile_program(
            sub_src,
            sub_bin,
            include_dirs,
            path_roots=[run_root, workspace],
            cxxflags=list(self.SUBMISSION_CPP_CXXFLAGS),
        )

        compile_diagnostics = self._persist_compile_outputs(
            compile_log_file,
            compile_workspace,
            cout,
            cerr,
        )
        if not ok:
            summary = {
                "error": "compile_error",
                "compile_log": "compile.log",
                "compile_diagnostics": compile_diagnostics,
                "toolchain_digest": toolchain_digest,
                "source": source_label,
                "selected_tests": selected_test_names,
                "selected_tests_count": len(selected_test_names),
                "run_config": run_cfg,
                "sandbox_backend": self.sandbox.name,
                "limits": sandbox_limits,
                "usage": {},
            }
            _attach_invocation_block(summary, completed=True)
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self._finalize_run(run_id, "failed", summary)
            return run_id

        test_meta = self._load_test_input_meta(artifact_root)
        if not test_meta:
            raise RuntimeError("selected build has no tests")
        if selected_test_names:
            test_meta_by_name = {name: (name, stem) for name, stem in test_meta}
            missing_tests = [name for name in selected_test_names if name not in test_meta_by_name]
            if missing_tests:
                shown = ", ".join(missing_tests[:6])
                if len(missing_tests) > 6:
                    shown += ", ..."
                raise RuntimeError(f"selected tests missing in build: {shown}")
            test_meta = [test_meta_by_name[name] for name in selected_test_names]
        answer_names = self._load_answer_file_set(artifact_root)
        effective_run_jobs = self._effective_run_jobs(run_cfg.get("run_jobs", 0), len(test_meta))
        run_cfg["run_jobs_effective"] = effective_run_jobs

        if checker.exists() and not self._is_safe_regular_file(checker.parent, checker):
            raise RuntimeError("checker binary path is invalid")
        if interactor.exists() and not self._is_safe_regular_file(interactor.parent, interactor):
            raise RuntimeError("interactor binary path is invalid")
        use_multi_pass_interactive = mode == "multi-pass" and interactor.exists()
        if use_multi_pass_interactive:
            # Each multi-pass case may require multiple interactive rounds.
            effective_run_jobs = 1
            run_cfg["run_jobs_effective"] = 1

        if mode == "interactive":
            for idx, (test_name, test_stem) in enumerate(test_meta):
                test = tests_dir / test_name
                ans_name = f"{test_stem}.ans"
                if ans_name not in answer_names:
                    raise RuntimeError(f"answer file missing or invalid for {test_name}")
                ans = ans_dir / ans_name
                test_feedback_dir = feedback_dir / test_stem
                test_feedback_dir.mkdir(parents=True, exist_ok=True)
                test_result = {
                    "test": test_name,
                    "passes": [],
                    "verdict": "OK",
                    "sandbox_status": "ok",
                    "time_ms": 0,
                    "time_user_ms": 0,
                    "time_wall_ms": 0,
                    "memory_kb": 0,
                    "feedback_files": [],
                }
                if not interactor.exists():
                    raise RuntimeError("interactive mode requested but interactor is missing in build artifacts")
                transcript = run_root / f"{test_stem}.transcript.txt"
                interactor_output = run_root / f"{test_stem}.out"
                verdict, user_ms, wall_ms, mem_kb = self._run_interactive_case(
                    interactor,
                    sub_bin,
                    test,
                    ans,
                    interactor_output,
                    transcript,
                    test_feedback_dir,
                    timeout_sec=run_timeout_sec,
                    timeout_ms=run_timeout_ms,
                    memory_mb=run_memory_mb,
                    process_limit=run_process_limit,
                    output_kb=run_output_kb,
                )
                if str(verdict or "").strip().upper().startswith("TL"):
                    user_ms = self._cap_tle_time_ms(user_ms, run_timeout_ms)
                test_result["passes"].append(
                    {
                        "verdict": verdict,
                        "time_ms": user_ms,
                        "time_user_ms": user_ms,
                        "time_wall_ms": wall_ms,
                        "memory_kb": mem_kb,
                    }
                )
                interactive_feedback = self._feedback_message_for_pass(test_feedback_dir / "interactor", run_root)
                if not interactive_feedback:
                    interactive_feedback = self._feedback_message_for_pass(test_feedback_dir, run_root)
                if interactive_feedback and test_result["passes"]:
                    test_result["passes"][0]["feedback"] = interactive_feedback
                test_result["verdict"] = verdict
                if str(verdict or "").strip().upper().startswith("TL"):
                    test_result["sandbox_status"] = "tle"
                elif verdict == "RE":
                    test_result["sandbox_status"] = "re"
                test_result["time_ms"] = user_ms
                test_result["time_user_ms"] = user_ms
                test_result["time_wall_ms"] = wall_ms
                test_result["memory_kb"] = mem_kb
                test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, run_root)
                test_result["transcript"] = str(transcript.relative_to(run_root))
                verdicts.append(test_result)
                if self._is_fl_verdict(test_result.get("verdict")):
                    self._append_fl_skip_tail_tests(
                        verdicts=verdicts,
                        test_meta=test_meta,
                        failed_index=idx,
                        caused_by_test=test_name,
                    )
                    _persist_running_summary(verdicts)
                    break
                _persist_running_summary(verdicts)
        elif use_multi_pass_interactive:
            for idx, (test_name, test_stem) in enumerate(test_meta):
                test = tests_dir / test_name
                ans_name = f"{test_stem}.ans"
                if ans_name not in answer_names:
                    raise RuntimeError(f"answer file missing or invalid for {test_name}")
                ans = ans_dir / ans_name
                test_result = self._run_multi_pass_interactive_test(
                    sub_bin,
                    interactor,
                    checker,
                    checker_args,
                    max_passes,
                    run_timeout_sec,
                    run_timeout_ms,
                    run_memory_mb,
                    run_process_limit,
                    run_output_kb,
                    test,
                    ans,
                    feedback_dir / test_stem,
                    run_root,
                )
                verdicts.append(test_result)
                if self._is_fl_verdict(test_result.get("verdict")):
                    self._append_fl_skip_tail_tests(
                        verdicts=verdicts,
                        test_meta=test_meta,
                        failed_index=idx,
                        caused_by_test=test_name,
                    )
                    _persist_running_summary(verdicts)
                    break
                _persist_running_summary(verdicts)
        elif mode != "interactive" and effective_run_jobs > 1:
            for test_name, test_stem in test_meta:
                ans_name = f"{test_stem}.ans"
                if ans_name not in answer_names:
                    raise RuntimeError(f"answer file missing or invalid for {test_name}")
            with ThreadPoolExecutor(max_workers=effective_run_jobs) as pool:
                future_map = {
                    pool.submit(
                        self._run_noninteractive_test,
                        mode,
                        sub_bin,
                        checker,
                        checker_args,
                        max_passes,
                        run_timeout_sec,
                        run_timeout_ms,
                        run_memory_mb,
                        run_process_limit,
                        run_output_kb,
                        tests_dir / test_name,
                        ans_dir / f"{test_stem}.ans",
                        feedback_dir / test_stem,
                        run_root,
                    ): idx
                    for idx, (test_name, test_stem) in enumerate(test_meta)
                }
                parallel_verdicts: list[dict] = [{} for _ in test_meta]
                for future in as_completed(future_map):
                    idx = future_map[future]
                    parallel_verdicts[idx] = future.result()
                    current_progress = [row for row in parallel_verdicts if isinstance(row, dict) and row]
                    _persist_running_summary(current_progress)
                first_fl_index = -1
                first_fl_test = ""
                for idx, row in enumerate(parallel_verdicts):
                    if not isinstance(row, dict):
                        continue
                    if not self._is_fl_verdict(row.get("verdict")):
                        continue
                    first_fl_index = idx
                    first_fl_test = str(row.get("test") or test_meta[idx][0])
                    break
                if first_fl_index >= 0:
                    if not first_fl_test:
                        first_fl_test = test_meta[first_fl_index][0]
                    for rem_idx in range(first_fl_index + 1, len(parallel_verdicts)):
                        rem_test_name = test_meta[rem_idx][0]
                        parallel_verdicts[rem_idx] = self._synthesized_fl_skip_test(
                            rem_test_name,
                            caused_by_test=first_fl_test,
                        )
                verdicts.extend(parallel_verdicts)
        else:
            for idx, (test_name, test_stem) in enumerate(test_meta):
                test = tests_dir / test_name
                ans_name = f"{test_stem}.ans"
                if ans_name not in answer_names:
                    raise RuntimeError(f"answer file missing or invalid for {test_name}")
                ans = ans_dir / ans_name
                test_result = self._run_noninteractive_test(
                    mode,
                    sub_bin,
                    checker,
                    checker_args,
                    max_passes,
                    run_timeout_sec,
                    run_timeout_ms,
                    run_memory_mb,
                    run_process_limit,
                    run_output_kb,
                    test,
                    ans,
                    feedback_dir / test_stem,
                    run_root,
                )
                verdicts.append(test_result)
                if self._is_fl_verdict(test_result.get("verdict")):
                    self._append_fl_skip_tail_tests(
                        verdicts=verdicts,
                        test_meta=test_meta,
                        failed_index=idx,
                        caused_by_test=test_name,
                    )
                    _persist_running_summary(verdicts)
                    break
                _persist_running_summary(verdicts)

        summary = {
            "mode": mode,
            "source": source_label,
            "selected_tests": selected_test_names,
            "selected_tests_count": len(selected_test_names),
            "tests": verdicts,
            "feedback_dir": "feedback_dir",
            "compile_diagnostics": compile_diagnostics,
            "compile_log": "compile.log",
            "toolchain_digest": toolchain_digest,
            "run_config": run_cfg,
            "sandbox_backend": self.sandbox.name,
            "limits": sandbox_limits,
            "usage": {
                "tests": len(verdicts),
                "time_ms_total": sum(int(t.get("time_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
                "time_user_ms_total": sum(int(t.get("time_user_ms", t.get("time_ms", 0)) or 0) for t in verdicts if isinstance(t, dict)),
                "time_wall_ms_total": sum(int(t.get("time_wall_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
                "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in verdicts if isinstance(t, dict)] or [0]),
            },
        }
        _attach_invocation_block(summary, completed=True)
        (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self._finalize_run(run_id, "ok", summary)
    except Exception as exc:
        if not compile_log_file.exists():
            compile_log_file.write_text(str(exc) + "\n", encoding="utf-8")
        summary = {
            "error": str(exc),
            "mode": mode,
            "source": source_label,
            "selected_tests": selected_test_names,
            "selected_tests_count": len(selected_test_names),
            "tests": verdicts,
            "feedback_dir": "feedback_dir",
            "compile_diagnostics": compile_diagnostics,
            "compile_log": "compile.log",
            "toolchain_digest": toolchain_digest,
            "run_config": run_cfg,
            "sandbox_backend": self.sandbox.name,
            "limits": sandbox_limits,
            "usage": {
                "tests": len(verdicts),
                "time_ms_total": sum(int(t.get("time_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
                "time_user_ms_total": sum(int(t.get("time_user_ms", t.get("time_ms", 0)) or 0) for t in verdicts if isinstance(t, dict)),
                "time_wall_ms_total": sum(int(t.get("time_wall_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
                "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in verdicts if isinstance(t, dict)] or [0]),
            },
        }
        _attach_invocation_block(summary, completed=True)
        (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self._finalize_run(run_id, "failed", summary)

    return run_id

