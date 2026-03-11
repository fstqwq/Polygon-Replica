from __future__ import annotations

import json
import os
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import IO

from app.db import DB, now_iso
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.fs.layout import FsManager
from app.service.sandbox.base import ExecSpec, SandboxBackend
from app.service.sandbox.native_backend import NativeSandboxBackend
from app.service.runtime.toolchain import ToolchainService
from app.service.repository.workspace import WorkspaceService
from .artifact import persist_compile_outputs, resolve_submission_source
from .checker import collect_diagnostics, run_checker, validator_style_verdict
from .failure import (
    append_fl_skip_tail_tests,
    build_failure_context,
    is_fl_verdict,
    synthesize_failure_tests,
    synthesized_fl_skip_test,
)
from .feedback import feedback_message_for_pass, files_equal
from .interactive import run_interactive_case, run_multi_pass_interactive_test
from .noninteractive import run_noninteractive_test
from .path_guard import build_path_guard_environment, ensure_path_guard_library
from .runtime import (
    coerce_int,
    effective_run_jobs,
    effective_run_timeout_ms,
    effective_run_timeout_sec,
    normalize_problem_mode,
    normalize_time_limit_ms,
    wall_time_slack_sec_for_mode,
)
from .safety import contains_symlink_component, is_safe_dir, is_safe_path_within, is_safe_regular_file, safe_top_level_suffix_names
from app.service.run.summary import (
    cap_run_test_feedback_files,
    compact_inline_error,
    summary_for_db,
)
from app.service.run.timing import cap_tle_time_ms, read_time_metrics

# Keep module-level `time` attribute for test patch target (`app.service.run.api.time.monotonic`).
_ = time


class Run:
    RUN_TIMEOUT_SENTINEL = -1_000_000_000
    ARTIFACT_CACHE_LIMIT = 256
    FEEDBACK_KEY_FILE_LIMIT = 256
    DB_SUMMARY_TESTS_LIMIT = 200
    DB_SUMMARY_DIAGNOSTICS_LIMIT = 200
    DB_SUMMARY_FEEDBACK_FILES_LIMIT = 32
    DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT = 4096
    RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS = 750
    RUN_PROGRESS_PERSIST_BATCH_UPDATES = 4
    DEFAULT_TIME_LIMIT_MS = 2000
    TIME_LIMIT_MIN_MS = 100
    TIME_LIMIT_MAX_MS = 30000
    RUN_TIMEOUT_MAX_SEC = 300
    SUBMISSION_CPP_CXXFLAGS = ["-O2", "-std=gnu++20", "-pipe", "-DDOMJUDGE"]
    PATH_GUARD_SOURCE = (Path(__file__).resolve().parent.parent / "sandbox" / "path_guard.c").resolve()

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        toolchain: ToolchainService,
        sandbox_backend: SandboxBackend | None = None,
        constants: RuntimeValues | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.toolchain = toolchain
        self.sandbox = sandbox_backend or NativeSandboxBackend()
        self.default_run_memory_mb = 1024
        self.default_run_process_limit = 64
        self.default_run_output_kb = 65536
        self.wall_time_slack_pass_fail_sec = 1
        self.wall_time_slack_multi_pass_sec = 15
        self.wall_time_slack_interactive_sec = 15
        self._run_config_cache: dict[str, dict[str, object]] = {}
        self._test_input_cache: dict[str, tuple[str, ...]] = {}
        self._answer_file_cache: dict[str, tuple[str, ...]] = {}
        self._answer_file_set_cache: dict[str, frozenset[str]] = {}
        self._test_input_meta_cache: dict[str, tuple[tuple[str, str], ...]] = {}
        self.fs_manager = FsManager(self.workspace_service.settings.artifacts_root, self.workspace_service.settings.run_root)
        self._cache_lock = threading.Lock()
        self._path_guard_lock = threading.Lock()
        self.apply_runtime_values(constants or build_runtime_values())

    def _coerce_int(self, raw: object, default: int, min_value: int, max_value: int) -> int:
        return coerce_int(raw, default, min_value, max_value)

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.default_run_memory_mb = self._coerce_int(
            values.get("RUN_EXEC_MEMORY_MB", 1024),
            default=1024,
            min_value=16,
            max_value=262144,
        )
        self.default_run_process_limit = self._coerce_int(
            values.get("RUN_EXEC_PROCESS_LIMIT", 64),
            default=64,
            min_value=1,
            max_value=4096,
        )
        self.default_run_output_kb = self._coerce_int(
            values.get("RUN_EXEC_OUTPUT_KB", 65536),
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
        with self._cache_lock:
            self._run_config_cache.clear()

    def _ensure_path_guard_library(self) -> Path | None:
        return ensure_path_guard_library(
            source=self.PATH_GUARD_SOURCE,
            cache_root=Path(self.workspace_service.settings.cache_root) / "runtime",
            lock=self._path_guard_lock,
        )

    def _path_guard_environment(
        self,
        *,
        base_env: dict[str, str] | None,
        deny_paths: list[Path] | tuple[Path, ...] | None,
        allow_paths: list[Path] | tuple[Path, ...] | None,
    ) -> dict[str, str] | None:
        return build_path_guard_environment(
            base_env=base_env,
            deny_paths=deny_paths,
            allow_paths=allow_paths,
            ensure_library=self._ensure_path_guard_library,
        )

    def _collect_diagnostics(self, workspace: Path | None, text: str) -> list[dict]:
        return collect_diagnostics(workspace, text)

    def _persist_compile_outputs(
        self,
        compile_log_file: Path,
        workspace: Path | None,
        stdout_text: str,
        stderr_text: str,
    ) -> list[dict]:
        return persist_compile_outputs(
            compile_log_file,
            workspace,
            stdout_text,
            stderr_text,
            collect_diagnostics=self._collect_diagnostics,
        )

    def _resolve_submission_source(self, workspace: Path, submission_path: str) -> Path:
        return resolve_submission_source(
            workspace,
            submission_path,
            contains_symlink_component=self._contains_symlink_component,
        )

    def _validator_style_verdict(self, rc: int) -> str:
        return validator_style_verdict(rc)

    def _run_checker(
        self,
        checker: Path,
        *,
        checker_args: list[str],
        test: Path,
        team_output: Path,
        answer: Path,
        pass_feedback_dir: Path,
        run_timeout_sec: int,
        run_memory_mb: int,
        run_process_limit: int,
        run_output_kb: int,
    ) -> tuple[object, str, bool]:
        return run_checker(
            self.sandbox,
            checker,
            checker_args=checker_args,
            test=test,
            team_output=team_output,
            answer=answer,
            pass_feedback_dir=pass_feedback_dir,
            run_timeout_sec=run_timeout_sec,
            run_memory_mb=run_memory_mb,
            run_process_limit=run_process_limit,
            run_output_kb=run_output_kb,
        )

    def _normalize_time_limit_ms(self, raw: object) -> int:
        return normalize_time_limit_ms(
            raw,
            default_ms=self.DEFAULT_TIME_LIMIT_MS,
            min_ms=self.TIME_LIMIT_MIN_MS,
            max_ms=self.TIME_LIMIT_MAX_MS,
        )

    def _normalize_problem_mode(self, raw: object, default: str = "pass-fail") -> str:
        return normalize_problem_mode(raw, default)

    def _wall_time_slack_sec_for_mode(self, mode: object) -> int:
        return wall_time_slack_sec_for_mode(
            mode,
            pass_fail_sec=int(self.wall_time_slack_pass_fail_sec),
            multi_pass_sec=int(self.wall_time_slack_multi_pass_sec),
            interactive_sec=int(self.wall_time_slack_interactive_sec),
        )

    def _effective_run_timeout_ms(self, time_limit_ms: int, *, mode: object = "pass-fail") -> int:
        return effective_run_timeout_ms(
            time_limit_ms,
            mode=mode,
            default_ms=self.DEFAULT_TIME_LIMIT_MS,
            min_ms=self.TIME_LIMIT_MIN_MS,
            max_ms=self.TIME_LIMIT_MAX_MS,
            pass_fail_slack_sec=int(self.wall_time_slack_pass_fail_sec),
            multi_pass_slack_sec=int(self.wall_time_slack_multi_pass_sec),
            interactive_slack_sec=int(self.wall_time_slack_interactive_sec),
        )

    def _effective_run_timeout_sec(self, run_timeout_ms: int) -> int:
        return effective_run_timeout_sec(run_timeout_ms, max_timeout_sec=self.RUN_TIMEOUT_MAX_SEC)

    def _cap_tle_time_ms(self, time_ms: int, timeout_ms: int) -> int:
        return cap_tle_time_ms(time_ms, timeout_ms)

    def _read_time_metrics(self, time_file: Path) -> tuple[int, int]:
        return read_time_metrics(time_file)

    def _load_run_config(self, artifact_root: Path, *, default_mode: object = "pass-fail") -> dict[str, object]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._run_config_cache, cache_key)
        if cached is not None:
            return dict(cached)

        normalized_default_mode = self._normalize_problem_mode(default_mode, "pass-fail")
        cfg: dict[str, object] = {
            "checker_args": [],
            "max_passes": 16,
            "run_jobs": 0,
            "mode": normalized_default_mode,
            "time_limit_ms": self.DEFAULT_TIME_LIMIT_MS,
            "run_timeout_ms": self._effective_run_timeout_ms(self.DEFAULT_TIME_LIMIT_MS, mode=normalized_default_mode),
            "run_timeout_sec": 30,
            "run_memory_mb": self.default_run_memory_mb,
            "run_process_limit": self.default_run_process_limit,
            "run_output_kb": self.default_run_output_kb,
        }
        loaded = False
        run_config = artifact_root / "logs" / "run_config.json"
        if run_config.exists():
            try:
                payload = json.loads(run_config.read_text(encoding="utf-8"))
                params = None
                if isinstance(payload, dict):
                    maybe = payload.get("generation_params")
                    if isinstance(maybe, dict):
                        params = maybe
                    else:
                        params = payload
                if isinstance(params, dict):
                    cfg.update(params)
                    loaded = True
            except Exception:
                loaded = False
        if not loaded:
            manifest = artifact_root / "manifest.json"
            if manifest.exists():
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    params = payload.get("generation_params")
                    if isinstance(params, dict):
                        cfg.update(params)
                        loaded = True
                        self._write_run_config_sidecar(run_config, params)
                except Exception:
                    pass
        checker_args = cfg.get("checker_args", [])
        if not isinstance(checker_args, list):
            checker_args = []
        try:
            max_passes = max(1, int(cfg.get("max_passes", 16)))
        except Exception:
            max_passes = 16
        try:
            run_jobs = max(0, min(16, int(cfg.get("run_jobs", 0))))
        except Exception:
            run_jobs = 0
        mode = self._normalize_problem_mode(cfg.get("mode"), normalized_default_mode)
        time_limit_ms = self._normalize_time_limit_ms(cfg.get("time_limit_ms", self.DEFAULT_TIME_LIMIT_MS))
        run_timeout_ms = self._effective_run_timeout_ms(time_limit_ms, mode=mode)
        run_timeout_sec = self._effective_run_timeout_sec(run_timeout_ms)
        try:
            run_memory_mb = max(16, min(262144, int(cfg.get("run_memory_mb", self.default_run_memory_mb))))
        except Exception:
            run_memory_mb = self.default_run_memory_mb
        try:
            run_process_limit = max(1, min(4096, int(cfg.get("run_process_limit", self.default_run_process_limit))))
        except Exception:
            run_process_limit = self.default_run_process_limit
        try:
            run_output_kb = max(64, min(1048576, int(cfg.get("run_output_kb", self.default_run_output_kb))))
        except Exception:
            run_output_kb = self.default_run_output_kb
        resolved_cfg = {
            "checker_args": [str(x) for x in checker_args],
            "max_passes": max_passes,
            "run_jobs": run_jobs,
            "mode": mode,
            "time_limit_ms": time_limit_ms,
            "run_timeout_ms": run_timeout_ms,
            "run_timeout_sec": run_timeout_sec,
            "run_memory_mb": run_memory_mb,
            "run_process_limit": run_process_limit,
            "run_output_kb": run_output_kb,
        }
        self._cache_put(self._run_config_cache, cache_key, dict(resolved_cfg))
        return dict(resolved_cfg)

    def _write_run_config_sidecar(self, path: Path, params: dict[str, object]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                tmp.write_text(json.dumps(params, indent=2), encoding="utf-8")
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        except Exception:
            # Sidecar backfill is a best-effort optimization and must not affect run behavior.
            pass

    def _load_test_inputs(self, artifact_root: Path) -> list[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._test_input_cache, cache_key)
        if cached is not None:
            return list(cached)
        tests_dir = artifact_root / "tests"
        names = tuple(self._safe_top_level_suffix_names(tests_dir, ".in"))
        self._cache_put(self._test_input_cache, cache_key, names)
        return list(names)

    def _load_answer_files(self, artifact_root: Path) -> list[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._answer_file_cache, cache_key)
        if cached is not None:
            return list(cached)
        ans_dir = artifact_root / "ans"
        names = tuple(self._safe_top_level_suffix_names(ans_dir, ".ans"))
        self._cache_put(self._answer_file_cache, cache_key, names)
        return list(names)

    def _load_answer_file_set(self, artifact_root: Path) -> frozenset[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._answer_file_set_cache, cache_key)
        if cached is not None:
            return cached
        names = frozenset(self._load_answer_files(artifact_root))
        self._cache_put(self._answer_file_set_cache, cache_key, names)
        return names

    def _load_test_input_meta(self, artifact_root: Path) -> list[tuple[str, str]]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._test_input_meta_cache, cache_key)
        if cached is not None:
            return list(cached)
        meta = tuple((name, self._test_name_stem(name)) for name in self._load_test_inputs(artifact_root))
        self._cache_put(self._test_input_meta_cache, cache_key, meta)
        return list(meta)

    def _test_name_stem(self, name: str) -> str:
        text = str(name or "")
        # Hot path for cached *.in test discovery names.
        if text.endswith(".in"):
            return text[:-3]
        return os.path.splitext(text)[0]

    def _artifact_cache_key(self, artifact_root: Path) -> str:
        try:
            return str(artifact_root.resolve())
        except OSError:
            return str(artifact_root)

    def _contains_symlink_component(self, root: Path, candidate: Path) -> bool:
        return contains_symlink_component(root, candidate)

    def _cache_get(self, cache: dict, key: str):
        with self._cache_lock:
            value = cache.get(key)
            if value is None:
                return None
            # Promote on access to keep hot build artifacts resident.
            cache.pop(key, None)
            cache[key] = value
            return value

    def _cache_put(self, cache: dict, key: str, value) -> None:
        with self._cache_lock:
            cache.pop(key, None)
            cache[key] = value
            while len(cache) > self.ARTIFACT_CACHE_LIMIT:
                oldest_key = next(iter(cache))
                cache.pop(oldest_key, None)

    def _effective_run_jobs(self, configured: object, test_count: int) -> int:
        return effective_run_jobs(configured, test_count=test_count)

    def _canonical_build_artifact_root(self, build_ref: str) -> Path:
        safe_ref = str(build_ref or "").strip().lower()
        try:
            root = self.fs_manager.build_paths(safe_ref).root.resolve()
        except Exception as exc:
            raise RuntimeError("invalid build ref") from exc
        return root

    def _is_safe_path_within(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        return is_safe_path_within(root, path, root_resolved=root_resolved)

    def _is_safe_dir(self, root: Path, path: Path) -> bool:
        return is_safe_dir(root, path)

    def _is_safe_regular_file(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        return is_safe_regular_file(root, path, root_resolved=root_resolved)

    def _safe_top_level_suffix_names(self, root: Path, suffix: str) -> list[str]:
        return safe_top_level_suffix_names(root, suffix)

    def _run_interactive_case(
        self,
        interactor_bin: Path,
        submission_bin: Path,
        test: Path,
        ans: Path,
        interactor_output: Path,
        transcript: Path,
        feedback_dir: Path,
        timeout_sec: int = 30,
        timeout_ms: int = 0,
        memory_mb: int = 1024,
        process_limit: int = 64,
        output_kb: int = 65536,
    ) -> tuple[str, int, int, int]:
        return run_interactive_case(
            sandbox=self.sandbox,
            read_time_metrics=self._read_time_metrics,
            validator_style_verdict=self._validator_style_verdict,
            cap_tle_time_ms=self._cap_tle_time_ms,
            interactor_bin=interactor_bin,
            submission_bin=submission_bin,
            test=test,
            ans=ans,
            interactor_output=interactor_output,
            transcript=transcript,
            feedback_dir=feedback_dir,
            timeout_sec=timeout_sec,
            timeout_ms=timeout_ms,
            memory_mb=memory_mb,
            process_limit=process_limit,
            output_kb=output_kb,
        )

    def _run_pass(
        self,
        submission_bin: Path,
        input_file: Path,
        output_file: Path,
        cwd: Path | None,
        timeout_sec: int,
        time_file: Path,
        memory_mb: int,
        process_limit: int,
        output_kb: int,
        base_env: dict[str, str] | None = None,
        deny_paths: list[Path] | tuple[Path, ...] | None = None,
        allow_paths: list[Path] | tuple[Path, ...] | None = None,
    ) -> tuple[int, int, int, int]:
        if time_file.exists():
            time_file.unlink()
        if Path("/usr/bin/time").exists():
            cmd = ["/usr/bin/time", "-f", "%M %U %S", "-o", str(time_file), str(submission_bin)]
        else:
            cmd = [str(submission_bin)]

        exec_env = self._path_guard_environment(
            base_env=base_env,
            deny_paths=deny_paths,
            allow_paths=allow_paths,
        )

        wall_ms = timeout_sec * 1000
        user_ms = timeout_sec * 1000
        returncode = self.RUN_TIMEOUT_SENTINEL
        exec_result = self.sandbox.run(
            ExecSpec(
                command=cmd,
                cwd=cwd,
                timeout_sec=timeout_sec,
                stdin_path=input_file,
                stdout_path=output_file,
                env=exec_env,
                memory_mb=memory_mb,
                process_limit=process_limit,
                output_kb=output_kb,
            )
        )
        wall_ms = exec_result.elapsed_ms
        status = str(exec_result.status or "").strip().lower()
        if exec_result.timed_out or status == "tle":
            returncode = self.RUN_TIMEOUT_SENTINEL
        else:
            raw_rc = exec_result.returncode
            if raw_rc is None:
                returncode = 1
            else:
                returncode = int(raw_rc)
                # /usr/bin/time returns 128+SIGNAL when the wrapped command is terminated by a signal.
                if cmd and cmd[0] == "/usr/bin/time":
                    tle_wrapped_codes = {
                        128 + int(signal.SIGXCPU),
                        128 + int(signal.SIGKILL),
                        128 + int(signal.SIGALRM),
                        128 + int(signal.SIGTERM),
                    }
                    if returncode in tle_wrapped_codes:
                        returncode = self.RUN_TIMEOUT_SENTINEL
        mem_kb, parsed_user_ms = self._read_time_metrics(time_file)
        if parsed_user_ms > 0:
            user_ms = parsed_user_ms
        else:
            user_ms = 0
        return returncode, user_ms, wall_ms, mem_kb

    def _feedback_key_files(self, test_feedback_dir: Path, base_root: Path) -> list[str]:
        if not test_feedback_dir.exists() or not test_feedback_dir.is_dir():
            return []
        try:
            base_root_resolved = base_root.resolve()
        except OSError:
            return []
        if test_feedback_dir.is_symlink():
            return []
        try:
            test_feedback_resolved = test_feedback_dir.resolve()
        except OSError:
            return []
        if base_root_resolved not in test_feedback_resolved.parents and base_root_resolved != test_feedback_resolved:
            return []
        wanted_judge = "judgemessage.txt"
        wanted_team = "teammessage.txt"
        cap = max(1, int(self.FEEDBACK_KEY_FILE_LIMIT))
        keys: list[str] = []
        stop_scan = False
        for dirpath, dirnames, filenames in os.walk(test_feedback_dir, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if base_root_resolved not in dir_root_resolved.parents and base_root_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            keep_dirs: list[str] = []
            for name in dirnames:
                d = dir_root / name
                if d.is_symlink():
                    continue
                keep_dirs.append(name)
            dirnames[:] = sorted(keep_dirs)

            has_judge = False
            has_team = False
            for name in filenames:
                if name == wanted_judge:
                    has_judge = True
                elif name == wanted_team:
                    has_team = True

            ordered_names: list[str] = []
            if has_judge:
                ordered_names.append(wanted_judge)
            if has_team:
                ordered_names.append(wanted_team)

            for name in ordered_names:
                p = dir_root / name
                if not self._is_safe_regular_file(base_root, p, root_resolved=base_root_resolved):
                    continue
                keys.append(str(p.relative_to(base_root)))
                if len(keys) >= cap:
                    stop_scan = True
                    break
            if stop_scan:
                break
        return keys

    def _feedback_message_for_pass(self, pass_feedback_dir: Path, base_root: Path) -> str:
        return feedback_message_for_pass(
            pass_feedback_dir,
            base_root,
            is_safe_regular_file=self._is_safe_regular_file,
            compact_inline_error=self._compact_inline_error,
        )

    def _files_equal(self, lhs: Path, rhs: Path) -> bool:
        return files_equal(lhs, rhs)

    def _cap_run_test_feedback_files(self, tests: list) -> list:
        return cap_run_test_feedback_files(tests, int(self.DB_SUMMARY_FEEDBACK_FILES_LIMIT))

    def _compact_inline_error(self, raw: object, *, max_chars: int = 240) -> str:
        return compact_inline_error(raw, max_chars=max_chars)

    def _build_failure_context(self, build_row: object) -> tuple[str, str]:
        return build_failure_context(build_row)

    def _synthesize_failure_tests(
        self,
        *,
        preferred_test: str = "",
        selected_test_names: list[str] | None = None,
        reason: str = "",
    ) -> list[dict]:
        return synthesize_failure_tests(
            preferred_test=preferred_test,
            selected_test_names=selected_test_names,
            reason=reason,
        )

    def _is_fl_verdict(self, verdict: object) -> bool:
        return is_fl_verdict(verdict)

    def _synthesized_fl_skip_test(self, test_name: str, *, caused_by_test: str) -> dict[str, object]:
        return synthesized_fl_skip_test(test_name, caused_by_test=caused_by_test)

    def _append_fl_skip_tail_tests(
        self,
        *,
        verdicts: list[dict],
        test_meta: list[tuple[str, str]],
        failed_index: int,
        caused_by_test: str,
    ) -> None:
        append_fl_skip_tail_tests(
            verdicts=verdicts,
            test_meta=test_meta,
            failed_index=failed_index,
            caused_by_test=caused_by_test,
        )

    def _finalize_run(self, run_id: str, status: str, summary: dict) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
            [
                status,
                summary_for_db(
                    summary,
                    tests_limit=self.DB_SUMMARY_TESTS_LIMIT,
                    diagnostics_limit=self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
                    feedback_files_limit=self.DB_SUMMARY_FEEDBACK_FILES_LIMIT,
                    diagnostic_message_limit=self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
                ),
                now_iso(),
                run_id,
            ],
        )

    def _run_noninteractive_test(
        self,
        mode: str,
        sub_bin: Path,
        checker: Path,
        checker_args: list[str],
        max_passes: int,
        run_timeout_sec: int,
        run_timeout_ms: int,
        run_memory_mb: int,
        run_process_limit: int,
        run_output_kb: int,
        test: Path,
        ans: Path,
        test_feedback_dir: Path,
        run_root: Path,
    ) -> dict:
        return run_noninteractive_test(
            run_timeout_sentinel=self.RUN_TIMEOUT_SENTINEL,
            run_pass=self._run_pass,
            run_checker=self._run_checker,
            validator_style_verdict=self._validator_style_verdict,
            files_equal=self._files_equal,
            feedback_message_for_pass=self._feedback_message_for_pass,
            feedback_key_files=self._feedback_key_files,
            cap_tle_time_ms=self._cap_tle_time_ms,
            mode=mode,
            sub_bin=sub_bin,
            checker=checker,
            checker_args=checker_args,
            max_passes=max_passes,
            run_timeout_sec=run_timeout_sec,
            run_timeout_ms=run_timeout_ms,
            run_memory_mb=run_memory_mb,
            run_process_limit=run_process_limit,
            run_output_kb=run_output_kb,
            test=test,
            ans=ans,
            test_feedback_dir=test_feedback_dir,
            run_root=run_root,
        )

    def _run_multi_pass_interactive_test(
        self,
        sub_bin: Path,
        interactor: Path,
        checker: Path,
        checker_args: list[str],
        max_passes: int,
        run_timeout_sec: int,
        run_timeout_ms: int,
        run_memory_mb: int,
        run_process_limit: int,
        run_output_kb: int,
        test: Path,
        ans: Path,
        test_feedback_dir: Path,
        run_root: Path,
    ) -> dict:
        return run_multi_pass_interactive_test(
            run_interactive_case_fn=self._run_interactive_case,
            run_checker=self._run_checker,
            validator_style_verdict=self._validator_style_verdict,
            files_equal=self._files_equal,
            feedback_message_for_pass=self._feedback_message_for_pass,
            feedback_key_files=self._feedback_key_files,
            cap_tle_time_ms=self._cap_tle_time_ms,
            sub_bin=sub_bin,
            interactor=interactor,
            checker=checker,
            checker_args=checker_args,
            max_passes=max_passes,
            run_timeout_sec=run_timeout_sec,
            run_timeout_ms=run_timeout_ms,
            run_memory_mb=run_memory_mb,
            run_process_limit=run_process_limit,
            run_output_kb=run_output_kb,
            test=test,
            ans=ans,
            test_feedback_dir=test_feedback_dir,
            run_root=run_root,
        )

    def run_submission(
        self,
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
        raise RuntimeError("native run execution path has been removed; use judgehost invocation backend")

