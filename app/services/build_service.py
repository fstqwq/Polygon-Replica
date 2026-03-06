from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db import DB, now_iso
from app.runtime_values import RuntimeValues, build_runtime_values
from app.services.artifact_service import ArtifactService
from app.services.fs_manager import FsManager
from app.services.hashing import canonical_json, sha256_hex_text
from app.services.sandbox import ExecSpec, SandboxBackend, NativeSandboxBackend
from app.services.tests_spec import (
    load_tests_spec,
    payload_rel_path_for_test,
    parse_gen_command_tokens,
)
from app.services.toolchain_service import ToolchainService
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from app.services.async_task_cache_service import AsyncTaskCacheService
    from app.services.invocation_backend_service import InvocationBackendService
    from app.services.judgehost_service import JudgehostTaskService


DIAG_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<level>warning|error|note):\s*(?P<msg>.*)$")
CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
GENERATOR_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_TEST_NAME_RE = re.compile(r"^[0-9]{3}\.in$")
STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[2] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
DEFAULT_TIME_LIMIT_MS = 2000
TIME_LIMIT_MIN_MS = 100
TIME_LIMIT_MAX_MS = 30000
CHECKER_TESTLIB_EXIT_CXXFLAGS = [
    "-DOK_EXIT_CODE=42",
    "-DWA_EXIT_CODE=43",
    "-DPE_EXIT_CODE=43",
]


class BuildService:
    DB_SUMMARY_DIAGNOSTICS_LIMIT = 200
    DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT = 4096
    BUILD_CACHE_NAMESPACE = "build.run"
    BUILD_CACHE_SCHEMA = "v3"
    BUILD_JOIN_WAIT_TIMEOUT_SEC = 180
    BUILD_JOIN_POLL_SEC = 0.25

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        artifacts: ArtifactService,
        toolchain: ToolchainService,
        sandbox_backend: SandboxBackend | None = None,
        constants: RuntimeValues | None = None,
        async_task_cache_service: AsyncTaskCacheService | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.toolchain = toolchain
        self.sandbox = sandbox_backend or NativeSandboxBackend()
        self.default_exec_memory_mb = 1024
        self.default_exec_process_limit = 64
        self.default_exec_output_kb = 65536
        self.wall_time_slack_pass_fail_sec = 1
        self.wall_time_slack_multi_pass_sec = 15
        self.wall_time_slack_interactive_sec = 15
        self._invocation_backend_service: InvocationBackendService | None = None
        self._judgehost_task_service: JudgehostTaskService | None = None
        self._async_task_cache_service = async_task_cache_service
        self._build_inflight_lock = threading.RLock()
        self._build_inflight: dict[str, str] = {}
        self.fs_manager = FsManager(self.workspace_service.settings.artifacts_root, self.workspace_service.settings.run_root)
        self.apply_runtime_values(constants or build_runtime_values())

    def bind_runtime_services(
        self,
        *,
        invocation_backend_service: InvocationBackendService | None = None,
        judgehost_task_service: JudgehostTaskService | None = None,
    ) -> None:
        self._invocation_backend_service = invocation_backend_service
        self._judgehost_task_service = judgehost_task_service

    def _active_solve_backend_name(self) -> str:
        service = self._invocation_backend_service
        if service is None:
            return "local-sandbox"
        try:
            token = str(service.active_backend_name() or "").strip().lower()
        except Exception:
            return "local-sandbox"
        return token or "local-sandbox"

    def _can_use_judge_backend_for_solve(self) -> bool:
        if self._active_solve_backend_name() != "domjudge-judgehost":
            return False
        service = self._judgehost_task_service
        if service is None:
            return False
        try:
            return bool(service.enabled() and service.auth_token_configured())
        except Exception:
            return False

    @staticmethod
    def _solve_result_ok() -> dict[str, object]:
        return {"rc": 0, "worker_error": "", "timed_out": False, "stderr": ""}

    @staticmethod
    def _solve_result_error(message: str) -> dict[str, object]:
        return {"rc": -1, "worker_error": str(message or "").strip(), "timed_out": False, "stderr": ""}

    def _judge_backend_compile_detail(self, summary_obj: dict[str, Any], run_root: Path) -> str:
        diagnostics = summary_obj.get("compile_diagnostics")
        if isinstance(diagnostics, list):
            for item in diagnostics:
                if not isinstance(item, dict):
                    continue
                message = str(item.get("message") or "").strip()
                if not message:
                    continue
                file_token = str(item.get("file") or "").strip()
                try:
                    line_no = int(item.get("line") or 0)
                except Exception:
                    line_no = 0
                try:
                    col_no = int(item.get("column") or 0)
                except Exception:
                    col_no = 0
                prefix = ""
                if file_token and line_no > 0 and col_no > 0:
                    prefix = f"{file_token}:{line_no}:{col_no}: "
                elif file_token and line_no > 0:
                    prefix = f"{file_token}:{line_no}: "
                elif file_token:
                    prefix = f"{file_token}: "
                return self._compact_single_line(prefix + message, 360)

        rel_compile_log = str(summary_obj.get("compile_log") or "").strip()
        for rel in [rel_compile_log, "compile.log"]:
            safe_rel = str(rel or "").strip()
            if not safe_rel:
                continue
            try:
                candidate = (run_root / safe_rel).resolve()
            except Exception:
                continue
            try:
                if candidate != run_root and run_root not in candidate.parents:
                    continue
            except Exception:
                continue
            if not candidate.exists() or (not candidate.is_file()):
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            compact = self._compact_single_line(text, 360)
            if compact:
                return compact

        fallback = str(summary_obj.get("error") or "").strip()
        if fallback:
            return self._compact_single_line(fallback, 360)
        return ""

    def _solve_with_judge_backend(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        accepted_source_rel: str,
        mode: str,
        test_files: list[Path],
        ans_dir: Path,
        solve_jobs: int = 1,
        source_answer_by_test: dict[str, Path] | None = None,
    ) -> dict[str, dict[str, object]]:
        service = self._judgehost_task_service
        if service is None:
            raise RuntimeError("judge backend service is unavailable")
        selected_tests = [str(p.name) for p in test_files if RUN_TEST_NAME_RE.fullmatch(str(p.name))]
        if not selected_tests:
            return {}
        solve_mode = str(mode or "pass-fail").strip().lower()
        if solve_mode not in {"pass-fail", "interactive", "multi-pass"}:
            solve_mode = "pass-fail"
        requested_parallelism = max(1, int(solve_jobs))
        effective_parallelism = max(1, min(requested_parallelism, len(selected_tests)))
        try:
            status = service.status()
        except Exception:
            status = {}
        if isinstance(status, dict):
            try:
                hosts_online = max(0, int(status.get("hosts_online") or 0))
            except Exception:
                hosts_online = 0
            try:
                hosts_total = max(0, int(status.get("hosts_total") or 0))
            except Exception:
                hosts_total = 0
            host_count = hosts_online if hosts_online > 0 else hosts_total
            try:
                fetch_batch_size = max(1, int(status.get("fetch_batch_size") or 1))
            except Exception:
                fetch_batch_size = 1
            if host_count > 0:
                effective_parallelism = max(
                    1,
                    min(effective_parallelism, host_count * fetch_batch_size),
                )
        # Keep one stable judgehost job per accepted source during build.solve.
        # This guarantees one compile per source and enables deterministic
        # per-test cache hydration keyed by a stable work root.
        effective_parallelism = 1

        solve_results: dict[str, dict[str, object]] = {}

        def _split_chunks(items: list[str], chunk_count: int) -> list[list[str]]:
            total = len(items)
            if total <= 0:
                return []
            count = max(1, min(chunk_count, total))
            base = total // count
            extra = total % count
            cursor = 0
            out: list[list[str]] = []
            for idx in range(count):
                size = base + (1 if idx < extra else 0)
                chunk = items[cursor : cursor + size]
                cursor += size
                if chunk:
                    out.append(chunk)
            return out

        plans: list[dict[str, object]] = []
        for idx, chunk in enumerate(_split_chunks(list(selected_tests), effective_parallelism)):
            plans.append(
                {
                    "index": idx,
                    "run_id": f"r-buildsolve-{uuid.uuid4().hex[:12]}",
                    "tests": chunk,
                }
            )
        if not plans:
            return solve_results
        invocation_run_ids = [
            str(plan.get("run_id") or "").strip()
            for plan in plans
            if str(plan.get("run_id") or "").strip()
        ]
        build_invocation_id = f"inv-buildsolve-{build_id}-{uuid.uuid4().hex[:8]}"

        def _submit_and_wait_chunk(plan: dict[str, object]) -> str:
            chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
            solve_run_id = str(plan.get("run_id") or "").strip() or f"r-buildsolve-{uuid.uuid4().hex[:12]}"
            task_id = service.enqueue_task(
                problem=problem,
                username=username,
                build_id=build_id,
                mode=solve_mode,
                submission_path=accepted_source_rel,
                upload_content=None,
                upload_filename=None,
                run_id=solve_run_id,
                selected_tests=chunk,
                invocation_id=build_invocation_id,
                invocation_run_ids=list(invocation_run_ids),
                expected_behavior="accepted",
                invocation_source="build.solve",
            )
            return str(service.wait_for_task(task_id, timeout_sec=None) or solve_run_id).strip() or solve_run_id

        def _consume_chunk_result(chunk: list[str], run_id: str) -> tuple[bool, str]:
            run_row = self.db.fetch_one("SELECT status,artifact_path,summary_json FROM runs WHERE id=?", [run_id])
            if run_row is None:
                msg = f"judge backend result missing for run {run_id}"
                for name in chunk:
                    solve_results[name] = self._solve_result_error(msg)
                return (False, chunk[0] if chunk else "")

            run_status = str(run_row["status"] or "").strip().lower()
            run_root = Path(str(run_row["artifact_path"] or "")).resolve()
            summary_obj: dict[str, Any] = {}
            raw_summary = str(run_row["summary_json"] or "").strip()
            if raw_summary:
                try:
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, dict):
                        summary_obj = parsed
                except Exception:
                    summary_obj = {}
            tests_summary = summary_obj.get("tests")
            tests_rows = [row for row in tests_summary if isinstance(row, dict)] if isinstance(tests_summary, list) else []
            feedback_by_test: dict[str, str] = {}
            verdict_by_test: dict[str, str] = {}
            output_ref_by_test: dict[str, str] = {}
            for row in tests_rows:
                test_name = str(row.get("test") or "").strip()
                if not test_name:
                    continue
                passes = row.get("passes")
                if not isinstance(passes, list) or (not passes):
                    continue
                pass_rows = [item for item in passes if isinstance(item, dict)]
                first_pass = pass_rows[0] if pass_rows else {}
                verdict = str(row.get("verdict") or first_pass.get("verdict") or "").strip().upper()
                if verdict:
                    verdict_by_test[test_name] = verdict
                final_pass_row: dict[str, Any] | None = None
                for item in pass_rows:
                    token = str(item.get("verdict") or "").strip().upper()
                    if token and token != "-":
                        final_pass_row = item
                if final_pass_row is None:
                    final_pass_row = first_pass if isinstance(first_pass, dict) else {}
                feedback = str(final_pass_row.get("feedback") or first_pass.get("feedback") or "").strip()
                if feedback:
                    feedback_by_test[test_name] = feedback
                for key in ("output_ref", "output_artifact", "output_rel"):
                    token = str(final_pass_row.get(key) or row.get(key) or "").strip()
                    if token:
                        output_ref_by_test[test_name] = token
                        break
            compile_detail = self._judge_backend_compile_detail(summary_obj, run_root)

            for idx, test_name in enumerate(chunk):
                if run_status and run_status != "ok":
                    detail = feedback_by_test.get(test_name) or str(summary_obj.get("error") or "").strip()
                    if (not detail) and compile_detail:
                        detail = compile_detail
                    if not detail:
                        detail = f"judge backend run status is {run_status}"
                    solve_results[test_name] = self._solve_result_error(detail)
                    for rest_name in chunk[idx + 1 :]:
                        if rest_name not in solve_results:
                            solve_results[rest_name] = self._solve_result_error(
                                f"skipped (prior test {test_name} failed)"
                            )
                    return (False, test_name)
                verdict = verdict_by_test.get(test_name, "")
                if verdict and verdict != "OK":
                    detail = feedback_by_test.get(test_name) or ""
                    if (not detail) and verdict == "CE":
                        detail = compile_detail
                    if not detail:
                        detail = f"judge verdict {verdict}"
                    solve_results[test_name] = self._solve_result_error(detail)
                    for rest_name in chunk[idx + 1 :]:
                        if rest_name not in solve_results:
                            solve_results[rest_name] = self._solve_result_error(
                                f"skipped (prior test {test_name} failed)"
                            )
                    return (False, test_name)
                stem = Path(test_name).stem
                source_out = (run_root / f"{stem}.out").resolve()
                target_ans = (ans_dir / f"{stem}.ans").resolve()
                source_answer = source_answer_by_test.get(test_name) if isinstance(source_answer_by_test, dict) else None
                if isinstance(source_answer, Path):
                    try:
                        if source_answer.exists() and source_answer.is_file() and (not source_answer.is_symlink()):
                            target_ans.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source_answer, target_ans)
                            solve_results[test_name] = self._solve_result_ok()
                            continue
                    except OSError:
                        pass
                output_ref = str(output_ref_by_test.get(test_name) or "").strip()
                if output_ref:
                    output_blob: bytes | None = None
                    judgehost = self._judgehost_task_service
                    if judgehost is not None:
                        try:
                            output_blob = judgehost.resolve_artifact_blob(output_ref)
                        except Exception:
                            output_blob = None
                    if (output_blob is None) and (not output_ref.startswith("cache://")):
                        source_ref = (run_root / output_ref).resolve()
                        if source_ref.exists() and source_ref.is_file() and (not source_ref.is_symlink()):
                            try:
                                output_blob = source_ref.read_bytes()
                            except OSError:
                                output_blob = None
                    if output_blob is not None:
                        target_ans.parent.mkdir(parents=True, exist_ok=True)
                        target_ans.write_bytes(output_blob)
                        solve_results[test_name] = self._solve_result_ok()
                        continue
                if source_out.exists() and source_out.is_file():
                    target_ans.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_out, target_ans)
                    solve_results[test_name] = self._solve_result_ok()
                    continue
                if verdict == "OK":
                    target_ans.parent.mkdir(parents=True, exist_ok=True)
                    target_ans.write_bytes(b"")
                    solve_results[test_name] = self._solve_result_ok()
                    continue
                detail = feedback_by_test.get(test_name) or str(summary_obj.get("error") or "").strip()
                if not detail:
                    detail = f"judge backend did not produce output for {test_name}"
                solve_results[test_name] = self._solve_result_error(detail)
                for rest_name in chunk[idx + 1 :]:
                    if rest_name not in solve_results:
                        solve_results[rest_name] = self._solve_result_error(
                            f"skipped (prior test {test_name} failed)"
                        )
                return (False, test_name)
            return (True, "")

        def _mark_remaining_chunks(start_index: int, failed_test_name: str) -> None:
            reason = (
                f"skipped (prior test {failed_test_name} failed)"
                if failed_test_name
                else "skipped (prior test failed)"
            )
            for idx in range(max(0, int(start_index)), len(plans)):
                chunk = [str(item or "").strip() for item in list(plans[idx].get("tests") or []) if str(item or "").strip()]
                for test_name in chunk:
                    if test_name not in solve_results:
                        solve_results[test_name] = self._solve_result_error(reason)

        def _mark_unresolved_tests(failed_test_name: str) -> None:
            reason = (
                f"skipped (prior test {failed_test_name} failed)"
                if failed_test_name
                else "skipped (prior test failed)"
            )
            for plan in plans:
                chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
                for test_name in chunk:
                    if test_name not in solve_results:
                        solve_results[test_name] = self._solve_result_error(reason)

        if len(plans) <= 1:
            plan = plans[0]
            chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
            run_id = str(plan.get("run_id") or "").strip() or f"r-buildsolve-{uuid.uuid4().hex[:12]}"
            try:
                run_id = _submit_and_wait_chunk(plan)
            except Exception as exc:
                detail = str(exc or "").strip() or "judge backend task failed"
                for name in chunk:
                    solve_results[name] = self._solve_result_error(detail)
                _mark_remaining_chunks(1, chunk[0] if chunk else "")
                return solve_results
            ok, failed_test = _consume_chunk_result(chunk, run_id)
            if not ok:
                _mark_remaining_chunks(1, failed_test)
            return solve_results

        pool = ThreadPoolExecutor(max_workers=effective_parallelism)
        pool_shutdown = False
        try:
            inflight: dict[object, int] = {}
            next_submit_idx = 0

            def _submit_plan(idx: int) -> None:
                plan = plans[idx]
                inflight[pool.submit(_submit_and_wait_chunk, plan)] = idx

            while (next_submit_idx < len(plans)) and (len(inflight) < effective_parallelism):
                _submit_plan(next_submit_idx)
                next_submit_idx += 1

            while inflight:
                done, _ = wait(set(inflight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    idx = inflight.pop(future)
                    plan = plans[idx]
                    fallback_run_id = str(plan.get("run_id") or "").strip() or f"r-buildsolve-{uuid.uuid4().hex[:12]}"
                    try:
                        run_id = str(future.result() or "").strip() or fallback_run_id
                        chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
                        ok, failed_test = _consume_chunk_result(chunk, run_id)
                        if not ok:
                            _mark_unresolved_tests(failed_test)
                            for pending in inflight:
                                pending.cancel()
                            pool.shutdown(wait=False, cancel_futures=True)
                            pool_shutdown = True
                            return solve_results
                    except Exception as exc:
                        detail = str(exc or "").strip() or "judge backend task failed"
                        chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
                        for name in chunk:
                            solve_results[name] = self._solve_result_error(detail)
                        failed_anchor = chunk[0] if chunk else ""
                        _mark_unresolved_tests(failed_anchor)
                        for pending in inflight:
                            pending.cancel()
                        pool.shutdown(wait=False, cancel_futures=True)
                        pool_shutdown = True
                        return solve_results

                while (next_submit_idx < len(plans)) and (len(inflight) < effective_parallelism):
                    _submit_plan(next_submit_idx)
                    next_submit_idx += 1
        finally:
            if not pool_shutdown:
                pool.shutdown(wait=True, cancel_futures=False)

        for test_name in selected_tests:
            if test_name not in solve_results:
                solve_results[test_name] = self._solve_result_error("judge backend result missing")
        return solve_results

    def _coerce_int(self, raw: object, default: int, min_value: int, max_value: int) -> int:
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.default_exec_memory_mb = self._coerce_int(
            values.get("BUILD_EXEC_MEMORY_MB", 1024),
            default=1024,
            min_value=16,
            max_value=262144,
        )
        self.default_exec_process_limit = self._coerce_int(
            values.get("BUILD_EXEC_PROCESS_LIMIT", 64),
            default=64,
            min_value=1,
            max_value=4096,
        )
        self.default_exec_output_kb = self._coerce_int(
            values.get("BUILD_EXEC_OUTPUT_KB", 65536),
            default=65536,
            min_value=64,
            max_value=1048576,
        )
        self.wall_time_slack_pass_fail_sec = self._coerce_int(
            values.get("RUN_WALL_TIME_SLACK_PASS_FAIL_SEC", 1),
            default=1,
            min_value=0,
            max_value=300,
        )
        self.wall_time_slack_multi_pass_sec = self._coerce_int(
            values.get("RUN_WALL_TIME_SLACK_MULTI_PASS_SEC", 15),
            default=15,
            min_value=0,
            max_value=300,
        )
        self.wall_time_slack_interactive_sec = self._coerce_int(
            values.get("RUN_WALL_TIME_SLACK_INTERACTIVE_SEC", 15),
            default=15,
            min_value=0,
            max_value=300,
        )

    def _normalize_problem_mode(self, raw: object, default: str = "pass-fail") -> str:
        token = str(raw or "").strip().lower()
        if token in {"pass-fail", "interactive", "multi-pass"}:
            return token
        return default

    def _sandbox_exec(
        self,
        cmd: list[str],
        timeout_sec: int,
        *,
        cwd: Path | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str, bool]:
        result = self.sandbox.run(
            ExecSpec(
                command=cmd,
                cwd=cwd,
                timeout_sec=max(1, int(timeout_sec)),
                stdin_path=stdin_path,
                stdout_path=stdout_path,
                env=env,
                memory_mb=self.default_exec_memory_mb,
                process_limit=self.default_exec_process_limit,
                output_kb=self.default_exec_output_kb,
            )
        )
        if result.timed_out:
            return -1, result.stdout, result.stderr, True
        return int(result.returncode or 0), result.stdout, result.stderr, False

    def _tests_spec_answer_source(self, snapshot: Path, test_id: str) -> Path | None:
        safe_test_id = str(test_id or "").strip()
        if not safe_test_id:
            return None
        candidate = snapshot / "tests" / "answers" / f"{safe_test_id}.ans"
        try:
            resolved_snapshot = snapshot.resolve()
            resolved = candidate.resolve()
        except OSError:
            return None
        if resolved_snapshot not in resolved.parents:
            return None
        try:
            if resolved.is_symlink() or (not resolved.exists()) or (not resolved.is_file()):
                return None
        except OSError:
            return None
        return resolved

    def _solve_interactive_case(
        self,
        interactor_bin: Path,
        accepted_bin: Path,
        test_input: Path,
        output_ans: Path,
        *,
        answer_source: Path | None = None,
        timeout_sec: int = 30,
        cwd: Path | None = None,
    ) -> tuple[int, str, bool]:
        sub_err_path = output_ans.with_name(f"{output_ans.stem}.submission.stderr.txt")
        itr_err_path = output_ans.with_name(f"{output_ans.stem}.interactor.stderr.txt")
        output_ans.parent.mkdir(parents=True, exist_ok=True)
        output_ans.unlink(missing_ok=True)
        case_root = cwd if cwd is not None else output_ans.parent
        sub_cwd = case_root / "submission"
        itr_cwd = case_root / "interactor"
        sub_cwd.mkdir(parents=True, exist_ok=True)
        itr_cwd.mkdir(parents=True, exist_ok=True)
        itr_input = itr_cwd / "input.in"
        itr_output = itr_cwd / "output.ans"
        itr_answer = itr_cwd / "answer.ans"
        shutil.copy2(test_input, itr_input)
        itr_output.unlink(missing_ok=True)
        has_reference_answer = answer_source is not None
        if has_reference_answer:
            shutil.copy2(answer_source, itr_answer)
        else:
            # Build solve for interactive mode should still be able to generate outputs
            # without a pre-existing answer file. Keep an empty placeholder so
            # registerInteraction variants that require an answer arg can start.
            itr_answer.write_text("", encoding="utf-8")
        sub_spec = ExecSpec(
            command=[str(accepted_bin)],
            cwd=sub_cwd,
            timeout_sec=max(1, int(timeout_sec)),
            memory_mb=self.default_exec_memory_mb,
            process_limit=self.default_exec_process_limit,
            output_kb=self.default_exec_output_kb,
        )
        # Primary testlib interactor convention:
        #   <input-file> <output-file> [answer-file]
        interactor_cmds: list[list[str]] = [
            [str(interactor_bin), itr_input.name, itr_output.name, itr_answer.name]
        ]
        # Some maintained testlib variants map registerInteraction args as:
        #   argv[1]=input, argv[2]=answer, argv[3]=result-dir
        # and fail with "Can not write to the result file" if argv[3] is not a directory.
        interactor_cmds.append([str(interactor_bin), itr_input.name, itr_answer.name, "."])

        for attempt_idx, interactor_cmd in enumerate(interactor_cmds):
            itr_spec = ExecSpec(
                command=interactor_cmd,
                cwd=itr_cwd,
                timeout_sec=max(1, int(timeout_sec)),
                memory_mb=self.default_exec_memory_mb,
                process_limit=self.default_exec_process_limit,
                output_kb=self.default_exec_output_kb,
            )
            itr_output.unlink(missing_ok=True)
            with sub_err_path.open("wb") as sub_err_fh, itr_err_path.open("wb") as itr_err_fh:
                itr_to_sub_r = -1
                itr_to_sub_w = -1
                sub_to_itr_r = -1
                sub_to_itr_w = -1
                sub: subprocess.Popen[bytes] | None = None
                itr: subprocess.Popen[bytes] | None = None
                itr_env = dict(os.environ)
                itr_env["FEEDBACK_DIR"] = str(itr_cwd)
                try:
                    # Full-duplex direct pipes:
                    # interactor stdout -> submission stdin
                    # submission stdout -> interactor stdin
                    itr_to_sub_r, itr_to_sub_w = os.pipe()
                    sub_to_itr_r, sub_to_itr_w = os.pipe()
                    sub = self.sandbox.popen(
                        sub_spec,
                        stdin=itr_to_sub_r,
                        stdout=sub_to_itr_w,
                        stderr=sub_err_fh,
                        text=False,
                        bufsize=0,
                    )
                    itr = self.sandbox.popen(
                        itr_spec,
                        stdin=sub_to_itr_r,
                        stdout=itr_to_sub_w,
                        stderr=itr_err_fh,
                        text=False,
                        bufsize=0,
                        env=itr_env,
                    )
                finally:
                    for fd in (itr_to_sub_r, itr_to_sub_w, sub_to_itr_r, sub_to_itr_w):
                        if fd < 0:
                            continue
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                if sub is None or itr is None:
                    return (-1, "interactive solve process setup failed", False)
                start = time.monotonic()
                timed_out = False
                try:
                    while True:
                        if time.monotonic() - start > max(1, int(timeout_sec)):
                            timed_out = True
                            if sub.poll() is None:
                                sub.kill()
                            if itr.poll() is None:
                                itr.kill()
                            break
                        if sub.poll() is not None and itr.poll() is not None:
                            break
                        time.sleep(0.01)
                finally:
                    for stream in (sub.stdin, sub.stdout, itr.stdin, itr.stdout):
                        if stream is None:
                            continue
                        try:
                            stream.close()
                        except Exception:
                            pass
                for proc in [sub, itr]:
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass

                if timed_out:
                    return (-1, "interactive solve timed out", True)
                sub_rc = int(sub.returncode or 0)
                if sub_rc != 0:
                    if attempt_idx + 1 < len(interactor_cmds):
                        continue
                    sub_err = sub_err_path.read_text(encoding="utf-8", errors="replace") if sub_err_path.exists() else ""
                    compact_sub_err = self._compact_single_line(sub_err, 220)
                    if compact_sub_err:
                        return (sub_rc, f"submission stderr: {compact_sub_err}", False)
                    return (sub_rc, "", False)
                itr_rc = int(itr.returncode or 0)
                if not self._validator_ok(itr_rc):
                    itr_err = itr_err_path.read_text(encoding="utf-8", errors="replace") if itr_err_path.exists() else ""
                    if attempt_idx + 1 < len(interactor_cmds):
                        continue
                    compact_itr_err = self._compact_single_line(itr_err, 220)
                    if compact_itr_err:
                        return (itr_rc, f"interactor stderr: {compact_itr_err}", False)
                    return (itr_rc, "", False)
                if has_reference_answer:
                    shutil.copy2(answer_source, output_ans)
                elif itr_output.exists():
                    shutil.copy2(itr_output, output_ans)
                elif not output_ans.exists():
                    output_ans.write_text("", encoding="utf-8")
                return (0, "", False)
        return (-1, "interactive solve failed", False)

    def _cap_summary_list_field(
        self,
        payload: dict,
        field: str,
        limit: int,
        truncated_key: str,
        total_key: str,
        limit_key: str,
    ) -> None:
        values = payload.get(field)
        if not isinstance(values, list):
            return
        cap = max(1, int(limit))
        total = len(values)
        payload[limit_key] = cap
        payload[total_key] = total
        if total > cap:
            payload[field] = values[:cap]
            payload[truncated_key] = True
            return
        payload[truncated_key] = False

    def _summary_for_db(self, summary: dict) -> str:
        payload = dict(summary)
        self._cap_summary_list_field(
            payload,
            "diagnostics",
            self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
            "diagnostics_truncated",
            "diagnostics_total",
            "diagnostics_limit",
        )
        diagnostics = payload.get("diagnostics")
        if isinstance(diagnostics, list):
            payload["diagnostics"] = self._normalize_diagnostics_for_db(
                diagnostics,
                self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
            )
        return json.dumps(payload)

    def _truncate_inline_text(self, value: str, max_chars: int) -> tuple[str, bool]:
        cap = max(1, int(max_chars))
        text = str(value or "")
        if len(text) <= cap:
            return text, False
        return text[:cap] + f"... [truncated; showing first {cap} characters]", True

    def _compact_single_line(self, value: str, max_chars: int) -> str:
        text = " ".join(str(value or "").split())
        cap = max(1, int(max_chars))
        if len(text) <= cap:
            return text
        return text[:cap].rstrip() + "..."

    def _normalize_diagnostics_for_db(self, entries: list, message_limit: int) -> list[dict]:
        normalized: list[dict] = []
        cap = max(1, int(message_limit))
        for raw in entries:
            item = raw if isinstance(raw, dict) else {"message": str(raw or "")}
            msg, msg_truncated = self._truncate_inline_text(str(item.get("message") or ""), cap)
            row = dict(item)
            row["message"] = msg
            row["message_truncated"] = bool(msg_truncated)
            row["message_limit"] = cap
            normalized.append(row)
        return normalized

    def _is_safe_source_in_dir(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        if path.is_symlink() or not path.exists() or not path.is_file():
            return False
        try:
            resolved_root = root_resolved if root_resolved is not None else root.resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents or resolved_root == resolved

    def _find_source_with_extensions(
        self,
        root: Path,
        folder: str,
        extensions: tuple[str, ...],
        preferred: str | None = None,
    ) -> Path | None:
        base = root / folder
        if not base.exists() or not base.is_dir():
            return None
        try:
            base_resolved = base.resolve()
        except OSError:
            return None
        if preferred:
            exact = base / preferred
            if self._is_safe_source_in_dir(base, exact, root_resolved=base_resolved):
                return exact
            stem = Path(preferred).stem
            for ext in extensions:
                candidate = base / f"{stem}{ext}"
                if self._is_safe_source_in_dir(base, candidate, root_resolved=base_resolved):
                    return candidate
        try:
            best: Path | None = None
            best_name = ""
            with os.scandir(base) as entries:
                for entry in entries:
                    name = entry.name
                    if Path(name).suffix.lower() not in extensions:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if best is None or name < best_name:
                        best = base / name
                        best_name = name
        except OSError:
            return None
        return best

    def _resolve_source(self, snapshot: Path, rel_path: str, snapshot_resolved: Path | None = None) -> Path:
        resolved_snapshot = snapshot_resolved if snapshot_resolved is not None else snapshot.resolve()
        p = (snapshot / rel_path).resolve()
        if resolved_snapshot not in p.parents:
            raise RuntimeError(f"invalid configured source path: {rel_path}")
        if not p.exists() or not p.is_file():
            raise RuntimeError(f"configured source does not exist: {rel_path}")
        return p

    def _normalize_standard_checker_name(self, raw: str) -> str:
        value = str(raw or "").strip()
        if value.startswith("std::"):
            value = value[5:]
        if not value:
            raise RuntimeError("checker_standard is empty")
        if "/" in value or "\\" in value:
            raise RuntimeError("checker_standard is invalid")
        if not value.endswith(".cpp"):
            value += ".cpp"
        if not STANDARD_CHECKER_NAME_RE.fullmatch(value):
            raise RuntimeError("checker_standard is invalid")
        return value

    def _resolve_standard_checker_source(self, checker_standard: str) -> Path | None:
        raw = str(checker_standard or "").strip()
        if not raw:
            return None
        checker_name = self._normalize_standard_checker_name(raw)
        source = (STANDARD_CHECKER_ROOT / checker_name).resolve()
        try:
            source.relative_to(STANDARD_CHECKER_ROOT)
        except ValueError:
            raise RuntimeError("checker_standard is invalid")
        try:
            if source.is_symlink() or not source.exists() or not source.is_file():
                raise RuntimeError(f"configured standard checker does not exist: std::{checker_name}")
        except OSError:
            raise RuntimeError("standard checker catalog is unavailable")
        return source

    def _select_checker_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        standard_source = self._resolve_standard_checker_source(str(build_cfg.get("checker_standard") or ""))
        if standard_source is not None:
            return standard_source
        return self._select_source(
            snapshot,
            build_cfg,
            "checker_source",
            "checkers",
            snapshot_resolved=snapshot_resolved,
        )

    def _select_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        config_key: str,
        folder: str,
        preferred: str | None = None,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        configured = build_cfg.get(config_key)
        if configured:
            return self._resolve_source(snapshot, str(configured), snapshot_resolved=snapshot_resolved)
        return self._find_source_with_extensions(
            snapshot,
            folder,
            CPP_EXTENSIONS,
            preferred=preferred,
        )

    def _load_build_config(self, snapshot: Path) -> dict:
        cfg = {
            "generator_runs": 3,
            "require_generator": False,
            "require_validator": True,
            "require_checker": True,
            "compile_jobs": 0,
            "validate_jobs": 0,
            "solve_jobs": 0,
            "run_jobs": 0,
            "run_timeout_sec": 30,
            "generator_args": [],
            "generator_sources": [],
            "validator_args": [],
            "checker_args": [],
            "checker_standard": "",
            "max_passes": 16,
        }
        path = snapshot / "config" / "build.json"
        if path.exists():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        if not isinstance(cfg.get("generator_args"), list):
            cfg["generator_args"] = []
        if not isinstance(cfg.get("generator_sources"), list):
            cfg["generator_sources"] = []
        if not isinstance(cfg.get("validator_args"), list):
            cfg["validator_args"] = []
        if not isinstance(cfg.get("checker_args"), list):
            cfg["checker_args"] = []
        if not isinstance(cfg.get("checker_standard"), str):
            cfg["checker_standard"] = ""
        cfg["checker_standard"] = str(cfg.get("checker_standard") or "").strip()
        try:
            cfg["compile_jobs"] = max(0, min(16, int(cfg.get("compile_jobs", 0))))
        except Exception:
            cfg["compile_jobs"] = 0
        try:
            cfg["validate_jobs"] = max(0, min(16, int(cfg.get("validate_jobs", 0))))
        except Exception:
            cfg["validate_jobs"] = 0
        try:
            cfg["solve_jobs"] = max(0, min(16, int(cfg.get("solve_jobs", 0))))
        except Exception:
            cfg["solve_jobs"] = 0
        try:
            cfg["run_jobs"] = max(0, min(16, int(cfg.get("run_jobs", 0))))
        except Exception:
            cfg["run_jobs"] = 0
        try:
            cfg["run_timeout_sec"] = max(1, min(300, int(cfg.get("run_timeout_sec", 30))))
        except Exception:
            cfg["run_timeout_sec"] = 30
        try:
            cfg["max_passes"] = max(1, int(cfg.get("max_passes", 16)))
        except Exception:
            cfg["max_passes"] = 16
        return cfg

    def _normalize_time_limit_ms(self, raw: object) -> int:
        try:
            value = int(raw)
        except Exception:
            value = DEFAULT_TIME_LIMIT_MS
        return max(TIME_LIMIT_MIN_MS, min(TIME_LIMIT_MAX_MS, value))

    def _wall_time_slack_sec_for_mode(self, mode: object) -> int:
        token = self._normalize_problem_mode(mode)
        if token == "interactive":
            return max(0, int(self.wall_time_slack_interactive_sec))
        if token == "multi-pass":
            return max(0, int(self.wall_time_slack_multi_pass_sec))
        return max(0, int(self.wall_time_slack_pass_fail_sec))

    def _effective_run_timeout_ms(self, time_limit_ms: int, *, mode: object = "pass-fail") -> int:
        tl = self._normalize_time_limit_ms(time_limit_ms)
        slack_ms = self._wall_time_slack_sec_for_mode(mode) * 1000
        return max(1, tl * 2 + slack_ms)

    def _effective_run_timeout_sec(self, run_timeout_ms: int) -> int:
        timeout_ms = max(1, int(run_timeout_ms))
        return max(1, (timeout_ms + 999) // 1000)

    def _load_problem_runtime_config(self, snapshot: Path) -> dict:
        cfg = {"time_limit_ms": DEFAULT_TIME_LIMIT_MS, "mode": "pass-fail"}
        path = snapshot / "config" / "problem.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    cfg.update(payload)
            except json.JSONDecodeError:
                pass
        cfg["time_limit_ms"] = self._normalize_time_limit_ms(cfg.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS))
        cfg["mode"] = self._normalize_problem_mode(cfg.get("mode"), "pass-fail")
        return cfg

    def _collect_diagnostics(self, snapshot: Path, text: str) -> list[dict]:
        result: list[dict] = []
        try:
            snapshot_resolved = snapshot.resolve()
        except OSError:
            snapshot_resolved = None
        for line in text.splitlines():
            m = DIAG_RE.match(line.strip())
            if not m:
                continue
            file_path = Path(m.group("file"))
            if file_path.is_absolute():
                try:
                    resolved = file_path.resolve()
                    if snapshot_resolved is not None:
                        rel = str(resolved.relative_to(snapshot_resolved))
                    else:
                        rel = str(resolved)
                except ValueError:
                    rel = str(file_path)
                except OSError:
                    rel = str(file_path)
            else:
                rel = str(file_path)
            result.append(
                {
                    "file": rel,
                    "line": int(m.group("line")),
                    "column": int(m.group("col")),
                    "level": m.group("level"),
                    "message": m.group("msg"),
                }
            )
        return result

    def _append_compile_streams(
        self,
        log_fh,
        snapshot: Path,
        stdout_text: str,
        stderr_text: str,
    ) -> list[dict]:
        diagnostics: list[dict] = []
        saw_stream_text = False
        wrote_stream = False
        for chunk in (stdout_text, stderr_text):
            text = str(chunk or "")
            if not text:
                continue
            saw_stream_text = True
            if wrote_stream and not text.startswith("\n"):
                log_fh.write("\n")
            log_fh.write(text)
            if not text.endswith("\n"):
                log_fh.write("\n")
            diagnostics.extend(self._collect_diagnostics(snapshot, text))
            wrote_stream = True
        if not saw_stream_text:
            diagnostics.extend(self._collect_diagnostics(snapshot, ""))
        return diagnostics

    def _validator_ok(self, returncode: int) -> bool:
        return returncode in {0, 42}

    def _checker_ok(self, returncode: int) -> bool:
        return int(returncode) == 42

    def _checker_feedback_message(self, feedback_dir: Path) -> str:
        for name in ("judgemessage.txt", "teammessage.txt", "checker.log"):
            candidate = feedback_dir / name
            try:
                if candidate.exists() and candidate.is_file() and (not candidate.is_symlink()):
                    text = candidate.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        return self._compact_single_line(text, 220)
            except OSError:
                continue
        return ""

    def _run_checker_with_submission_output(
        self,
        checker: Path,
        checker_args: list[str],
        test_input: Path,
        expected_answer: Path,
        submission_output_text: str,
        run_root: Path,
    ) -> tuple[int, bool, str]:
        run_root.mkdir(parents=True, exist_ok=True)
        submission_output = run_root / "submission.out"
        feedback_dir = run_root / "feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        submission_output.write_text(str(submission_output_text or ""), encoding="utf-8")
        rc, out, err, timed_out = self._sandbox_exec(
            [str(checker), str(test_input), str(expected_answer), str(feedback_dir), *checker_args],
            timeout_sec=30,
            stdin_path=submission_output,
            cwd=run_root,
        )
        message = self._checker_feedback_message(feedback_dir)
        if not message:
            stream = self._compact_single_line((str(out or "") + "\n" + str(err or "")).strip(), 220)
            if stream:
                message = stream
        return int(rc), bool(timed_out), message

    def _manual_test_sources(self, snapshot: Path) -> list[Path]:
        manual_root = snapshot / "tests" / "manual"
        if not manual_root.exists():
            return []
        try:
            manual_root_resolved = manual_root.resolve()
        except OSError:
            return []

        def _is_in_name(name: str) -> bool:
            return os.path.splitext(name)[1].lower() == ".in"

        def _collect_safe_entries(
            dir_root: Path,
            names: list[str],
            rel_prefix: str,
        ) -> list[tuple[str, Path, bool]]:
            safe_entries: list[tuple[str, Path, bool]] = []
            for name in names:
                p = dir_root / name
                if p.is_symlink() or not p.exists() or not p.is_file():
                    continue
                rel = f"{rel_prefix}/{name}" if rel_prefix else name
                safe_entries.append((rel, p, _is_in_name(name)))
            return safe_entries

        in_files: list[tuple[str, Path]] = []
        all_files: list[tuple[str, Path]] | None = []
        for dirpath, dirnames, filenames in os.walk(manual_root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if manual_root_resolved not in dir_root_resolved.parents and manual_root_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            try:
                rel_root = dir_root.relative_to(manual_root)
            except ValueError:
                dirnames[:] = []
                continue
            rel_prefix = "" if rel_root == Path(".") else rel_root.as_posix()
            keep_dirs: list[str] = []
            for name in dirnames:
                d = dir_root / name
                if d.is_symlink():
                    continue
                keep_dirs.append(name)
            dirnames[:] = sorted(keep_dirs)

            in_candidates = [name for name in filenames if _is_in_name(name)]
            has_in_file = False
            if in_candidates:
                # Fast path: when safe *.in files exist, we can skip validating sidecar files.
                safe_entries = _collect_safe_entries(dir_root, in_candidates, rel_prefix)
                has_in_file = bool(safe_entries)
                if not has_in_file and all_files is not None:
                    safe_entries = _collect_safe_entries(dir_root, filenames, rel_prefix)
                    has_in_file = any(is_in for _, _, is_in in safe_entries)
            elif all_files is None:
                safe_entries = []
            else:
                safe_entries = _collect_safe_entries(dir_root, filenames, rel_prefix)

            if has_in_file:
                if all_files is not None:
                    all_files.clear()
                    all_files = None
                for rel, p, is_in in safe_entries:
                    if is_in:
                        in_files.append((rel, p))
            elif all_files is not None:
                for rel, p, _ in safe_entries:
                    all_files.append((rel, p))

        if in_files:
            return [p for _, p in sorted(in_files)]
        return []

    def _load_tests_spec(self, snapshot: Path) -> list[dict] | None:
        spec_path = snapshot / "tests" / "spec.json"
        if not spec_path.exists():
            return None
        try:
            return load_tests_spec(spec_path)
        except ValueError as exc:
            raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc

    def _generator_source_catalog(self, snapshot: Path) -> list[tuple[str, Path]]:
        generators_root = snapshot / "generators"
        try:
            if not generators_root.exists() or not generators_root.is_dir() or generators_root.is_symlink():
                return []
        except OSError:
            return []
        try:
            generators_root_resolved = generators_root.resolve()
        except OSError:
            return []

        rows: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(generators_root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if (
                generators_root_resolved not in dir_root_resolved.parents
                and generators_root_resolved != dir_root_resolved
            ):
                dirnames[:] = []
                continue

            safe_dirs: list[str] = []
            for name in dirnames:
                p = dir_root / name
                try:
                    if p.is_symlink() or not p.exists() or not p.is_dir():
                        continue
                except OSError:
                    continue
                safe_dirs.append(name)
            dirnames[:] = sorted(safe_dirs)

            for name in sorted(filenames):
                if Path(name).suffix.lower() not in GENERATOR_SOURCE_EXTENSIONS:
                    continue
                p = dir_root / name
                try:
                    if p.is_symlink() or not p.exists() or not p.is_file():
                        continue
                    rel = str(p.relative_to(snapshot)).replace("\\", "/")
                except (OSError, ValueError):
                    continue
                rows.append((rel, p))
        rows.sort(key=lambda item: item[0])
        return rows

    def _resolve_generator_source_from_token(
        self,
        token: str,
        generator_catalog: list[tuple[str, Path]],
    ) -> tuple[str, Path]:
        raw = str(token or "").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        if not raw:
            raise RuntimeError("generator command is empty")
        if any(part == ".." for part in raw.split("/")):
            raise RuntimeError(f"invalid generator command '{token}'")

        by_rel = {rel: path for rel, path in generator_catalog}
        candidates: list[str] = []
        token_path = Path(raw)
        suffix = token_path.suffix.lower()
        if raw.startswith("generators/"):
            if suffix in GENERATOR_SOURCE_EXTENSIONS:
                candidates.append(raw)
            else:
                for ext in GENERATOR_SOURCE_EXTENSIONS:
                    candidates.append(f"{raw}{ext}")
        else:
            if suffix in GENERATOR_SOURCE_EXTENSIONS:
                candidates.append(f"generators/{raw}")
            else:
                candidates.append(f"generators/{raw}")
                for ext in GENERATOR_SOURCE_EXTENSIONS:
                    candidates.append(f"generators/{raw}{ext}")

        seen: set[str] = set()
        for rel in candidates:
            rel_key = str(rel or "").strip()
            if not rel_key or rel_key in seen:
                continue
            seen.add(rel_key)
            hit = by_rel.get(rel_key)
            if hit is not None:
                return rel_key, hit

        name = token_path.name
        if suffix in GENERATOR_SOURCE_EXTENSIONS:
            exact = [(rel, p) for rel, p in generator_catalog if Path(rel).name == name]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                raise RuntimeError(f"ambiguous generator source for command '{token}'")
        else:
            stem = token_path.name
            stem_matches = [(rel, p) for rel, p in generator_catalog if Path(rel).stem == stem]
            if len(stem_matches) == 1:
                return stem_matches[0]
            if len(stem_matches) > 1:
                raise RuntimeError(f"ambiguous generator source for command '{token}'")

        raise RuntimeError(f"cannot resolve generator source for command '{token}'")

    def _tests_spec_payload_text(self, snapshot: Path, row: dict, index: int) -> tuple[str, str]:
        test_id = str(row.get("id") or "").strip()
        if not test_id:
            raise RuntimeError(f"tests/spec.json entry {index} missing id")
        kind = str(row.get("kind") or "").strip().lower()
        if kind not in {"manual", "gen"}:
            raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}")
        rel = payload_rel_path_for_test(test_id, kind)
        payload_path = snapshot / rel
        try:
            if payload_path.exists() and payload_path.is_file() and not payload_path.is_symlink():
                return rel, payload_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read tests payload for id {test_id}: {exc}") from exc
        raise RuntimeError(f"missing tests payload file for id {test_id}: {rel}")

    def _prepare_tests_spec_runtime(
        self,
        snapshot: Path,
        tests_spec_entries: list[dict],
        bin_dir: Path,
    ) -> tuple[list[dict], list[tuple[str, Path, Path]]]:
        runtime_entries: list[dict] = []
        generator_targets: list[tuple[str, Path, Path]] = []
        by_source_rel: dict[str, tuple[str, Path]] = {}
        generator_catalog = self._generator_source_catalog(snapshot)

        for index, row in enumerate(tests_spec_entries, start=1):
            kind = str(row.get("kind") or "").strip()
            test_id = str(row.get("id") or "").strip()
            sample = bool(row.get("sample"))
            sample_input = str(row.get("sample_input") or "")
            sample_output = str(row.get("sample_output") or "")
            sample_output_validate = bool(row.get("sample_output_validate", True))
            payload_rel, payload = self._tests_spec_payload_text(snapshot, row, index)
            if kind == "manual":
                runtime_entries.append(
                    {
                        "index": index,
                        "id": test_id,
                        "kind": "manual",
                        "sample": sample,
                        "sample_input": sample_input,
                        "sample_output": sample_output,
                        "sample_output_validate": sample_output_validate,
                        "source_rel": payload_rel,
                        "input": payload,
                    }
                )
                continue
            if kind != "gen":
                raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}")
            command = str(payload or "").strip()
            tokens = parse_gen_command_tokens(command)
            source_rel, source_path = self._resolve_generator_source_from_token(tokens[0], generator_catalog)
            compiled = by_source_rel.get(source_rel)
            if compiled is None:
                gen_index = len(by_source_rel) + 1
                target_name = f"generator_spec_{gen_index}"
                target_bin = bin_dir / target_name
                by_source_rel[source_rel] = (target_name, target_bin)
                generator_targets.append((target_name, source_path, target_bin))
                compiled = (target_name, target_bin)
            runtime_entries.append(
                {
                    "index": index,
                    "id": test_id,
                    "kind": "gen",
                    "sample": sample,
                    "sample_input": sample_input,
                    "sample_output": sample_output,
                    "sample_output_validate": sample_output_validate,
                    "cmd": command,
                    "args": [str(x) for x in tokens[1:]],
                    "source_rel": source_rel,
                    "payload_rel": payload_rel,
                    "target_name": compiled[0],
                }
            )

        return runtime_entries, generator_targets

    def _effective_compile_jobs(self, configured: object, target_count: int) -> int:
        auto_jobs = max(1, min(4, os.cpu_count() or 1))
        try:
            requested = int(configured)
        except Exception:
            requested = 0
        bounded = auto_jobs if requested <= 0 else max(1, min(16, requested))
        return max(1, min(bounded, max(1, target_count)))

    @staticmethod
    def _canonical_digest(payload: object) -> str:
        text = canonical_json(payload, ensure_ascii=False)
        return sha256_hex_text(text)

    def _generation_params_digest(self, source_root: Path, *, sample_only: bool) -> str:
        build_cfg = self._load_build_config(source_root)
        runtime_cfg = self._load_problem_runtime_config(source_root)
        tests_spec_rows: list[dict[str, object]] = []
        try:
            tests_spec_rows = [dict(row) for row in load_tests_spec(source_root / "tests" / "spec.json")]
        except Exception:
            tests_spec_rows = []
        return self._canonical_digest(
            {
                "schema": "v1",
                "sample_only": bool(sample_only),
                "build_config": build_cfg,
                "runtime_config": runtime_cfg,
                "tests_spec_rows": tests_spec_rows,
            }
        )

    def _toolchain_cmd_digest(self) -> str:
        try:
            token = str(self.toolchain.current_cpp_command_digest() or "").strip().lower()
        except Exception:
            token = ""
        return token

    @staticmethod
    def _build_cache_key_hash(key_obj: dict[str, object]) -> str:
        text = canonical_json(key_obj, ensure_ascii=False)
        return sha256_hex_text(text)

    def _build_ref_from_cache_key_hash(self, cache_key_hash: str) -> str:
        digest = str(cache_key_hash or "").strip().lower()
        if not digest:
            digest = sha256_hex_text(f"{self.BUILD_CACHE_SCHEMA}:empty")
        return self.fs_manager.compute_build_ref(
            {
                "schema": self.BUILD_CACHE_SCHEMA,
                "cache_key_hash": digest,
            }
        )

    def _artifact_root_from_build_ref(self, problem_slug: str, build_ref: str) -> Path:
        _ = str(problem_slug or "").strip()
        return self.fs_manager.build_paths(str(build_ref or "").strip().lower()).root.resolve()

    def _build_paths(self, problem_slug: str, build_ref: str):
        _ = str(problem_slug or "").strip()
        return self.fs_manager.ensure_build_layout(str(build_ref or "").strip().lower())

    def _wait_build_terminal_status(self, build_id: str, timeout_sec: float) -> str:
        safe_build_id = str(build_id or "").strip()
        if not safe_build_id:
            return ""
        deadline = time.monotonic() + max(0.5, float(timeout_sec))
        while time.monotonic() < deadline:
            row = self.db.fetch_one("SELECT status FROM builds WHERE id=?", [safe_build_id])
            status = str(row["status"] or "").strip().lower() if row is not None else ""
            if status in {"ok", "failed", "cancelled"}:
                return status
            time.sleep(self.BUILD_JOIN_POLL_SEC)
        return ""

    def _build_cache_key(
        self,
        *,
        problem_id: int,
        workspace_id: int,
        source_commit: str,
        source_ref: str,
        generation_params_digest: str,
        toolchain_cmd_digest: str,
        sample_only: bool = False,
    ) -> dict[str, object]:
        return {
            "problem_id": int(problem_id),
            "workspace_id": int(workspace_id),
            "source_commit": str(source_commit or "").strip(),
            "source_ref": str(source_ref or "").strip(),
            "generation_params_digest": str(generation_params_digest or "").strip().lower(),
            "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
            "sample_only": bool(sample_only),
            "schema": self.BUILD_CACHE_SCHEMA,
        }

    def _cached_build_id_for_source(
        self,
        *,
        problem_slug: str = "",
        problem_id: int,
        workspace_id: int,
        source_commit: str,
        source_ref: str,
        generation_params_digest: str,
        toolchain_cmd_digest: str,
        sample_only: bool = False,
    ) -> str:
        service = self._async_task_cache_service
        if service is None:
            return ""
        safe_commit = str(source_commit or "").strip()
        if not safe_commit:
            return ""
        entry = service.get(
            self.BUILD_CACHE_NAMESPACE,
            self._build_cache_key(
                problem_id=int(problem_id),
                workspace_id=int(workspace_id),
                source_commit=safe_commit,
                source_ref=str(source_ref or "").strip(),
                generation_params_digest=str(generation_params_digest or "").strip().lower(),
                toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
                sample_only=bool(sample_only),
            ),
        )
        if not isinstance(entry, dict):
            return ""
        value = entry.get("value")
        value_obj = value if isinstance(value, dict) else {}
        cached_build_id = str(value_obj.get("build_id") or "").strip()
        if not cached_build_id:
            return ""
        row = self.db.fetch_one(
            "SELECT status,build_ref,artifact_path,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
            [cached_build_id, int(problem_id), int(workspace_id)],
        )
        cache_key = self._build_cache_key(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            source_commit=safe_commit,
            source_ref=str(source_ref or "").strip(),
            generation_params_digest=str(generation_params_digest or "").strip().lower(),
            toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
            sample_only=bool(sample_only),
        )
        if row is None:
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        if str(row["status"] or "").strip().lower() != "ok":
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        try:
            summary_obj = json.loads(str(row["summary_json"] or "{}"))
        except Exception:
            summary_obj = {}
        generation_params = summary_obj.get("generation_params") if isinstance(summary_obj, dict) else {}
        if not isinstance(generation_params, dict):
            generation_params = {}
        if bool(generation_params.get("sample_only", False)) != bool(sample_only):
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        if str(generation_params.get("toolchain_cmd_digest") or "").strip().lower() != str(toolchain_cmd_digest or "").strip().lower():
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        if str(generation_params.get("generation_params_digest") or "").strip().lower() != str(generation_params_digest or "").strip().lower():
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        cached_build_ref = str(row["build_ref"] or "").strip()
        if not cached_build_ref:
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        if str(problem_slug or "").strip():
            artifact_root = self._artifact_root_from_build_ref(str(problem_slug or "").strip(), cached_build_ref)
        else:
            artifact_root = Path(str(row["artifact_path"] or "")).resolve()
        tests_dir = artifact_root / "tests"
        ans_dir = artifact_root / "ans"
        if not tests_dir.exists() or (not tests_dir.is_dir()) or tests_dir.is_symlink():
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        if not ans_dir.exists() or (not ans_dir.is_dir()) or ans_dir.is_symlink():
            service.delete(self.BUILD_CACHE_NAMESPACE, cache_key)
            return ""
        return cached_build_id

    def run_build(
        self,
        problem: str,
        username: str,
        commit: str | None = None,
        ref: str | None = None,
        *,
        prefer_local_solve_backend: bool = False,
        sample_only: bool = False,
    ) -> str:
        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(ctx["workspace"]["path"])
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        source_commit = ""
        source_ref = ref or commit or ctx["workspace"].get("branch") or "main"
        resolved_commit_override = ""
        generation_params_digest = ""
        toolchain_cmd_digest = self._toolchain_cmd_digest() or "unknown"
        use_build_result_cache = False
        cache_key: dict[str, object] | None = None
        cache_key_hash = ""
        inflight_owner = False
        inflight_snapshot: Path | None = None
        try:
            if commit:
                try:
                    resolved_commit_override = self.workspace_service.resolve_commit(workspace, commit)
                except Exception:
                    resolved_commit_override = ""
                if resolved_commit_override:
                    source_commit = resolved_commit_override
                    source_ref = ref or commit
                    inflight_snapshot = self.workspace_service.create_snapshot(workspace, source_commit)
                    try:
                        generation_params_digest = self._generation_params_digest(
                            inflight_snapshot,
                            sample_only=bool(sample_only),
                        )
                    except Exception:
                        generation_params_digest = ""
            else:
                with self.workspace_service.workspace_lock(workspace):
                    status = self.workspace_service.read_workspace_status(workspace)
                    workspace_dirty = bool(status.get("dirty"))
                    workspace_head = str(status.get("head_commit") or "").strip()
                    workspace_branch = str(status.get("branch") or "").strip()
                    if not workspace_head:
                        workspace_head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
                    if workspace_branch and (not ref):
                        source_ref = workspace_branch
                if workspace_head and (not workspace_dirty):
                    source_commit = workspace_head
                    try:
                        generation_params_digest = self._generation_params_digest(
                            workspace,
                            sample_only=bool(sample_only),
                        )
                    except Exception:
                        generation_params_digest = ""
                elif not workspace_dirty:
                    try:
                        generation_params_digest = self._generation_params_digest(
                            workspace,
                            sample_only=bool(sample_only),
                        )
                    except Exception:
                        generation_params_digest = ""
                    if generation_params_digest:
                        source_commit = f"workspace:{generation_params_digest}"
        finally:
            if inflight_snapshot is not None:
                shutil.rmtree(inflight_snapshot.parent, ignore_errors=True)

        if source_commit and generation_params_digest:
            use_build_result_cache = True
            cache_key = self._build_cache_key(
                problem_id=problem_id,
                workspace_id=workspace_id,
                source_commit=str(source_commit or "").strip(),
                source_ref=str(source_ref or "").strip(),
                generation_params_digest=str(generation_params_digest or "").strip().lower(),
                toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
                sample_only=bool(sample_only),
            )
            cached_build_id = self._cached_build_id_for_source(
                problem_slug=problem,
                problem_id=problem_id,
                workspace_id=workspace_id,
                source_commit=str(source_commit or "").strip(),
                source_ref=str(source_ref or "").strip(),
                generation_params_digest=str(generation_params_digest or "").strip().lower(),
                toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
                sample_only=bool(sample_only),
            )
            if cached_build_id:
                return cached_build_id
            cache_key_hash = self._build_cache_key_hash(cache_key)

        build_ref_key = (
            cache_key
            if isinstance(cache_key, dict)
            else self._build_cache_key(
                problem_id=problem_id,
                workspace_id=workspace_id,
                source_commit=str(source_commit or "").strip(),
                source_ref=str(source_ref or "").strip(),
                generation_params_digest=str(generation_params_digest or "").strip().lower(),
                toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
                sample_only=bool(sample_only),
            )
        )
        build_ref = self._build_ref_from_cache_key_hash(self._build_cache_key_hash(build_ref_key))
        build_id = f"b-{uuid.uuid4().hex[:12]}"
        if cache_key is not None:
            existing_build_id = ""
            with self._build_inflight_lock:
                existing_build_id = str(self._build_inflight.get(cache_key_hash) or "").strip()
                if not existing_build_id:
                    self._build_inflight[cache_key_hash] = build_id
                    inflight_owner = True
            if existing_build_id:
                status = self._wait_build_terminal_status(existing_build_id, self.BUILD_JOIN_WAIT_TIMEOUT_SEC)
                if status == "ok":
                    return existing_build_id
                if status in {"failed", "cancelled"}:
                    raise RuntimeError("same-configuration build already failed; check logs and retry")
                raise RuntimeError("same-configuration build is still running; refresh later")

        artifact_paths = self._build_paths(problem, build_ref)
        logs_dir = artifact_paths.logs
        bin_dir = artifact_paths.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        self.db.execute(
            "INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [build_id, build_ref, problem_id, workspace_id, source_commit, source_ref, "running", str(artifact_paths.root), now_iso()],
        )

        steps: list[dict] = []
        toolchain_digest = "unknown"
        seed = random.randint(1, 10**9)
        random.seed(seed)
        diagnostics: list[dict] = []
        build_cfg: dict = {}
        tests_spec_entries: list[dict] | None = None
        tests_spec_runtime: list[dict] = []
        custom_sample_rows_by_test: dict[str, dict[str, object]] = {}
        current_step = "compile"
        failing_test: str | None = None
        snapshot: Path | None = None
        final_status = "running"

        try:
            if commit:
                source_commit = resolved_commit_override or self.workspace_service.resolve_commit(workspace, commit)
                source_ref = ref or commit
                self.db.execute("UPDATE builds SET source_commit=?, source_ref=? WHERE id=?", [source_commit, source_ref, build_id])
                snapshot = self.workspace_service.create_snapshot(workspace, source_commit)
            else:
                with self.workspace_service.workspace_lock(workspace):
                    status = self.workspace_service.read_workspace_status(workspace)
                    source_commit = str(status.get("head_commit") or "").strip()
                    branch = str(status.get("branch") or "").strip()
                    dirty = bool(status.get("dirty"))
                    if not source_commit:
                        source_commit = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
                    if (not source_commit) and (not dirty):
                        try:
                            synthetic_digest = self._generation_params_digest(
                                workspace,
                                sample_only=bool(sample_only),
                            )
                        except Exception:
                            synthetic_digest = ""
                        if synthetic_digest:
                            source_commit = f"workspace:{synthetic_digest}"
                    if branch:
                        source_ref = ref or branch
                    self.db.execute("UPDATE builds SET source_commit=?, source_ref=? WHERE id=?", [source_commit, source_ref, build_id])
                    snapshot = self.workspace_service.create_snapshot(
                        workspace,
                        None,
                        workspace_head=source_commit,
                        workspace_dirty=dirty,
                    )

            build_cfg = self._load_build_config(snapshot)
            runtime_cfg = self._load_problem_runtime_config(snapshot)
            problem_mode = self._normalize_problem_mode(runtime_cfg.get("mode"), "pass-fail")
            interactive_mode = problem_mode == "interactive"
            time_limit_ms = int(runtime_cfg.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS))
            run_timeout_ms = self._effective_run_timeout_ms(time_limit_ms, mode=problem_mode)
            run_timeout_sec = self._effective_run_timeout_sec(run_timeout_ms)
            build_solve_timeout_sec = max(1, (max(1, int(time_limit_ms)) + 999) // 1000)
            try:
                snapshot_resolved = snapshot.resolve()
            except OSError:
                snapshot_resolved = None
            include_dirs = [snapshot / "third_party/testlib"]
            generator_targets: list[tuple[str, Path | None, Path]] = []
            tests_spec_entries = self._load_tests_spec(snapshot)
            if tests_spec_entries is not None:
                tests_spec_runtime, tests_spec_generators = self._prepare_tests_spec_runtime(
                    snapshot,
                    tests_spec_entries,
                    bin_dir,
                )
                generator_targets.extend(tests_spec_generators)
            else:
                configured_generators = [str(x) for x in build_cfg.get("generator_sources", []) if str(x).strip()]
                if configured_generators:
                    for idx, rel in enumerate(configured_generators, start=1):
                        generator_targets.append(
                            (
                                f"generator_{idx}",
                                self._resolve_source(snapshot, rel, snapshot_resolved=snapshot_resolved),
                                bin_dir / f"generator_{idx}",
                            )
                        )
                else:
                    generator_targets.append(("generator", None, bin_dir / "generator"))

            accepted_rel = str(build_cfg.get("accepted_solution_source") or "").strip()
            if not accepted_rel:
                raise RuntimeError("accepted solution source is required")
            if not accepted_rel.startswith("solutions/"):
                raise RuntimeError("accepted solution source must be under solutions/")
            if Path(accepted_rel).suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
                raise RuntimeError("accepted solution source must be .cpp/.cc/.cxx/.c++/.py/.java")
            accepted_src = self._resolve_source(
                snapshot,
                accepted_rel,
                snapshot_resolved=snapshot_resolved,
            )

            compile_targets = [
                *generator_targets,
                (
                    "validator",
                    self._select_source(
                        snapshot,
                        build_cfg,
                        "validator_source",
                        "validators",
                        snapshot_resolved=snapshot_resolved,
                    ),
                    bin_dir / "validator",
                ),
                (
                    "checker",
                    self._select_checker_source(snapshot, build_cfg, snapshot_resolved=snapshot_resolved),
                    bin_dir / "checker",
                ),
                (
                    "interactor",
                    self._select_source(
                        snapshot,
                        build_cfg,
                        "interactor_source",
                        "interactors",
                        snapshot_resolved=snapshot_resolved,
                    ),
                    bin_dir / "interactor",
                ),
                ("accepted_solution", accepted_src, bin_dir / "accepted_solution"),
            ]

            compile_plan = [(name, source, output) for name, source, output in compile_targets if source is not None]
            compile_plan_cpp = [
                (name, source, output)
                for name, source, output in compile_plan
                if (name != "accepted_solution") and (source.suffix.lower() in CPP_EXTENSIONS)
            ]
            compile_plan_non_cpp = [
                (name, source, output)
                for name, source, output in compile_plan
                if (name != "accepted_solution") and (source.suffix.lower() not in CPP_EXTENSIONS)
            ]
            compile_jobs = self._effective_compile_jobs(build_cfg.get("compile_jobs", 0), len(compile_plan_cpp))
            compile_results: dict[str, tuple[bool, str, str, str]] = {}

            def _compile_cpp_target(name: str, source: Path, output: Path) -> tuple[bool, str, str, str]:
                effective_include_dirs = list(include_dirs)
                effective_cxxflags: list[str] | None = None
                # Checker/interactor verdict semantics must stay aligned with DOMjudge-style
                # testlib exit codes (OK=42/WA=43), independent of imported testlib variants.
                if name in {"checker", "interactor"}:
                    effective_cxxflags = [*CHECKER_TESTLIB_EXIT_CXXFLAGS]
                    if name == "checker":
                        try:
                            source.resolve().relative_to(STANDARD_CHECKER_ROOT)
                            upstream_testlib = STANDARD_CHECKER_ROOT.parent
                            effective_include_dirs = [upstream_testlib, *effective_include_dirs]
                        except Exception:
                            pass
                deduped_include_dirs: list[Path] = []
                seen_dirs: set[str] = set()
                for inc in effective_include_dirs:
                    key = str(inc.resolve())
                    if key in seen_dirs:
                        continue
                    seen_dirs.add(key)
                    deduped_include_dirs.append(inc)
                return self.toolchain.compile_cpp(
                    source,
                    output,
                    deduped_include_dirs,
                    [snapshot],
                    cxxflags=effective_cxxflags,
                )

            if compile_plan_cpp:
                with ThreadPoolExecutor(max_workers=compile_jobs) as pool:
                    future_map = {
                        pool.submit(_compile_cpp_target, name, source, output): name
                        for name, source, output in compile_plan_cpp
                    }
                    for future in as_completed(future_map):
                        name = future_map[future]
                        compile_results[name] = future.result()
            for name, source, output in compile_plan_non_cpp:
                compile_results[name] = self.toolchain.compile_program(
                    source,
                    output,
                    include_dirs,
                    path_roots=[snapshot],
                )
            accepted_target = next((row for row in compile_targets if row[0] == "accepted_solution"), None)
            if accepted_target is not None:
                _accepted_name, accepted_source, accepted_output = accepted_target
                if accepted_source is not None:
                    compile_results["accepted_solution"] = self.toolchain.compile_program(
                        accepted_source,
                        accepted_output,
                        include_dirs,
                        path_roots=[snapshot],
                    )

            compiled_bins: dict[str, Path] = {}
            compile_log_path = logs_dir / "compile.log"
            with compile_log_path.open("w", encoding="utf-8") as clog:
                clog.write(f"compile_jobs={compile_jobs}\n")
                for name, source, output in compile_targets:
                    if source is None:
                        clog.write(f"[{name}] missing source\n\n")
                        continue
                    ok, out, err, toolchain_digest = compile_results[name]
                    clog.write(f"[{name}] source={source}\n")
                    diagnostics.extend(
                        self._append_compile_streams(
                            clog,
                            snapshot,
                            out,
                            err,
                        )
                    )
                    clog.write("\n")
                    if not ok:
                        raise RuntimeError(f"compile failed: {name}")
                    compiled_bins[name] = output

            has_generator_compiled = any(name.startswith("generator") for name in compiled_bins)
            if build_cfg.get("require_generator") and not has_generator_compiled:
                raise RuntimeError("generator is required by config/build.json but missing")
            if build_cfg.get("require_validator", True) and "validator" not in compiled_bins:
                raise RuntimeError("validator source is required")
            if build_cfg.get("require_checker", True) and "checker" not in compiled_bins:
                raise RuntimeError("checker source is required")
            if "accepted_solution" not in compiled_bins:
                raise RuntimeError("accepted solution source is required")
            if interactive_mode and "interactor" not in compiled_bins:
                raise RuntimeError("interactor source is required for interactive mode")

            steps.append({"step": "compile", "status": "ok", "log": "logs/compile.log"})

            current_step = "generate"
            test_files: list[Path] = []
            tests_meta: list[dict] = []
            source_answer_by_test: dict[str, Path] = {}
            counter = 1
            manual_count = 0
            generated_count = 0
            generate_log_path = logs_dir / "generate.log"
            with generate_log_path.open("w", encoding="utf-8") as glog:
                if tests_spec_entries is not None:
                    glog.write("tests_source=tests/spec.json\n")
                    for row in tests_spec_runtime:
                        kind = str(row.get("kind") or "")
                        test_id = str(row.get("id") or "").strip()
                        is_sample = bool(row.get("sample"))
                        if sample_only and (not is_sample):
                            continue
                        custom_sample_input = str(row.get("sample_input") or "")
                        custom_sample_output = str(row.get("sample_output") or "")
                        custom_sample_output_validate = bool(row.get("sample_output_validate", True))
                        file_index = int(row.get("index") or counter) if sample_only else counter
                        dst = artifact_paths.tests / f"{file_index:03d}.in"
                        if kind == "manual":
                            input_text = str(row.get("input") or "")
                            dst.write_text(input_text, encoding="utf-8")
                            test_files.append(dst)
                            manual_count += 1
                            tests_meta.append(
                                {
                                    "index": file_index,
                                    "kind": "manual",
                                    "id": test_id,
                                    "sample": is_sample,
                                    "sample_input_custom": bool(custom_sample_input),
                                    "sample_output_custom": bool(custom_sample_output),
                                    "sample_output_validate": bool(custom_sample_output_validate),
                                    "desc": f"manual {test_id}" if test_id else "manual",
                                    "source": str(row.get("source_rel") or "tests/spec.json"),
                                }
                            )
                            if is_sample and custom_sample_output:
                                custom_sample_rows_by_test[dst.name] = {
                                    "id": test_id,
                                    "sample_input": custom_sample_input,
                                    "sample_output": custom_sample_output,
                                    "sample_output_validate": custom_sample_output_validate,
                                }
                            answer_source = self._tests_spec_answer_source(snapshot, test_id)
                            if answer_source is not None:
                                source_answer_by_test[dst.name] = answer_source
                            glog.write(f"manual id={test_id} index={row.get('index')} -> {dst.name}\n")
                            if not sample_only:
                                counter += 1
                            continue

                        if kind != "gen":
                            raise RuntimeError(f"invalid test kind at tests/spec.json entry {row.get('index')}")
                        target_name = str(row.get("target_name") or "")
                        gen_bin = compiled_bins.get(target_name)
                        if gen_bin is None:
                            raise RuntimeError(
                                f"generator source is required for tests/spec.json entry {row.get('index')}"
                            )
                        args = [str(x) for x in row.get("args") or []]
                        rc, _out, err, timed_out = self._sandbox_exec(
                            [str(gen_bin), *args],
                            timeout_sec=30,
                            stdout_path=dst,
                        )
                        glog.write(
                            f"gen id={test_id} index={row.get('index')} source={row.get('source_rel')} cmd={row.get('cmd')} rc={rc}\n{err}\n"
                        )
                        if timed_out or rc != 0:
                            dst.unlink(missing_ok=True)
                            failing_test = dst.name
                            raise RuntimeError(
                                f"generator failed on tests/spec.json entry {row.get('index')} (id={test_id})"
                            )
                        test_files.append(dst)
                        generated_count += 1
                        desc = str(row.get("cmd") or "").strip() or "gen"
                        tests_meta.append(
                            {
                                "index": file_index,
                                "kind": "gen",
                                "id": test_id,
                                "sample": is_sample,
                                "sample_input_custom": bool(custom_sample_input),
                                "sample_output_custom": bool(custom_sample_output),
                                "sample_output_validate": bool(custom_sample_output_validate),
                                "desc": desc,
                                "command": str(row.get("cmd") or "").strip(),
                                "source": str(row.get("source_rel") or "").strip(),
                                "payload_source": str(row.get("payload_rel") or "").strip(),
                            }
                        )
                        if is_sample and custom_sample_output:
                            custom_sample_rows_by_test[dst.name] = {
                                "id": test_id,
                                "sample_input": custom_sample_input,
                                "sample_output": custom_sample_output,
                                "sample_output_validate": custom_sample_output_validate,
                            }
                        answer_source = self._tests_spec_answer_source(snapshot, test_id)
                        if answer_source is not None:
                            source_answer_by_test[dst.name] = answer_source
                        if not sample_only:
                            counter += 1
                else:
                    tests = self._manual_test_sources(snapshot)
                    for t in tests:
                        dst = artifact_paths.tests / f"{counter:03d}.in"
                        shutil.copy2(t, dst)
                        test_files.append(dst)
                        manual_count += 1
                        try:
                            source_rel = str(t.relative_to(snapshot)).replace("\\", "/")
                        except ValueError:
                            source_rel = str(t.name)
                        tests_meta.append(
                            {
                                "index": counter,
                                "kind": "manual",
                                "desc": f"manual: {source_rel}",
                                "source": source_rel,
                            }
                        )
                        counter += 1

                    generator_execs: list[tuple[int, str, Path]] = []
                    for gen_index, (name, source, _target) in enumerate(generator_targets, start=1):
                        gen_bin = compiled_bins.get(name)
                        if gen_bin is None:
                            continue
                        if source is None:
                            source_label = f"generator:{name}"
                        else:
                            try:
                                source_label = str(source.relative_to(snapshot)).replace("\\", "/")
                            except ValueError:
                                source_label = str(source)
                        generator_execs.append((gen_index, source_label, gen_bin))

                    if generator_execs:
                        runs = int(build_cfg.get("generator_runs", 3))
                        generator_args = [str(x) for x in build_cfg.get("generator_args", [])]
                        for gen_index, source_label, gen in generator_execs:
                            for i in range(runs):
                                dst = artifact_paths.tests / f"{counter:03d}.in"
                                rc, _out, err, timed_out = self._sandbox_exec(
                                    [str(gen), *generator_args],
                                    timeout_sec=30,
                                    stdout_path=dst,
                                )
                                glog.write(
                                    f"generator={gen_index} source={source_label} case={i + 1} rc={rc}\n{err}\n"
                                )
                                if timed_out or rc != 0:
                                    dst.unlink(missing_ok=True)
                                    failing_test = dst.name
                                    raise RuntimeError(f"generator failed on generator={gen_index} case={i + 1}")
                                test_files.append(dst)
                                generated_count += 1
                                desc = f"gen: {source_label}"
                                if generator_args:
                                    desc = f"{desc} {' '.join(generator_args)}"
                                tests_meta.append(
                                    {
                                        "index": counter,
                                        "kind": "gen",
                                        "desc": desc,
                                        "source": source_label,
                                    }
                                )
                                counter += 1
                glog.write(f"manual_tests={manual_count}\n")
                glog.write(f"generated_tests={generated_count}\n")
                glog.write(f"total_tests={len(test_files)}\n")
            if not test_files:
                if tests_spec_entries is not None:
                    if sample_only:
                        raise RuntimeError("no sample tests were generated from tests/spec.json")
                    raise RuntimeError("no tests were generated from tests/spec.json")
                raise RuntimeError("no tests were generated (manual + generator)")
            (logs_dir / "tests_meta.json").write_text(json.dumps(tests_meta, indent=2), encoding="utf-8")
            steps.append({"step": "generate", "status": "ok", "log": "logs/generate.log"})

            current_step = "validate"
            validator = compiled_bins["validator"]
            validator_args = [str(x) for x in build_cfg.get("validator_args", [])]
            validate_jobs = self._effective_compile_jobs(build_cfg.get("validate_jobs", 0), len(test_files))
            validate_results: dict[str, dict[str, object]] = {}
            validate_root = logs_dir / "validate_runs"
            validate_root.mkdir(parents=True, exist_ok=True)
            with (logs_dir / "validate.log").open("w", encoding="utf-8") as vlog:
                vlog.write(f"validate_jobs={validate_jobs}\n")

                def _validate_case(test_path: Path, test_cwd: Path) -> tuple[int, str, str, bool]:
                    return self._sandbox_exec(
                        [str(validator), *validator_args],
                        timeout_sec=30,
                        stdin_path=test_path,
                        cwd=test_cwd,
                    )

                with ThreadPoolExecutor(max_workers=validate_jobs) as pool:
                    future_map = {}
                    for t in test_files:
                        test_cwd = validate_root / t.stem
                        test_cwd.mkdir(parents=True, exist_ok=True)
                        future_map[pool.submit(_validate_case, t, test_cwd)] = t
                    for future in as_completed(future_map):
                        t = future_map[future]
                        try:
                            rc, out, err, timed_out = future.result()
                            validate_results[t.name] = {
                                "rc": int(rc),
                                "worker_error": "",
                                "timed_out": bool(timed_out),
                                "stderr": str(err or ""),
                            }
                            timeout_note = " timed_out=1" if timed_out else ""
                            vlog.write(f"{t.name}: args={validator_args} rc={rc}{timeout_note}\n{out}{err}\n")
                        except Exception as exc:
                            validate_results[t.name] = {
                                "rc": -1,
                                "worker_error": str(exc),
                                "timed_out": False,
                                "stderr": "",
                            }
                            vlog.write(f"{t.name}: args={validator_args} rc=-1\n{exc}\n")

                for t in test_files:
                    failing_test = t.name
                    row = validate_results[t.name]
                    rc = int(row.get("rc") or 0)
                    worker_error = str(row.get("worker_error") or "").strip()
                    timed_out = bool(row.get("timed_out"))
                    stderr_text = self._compact_single_line(str(row.get("stderr") or ""), 220)
                    if worker_error:
                        raise RuntimeError(f"validator failed on {t.name}: {worker_error}")
                    if not self._validator_ok(rc):
                        if timed_out:
                            base_msg = f"validator failed on {t.name} (rc={rc}, timed_out=1)"
                        else:
                            base_msg = f"validator failed on {t.name} (rc={rc})"
                        if stderr_text:
                            raise RuntimeError(f"{base_msg}: stderr: {stderr_text}")
                        raise RuntimeError(base_msg)
            steps.append({"step": "validate", "status": "ok", "log": "logs/validate.log"})

            current_step = "solve"
            accepted = compiled_bins["accepted_solution"]
            solve_jobs = self._effective_compile_jobs(build_cfg.get("solve_jobs", 0), len(test_files))
            custom_sample_output_validate_total = 0
            custom_sample_output_validate_checked = 0
            multi_pass_interactive_mode = problem_mode == "multi-pass" and ("interactor" in compiled_bins)
            solve_results: dict[str, dict[str, object]] = {}
            solve_backend = "local-sandbox"
            use_judge_backend = (not bool(prefer_local_solve_backend)) and self._can_use_judge_backend_for_solve()
            if use_judge_backend:
                solve_backend = self._active_solve_backend_name()
            if (interactive_mode or multi_pass_interactive_mode) and (not use_judge_backend):
                # Local interactive solving spawns paired processes per test; serial execution
                # keeps wall-time predictable and avoids scheduler-induced false TLE.
                solve_jobs = 1
            with (logs_dir / "solve.log").open("w", encoding="utf-8") as slog:
                slog.write(f"solve_jobs={solve_jobs}\n")
                slog.write(f"solve_backend={solve_backend}\n")
                slog.write(f"build_solve_timeout_sec={build_solve_timeout_sec}\n")

                def _solve_failure_message(test_name: str, row: dict[str, object]) -> str:
                    rc = int(row.get("rc") or 0)
                    worker_error = str(row.get("worker_error") or "").strip()
                    timed_out = bool(row.get("timed_out"))
                    stderr_text = self._compact_single_line(str(row.get("stderr") or ""), 220)
                    if worker_error:
                        return f"accepted solution failed on {test_name}: {worker_error}"
                    if rc == 0:
                        return ""
                    if timed_out:
                        base_msg = f"accepted solution failed on {test_name} (rc={rc}, timed_out=1)"
                    else:
                        base_msg = f"accepted solution failed on {test_name} (rc={rc})"
                    if stderr_text:
                        return f"{base_msg}: stderr: {stderr_text}"
                    return base_msg

                if use_judge_backend:
                    solve_results = self._solve_with_judge_backend(
                        problem=problem,
                        username=username,
                        build_id=build_id,
                        accepted_source_rel=accepted_rel,
                        mode=problem_mode,
                        test_files=test_files,
                        ans_dir=artifact_paths.ans,
                        solve_jobs=solve_jobs,
                        source_answer_by_test=source_answer_by_test,
                    )
                    for t in test_files:
                        row = solve_results.get(t.name) or self._solve_result_error("missing judge solve result")
                        solve_results[t.name] = row
                        rc = int(row.get("rc") or 0)
                        timed_out = bool(row.get("timed_out"))
                        err = str(row.get("worker_error") or row.get("stderr") or "")
                        timeout_note = " timed_out=1" if timed_out else ""
                        slog.write(f"{t.name}: rc={rc}{timeout_note}\n{err}\n")
                        fail_msg = _solve_failure_message(t.name, row)
                        if fail_msg:
                            failing_test = t.name
                            slog.write(f"early_stop={t.name}\n")
                            raise RuntimeError(fail_msg)
                else:
                    if self._active_solve_backend_name() == "domjudge-judgehost":
                        slog.write("judge backend unavailable for build solve; fallback=local-sandbox\n")

                    if interactive_mode:
                        interactor = compiled_bins["interactor"]
                        solve_interactive_root = logs_dir / "solve_interactive"
                        solve_interactive_root.mkdir(parents=True, exist_ok=True)
                        interactive_case_timeout_sec = build_solve_timeout_sec

                        def _solve_case(test_path: Path, out_path: Path) -> tuple[int, str, bool]:
                            answer_source = source_answer_by_test.get(test_path.name)
                            work_dir = solve_interactive_root / test_path.stem
                            work_dir.mkdir(parents=True, exist_ok=True)
                            return self._solve_interactive_case(
                                interactor,
                                accepted,
                                test_path,
                                out_path,
                                answer_source=answer_source,
                                timeout_sec=interactive_case_timeout_sec,
                                cwd=work_dir,
                            )
                    elif multi_pass_interactive_mode:
                        def _solve_case(test_path: Path, out_path: Path) -> tuple[int, str, bool]:
                            # Polygon package imports may already provide canonical answer payloads
                            # for multi-pass+interactor problems. Preserve them as build answers.
                            answer_source = source_answer_by_test.get(test_path.name)
                            if answer_source is not None and answer_source.exists():
                                shutil.copy2(answer_source, out_path)
                                return (0, "", False)
                            rc, _out, err, timed_out = self._sandbox_exec(
                                [str(accepted)],
                                timeout_sec=build_solve_timeout_sec,
                                stdin_path=test_path,
                                stdout_path=out_path,
                            )
                            return (rc, str(err or ""), bool(timed_out))
                    else:
                        def _solve_case(test_path: Path, out_path: Path) -> tuple[int, str, bool]:
                            rc, _out, err, timed_out = self._sandbox_exec(
                                [str(accepted)],
                                timeout_sec=build_solve_timeout_sec,
                                stdin_path=test_path,
                                stdout_path=out_path,
                            )
                            return (rc, str(err or ""), bool(timed_out))

                    if solve_jobs <= 1:
                        for t in test_files:
                            out = artifact_paths.ans / t.name.replace(".in", ".ans")
                            try:
                                rc, err, timed_out = _solve_case(t, out)
                                row = {
                                    "rc": int(rc),
                                    "worker_error": "",
                                    "timed_out": bool(timed_out),
                                    "stderr": str(err or ""),
                                }
                                solve_results[t.name] = row
                                timeout_note = " timed_out=1" if timed_out else ""
                                slog.write(f"{t.name}: rc={rc}{timeout_note}\n{err}\n")
                            except Exception as exc:
                                row = {
                                    "rc": -1,
                                    "worker_error": str(exc),
                                    "timed_out": False,
                                    "stderr": "",
                                }
                                solve_results[t.name] = row
                                slog.write(f"{t.name}: rc=-1\n{exc}\n")
                            fail_msg = _solve_failure_message(t.name, row)
                            if fail_msg:
                                failing_test = t.name
                                slog.write(f"early_stop={t.name}\n")
                                raise RuntimeError(fail_msg)
                    else:
                        pool = ThreadPoolExecutor(max_workers=solve_jobs)
                        pool_shutdown = False
                        try:
                            inflight: dict[object, int] = {}
                            finished_rows: dict[int, dict[str, object]] = {}
                            next_submit_idx = 0
                            next_check_idx = 0

                            def _submit_one(idx: int) -> None:
                                t = test_files[idx]
                                out = artifact_paths.ans / t.name.replace(".in", ".ans")
                                inflight[pool.submit(_solve_case, t, out)] = idx

                            while (next_submit_idx < len(test_files)) and (len(inflight) < solve_jobs):
                                _submit_one(next_submit_idx)
                                next_submit_idx += 1

                            while inflight:
                                done, _ = wait(set(inflight.keys()), return_when=FIRST_COMPLETED)
                                for future in done:
                                    idx = inflight.pop(future)
                                    t = test_files[idx]
                                    try:
                                        rc, err, timed_out = future.result()
                                        row = {
                                            "rc": int(rc),
                                            "worker_error": "",
                                            "timed_out": bool(timed_out),
                                            "stderr": str(err or ""),
                                        }
                                        solve_results[t.name] = row
                                        timeout_note = " timed_out=1" if timed_out else ""
                                        slog.write(f"{t.name}: rc={rc}{timeout_note}\n{err}\n")
                                    except Exception as exc:
                                        row = {
                                            "rc": -1,
                                            "worker_error": str(exc),
                                            "timed_out": False,
                                            "stderr": "",
                                        }
                                        solve_results[t.name] = row
                                        slog.write(f"{t.name}: rc=-1\n{exc}\n")
                                    finished_rows[idx] = row

                                while next_check_idx in finished_rows:
                                    t = test_files[next_check_idx]
                                    row = finished_rows.pop(next_check_idx)
                                    fail_msg = _solve_failure_message(t.name, row)
                                    if fail_msg:
                                        failing_test = t.name
                                        for pending in inflight:
                                            pending.cancel()
                                        pool.shutdown(wait=False, cancel_futures=True)
                                        pool_shutdown = True
                                        slog.write(f"early_stop={t.name}\n")
                                        raise RuntimeError(fail_msg)
                                    next_check_idx += 1
                                    if next_submit_idx < len(test_files):
                                        _submit_one(next_submit_idx)
                                        next_submit_idx += 1
                        finally:
                            if not pool_shutdown:
                                pool.shutdown(wait=True, cancel_futures=False)

                for t in test_files:
                    failing_test = t.name
                    row = solve_results[t.name]
                    rc = int(row.get("rc") or 0)
                    worker_error = str(row.get("worker_error") or "").strip()
                    timed_out = bool(row.get("timed_out"))
                    stderr_text = self._compact_single_line(str(row.get("stderr") or ""), 220)
                    if worker_error:
                        raise RuntimeError(f"accepted solution failed on {t.name}: {worker_error}")
                    if rc != 0:
                        if timed_out:
                            base_msg = f"accepted solution failed on {t.name} (rc={rc}, timed_out=1)"
                        else:
                            base_msg = f"accepted solution failed on {t.name} (rc={rc})"
                        if stderr_text:
                            raise RuntimeError(f"{base_msg}: stderr: {stderr_text}")
                        raise RuntimeError(base_msg)

                if custom_sample_rows_by_test:
                    custom_validate_log = logs_dir / "sample_output_validate.log"
                    custom_validate_root = logs_dir / "sample_output_validate_runs"
                    custom_validate_root.mkdir(parents=True, exist_ok=True)
                    checker = compiled_bins.get("checker")
                    checker_args = [str(x) for x in build_cfg.get("checker_args", [])]
                    with custom_validate_log.open("w", encoding="utf-8") as cvlog:
                        for t in test_files:
                            row = custom_sample_rows_by_test.get(t.name)
                            if not isinstance(row, dict):
                                continue
                            if not bool(row.get("sample_output_validate", True)):
                                cvlog.write(f"{t.name}: skipped validate=0\n")
                                continue
                            if problem_mode != "pass-fail":
                                cvlog.write(f"{t.name}: skipped mode={problem_mode}\n")
                                continue
                            custom_sample_output_validate_total += 1
                            failing_test = t.name
                            sample_id = str(row.get("id") or "").strip()
                            custom_output_text = str(row.get("sample_output") or "")
                            if not custom_output_text:
                                cvlog.write(f"{t.name}: skipped empty custom_output\n")
                                continue
                            if checker is None:
                                raise RuntimeError(
                                    f"sample custom output validation requires checker source (test id {sample_id or '?'})"
                                )

                            case_root = custom_validate_root / t.stem
                            case_root.mkdir(parents=True, exist_ok=True)
                            test_input_for_check = t
                            expected_answer_for_check = artifact_paths.ans / t.name.replace(".in", ".ans")
                            custom_input_text = str(row.get("sample_input") or "")
                            if custom_input_text:
                                custom_input_file = case_root / "sample.in"
                                custom_input_file.write_text(custom_input_text, encoding="utf-8")
                                test_input_for_check = custom_input_file
                                expected_answer_for_check = case_root / "sample.ans"
                                if problem_mode != "pass-fail":
                                    raise RuntimeError(
                                        f"sample custom output validation does not support custom sample input in mode {problem_mode} (test id {sample_id or '?'})"
                                    )
                                rc_expected, _out_expected, err_expected, timed_out_expected = self._sandbox_exec(
                                    [str(accepted)],
                                    timeout_sec=30,
                                    stdin_path=custom_input_file,
                                    stdout_path=expected_answer_for_check,
                                )
                                timeout_note = " timed_out=1" if timed_out_expected else ""
                                cvlog.write(
                                    f"{t.name}: generate_expected rc={rc_expected}{timeout_note}\n{err_expected}\n"
                                )
                                if timed_out_expected or int(rc_expected) != 0:
                                    stderr_text = self._compact_single_line(str(err_expected or ""), 220)
                                    if timed_out_expected:
                                        base_msg = f"sample custom output validation failed on {t.name} (id={sample_id or '?'}) while generating expected answer (rc={rc_expected}, timed_out=1)"
                                    else:
                                        base_msg = f"sample custom output validation failed on {t.name} (id={sample_id or '?'}) while generating expected answer (rc={rc_expected})"
                                    if stderr_text:
                                        raise RuntimeError(f"{base_msg}: stderr: {stderr_text}")
                                    raise RuntimeError(base_msg)

                            if not expected_answer_for_check.exists() or (not expected_answer_for_check.is_file()):
                                raise RuntimeError(
                                    f"sample custom output validation failed on {t.name} (id={sample_id or '?'}) because expected answer is missing"
                                )

                            rc_checker, checker_timed_out, checker_message = self._run_checker_with_submission_output(
                                checker,
                                checker_args,
                                test_input_for_check,
                                expected_answer_for_check,
                                custom_output_text,
                                case_root,
                            )
                            timeout_note = " timed_out=1" if checker_timed_out else ""
                            cvlog.write(
                                f"{t.name}: validate rc={rc_checker}{timeout_note} id={sample_id or '-'}\n{checker_message}\n"
                            )
                            if checker_timed_out:
                                raise RuntimeError(
                                    f"sample custom output validation failed on {t.name} (id={sample_id or '?'}) because checker timed out"
                                )
                            if not self._checker_ok(rc_checker):
                                detail = checker_message or f"checker returned rc={rc_checker}, expected 42"
                                raise RuntimeError(
                                    f"sample custom output validation failed on {t.name} (id={sample_id or '?'}): {detail}"
                                )
                            custom_sample_output_validate_checked += 1
                    if custom_sample_output_validate_total > 0:
                        steps.append(
                            {
                                "step": "sample_output_validate",
                                "status": "ok",
                                "log": "logs/sample_output_validate.log",
                            }
                        )
            steps.append({"step": "solve", "status": "ok", "log": "logs/solve.log"})

            (logs_dir / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
            generation_params = {
                "tests_spec_enabled": tests_spec_entries is not None,
                "tests_spec_entries": len(tests_spec_runtime) if tests_spec_entries is not None else 0,
                "tests_spec_sample_custom_output_validate_total": custom_sample_output_validate_total,
                "tests_spec_sample_custom_output_validate_checked": custom_sample_output_validate_checked,
                "generator_runs": int(build_cfg.get("generator_runs", 3)),
                "compile_jobs": compile_jobs,
                "validate_jobs": int(build_cfg.get("validate_jobs", 0)),
                "validate_jobs_effective": validate_jobs,
                "solve_jobs": int(build_cfg.get("solve_jobs", 0)),
                "solve_jobs_effective": solve_jobs,
                "run_jobs": int(build_cfg.get("run_jobs", 0)),
                "mode": problem_mode,
                "sample_only": bool(sample_only),
                "build_ref": build_ref,
                "solve_backend": solve_backend,
                "time_limit_ms": time_limit_ms,
                "run_timeout_ms": run_timeout_ms,
                "run_timeout_sec": run_timeout_sec,
                "generator_sources": [str(x) for x in build_cfg.get("generator_sources", [])],
                "generator_args": [str(x) for x in build_cfg.get("generator_args", [])],
                "validator_args": [str(x) for x in build_cfg.get("validator_args", [])],
                "checker_args": [str(x) for x in build_cfg.get("checker_args", [])],
                "checker_standard": str(build_cfg.get("checker_standard", "")),
                "max_passes": int(build_cfg.get("max_passes", 16)),
                "sandbox_backend": self.sandbox.name,
                "sandbox_memory_mb": self.default_exec_memory_mb,
                "sandbox_process_limit": self.default_exec_process_limit,
                "sandbox_output_kb": self.default_exec_output_kb,
                "generation_params_digest": str(generation_params_digest or "").strip().lower(),
                "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
            }
            # Small runner-focused config sidecar avoids full manifest reads on run setup hot paths.
            (logs_dir / "run_config.json").write_text(json.dumps(generation_params, indent=2), encoding="utf-8")
            self.artifacts.write_manifest(
                artifact_paths,
                source_commit=source_commit,
                source_ref=source_ref,
                toolchain_digest=toolchain_digest,
                seed=seed,
                generation_params=generation_params,
                steps=steps,
            )

            self.db.execute(
                "UPDATE builds SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [
                    "ok",
                    self._summary_for_db(
                        {
                            "build_ref": build_ref,
                            "steps": steps,
                            "diagnostics": diagnostics,
                            "generation_params": generation_params,
                        }
                    ),
                    now_iso(),
                    build_id,
                ],
            )
            if use_build_result_cache and self._async_task_cache_service is not None and str(source_commit or "").strip():
                self._async_task_cache_service.put(
                    self.BUILD_CACHE_NAMESPACE,
                    cache_key
                    if isinstance(cache_key, dict)
                    else self._build_cache_key(
                        problem_id=problem_id,
                        workspace_id=workspace_id,
                        source_commit=str(source_commit or "").strip(),
                        source_ref=str(source_ref or "").strip(),
                        generation_params_digest=str(generation_params_digest or "").strip().lower(),
                        toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
                        sample_only=bool(sample_only),
                    ),
                    {"build_id": build_id},
                    tags={
                        "problem_id": str(problem_id),
                        "workspace_id": str(workspace_id),
                        "source_commit": str(source_commit or "").strip(),
                        "sample_only": "1" if sample_only else "0",
                    },
                )
            final_status = "ok"
        except Exception as exc:
            try:
                logs_dir.mkdir(parents=True, exist_ok=True)
                (logs_dir / "failure.log").write_text(str(exc), encoding="utf-8")
            except Exception:
                pass
            steps.append({"step": current_step, "status": "error", "log": "logs/failure.log"})
            self.db.execute(
                "UPDATE builds SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [
                    "failed",
                    self._summary_for_db(
                        {
                            "build_ref": build_ref,
                            "error": str(exc),
                            "failed_step": current_step,
                            "failed_test": failing_test,
                            "steps": steps,
                            "diagnostics": diagnostics,
                        }
                    ),
                    now_iso(),
                    build_id,
                ],
            )
            final_status = "failed"
        finally:
            if final_status != "running":
                self.db.execute(
                    "UPDATE workspaces SET recent_build_status=? WHERE id=?",
                    [final_status, workspace_id],
                )
            if snapshot is not None:
                shutil.rmtree(snapshot.parent, ignore_errors=True)
            if inflight_owner and cache_key_hash:
                with self._build_inflight_lock:
                    current = str(self._build_inflight.get(cache_key_hash) or "").strip()
                    if current == build_id:
                        self._build_inflight.pop(cache_key_hash, None)

        return build_id
