from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from app.db import DB
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.artifact import ArtifactService
from app.service.disk.verification_store import VerificationStore
from app.service.platform.fs.layout import FsManager
from app.service.problem.test_spec import (
    parse_gen_command_tokens,
)
from app.service.repository.workspace import WorkspaceService
from app.service.verification.types import Kind

from app.service.verification.pipeline import effective_compile_jobs
from app.service.verification.pipeline import wait_build_terminal_status
from app.service.verification.read_model import (
    logical_run_ids,
    running_tasks,
    solution_source_paths,
    task_counts,
)
from app.service.verification.runtime import coerce_int, effective_run_timeout_ms, effective_run_timeout_sec, load_problem_runtime_config
from app.service.verification.signature import verification_signature
from app.service.verification.source import resolve_standard_checker_source, select_checker_source, select_source
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.test_spec import load_tests_spec_entries, manual_test_sources, prepare_tests_spec_runtime

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
    VERIFICATION_JOIN_WAIT_TIMEOUT_SEC = 180
    VERIFICATION_JOIN_POLL_SEC = 0.25

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        artifacts: ArtifactService,
        judgehost_task_service: Judgehost,
        constants: RuntimeValues | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.default_exec_memory_mb = 1024
        self.default_exec_process_limit = 64
        self.default_exec_output_kb = 65536
        self.wall_time_slack_pass_fail_sec = 1
        self.wall_time_slack_multi_pass_sec = 15
        self.wall_time_slack_interactive_sec = 15
        self.judgehost_task_service = judgehost_task_service
        self._verification_inflight_lock = threading.RLock()
        self._verification_inflight: dict[str, str] = {}
        self.fs_manager = FsManager(self.workspace_service.settings.artifacts_root, self.workspace_service.settings.run_root)
        self._verification_store = VerificationStore(db, self.fs_manager)
        self.apply_runtime_values(constants or build_runtime_values())

    def export_runtime_verification(self, problem_id: int, verification_id: str) -> dict[str, object] | None:
        row = self._verification_store.get_runtime_row(int(problem_id), verification_id)
        if row is None:
            return None
        return row

    def workspace_runtime_verification(
        self,
        problem_id: int,
        workspace_id: int | None,
        verification_id: str,
    ) -> dict[str, object] | None:
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
        task_store = VerificationTaskStore(self.db)
        for row in self._verification_store.list_rows(
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            limit=512,
            kinds=(Kind.ALL, Kind.SAMPLE, Kind.CUSTOM),
        ):
            verification_id = str(row["id"])
            for task_row in task_store.list_rows(verification_id):
                if str(task_row["logical_run_id"] or "") == token:
                    return verification_id
                if str(task_row["run_id"] or "") == token:
                    return verification_id
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

    def latest_problem_verification_id_for_signature(self, problem_id: int, signature: str) -> str:
        return self._verification_store.latest_problem_verification_id_for_signature(int(problem_id), signature)

    def latest_workspace_verification_id_for_signature(
        self,
        problem_id: int,
        workspace_id: int,
        signature: str,
        *,
        ok_only: bool = False,
    ) -> str:
        return self._verification_store.latest_workspace_verification_id_for_signature(
            int(problem_id),
            int(workspace_id),
            signature,
            ok_only=bool(ok_only),
        )

    def verification_record(self, verification_id: str) -> dict[str, object] | None:
        row = self._verification_store.record_row(verification_id)
        if row is None:
            return None
        return dict(row)

    def verification_metadata(self, verification_id: str) -> dict[str, object]:
        return self._verification_store.metadata(verification_id)

    def persist_verification_metadata(self, verification_id: str, metadata: dict[str, object]) -> None:
        self._verification_store.save_metadata(verification_id, metadata)

    def verification_runtime_snapshot(self, verification_id: str) -> dict[str, object]:
        rows = VerificationTaskStore(self.db).list_rows_for_list(verification_id)
        counts = task_counts(rows)
        return {
            "task_graph": bool(rows),
            "task_counts": counts,
            "running_tasks": running_tasks(rows),
            "source_paths": solution_source_paths(rows),
            "logical_run_ids": logical_run_ids(rows, include_main_correct=True),
            "has_running": bool(int(counts["pending"]) or int(counts["queued"]) or int(counts["running"])),
            "test_names": [str(row["test_name"] or "") for row in rows if str(row["test_name"] or "")],
        }

    def verification_run_ids(self, verification_id: str) -> list[str]:
        snapshot = self.verification_runtime_snapshot(verification_id)
        return list(snapshot["logical_run_ids"])

    def verification_source_paths(self, verification_id: str) -> list[str]:
        snapshot = self.verification_runtime_snapshot(verification_id)
        return list(snapshot["source_paths"])

    def list_workspace_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int = 40,
        kinds: tuple[str, ...] = (Kind.ALL, Kind.SAMPLE, Kind.CUSTOM),
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
        signature: str = "",
        kind: str,
        status: str,
        metadata: dict[str, object] | None = None,
    ) -> str:
        root = self._verification_store.create_or_update_record(
            self.fs_manager,
            verification_id=verification_id,
            problem_id=int(problem_id),
            workspace_id=None if workspace_id is None else int(workspace_id),
            signature=signature,
            kind=kind,
            status=status,
        )
        if metadata is not None:
            self._verification_store.save_metadata(verification_id, metadata)
        return root

    def cancel_verification_if_active(self, verification_id: str, *, reason: str, now_text: str) -> bool:
        return self._verification_store.cancel_active_verification(
            verification_id,
            reason=reason,
            now_text=now_text,
        )

    def update_verification_record_status(
        self,
        verification_id: str,
        *,
        status: str,
        fail_reason: str,
        finished: bool,
    ) -> None:
        self._verification_store.update_record_status(
            verification_id,
            status=status,
            fail_reason=fail_reason,
            finished=finished,
        )

    def wait_for_terminal_status(self, verification_id: str, *, timeout_sec: float) -> str:
        waited = wait_build_terminal_status(
            self._verification_store,
            verification_id=verification_id,
            timeout_sec=float(timeout_sec),
            poll_sec=self.VERIFICATION_JOIN_POLL_SEC,
        )
        return waited or ""

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
            values.get("RUN_WALL_TIME_SLACK_PASS_LIMIT_SEC", 15),
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
        return cfg

    def _effective_run_timeout_ms(self, time_limit_ms: int, *, mode: object = "pass-fail", pass_limit: object = 1) -> int:
        return effective_run_timeout_ms(
            time_limit_ms,
            mode=mode,
            pass_limit=pass_limit,
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
        from app.impl.workspace.verification_dag import run_workspace_verification_dag

        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace_path = Path(str(ctx["workspace"]["path"])).resolve()
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(self.workspace_service.global_user_context(username)["id"])
        status = self.workspace_service.read_workspace_status(workspace_path)
        workspace_head = str(status.get("head_commit") or "")
        workspace_dirty = bool(status.get("dirty"))
        snapshot_root: Path | None = None
        signature_root = workspace_path
        if commit:
            resolved_commit = self.workspace_service.resolve_commit(workspace_path, commit)
            snapshot_root = self.workspace_service.create_snapshot(workspace_path, resolved_commit)
            workspace_dirty = False
            signature_root = snapshot_root
        elif workspace_dirty or (not workspace_head):
            snapshot_root = self.workspace_service.create_snapshot(
                workspace_path,
                None,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
            )
            signature_root = snapshot_root
        signature = verification_signature(signature_root)
        target_verification_id = verification_id or self._verification_store.allocate_id()
        run_workspace_verification_dag(
            problem,
            username,
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
            targets=[],
            verification_id=target_verification_id,
            signature=signature,
            kind=Kind.SAMPLE.value if sample_only else Kind.ALL.value,
            sample_only=bool(sample_only),
            snapshot_root_override=snapshot_root,
        )
        return target_verification_id
