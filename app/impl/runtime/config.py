from __future__ import annotations
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.db import DB
from app.main_util import configure_runtime_values
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.artifact import ArtifactService
from app.service.platform.async_task_cache import AsyncTaskCacheService
from app.service.auth.service import AuthService
from app.service.contest.service import ContestService
from app.service.platform.fs.layout import FsManager
from app.service.verification.service import VerificationService
from app.service.export.api import ExportService
from app.service.repository.git import GitService
from app.service.platform.judge_fs_index import JudgeFsIndexService
from app.service.judgehost.api import Judgehost
from app.service.sandbox.base import SandboxBackend
from app.service.sandbox.tex_backend import TexSandboxBackend
from app.service.statement.preview import PreviewService
from app.service.platform.system_config import SystemConfigService
from app.service.disk.auth_store import AuthStore
from app.service.disk.runtime_state_store import RuntimeStateStore
from app.service.runtime.state_service import RuntimeStateService
from app.service.problem import test_spec
from app.service.runtime import toolchain
from app.service.platform.worker_queue import WorkerFuture, WorkerQueueService
from app.service.repository import workspace
from app.setting import Settings, load_settings
from app.service.platform import workspace_path

@dataclass
class RuntimeConfig:
    TEMPLATE_ROOT: Path = Path(__file__).resolve().parents[2] / "template"
    settings: Settings = field(default_factory=load_settings)
    constants: RuntimeValues = field(init=False)
    db: DB = field(init=False)
    workspace_service: workspace.WorkspaceService = field(init=False)
    auth_service: AuthService = field(init=False)
    contest_service: ContestService = field(init=False)
    git_service: GitService = field(init=False)
    fs_manager: FsManager = field(init=False)
    artifact_service: ArtifactService = field(init=False)
    preview_sandbox_backend: SandboxBackend = field(init=False)
    verification_service: VerificationService = field(init=False)
    preview_service: PreviewService = field(init=False)
    judgehost_task_service: Judgehost = field(init=False)
    export_service: ExportService = field(init=False)
    async_task_cache_service: AsyncTaskCacheService = field(init=False)
    judge_fs_index_service: JudgeFsIndexService = field(init=False)
    worker_queue_service: WorkerQueueService = field(init=False)
    system_config_service: SystemConfigService = field(init=False)
    runtime_state_service: RuntimeStateService = field(init=False)
    templates: Jinja2Templates = field(
        default_factory=lambda: Jinja2Templates(directory=str(RuntimeConfig.TEMPLATE_ROOT))
    )
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
        configure_runtime_values(self.constants)
        workspace_path.apply_runtime_values(self.constants)
        self.db.apply_runtime_values(self.constants)
        test_spec.apply_runtime_values(self.constants)
        toolchain.apply_runtime_values(self.constants)
        workspace.apply_runtime_values(self.constants)
        self.auth_service.apply_runtime_values(self.constants)
        self.verification_service.apply_runtime_values(self.constants)
        self.preview_service.apply_runtime_values(self.constants)
        self.judgehost_task_service.apply_runtime_values(self.constants)
        self.password_form_csrf_secret = self._resolve_password_form_csrf_secret()
        return runtime_overrides

    def __post_init__(self) -> None:
        self.db = DB(self.settings.db_path)
        self.system_config_service = SystemConfigService(self.db)
        self.async_task_cache_service = AsyncTaskCacheService(self.db, self.settings.cache_root)
        self.judge_fs_index_service = JudgeFsIndexService(self.settings.cache_root)
        runtime_overrides = self.system_config_service.refresh()
        self.constants = build_runtime_values(runtime_overrides)
        self.db.apply_runtime_values(self.constants)
        configure_runtime_values(self.constants)
        self.auth_service = AuthService(AuthStore(self.db, constants=self.constants), constants=self.constants)
        self.runtime_state_service = RuntimeStateService(self.db, RuntimeStateStore(self.db))
        workspace_path.apply_runtime_values(self.constants)
        test_spec.apply_runtime_values(self.constants)
        toolchain.apply_runtime_values(self.constants)
        workspace.apply_runtime_values(self.constants)
        self.workspace_service = workspace.WorkspaceService(self.db, self.settings)
        self.contest_service = ContestService(self.db, self.settings)
        self.git_service = GitService()
        self.fs_manager = FsManager(self.settings.artifacts_root, self.settings.run_root)
        self.artifact_service = ArtifactService(self.settings.artifacts_root)
        self.preview_sandbox_backend = TexSandboxBackend()
        self.judgehost_task_service = Judgehost(
            self.db,
            self.workspace_service,
            self.fs_manager,
            self.settings,
            self.constants,
            judge_fs_index_service=self.judge_fs_index_service,
        )
        self.verification_service = VerificationService(
            self.db,
            self.workspace_service,
            self.artifact_service,
            self.judgehost_task_service,
            constants=self.constants,
        )
        self.preview_service = PreviewService(
            self.db,
            self.workspace_service,
            self.artifact_service,
            verification_service=self.verification_service,
            sandbox_backend=self.preview_sandbox_backend,
            constants=self.constants,
            async_task_cache_service=self.async_task_cache_service,
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

