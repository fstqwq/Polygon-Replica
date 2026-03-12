from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db import DB
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.artifact import ArtifactService
from app.service.platform.fs.layout import FsManager
from app.service.platform.hashing import sha256_file
from app.service.problem.test_spec import (
    load_tests_spec,
    parse_gen_command_tokens,
)
from app.service.runtime.toolchain import ToolchainService
from app.service.repository.workspace import WorkspaceService

from app.service.build.cache import (
    artifact_root_from_build_ref,
    build_cache_key,
    build_cache_key_hash,
    build_ref_from_cache_key_hash,
    canonical_digest,
    ensure_build_paths,
)
from app.service.build.diagnostic import collect_diagnostics, judge_backend_compile_detail
from app.service.build.judge_solve import solve_with_judge_backend
from app.service.build.pipeline import effective_compile_jobs, wait_build_terminal_status
from app.service.build.runner import run_build
from app.service.build.runtime import coerce_int, effective_run_timeout_ms, effective_run_timeout_sec, load_problem_runtime_config, normalize_problem_mode, normalize_time_limit_ms, wall_time_slack_sec_for_mode
from app.service.build.source import resolve_standard_checker_source, select_checker_source, select_source
from app.service.build.test_spec import load_tests_spec_entries, manual_test_sources, prepare_tests_spec_runtime

if TYPE_CHECKING:
    from app.service.platform.async_task_cache import AsyncTaskCacheService
    from app.service.judgehost.api import Judgehost


DIAG_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<level>warning|error|note):\s*(?P<msg>.*)$")
CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
GENERATOR_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_TEST_NAME_RE = re.compile(r"^[0-9]{3}\.in$")
STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[3] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
DEFAULT_TIME_LIMIT_MS = 2000
TIME_LIMIT_MIN_MS = 100
TIME_LIMIT_MAX_MS = 30000
CHECKER_TESTLIB_EXIT_CXXFLAGS = [
    "-DOK_EXIT_CODE=42",
    "-DWA_EXIT_CODE=43",
    "-DPE_EXIT_CODE=43",
]


def can_use_judge_backend_for_solve(*, judgehost_service: Any) -> bool:
    if judgehost_service is None:
        return False
    try:
        return bool(judgehost_service.enabled() and judgehost_service.auth_token_configured())
    except Exception:
        return False


def solve_result_ok() -> dict[str, object]:
    return {"rc": 0, "worker_error": "", "timed_out": False, "stderr": "", "verdict": "AC"}


def solve_result_error(message: str, *, verdict: str = "") -> dict[str, object]:
    return {
        "rc": -1,
        "worker_error": str(message or "").strip(),
        "timed_out": False,
        "stderr": "",
        "verdict": str(verdict or "").strip().upper(),
    }


class Build:
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
        constants: RuntimeValues | None = None,
        async_task_cache_service: AsyncTaskCacheService | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.toolchain = toolchain
        self.execution_backend_name = "domjudge-judgehost"
        self.default_exec_memory_mb = 1024
        self.default_exec_process_limit = 64
        self.default_exec_output_kb = 65536
        self.wall_time_slack_pass_fail_sec = 1
        self.wall_time_slack_multi_pass_sec = 15
        self.wall_time_slack_interactive_sec = 15
        self._judgehost_task_service: Judgehost | None = None
        self._async_task_cache_service = async_task_cache_service
        self._build_inflight_lock = threading.RLock()
        self._build_inflight: dict[str, str] = {}
        self.fs_manager = FsManager(self.workspace_service.settings.artifacts_root, self.workspace_service.settings.run_root)
        self.apply_runtime_values(constants or build_runtime_values())

    def bind_runtime_services(
        self,
        *,
        judgehost_task_service: Judgehost | None = None,
    ) -> None:
        self._judgehost_task_service = judgehost_task_service

    def _can_use_judge_backend_for_solve(self) -> bool:
        return can_use_judge_backend_for_solve(
            judgehost_service=self._judgehost_task_service,
        )

    @staticmethod
    def _solve_result_ok() -> dict[str, object]:
        return solve_result_ok()

    @staticmethod
    def _solve_result_error(message: str, *, verdict: str = "") -> dict[str, object]:
        return solve_result_error(message, verdict=verdict)

    def _judge_backend_compile_detail(self, summary_obj: dict[str, Any], run_root: Path) -> str:
        return judge_backend_compile_detail(summary_obj, run_root)

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
        return solve_with_judge_backend(
            self,
            problem=problem,
            username=username,
            build_id=build_id,
            accepted_source_rel=accepted_source_rel,
            mode=mode,
            test_files=test_files,
            ans_dir=ans_dir,
            solve_jobs=solve_jobs,
            source_answer_by_test=source_answer_by_test,
        )


    def _coerce_int(self, raw: object, default: int, min_value: int, max_value: int) -> int:
        return coerce_int(raw, default, min_value, max_value)

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
        return normalize_problem_mode(raw, default)

    def _resolve_standard_checker_source(self, checker_standard: str) -> Path | None:
        return resolve_standard_checker_source(
            checker_standard,
            standard_checker_root=STANDARD_CHECKER_ROOT,
            name_pattern=STANDARD_CHECKER_NAME_RE,
        )

    def _select_checker_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        return select_checker_source(
            snapshot=snapshot,
            build_cfg=build_cfg,
            standard_checker_root=STANDARD_CHECKER_ROOT,
            standard_checker_name_re=STANDARD_CHECKER_NAME_RE,
            cpp_extensions=CPP_EXTENSIONS,
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
        return select_source(
            snapshot=snapshot,
            build_cfg=build_cfg,
            config_key=config_key,
            folder=folder,
            cpp_extensions=CPP_EXTENSIONS,
            preferred=preferred,
            snapshot_resolved=snapshot_resolved,
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
        return normalize_time_limit_ms(
            raw,
            default_ms=DEFAULT_TIME_LIMIT_MS,
            min_ms=TIME_LIMIT_MIN_MS,
            max_ms=TIME_LIMIT_MAX_MS,
        )

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
            default_ms=DEFAULT_TIME_LIMIT_MS,
            min_ms=TIME_LIMIT_MIN_MS,
            max_ms=TIME_LIMIT_MAX_MS,
            pass_fail_slack_sec=int(self.wall_time_slack_pass_fail_sec),
            multi_pass_slack_sec=int(self.wall_time_slack_multi_pass_sec),
            interactive_slack_sec=int(self.wall_time_slack_interactive_sec),
        )

    def _effective_run_timeout_sec(self, run_timeout_ms: int) -> int:
        return effective_run_timeout_sec(run_timeout_ms)

    def _load_problem_runtime_config(self, snapshot: Path) -> dict:
        return load_problem_runtime_config(
            snapshot,
            default_time_limit_ms=DEFAULT_TIME_LIMIT_MS,
            default_mode="pass-fail",
            min_time_limit_ms=TIME_LIMIT_MIN_MS,
            max_time_limit_ms=TIME_LIMIT_MAX_MS,
        )

    def _collect_diagnostics(self, snapshot: Path, text: str) -> list[dict]:
        return collect_diagnostics(snapshot, text, DIAG_RE)

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

    def _manual_test_sources(self, snapshot: Path) -> list[Path]:
        return manual_test_sources(snapshot)

    def _load_tests_spec(self, snapshot: Path) -> list[dict] | None:
        return load_tests_spec_entries(snapshot)

    def _prepare_tests_spec_runtime(
        self,
        snapshot: Path,
        tests_spec_entries: list[dict],
        bin_dir: Path,
    ) -> tuple[list[dict], list[tuple[str, Path, Path]]]:
        return prepare_tests_spec_runtime(
            snapshot,
            tests_spec_entries,
            bin_dir,
            generator_source_extensions=GENERATOR_SOURCE_EXTENSIONS,
            parse_gen_command_tokens_fn=parse_gen_command_tokens,
        )

    def _effective_compile_jobs(self, configured: object, target_count: int) -> int:
        return effective_compile_jobs(configured, target_count)

    @staticmethod
    def _canonical_digest(payload: object) -> str:
        return canonical_digest(payload)

    def _build_source_tree_entries(self, source_root: Path) -> list[dict[str, object]]:
        include_dirs = (
            "checkers",
            "generators",
            "interactors",
            "solutions",
            "tests",
            "validators",
            "third_party/testlib",
        )
        entries: list[dict[str, object]] = []
        seen: set[str] = set()

        def _add_file(path: Path) -> None:
            try:
                rel = path.relative_to(source_root).as_posix()
            except Exception:
                return
            if rel in seen:
                return
            seen.add(rel)
            try:
                stat = path.stat()
            except OSError:
                return
            entries.append(
                {
                    "path": rel,
                    "size": int(stat.st_size),
                    "sha256": sha256_file(path),
                }
            )

        for rel_dir in include_dirs:
            root = (source_root / rel_dir).resolve()
            if not root.exists() or not root.is_dir():
                continue
            for path in sorted(p for p in root.rglob("*") if p.is_file()):
                _add_file(path)
        return entries

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
                "schema": "v2",
                "sample_only": bool(sample_only),
                "build_config": build_cfg,
                "runtime_config": runtime_cfg,
                "tests_spec_rows": tests_spec_rows,
                "source_tree": self._build_source_tree_entries(source_root),
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
        return build_cache_key_hash(key_obj)

    def _build_ref_from_cache_key_hash(self, cache_key_hash: str) -> str:
        return build_ref_from_cache_key_hash(
            self.fs_manager,
            schema=self.BUILD_CACHE_SCHEMA,
            cache_key_hash=cache_key_hash,
        )

    def _artifact_root_from_build_ref(self, problem_slug: str, build_ref: str) -> Path:
        _ = str(problem_slug or "").strip()
        return artifact_root_from_build_ref(self.fs_manager, build_ref=build_ref)

    def _build_paths(self, problem_slug: str, build_ref: str):
        _ = str(problem_slug or "").strip()
        return ensure_build_paths(self.fs_manager, build_ref=build_ref)

    def _wait_build_terminal_status(self, build_id: str, timeout_sec: float) -> str:
        return wait_build_terminal_status(
            self.db,
            build_id=build_id,
            timeout_sec=timeout_sec,
            poll_sec=self.BUILD_JOIN_POLL_SEC,
        )

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
        return build_cache_key(
            schema=self.BUILD_CACHE_SCHEMA,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=source_commit,
            source_ref=source_ref,
            generation_params_digest=generation_params_digest,
            toolchain_cmd_digest=toolchain_cmd_digest,
            sample_only=sample_only,
        )

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
        sample_only: bool = False,
        verification_pipeline: bool = False,
    ) -> str:
        return run_build(
            self,
            problem,
            username,
            commit=commit,
            ref=ref,
            sample_only=sample_only,
            verification_pipeline=verification_pipeline,
        )




