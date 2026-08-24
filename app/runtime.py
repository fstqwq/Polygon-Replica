"""Composition root for one Polygon Replica application process."""

import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.db import DB, SchemaRequirementsError
from app.config import ConfigValues, build_config_values
from app.service.auth.service import AuthService
from app.service.access.query import AccessQuery
from app.service.access.command import AccessCommand
from app.service.agent.service import AgentService
from app.service.contest.service import ContestService
from app.service.contest.package import ContestPackageService
from app.service.contest.snapshot import ContestSourceSnapshotService
from app.service.contest.statement import ContestStatementService
from app.service.contest.problem_query import ContestProblemQueryService
from app.service.contest.statement_preview import ContestStatementPreviewService
from app.service.platform.fs.layout import StorageLayout
from app.service.verification.service import VerificationService
from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.execution import VerificationExecutionService
from app.service.verification.execution_plan import VerificationExecutionPlanner
from app.service.verification.sanity import VerificationSanityService
from app.service.verification.workflow import VerificationWorkflow
from app.service.verification.runtime_registry import VerificationRuntimeRegistry
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.judgehost_adapter import VerificationJudgehostAdapter
from app.service.export.service import ExportService
from app.service.problem_package.service import ProblemPackageService
from app.service.problem_package.workflow import NativePackageWorkflow
from app.service.problem.readiness import ProblemReadinessService
from app.service.problem.query import ProblemSourceQueryService
from app.service.repository.git import GitService
from app.service.repository.merge import WorkspaceMergeService
from app.service.platform.archive_integrity import ArchiveIntegrityVerifier
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.platform.static_assets import StaticAssetManifest
from app.service.judgehost.api import Judgehost
from app.service.sandbox.base import SandboxBackend
from app.service.sandbox.tex_backend import TexSandboxBackend
from app.service.statement.tex_compile import TexCompileService
from app.service.statement.html_render import StatementHtmlRenderer
from app.service.statement.transient_preview import StatementPreviewService
from app.service.statement.examples import StatementExamplesProducer
from app.service.platform.system_config import SystemConfigService
from app.service.mail.smtp_config import SmtpConfigService
from app.service.workspace.archive import WorkspaceArchiveService
from app.service.workspace.files import WorkspaceFileService
from app.service.workspace.mutation import WorkspaceMutationService
from app.service.disk.auth_store import AuthStore
from app.service.disk.runtime_state_store import RuntimeStateStore
from app.service.disk.statement_preview_store import StatementPreviewStore
from app.service.runtime.state_service import RuntimeStateService
from app.service.platform.worker_queue import WorkerFuture, WorkerQueueService
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate
from app.service.platform.maintenance.artifact import ArtifactCleanupService
from app.service.platform.maintenance.coordinator import MaintenanceCoordinator
from app.service.platform.maintenance.database import ArtifactCleanupDatabase
from app.service.platform.maintenance.filesystem import (
    ArtifactCleanupFilesystem,
    validate_runtime_startup_preconditions,
)
from app.service.platform.source_backup import SourceBackupService
from app.service.repository import workspace
from app.setting import Settings


@dataclass
class ApplicationRuntime:  # pylint: disable=too-many-instance-attributes,invalid-name
    """Own every concrete service and process-scoped coordination primitive."""

    settings: Settings
    TEMPLATE_ROOT: Path = (
        Path(__file__).resolve().parent / "template"
    )  # pylint: disable=invalid-name
    STATIC_ROOT: Path = (
        Path(__file__).resolve().parent / "static"
    )  # pylint: disable=invalid-name
    config_values: ConfigValues = field(init=False)
    db: DB = field(init=False)  # pylint: disable=invalid-name
    schema_error: SchemaRequirementsError | None = field(init=False, default=None)
    workspace_service: workspace.WorkspaceService = field(init=False)
    access_query: AccessQuery = field(init=False)
    access_command: AccessCommand = field(init=False)
    auth_service: AuthService = field(init=False)
    agent_service: AgentService = field(init=False)
    contest_service: ContestService = field(init=False)
    contest_package_service: ContestPackageService = field(init=False)
    contest_snapshot_service: ContestSourceSnapshotService = field(init=False)
    contest_statement_service: ContestStatementService = field(init=False)
    contest_problem_query_service: ContestProblemQueryService = field(init=False)
    git_service: GitService = field(init=False)
    workspace_merge_service: WorkspaceMergeService = field(init=False)
    storage_layout: StorageLayout = field(init=False)
    runtime_blob_store: RuntimeBlobStore = field(init=False)
    runtime_cache_index: RuntimeCacheIndex = field(init=False)
    archive_integrity: ArchiveIntegrityVerifier = field(init=False)
    tex_sandbox_backend: SandboxBackend = field(init=False)
    tex_compile_service: TexCompileService = field(init=False)
    verification_service: VerificationService = field(init=False)
    verification_task_store: VerificationTaskStore = field(init=False)
    verification_task_completion_service: VerificationTaskCompletionService = field(
        init=False
    )
    verification_runtime_registry: VerificationRuntimeRegistry = field(init=False)
    verification_execution_service: VerificationExecutionService = field(init=False)
    verification_planner: VerificationExecutionPlanner = field(init=False)
    verification_sanity_service: VerificationSanityService = field(init=False)
    verification_workflow: VerificationWorkflow = field(init=False)
    statement_html_renderer: StatementHtmlRenderer = field(init=False)
    statement_preview_service: StatementPreviewService = field(init=False)
    contest_statement_preview_service: ContestStatementPreviewService = field(init=False)
    judgehost_task_service: Judgehost = field(init=False)
    export_service: ExportService = field(init=False)
    problem_package_service: ProblemPackageService = field(init=False)
    native_package_workflow: NativePackageWorkflow = field(init=False)
    problem_readiness_service: ProblemReadinessService = field(init=False)
    problem_source_query_service: ProblemSourceQueryService = field(init=False)
    worker_queue_service: WorkerQueueService = field(init=False)
    artifact_cleanup_service: ArtifactCleanupService = field(init=False)
    maintenance_admission_gate: MaintenanceAdmissionGate = field(init=False)
    source_backup_service: SourceBackupService = field(init=False)
    maintenance_service: MaintenanceCoordinator = field(init=False)
    system_config_service: SystemConfigService = field(init=False)
    smtp_config_service: SmtpConfigService = field(init=False)
    runtime_state_service: RuntimeStateService = field(init=False)
    workspace_archive_service: WorkspaceArchiveService = field(init=False)
    workspace_file_service: WorkspaceFileService = field(init=False)
    workspace_mutation_service: WorkspaceMutationService = field(init=False)
    static_assets: StaticAssetManifest = field(init=False)
    templates: Jinja2Templates = field(
        default_factory=lambda: Jinja2Templates(
            directory=str(ApplicationRuntime.TEMPLATE_ROOT)
        )
    )
    export_lock: threading.Lock = field(default_factory=threading.Lock)
    export_workers: set[WorkerFuture] = field(default_factory=set)
    export_inflight: set[str] = field(default_factory=set)
    verification_lock: threading.Lock = field(default_factory=threading.Lock)
    verification_workers: set[WorkerFuture] = field(default_factory=set)
    verification_inflight: set[str] = field(default_factory=set)
    login_rate_limit_lock: threading.Lock = field(default_factory=threading.Lock)
    login_rate_limit_state: dict[str, dict[str, float | int]] = field(
        default_factory=dict
    )
    password_form_csrf_secret: bytes = field(init=False)

    def _resolve_password_form_csrf_secret(self) -> bytes:
        """Resolve the runtime-owned form secret from canonical configuration."""
        configured = self.config_values.text("PASSWORD_FORM_CSRF_SECRET").strip()
        if configured:
            return configured.encode("utf-8")
        existing = bytes(getattr(self, "password_form_csrf_secret", b"") or b"")
        if existing:
            return existing
        return secrets.token_hex(32).encode("utf-8")

    def _reset_process_job_tracking(self) -> None:
        with self.export_lock:
            self.export_workers.clear()
            self.export_inflight.clear()
        with self.verification_lock:
            self.verification_workers.clear()
            self.verification_inflight.clear()

    def _cleanup_problem_judgehost_runtime(self, problem_slug: str) -> None:
        """Remove Judgehost state owned by one deleted problem."""
        run_ids = self.judgehost_task_service.problem_run_ids(problem_slug)
        self.judgehost_task_service.forget_domjudge_runs(run_ids)
        self.judgehost_task_service.forget_problem_tasks(problem_slug)

    def reload_config(
        self, *, include_restart_required: bool = False
    ) -> dict[str, object]:
        """Refresh mutable runtime configuration from its durable store."""
        runtime_overrides = self.system_config_service.refresh(
            include_restart_required=include_restart_required,
        )
        effective = build_config_values(runtime_overrides)
        self.config_values.replace(effective.snapshot())
        self.password_form_csrf_secret = self._resolve_password_form_csrf_secret()
        return runtime_overrides

    def __post_init__(self) -> None:  # pylint: disable=too-many-statements
        self.storage_layout = StorageLayout.from_settings(self.settings)
        validate_runtime_startup_preconditions(self.storage_layout)
        self.static_assets = StaticAssetManifest(self.STATIC_ROOT)
        self.templates.env.globals["static_asset_url"] = self.static_assets.url
        self.config_values = build_config_values()
        self.db = DB(
            self.storage_layout.database_path, config_values=self.config_values
        )
        try:
            self.db.init()
        except SchemaRequirementsError as exc:
            self.schema_error = exc
            return
        self.verification_task_store = VerificationTaskStore(self.db)
        self.system_config_service = SystemConfigService(self.db)
        self.smtp_config_service = SmtpConfigService(self.db)
        runtime_overrides = self.system_config_service.refresh(
            include_restart_required=True
        )
        effective = build_config_values(runtime_overrides)
        self.config_values.replace(effective.snapshot())
        self.auth_service = AuthService(
            AuthStore(self.db, config_values=self.config_values),
            config_values=self.config_values,
        )
        self.runtime_state_service = RuntimeStateService(
            self.db, RuntimeStateStore(self.db)
        )
        self.access_query = AccessQuery(self.db)
        self.access_command = AccessCommand(self.db)
        self.workspace_service = workspace.WorkspaceService(
            self.db,
            self.storage_layout,
            access_query=self.access_query,
            verification_task_store=self.verification_task_store,
            config_values=self.config_values,
        )
        self.agent_service = AgentService(
            self.db, self.workspace_service, self.access_query
        )
        self.contest_service = ContestService(
            self.db,
            self.storage_layout,
            access_query=self.access_query,
            config_values=self.config_values,
        )
        self.git_service = GitService()
        self.workspace_merge_service = WorkspaceMergeService(
            self.storage_layout,
            self.workspace_service,
        )
        self.workspace_archive_service = WorkspaceArchiveService()
        self.workspace_file_service = WorkspaceFileService(
            self.git_service,
            self.workspace_service,
            config_values=self.config_values,
        )
        self.workspace_mutation_service = WorkspaceMutationService(
            self.workspace_service
        )
        self.runtime_blob_store = RuntimeBlobStore(
            self.storage_layout.runtime_blob_root
        )
        self.archive_integrity = ArchiveIntegrityVerifier(
            self.storage_layout.artifacts_root
        )
        self.runtime_cache_index = RuntimeCacheIndex(self.runtime_blob_store)
        self.verification_runtime_registry = VerificationRuntimeRegistry()
        self.verification_task_completion_service = VerificationTaskCompletionService(
            self.verification_task_store,
            self.runtime_blob_store,
            self.verification_runtime_registry.completion_committed,
        )
        verification_judgehost_adapter = VerificationJudgehostAdapter(
            self.db,
            self.verification_task_store,
            self.verification_task_completion_service,
            self.verification_runtime_registry,
        )
        self.tex_sandbox_backend = TexSandboxBackend()
        self.tex_compile_service = TexCompileService(
            sandbox_backend=self.tex_sandbox_backend,
            config_values=self.config_values,
        )
        self.judgehost_task_service = Judgehost(
            self.workspace_service,
            self.config_values,
            execution_port=verification_judgehost_adapter,
            runtime_blob_store=self.runtime_blob_store,
            runtime_cache_index=self.runtime_cache_index,
        )
        self.verification_service = VerificationService(
            self.db,
            self.workspace_service,
            self.judgehost_task_service,
            task_store=self.verification_task_store,
            runtime_blob_store=self.runtime_blob_store,
            storage_layout=self.storage_layout,
            config_values=self.config_values,
        )
        self.verification_execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            self.verification_runtime_registry,
            self.judgehost_task_service,
        )
        self.verification_planner = VerificationExecutionPlanner(
            self.config_values,
            self.runtime_blob_store,
        )
        self.verification_sanity_service = VerificationSanityService(
            self.judgehost_task_service,
            self.runtime_blob_store,
        )
        self.verification_workflow = VerificationWorkflow(
            planner=self.verification_planner,
            sanity_service=self.verification_sanity_service,
            verification_service=self.verification_service,
            execution_service=self.verification_execution_service,
            judgehost=self.judgehost_task_service,
            workspace_service=self.workspace_service,
            storage_layout=self.storage_layout,
            runtime_blob_store=self.runtime_blob_store,
            task_store=self.verification_task_store,
            config_values=self.config_values,
        )
        self.statement_examples_producer = StatementExamplesProducer(
            self.verification_service
        )
        self.problem_package_service = ProblemPackageService(
            self.db,
            self.storage_layout,
            archive_integrity=self.archive_integrity,
            artifact_file_resolver=self.runtime_blob_store.descriptor,
            verification_id_allocator=self.verification_service.allocate_verification_id,
            statement_examples_producer=self.statement_examples_producer,
        )
        self.statement_html_renderer = StatementHtmlRenderer(
            self.tex_sandbox_backend,
            self.config_values,
        )
        statement_preview_store = StatementPreviewStore(self.db)
        self.statement_preview_service = StatementPreviewService(
            self.db,
            self.storage_layout,
            self.workspace_service,
            self.problem_package_service,
            self.statement_examples_producer,
            self.verification_workflow,
            self.statement_html_renderer,
            self.tex_compile_service,
            statement_preview_store,
        )
        self.native_package_workflow = NativePackageWorkflow(
            self.problem_package_service,
            self.verification_service,
            self.verification_workflow,
        )
        self.problem_readiness_service = ProblemReadinessService(
            self.verification_service,
            self.problem_package_service,
        )
        self.problem_source_query_service = ProblemSourceQueryService(
            self.config_values,
        )
        self.contest_problem_query_service = ContestProblemQueryService(
            self.contest_service,
            self.access_query,
            self.workspace_service,
            self.problem_readiness_service,
            self.storage_layout,
            self.config_values,
        )
        self.export_service = ExportService(
            self.db,
            self.storage_layout,
            self.tex_compile_service,
            archive_integrity=self.archive_integrity,
            problem_package_service=self.problem_package_service,
            config_values=self.config_values,
        )
        self.worker_queue_service = WorkerQueueService(
            worker_count=self.config_values.integer("WORKER_QUEUE_THREADS"),
            history_limit=self.config_values.integer("WORKER_QUEUE_HISTORY_LIMIT"),
            queue_capacity=self.config_values.integer("WORKER_QUEUE_CAPACITY"),
            durable_history_limit=self.config_values.integer(
                "WORKER_QUEUE_DURABLE_HISTORY_LIMIT"
            ),
            durable_log_path=self.storage_layout.worker_history_path,
        )
        self.contest_statement_service = ContestStatementService(
            self.contest_service,
            self.tex_compile_service,
            error_text_limit_bytes=self.config_values.integer(
                "AUX_DISPLAY_TEXT_LIMIT_BYTES"
            ),
        )
        self.contest_package_service = ContestPackageService(
            self.contest_service,
            self.export_service.package_adapters,
            self.problem_package_service,
        )
        self.contest_snapshot_service = ContestSourceSnapshotService(
            self.storage_layout,
        )
        self.contest_statement_preview_service = ContestStatementPreviewService(
            contest_service=self.contest_service,
            access_query=self.access_query,
            workspace_service=self.workspace_service,
            package_service=self.problem_package_service,
            problem_preview_service=self.statement_preview_service,
            storage_layout=self.storage_layout,
            preview_store=statement_preview_store,
            statement_service=self.contest_statement_service,
            snapshot_service=self.contest_snapshot_service,
        )

        cleanup_database = ArtifactCleanupDatabase(
            self.db,
            self.storage_layout.database_path,
        )
        cleanup_filesystem = ArtifactCleanupFilesystem(
            self.storage_layout,
        )
        self.artifact_cleanup_service = ArtifactCleanupService(
            cleanup_database,
            cleanup_filesystem,
            self.runtime_cache_index,
            self.runtime_blob_store,
            self.worker_queue_service,
            self.judgehost_task_service,
            self.verification_task_store,
            self._reset_process_job_tracking,
        )
        self.source_backup_service = SourceBackupService(
            self.storage_layout,
        )
        self.maintenance_admission_gate = MaintenanceAdmissionGate()
        self.maintenance_service = MaintenanceCoordinator(
            admission_gate=self.maintenance_admission_gate,
            cleanup_service=self.artifact_cleanup_service,
            source_backup_service=self.source_backup_service,
            worker_queue_service=self.worker_queue_service,
            judgehost_task_service=self.judgehost_task_service,
        )
        self.worker_queue_service.set_admission_gate(self.maintenance_admission_gate)
        self.judgehost_task_service.set_admission_gate(self.maintenance_admission_gate)
        self.workspace_service.configure_problem_deletion_runtime(
            guard=self.maintenance_service.problem_deletion_guard,
            cleanup_problem_runtime=self._cleanup_problem_judgehost_runtime,
        )
        self.password_form_csrf_secret = self._resolve_password_form_csrf_secret()


def build_runtime(settings: Settings) -> ApplicationRuntime:
    """Construct one complete process runtime without installing global state."""

    return ApplicationRuntime(settings=settings)
