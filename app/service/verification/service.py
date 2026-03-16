from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from app.db import DB
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.artifact import ArtifactService
from app.service.disk.verification_store import VerificationStore
from app.service.platform.fs.layout import FsManager
from app.service.platform.hashing import sha256_file
from app.service.problem.test_spec import (
    load_tests_spec,
    parse_gen_command_tokens,
)
from app.service.runtime.toolchain import current_cpp_command_digest
from app.service.repository.workspace import WorkspaceService
from app.service.verification.types import Kind, Status

from app.service.verification.cache import (
    verification_cache_key,
    canonical_digest,
)
from app.service.verification.pipeline import effective_compile_jobs
from app.service.verification.pipeline import wait_build_terminal_status
from app.service.verification.store import (
    VerificationRunRow,
    VerificationSummary,
    create_verification_record,
    default_verification_run,
    default_verification_summary,
    load_verification_run,
    save_verification_run_summary,
    save_verification_summary,
    verification_run_ids,
    verification_source_paths,
    verification_stage_results,
)
from app.service.verification.runner import run_verification_job
from app.service.verification.runtime import coerce_int, effective_run_timeout_ms, effective_run_timeout_sec, load_problem_runtime_config
from app.service.verification.source import resolve_standard_checker_source, select_checker_source, select_source
from app.service.verification.test_spec import load_tests_spec_entries, manual_test_sources, prepare_tests_spec_runtime

if TYPE_CHECKING:
    from app.service.platform.async_task_cache import AsyncTaskCacheService
    from app.service.judgehost.api import Judgehost


CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
GENERATOR_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_TEST_NAME_RE = re.compile(r"^[0-9]{3}\.in$")
STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[3] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
DEFAULT_TIME_LIMIT_MS = 2000
TIME_LIMIT_MIN_MS = 100
TIME_LIMIT_MAX_MS = 30000

class VerificationService:
    DB_SUMMARY_TESTS_LIMIT = 8192
    DB_SUMMARY_DIAGNOSTICS_LIMIT = 512
    DB_SUMMARY_FEEDBACK_FILES_LIMIT = 16
    DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT = 4096
    VERIFICATION_CACHE_NAMESPACE = "verification.run"
    VERIFICATION_CACHE_SCHEMA = "v3"
    VERIFICATION_JOIN_WAIT_TIMEOUT_SEC = 180
    VERIFICATION_JOIN_POLL_SEC = 0.25

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        artifacts: ArtifactService,
        judgehost_task_service: Judgehost,
        constants: RuntimeValues | None = None,
        async_task_cache_service: AsyncTaskCacheService | None = None,
    ):
        self.db = db
        self._verification_store = VerificationStore(db)
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.execution_backend_name = "domjudge-judgehost"
        self.default_exec_memory_mb = 1024
        self.default_exec_process_limit = 64
        self.default_exec_output_kb = 65536
        self.wall_time_slack_pass_fail_sec = 1
        self.wall_time_slack_multi_pass_sec = 15
        self.wall_time_slack_interactive_sec = 15
        self.judgehost_task_service = judgehost_task_service
        self._async_task_cache_service = async_task_cache_service
        self._verification_inflight_lock = threading.RLock()
        self._verification_inflight: dict[str, str] = {}
        self.fs_manager = FsManager(self.workspace_service.settings.artifacts_root, self.workspace_service.settings.run_root)
        self.apply_runtime_values(constants or build_runtime_values())

    def export_runtime_verification(self, problem_id: int, verification_id: str) -> dict[str, str] | None:
        row = self._verification_store.get_runtime_row(int(problem_id), verification_id)
        if row is None:
            return None
        return row

    def workspace_runtime_verification(
        self,
        problem_id: int,
        workspace_id: int | None,
        verification_id: str,
    ) -> dict[str, str] | None:
        row = self._verification_store.get_workspace_runtime_row(int(problem_id), int(workspace_id), verification_id)
        if row is None:
            return None
        return row

    def has_export_detail_verification(self, problem_id: int, verification_id: str) -> bool:
        row = self._verification_store.get_status_row(int(problem_id), verification_id)
        if row is None:
            return False
        return row["status"] in {"queued", "pending", "running", "ok", "failed"}

    def artifact_path_for_problem_artifact(self, problem_id: int, artifact_id: str) -> str:
        return self._verification_store.artifact_path_for_problem_artifact(int(problem_id), artifact_id)

    def artifact_path_for_verification(self, verification_id: str) -> str:
        return self._verification_store.artifact_path_for_verification(verification_id)

    def workspace_verification_id_for_run(self, problem_id: int, workspace_id: int, run_id: str) -> str:
        token = run_id
        if not token:
            return ""
        for row in self._verification_store.list_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=512,
            kinds=(Kind.VERIFICATION,),
        ):
            text = row["summary_json"]
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            runs = payload.get("runs")
            if isinstance(runs, dict) and token in runs:
                return row["id"]
            order = payload.get("runs_order")
            if isinstance(order, list) and token in order:
                return row["id"]
        return ""

    def workspace_verification_exists(self, problem_id: int, workspace_id: int, verification_id: str) -> bool:
        return self._verification_store.workspace_verification_exists(int(problem_id), int(workspace_id), verification_id)

    def workspace_artifact_exists(self, problem_id: int, workspace_id: int, artifact_id: str) -> bool:
        return self._verification_store.workspace_artifact_exists(int(problem_id), int(workspace_id), artifact_id)

    def workspace_verification_meta(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> dict[str, str] | None:
        row = self._verification_store.workspace_verification_meta(int(problem_id), int(workspace_id), verification_id)
        if row is None:
            return None
        return row

    def latest_problem_verification_id_for_source_commit(self, problem_id: int, source_commit: str) -> str:
        return self._verification_store.latest_problem_verification_id_for_source_commit(int(problem_id), source_commit)

    def verification_summary(self, verification_id: str) -> dict[str, object]:
        return self._verification_store.runtime_summary(verification_id)

    def verification_stage_summary(self, verification_id: str, stage_key: str) -> VerificationSummary:
        stage_results = self.verification_stage_results(verification_id)
        return dict(stage_results.get(stage_key) or {})

    def verification_record(self, verification_id: str) -> dict[str, object] | None:
        row = self._verification_store.record_row(verification_id)
        if row is None:
            return None
        return dict(row)

    def verification_run(self, verification_id: str, run_id: str) -> VerificationRunRow:
        return load_verification_run(
            self.db,
            verification_id=verification_id,
            run_id=run_id,
        )

    def verification_run_ids(self, verification_id: str) -> list[str]:
        return verification_run_ids(self.verification_summary(verification_id))

    def verification_source_paths(self, verification_id: str) -> list[str]:
        return verification_source_paths(self.verification_summary(verification_id))

    def verification_stage_results(self, verification_id: str) -> dict[str, VerificationSummary]:
        return verification_stage_results(self.verification_summary(verification_id))

    def list_workspace_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.VERIFICATION,),
    ) -> list[dict[str, object]]:
        rows = self._verification_store.list_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=int(limit),
            kinds=kinds,
        )
        return [dict(row) for row in rows]

    def begin_verification_record(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int | None,
        source_commit: str = "",
        source_ref: str = "",
        kind: str,
        status: str,
        summary: dict[str, object] | None = None,
        artifact_path: str | Path | None = None,
    ) -> str:
        return create_verification_record(
            self.db,
            self.fs_manager,
            verification_id=verification_id,
            problem_id=int(problem_id),
            workspace_id=None if workspace_id is None else int(workspace_id),
            source_commit=source_commit,
            source_ref=source_ref,
            kind=kind,
            status=status,
            summary=summary,
            artifact_path=artifact_path,
        )

    def persist_verification_summary(
        self,
        *,
        verification_id: str,
        status: str,
        summary: VerificationSummary,
        finished: bool = False,
    ) -> None:
        save_verification_summary(
            self.db,
            verification_id=verification_id,
            status=status,
            summary=summary,
            finished=bool(finished),
        )

    def persist_run_summary(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int,
        kind: str,
        mode: str,
        verification_source: str,
        source_paths: list[str],
        run_id: str,
        run_status: str,
        source_label: str,
        expected_behavior: str,
        run_summary: dict[str, object],
        artifact_path: str,
        task_kind: str = "",
        error_text: str = "",
        finished: bool = False,
    ) -> None:
        save_verification_run_summary(
            self.db,
            self.fs_manager,
            verification_id=verification_id,
            problem_id=int(problem_id),
            workspace_id=None if workspace_id is None else int(workspace_id),
            kind=kind,
            mode=mode,
            verification_source=verification_source,
            source_paths=source_paths,
            run_id=run_id,
            run_status=run_status,
            source_label=source_label,
            expected_behavior=expected_behavior,
            run_summary=run_summary,
            artifact_path=artifact_path,
            task_kind=task_kind,
            error_text=error_text,
            finished=bool(finished),
        )

    def new_verification_summary(
        self,
        *,
        kind: str,
        mode: str,
        source_commit: str = "",
        source_ref: str = "",
        source_paths: list[str] | None = None,
        verification_source: str = "",
    ) -> VerificationSummary:
        return default_verification_summary(
            kind=kind,
            mode=mode,
            source_commit=source_commit,
            source_ref=source_ref,
            source_paths=source_paths,
            verification_source=verification_source,
        )

    def new_verification_run(
        self,
        *,
        run_id: str,
        source_label: str,
        expected_behavior: str,
        run_status: str = Status.RUNNING.value,
        artifact_path: str = "",
        task_kind: str = "",
    ) -> VerificationRunRow:
        return default_verification_run(
            run_id=run_id,
            source_label=source_label,
            expected_behavior=expected_behavior,
            run_status=run_status,
            artifact_path=artifact_path,
            task_kind=task_kind,
        )

    def cancel_verification_if_active(self, verification_id: str, *, reason: str, now_text: str) -> bool:
        return self._verification_store.cancel_active_verification(
            verification_id,
            reason=reason,
            now_text=now_text,
        )

    def wait_for_terminal_status(self, verification_id: str, *, timeout_sec: float) -> str:
        waited = wait_build_terminal_status(
            self._verification_store,
            verification_id=verification_id,
            timeout_sec=float(timeout_sec),
            poll_sec=self.VERIFICATION_JOIN_POLL_SEC,
        )
        return waited or ""

    def latest_workspace_stage_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int,
        ok_only: bool = False,
    ) -> list[dict[str, str]]:
        return self._verification_store.workspace_stage_rows(
            int(problem_id),
            int(workspace_id),
            limit=max(1, int(limit)),
            ok_only=bool(ok_only),
        )

    def latest_workspace_committed_stage_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        source_commit: str,
        source_ref: str,
        limit: int,
        ok_only: bool = False,
    ) -> list[dict[str, str]]:
        return self._verification_store.workspace_committed_stage_rows(
            int(problem_id),
            int(workspace_id),
            source_commit=source_commit,
            source_ref=source_ref,
            limit=max(1, int(limit)),
            ok_only=bool(ok_only),
        )

    def workspace_stage_row(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> dict[str, str] | None:
        return self._verification_store.workspace_stage_row(
            int(problem_id),
            int(workspace_id),
            verification_id,
        )

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.default_exec_memory_mb = coerce_int(
            values.get("VERIFICATION_EXEC_MEMORY_MB", 1024),
            default=1024,
            min_value=16,
            max_value=262144,
        )
        self.default_exec_process_limit = coerce_int(
            values.get("VERIFICATION_EXEC_PROCESS_LIMIT", 64),
            default=64,
            min_value=1,
            max_value=4096,
        )
        self.default_exec_output_kb = coerce_int(
            values.get("VERIFICATION_EXEC_OUTPUT_KB", 65536),
            default=65536,
            min_value=64,
            max_value=1048576,
        )
        self.wall_time_slack_pass_fail_sec = coerce_int(
            values.get("RUN_WALL_TIME_SLACK_PASS_FAIL_SEC", 1),
            default=1,
            min_value=0,
            max_value=300,
        )
        self.wall_time_slack_multi_pass_sec = coerce_int(
            values.get("RUN_WALL_TIME_SLACK_MULTI_PASS_SEC", 15),
            default=15,
            min_value=0,
            max_value=300,
        )
        self.wall_time_slack_interactive_sec = coerce_int(
            values.get("RUN_WALL_TIME_SLACK_INTERACTIVE_SEC", 15),
            default=15,
            min_value=0,
            max_value=300,
        )

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
                cfg.update(dict(json.loads(path.read_text(encoding="utf-8"))))
            except json.JSONDecodeError:
                pass
        cfg["checker_standard"] = cfg["checker_standard"].strip()
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
        return canonical_digest(
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
            token = current_cpp_command_digest()
        except Exception:
            token = ""
        return token

    def _cached_verification_id_for_source(
        self,
        *,
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
        if not source_commit:
            return ""
        cache_key = verification_cache_key(
            schema=self.VERIFICATION_CACHE_SCHEMA,
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            source_commit=source_commit,
            source_ref=source_ref,
            generation_params_digest=generation_params_digest,
            toolchain_cmd_digest=toolchain_cmd_digest,
            sample_only=bool(sample_only),
        )
        entry = service.get(
            self.VERIFICATION_CACHE_NAMESPACE,
            cache_key,
        )
        if entry is None:
            return ""
        value = entry["value"]
        cached_verification_id = value["verification_id"]
        if not cached_verification_id:
            return ""
        row = self._verification_store.get_workspace_runtime_row(
            int(problem_id),
            int(workspace_id),
            cached_verification_id,
        )
        if row is None:
            service.delete(self.VERIFICATION_CACHE_NAMESPACE, cache_key)
            return ""
        if row["status"] != Status.OK.value:
            service.delete(self.VERIFICATION_CACHE_NAMESPACE, cache_key)
            return ""
        try:
            summary_obj = dict(json.loads(row["summary_json"]))
        except Exception:
            summary_obj = {}
        generation_params = summary_obj.get("generation_params") or {}
        if bool(generation_params.get("sample_only", False)) != bool(sample_only):
            service.delete(self.VERIFICATION_CACHE_NAMESPACE, cache_key)
            return ""
        generation_toolchain_digest = generation_params.get("toolchain_cmd_digest")
        if generation_toolchain_digest != toolchain_cmd_digest:
            service.delete(self.VERIFICATION_CACHE_NAMESPACE, cache_key)
            return ""
        stored_generation_params_digest = generation_params.get("generation_params_digest")
        if stored_generation_params_digest != generation_params_digest:
            service.delete(self.VERIFICATION_CACHE_NAMESPACE, cache_key)
            return ""
        artifact_root = Path(row["artifact_path"]).resolve()
        tests_dir = artifact_root / "tests"
        ans_dir = artifact_root / "ans"
        if not tests_dir.exists() or (not tests_dir.is_dir()) or tests_dir.is_symlink():
            service.delete(self.VERIFICATION_CACHE_NAMESPACE, cache_key)
            return ""
        if not ans_dir.exists() or (not ans_dir.is_dir()) or ans_dir.is_symlink():
            service.delete(self.VERIFICATION_CACHE_NAMESPACE, cache_key)
            return ""
        return cached_verification_id

    def run_verification(
        self,
        problem: str,
        username: str,
        commit: str | None = None,
        ref: str | None = None,
        *,
        sample_only: bool = False,
        verification_id: str = "",
    ) -> str:
        return run_verification_job(
            self,
            problem,
            username,
            commit=commit,
            ref=ref,
            sample_only=sample_only,
            verification_id=verification_id,
        )
