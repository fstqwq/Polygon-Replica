from __future__ import annotations
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.db import DB
from app.main_utils import configure_runtime_values as configure_main_utils_runtime_values
from app.runtime_values import RuntimeValues, build_runtime_values
from app.services.artifact_service import ArtifactService
from app.services.async_task_cache_service import AsyncTaskCacheService
from app.services.build_service import BuildService
from app.services.export_service import ExportService
from app.services.fs_manager import FsManager
from app.services.git_service import GitService
from app.services.judge_fs_index_service import JudgeFsIndexService
from app.services.invocation_backend_service import InvocationBackendService
from app.services.judgehost_service import JudgehostTaskService
from app.services.preview_service import PreviewService
from app.services.run_service import RunService
from app.services.sandbox import NativeSandboxBackend
from app.services.system_config_service import SystemConfigService
from app.services.tests_spec import apply_runtime_values as apply_tests_spec_runtime_values
from app.services.toolchain_service import ToolchainService
from app.services.toolchain_service import apply_runtime_values as apply_toolchain_runtime_values
from app.services.worker_queue_service import WorkerFuture, WorkerQueueService
from app.services.workspace_service import WorkspaceService
from app.services.workspace_service import apply_runtime_values as apply_workspace_runtime_values
from app.settings import Settings, load_settings

@dataclass
class RuntimeConfig:
    settings: Settings = field(default_factory=load_settings)
    constants: RuntimeValues = field(init=False)
    db: DB = field(init=False)
    workspace_service: WorkspaceService = field(init=False)
    git_service: GitService = field(init=False)
    fs_manager: FsManager = field(init=False)
    artifact_service: ArtifactService = field(init=False)
    sandbox_backend: object = field(init=False)
    toolchain_service: ToolchainService = field(init=False)
    build_service: BuildService = field(init=False)
    preview_service: PreviewService = field(init=False)
    run_service: RunService = field(init=False)
    judgehost_task_service: JudgehostTaskService = field(init=False)
    invocation_backend_service: InvocationBackendService = field(init=False)
    export_service: ExportService = field(init=False)
    async_task_cache_service: AsyncTaskCacheService = field(init=False)
    judge_fs_index_service: JudgeFsIndexService = field(init=False)
    worker_queue_service: WorkerQueueService = field(init=False)
    system_config_service: SystemConfigService = field(init=False)
    templates: Jinja2Templates = field(default_factory=lambda: Jinja2Templates(directory='app/templates'))
    run_execute_lock: threading.Lock = field(default_factory=threading.Lock)
    run_execute_workers: set[WorkerFuture] = field(default_factory=set)
    preview_lock: threading.Lock = field(default_factory=threading.Lock)
    preview_inflight: set[str] = field(default_factory=set)
    export_lock: threading.Lock = field(default_factory=threading.Lock)
    export_workers: set[WorkerFuture] = field(default_factory=set)
    export_inflight: set[str] = field(default_factory=set)
    verification_lock: threading.Lock = field(default_factory=threading.Lock)
    verification_workers: set[WorkerFuture] = field(default_factory=set)
    verification_inflight: set[str] = field(default_factory=set)
    login_rate_limit_lock: threading.Lock = field(default_factory=threading.Lock)
    login_rate_limit_state: dict[str, dict[str, float | int]] = field(default_factory=dict)
    password_form_csrf_secret: bytes = field(init=False)

    def _resolve_password_form_csrf_secret(self) -> bytes:
        configured = str(self.constants.PASSWORD_FORM_CSRF_SECRET or "").strip()
        if configured:
            return configured.encode("utf-8")
        existing = bytes(getattr(self, "password_form_csrf_secret", b"") or b"")
        if existing:
            return existing
        return secrets.token_hex(32).encode("utf-8")

    def reload_runtime_values(self) -> dict[str, object]:
        runtime_overrides = self.system_config_service.refresh()
        effective = build_runtime_values(runtime_overrides)
        self.constants.replace(effective.to_dict())
        configure_main_utils_runtime_values(self.constants)
        apply_tests_spec_runtime_values(self.constants)
        apply_toolchain_runtime_values(self.constants)
        apply_workspace_runtime_values(self.constants)
        self.toolchain_service.apply_runtime_values(self.constants)
        self.build_service.apply_runtime_values(self.constants)
        self.preview_service.apply_runtime_values(self.constants)
        self.run_service.apply_runtime_values(self.constants)
        self.judgehost_task_service.apply_runtime_values(self.constants)
        self.invocation_backend_service.apply_runtime_values(self.constants)
        self.password_form_csrf_secret = self._resolve_password_form_csrf_secret()
        return runtime_overrides

    def __post_init__(self) -> None:
        self.db = DB(self.settings.db_path)
        self.system_config_service = SystemConfigService(self.db)
        self.async_task_cache_service = AsyncTaskCacheService(self.db, self.settings.cache_root)
        self.judge_fs_index_service = JudgeFsIndexService(self.settings.cache_root)
        runtime_overrides = self.system_config_service.refresh()
        self.constants = build_runtime_values(runtime_overrides)
        configure_main_utils_runtime_values(self.constants)
        apply_tests_spec_runtime_values(self.constants)
        apply_toolchain_runtime_values(self.constants)
        apply_workspace_runtime_values(self.constants)
        self.workspace_service = WorkspaceService(self.db, self.settings)
        self.git_service = GitService()
        self.fs_manager = FsManager(self.settings.artifacts_root, self.settings.run_root)
        self.artifact_service = ArtifactService(self.settings.artifacts_root)
        self.sandbox_backend = NativeSandboxBackend(root_switch_tool=str(self.constants.SANDBOX_ROOT_SWITCH_TOOL))
        self.toolchain_service = ToolchainService(
            self.settings.cache_root,
            sandbox_backend=self.sandbox_backend,
            constants=self.constants,
        )
        self.build_service = BuildService(
            self.db,
            self.workspace_service,
            self.artifact_service,
            self.toolchain_service,
            sandbox_backend=self.sandbox_backend,
            constants=self.constants,
            async_task_cache_service=self.async_task_cache_service,
        )
        self.preview_service = PreviewService(
            self.db,
            self.workspace_service,
            self.artifact_service,
            build_service=self.build_service,
            sandbox_backend=self.sandbox_backend,
            constants=self.constants,
            async_task_cache_service=self.async_task_cache_service,
        )
        self.run_service = RunService(
            self.db,
            self.workspace_service,
            self.toolchain_service,
            sandbox_backend=self.sandbox_backend,
            constants=self.constants,
        )
        self.judgehost_task_service = JudgehostTaskService(
            self.db,
            self.run_service,
            self.settings,
            self.constants,
            judge_fs_index_service=self.judge_fs_index_service,
        )
        self.invocation_backend_service = InvocationBackendService(
            self.run_service,
            judgehost_task_service=self.judgehost_task_service,
            configured_backend_name=str(self.constants.INVOCATION_BACKEND),
        )
        self.build_service.bind_runtime_services(
            invocation_backend_service=self.invocation_backend_service,
            judgehost_task_service=self.judgehost_task_service,
        )
        self.export_service = ExportService(self.db, self.settings.artifacts_root, self.settings.workspace_root)
        durable_log_raw = str(self.constants.WORKER_QUEUE_DURABLE_LOG or "").strip()
        durable_log_path = self.settings.cache_root / "worker-queue-events.jsonl"
        if durable_log_raw:
            durable_log_path = Path(durable_log_raw).expanduser().resolve()
        self.worker_queue_service = WorkerQueueService(
            worker_count=int(self.constants.WORKER_QUEUE_THREADS),
            history_limit=int(self.constants.WORKER_QUEUE_HISTORY_LIMIT),
            queue_capacity=int(self.constants.WORKER_QUEUE_CAPACITY),
            durable_history_limit=int(self.constants.WORKER_QUEUE_DURABLE_HISTORY_LIMIT),
            durable_log_path=durable_log_path,
        )
        self.password_form_csrf_secret = self._resolve_password_form_csrf_secret()
config = RuntimeConfig()
