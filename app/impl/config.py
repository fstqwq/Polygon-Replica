from __future__ import annotations
import os
import secrets
import threading
from dataclasses import dataclass, field
from fastapi.templating import Jinja2Templates
from app.db import DB
from app.main_utils import configure_runtime_values as configure_main_utils_runtime_values
from app.runtime_values import RuntimeValues, build_runtime_values
from app.services.artifact_service import ArtifactService
from app.services.build_service import BuildService
from app.services.export_service import ExportService
from app.services.git_service import GitService
from app.services.invocation_backend_service import InvocationBackendService
from app.services.preview_service import PreviewService
from app.services.run_service import RunService
from app.services.runtime_cache_service import RuntimeCacheService
from app.services.sandbox import create_sandbox_backend
from app.services.system_config_service import SystemConfigService
from app.services.tests_spec import configure_runtime_values as configure_tests_spec_runtime_values
from app.services.toolchain_service import ToolchainService
from app.services.toolchain_service import configure_runtime_values as configure_toolchain_runtime_values
from app.services.worker_queue_service import WorkerFuture, WorkerQueueService
from app.services.workspace_service import WorkspaceService
from app.services.workspace_service import configure_runtime_values as configure_workspace_runtime_values
from app.settings import Settings, load_settings

@dataclass
class RuntimeConfig:
    settings: Settings = field(default_factory=load_settings)
    constants: RuntimeValues = field(init=False)
    db: DB = field(init=False)
    workspace_service: WorkspaceService = field(init=False)
    git_service: GitService = field(init=False)
    artifact_service: ArtifactService = field(init=False)
    sandbox_backend: object = field(init=False)
    toolchain_service: ToolchainService = field(init=False)
    build_service: BuildService = field(init=False)
    preview_service: PreviewService = field(init=False)
    run_service: RunService = field(init=False)
    invocation_backend_service: InvocationBackendService = field(init=False)
    export_service: ExportService = field(init=False)
    runtime_cache_service: RuntimeCacheService = field(init=False)
    worker_queue_service: WorkerQueueService = field(init=False)
    system_config_service: SystemConfigService = field(init=False)
    templates: Jinja2Templates = field(default_factory=lambda: Jinja2Templates(directory='app/templates'))
    run_execute_lock: threading.Lock = field(default_factory=threading.Lock)
    run_execute_workers: set[WorkerFuture] = field(default_factory=set)
    preview_lock: threading.Lock = field(default_factory=threading.Lock)
    preview_workers: set[WorkerFuture] = field(default_factory=set)
    export_lock: threading.Lock = field(default_factory=threading.Lock)
    export_workers: set[WorkerFuture] = field(default_factory=set)
    export_inflight: set[str] = field(default_factory=set)
    verification_lock: threading.Lock = field(default_factory=threading.Lock)
    verification_workers: set[WorkerFuture] = field(default_factory=set)
    verification_inflight: set[str] = field(default_factory=set)
    login_rate_limit_lock: threading.Lock = field(default_factory=threading.Lock)
    login_rate_limit_state: dict[str, dict[str, float | int]] = field(default_factory=dict)
    password_form_csrf_secret: bytes = field(init=False)

    def __post_init__(self) -> None:
        self.db = DB(self.settings.db_path)
        self.system_config_service = SystemConfigService(self.db)
        runtime_overrides = self.system_config_service.refresh()
        self.constants = build_runtime_values(runtime_overrides)
        configure_main_utils_runtime_values(self.constants)
        configure_tests_spec_runtime_values(self.constants)
        configure_toolchain_runtime_values(self.constants)
        configure_workspace_runtime_values(self.constants)
        self.workspace_service = WorkspaceService(self.db, self.settings)
        self.git_service = GitService()
        self.artifact_service = ArtifactService(self.settings.artifacts_root)
        self.sandbox_backend = create_sandbox_backend()
        self.toolchain_service = ToolchainService(self.settings.cache_root, sandbox_backend=self.sandbox_backend)
        self.build_service = BuildService(self.db, self.workspace_service, self.artifact_service, self.toolchain_service, sandbox_backend=self.sandbox_backend)
        self.preview_service = PreviewService(self.db, self.workspace_service, self.artifact_service, sandbox_backend=self.sandbox_backend)
        self.run_service = RunService(self.db, self.workspace_service, self.toolchain_service, sandbox_backend=self.sandbox_backend)
        self.invocation_backend_service = InvocationBackendService(self.run_service)
        self.export_service = ExportService(self.db, self.settings.artifacts_root, self.settings.workspace_root)
        self.runtime_cache_service = RuntimeCacheService(self.db, self.settings.artifacts_root, self.settings.run_root)
        self.worker_queue_service = WorkerQueueService()
        secret = str(os.getenv('POLYGONLIKE_PASSWORD_FORM_CSRF_SECRET') or '').strip() or secrets.token_hex(32)
        self.password_form_csrf_secret = secret.encode('utf-8')
config = RuntimeConfig()
