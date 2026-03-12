from __future__ import annotations

from typing import IO

from app.db import DB
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.fs.layout import FsManager
from app.service.repository.workspace import WorkspaceService
from app.service.run.runtime import coerce_int
from app.service.runtime.toolchain import ToolchainService


class Run:
    DB_SUMMARY_TESTS_LIMIT = 200
    DB_SUMMARY_DIAGNOSTICS_LIMIT = 200
    DB_SUMMARY_FEEDBACK_FILES_LIMIT = 32
    DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT = 4096

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        toolchain: ToolchainService,
        constants: RuntimeValues | None = None,
    ) -> None:
        self.db = db
        self.workspace_service = workspace_service
        self.toolchain = toolchain
        self.execution_backend_name = "domjudge-judgehost"
        self.fs_manager = FsManager(self.workspace_service.settings.artifacts_root, self.workspace_service.settings.run_root)
        self.default_run_memory_mb = 1024
        self.default_run_process_limit = 64
        self.default_run_output_kb = 65536
        self.wall_time_slack_pass_fail_sec = 1
        self.wall_time_slack_multi_pass_sec = 15
        self.wall_time_slack_interactive_sec = 15
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
        _ = (
            problem,
            username,
            build_id,
            submission_path,
            mode,
            upload_content,
            upload_filename,
            upload_stream,
            run_id,
            selected_tests,
            invocation_id,
            invocation_run_ids,
            expected_behavior,
            invocation_source,
            force_recompile,
        )
        raise RuntimeError("native run execution path has been removed; use judgehost invocation backend")
