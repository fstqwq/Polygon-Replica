from __future__ import annotations
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.db import DB
from app.main_util import configure_runtime_values
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.auth.service import AuthService
from app.service.agent.service import AgentService
from app.service.contest.service import ContestService
from app.service.platform.fs.layout import FsManager
from app.service.verification.service import VerificationService
from app.service.verification.task_store import VerificationTaskStore
from app.service.export.service import ExportService
from app.service.problem_package.service import ProblemPackageService
from app.service.repository.git import GitService
from app.service.repository.merge import WorkspaceMergeService
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.platform.static_assets import StaticAssetManifest
from app.service.judgehost.api import Judgehost
from app.service.sandbox.base import SandboxBackend
from app.service.sandbox.tex_backend import TexSandboxBackend
from app.service.statement.tex_compile import TexCompileService
from app.service.statement.preview import PreviewService
from app.service.platform.system_config import SystemConfigService
from app.service.mail.smtp_config import SmtpConfigService
from app.service.workspace.archive import WorkspaceArchiveService
from app.service.workspace.files import WorkspaceFileService
from app.service.workspace.mutation import WorkspaceMutationService
from app.service.disk.auth_store import AuthStore
from app.service.disk.runtime_state_store import RuntimeStateStore
from app.service.runtime.state_service import RuntimeStateService
from app.service.problem import test_spec
from app.service.runtime import toolchain
from app.service.platform.worker_queue import WorkerFuture, WorkerQueueService
from app.service.platform.maintenance import (
    ArtifactCleanupService,
    MaintenanceCoordinator,
    validate_runtime_startup_preconditions,
)
from app.service.repository import workspace
from app.setting import Settings, load_settings
from app.service.platform import workspace_path

@dataclass
class RuntimeConfig:
    TEMPLATE_ROOT: Path = Path(__file__).resolve().parents[2] / "template"
    STATIC_ROOT: Path = Path(__file__).resolve().parents[2] / "static"
    settings: Settings = field(default_factory=load_settings)
    constants: RuntimeValues = field(init=False)
    db: DB = field(init=False)
    workspace_service: workspace.WorkspaceService = field(init=False)
    auth_service: AuthService = field(init=False)
    agent_service: AgentService = field(init=False)
    contest_service: ContestService = field(init=False)
    git_service: GitService = field(init=False)
    workspace_merge_service: WorkspaceMergeService = field(init=False)
    fs_manager: FsManager = field(init=False)
    runtime_blob_store: RuntimeBlobStore = field(init=False)
    runtime_cache_index: RuntimeCacheIndex = field(init=False)
    tex_sandbox_backend: SandboxBackend = field(init=False)
    tex_compile_service: TexCompileService = field(init=False)
    verification_service: VerificationService = field(init=False)
    verification_task_store: VerificationTaskStore = field(init=False)
    preview_service: PreviewService = field(init=False)
    judgehost_task_service: Judgehost = field(init=False)
    export_service: ExportService = field(init=False)
    problem_package_service: ProblemPackageService = field(init=False)
    worker_queue_service: WorkerQueueService = field(init=False)
    artifact_cleanup_service: ArtifactCleanupService = field(init=False)
    maintenance_service: MaintenanceCoordinator = field(init=False)
    system_config_service: SystemConfigService = field(init=False)
    smtp_config_service: SmtpConfigService = field(init=False)
    runtime_state_service: RuntimeStateService = field(init=False)
    workspace_archive_service: WorkspaceArchiveService = field(init=False)
    workspace_file_service: WorkspaceFileService = field(init=False)
    workspace_mutation_service: WorkspaceMutationService = field(init=False)
    static_assets: StaticAssetManifest = field(init=False)
    templates: Jinja2Templates = field(
        default_factory=lambda: Jinja2Templates(directory=str(RuntimeConfig.TEMPLATE_ROOT))
    )
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

    def _reset_process_job_tracking(self) -> None:
        with self.preview_lock:
            self.preview_inflight.clear()
        with self.export_lock:
            self.export_workers.clear()
            self.export_inflight.clear()
        with self.verification_lock:
            self.verification_workers.clear()
            self.verification_inflight.clear()

    def reload_runtime_values(self, *, include_restart_required: bool = False) -> dict[str, object]:
        runtime_overrides = self.system_config_service.refresh(
            include_restart_required=include_restart_required,
        )
        effective = build_runtime_values(runtime_overrides)
        self.constants.replace(effective.to_dict())
        configure_runtime_values(self.constants)
        workspace_path.apply_runtime_values(self.constants)
        self.db.apply_runtime_values(self.constants)
        test_spec.apply_runtime_values(self.constants)
        toolchain.apply_runtime_values(self.constants)
        workspace.apply_runtime_values(self.constants)
        self.auth_service.apply_runtime_values(self.constants)
        self.agent_service.apply_runtime_values(self.constants)
        self.verification_service.apply_runtime_values(self.constants)
        self.tex_compile_service.apply_runtime_values(self.constants)
        self.judgehost_task_service.apply_runtime_values(self.constants)
        self.password_form_csrf_secret = self._resolve_password_form_csrf_secret()
        return runtime_overrides

    def __post_init__(self) -> None:
        validate_runtime_startup_preconditions(self.settings)
        self.static_assets = StaticAssetManifest(self.STATIC_ROOT)
        self.templates.env.globals["static_asset_url"] = self.static_assets.url
        self.db = DB(self.settings.db_path)
        self.verification_task_store = VerificationTaskStore(self.db)
        self.system_config_service = SystemConfigService(self.db)
        self.smtp_config_service = SmtpConfigService(self.db)
        runtime_overrides = self.system_config_service.refresh(include_restart_required=True)
        self.constants = build_runtime_values(runtime_overrides)
        self.db.apply_runtime_values(self.constants)
        configure_runtime_values(self.constants)
        self.auth_service = AuthService(AuthStore(self.db, constants=self.constants), constants=self.constants)
        self.runtime_state_service = RuntimeStateService(self.db, RuntimeStateStore(self.db))
        workspace_path.apply_runtime_values(self.constants)
        test_spec.apply_runtime_values(self.constants)
        toolchain.apply_runtime_values(self.constants)
        workspace.apply_runtime_values(self.constants)
        self.workspace_service = workspace.WorkspaceService(
            self.db,
            self.settings,
            verification_task_store=self.verification_task_store,
        )
        self.agent_service = AgentService(self.db, self.workspace_service, constants=self.constants)
        self.contest_service = ContestService(self.db, self.settings)
        self.git_service = GitService()
        self.workspace_merge_service = WorkspaceMergeService(self.settings, self.workspace_service)
        self.workspace_archive_service = WorkspaceArchiveService()
        self.workspace_file_service = WorkspaceFileService(self.git_service, self.workspace_service)
        self.workspace_mutation_service = WorkspaceMutationService(self.workspace_service)
        self.fs_manager = FsManager(self.settings.cache_root, self.settings.artifacts_root)
        self.runtime_blob_store = RuntimeBlobStore(self.fs_manager.runtime_root)
        self.runtime_cache_index = RuntimeCacheIndex(self.runtime_blob_store)
        self.tex_sandbox_backend = TexSandboxBackend()
        self.tex_compile_service = TexCompileService(
            sandbox_backend=self.tex_sandbox_backend,
            constants=self.constants,
        )
        self.judgehost_task_service = Judgehost(
            self.db,
            self.workspace_service,
            self.fs_manager,
            self.settings,
            self.constants,
            runtime_blob_store=self.runtime_blob_store,
            runtime_cache_index=self.runtime_cache_index,
            verification_task_store=self.verification_task_store,
        )
        self.verification_service = VerificationService(
            self.db,
            self.workspace_service,
            self.judgehost_task_service,
            task_store=self.verification_task_store,
            runtime_blob_store=self.runtime_blob_store,
            fs_manager=self.fs_manager,
            constants=self.constants,
        )
        self.preview_service = PreviewService(
            self.db,
            self.workspace_service,
            self.tex_compile_service,
            verification_service=self.verification_service,
        )
        self.problem_package_service = ProblemPackageService(
            self.db,
            self.settings,
            artifact_file_resolver=self.runtime_blob_store.descriptor,
            verification_id_allocator=self.verification_service.allocate_verification_id,
        )
        self.export_service = ExportService(
            self.db,
            self.settings.artifacts_root,
            self.settings.workspace_root,
            self.tex_compile_service,
            problem_package_service=self.problem_package_service,
        )
        durable_log_path = self.settings.cache_root / "runtime" / "worker-queue-events.jsonl"
        self.worker_queue_service = WorkerQueueService(
            worker_count=int(self.constants.WORKER_QUEUE_THREADS),
            history_limit=int(self.constants.WORKER_QUEUE_HISTORY_LIMIT),
            queue_capacity=int(self.constants.WORKER_QUEUE_CAPACITY),
            durable_history_limit=int(self.constants.WORKER_QUEUE_DURABLE_HISTORY_LIMIT),
            durable_log_path=durable_log_path,
        )
        self.artifact_cleanup_service = ArtifactCleanupService(
            self.db,
            self.settings,
            self.runtime_cache_index,
            self.runtime_blob_store,
            self.worker_queue_service,
            self.judgehost_task_service,
            self.verification_task_store,
            self._reset_process_job_tracking,
        )
        self.maintenance_service = MaintenanceCoordinator(
            self.artifact_cleanup_service,
            self.worker_queue_service,
            self.judgehost_task_service,
        )
        self.worker_queue_service.set_admission_gate(
            self.maintenance_service.admission_gate
        )
        self.judgehost_task_service.set_admission_gate(
            self.maintenance_service.admission_gate
        )
        self.password_form_csrf_secret = self._resolve_password_form_csrf_secret()
config = RuntimeConfig()
