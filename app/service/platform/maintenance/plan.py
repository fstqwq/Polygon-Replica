"""Declarative inventory of cleanup-safe derived database state."""

from typing import Literal, Protocol, TypedDict, runtime_checkable


ARTIFACT_TABLES = (
    "statement_previews",
    "export_jobs",
    "exports",
    "problem_package_builds",
    "problem_package_materializations",
    "verification_task_artifacts",
    "verification_selected_tests",
    "verification_source_paths",
    "verification_sanity_check_messages",
    "verification_sanity_checks",
    "verification_tests_meta",
    "verification_task_diagnostics",
    "verification_tasks",
    "verifications",
)

REDUNDANT_DATABASE_INDEXES = (
    "idx_workspaces_problem_user",
    "idx_contests_slug",
    "idx_contest_members_contest",
    "idx_contest_problems_contest",
    "idx_verification_selected_tests_verification_ordinal",
    "idx_verification_source_paths_verification_ordinal",
    "idx_verification_sanity_checks_verification_ordinal",
    "idx_verification_sanity_check_messages_verification_check",
    "idx_verification_tests_meta_verification_ordinal",
    "idx_pending_registrations_token",
)

CleanupFilesystemClass = Literal["artifacts_root", "cache_root"]
CLEANUP_FILESYSTEM_CLASSES: tuple[CleanupFilesystemClass, ...] = (
    "artifacts_root",
    "cache_root",
)


class ArtifactUsageSnapshot(TypedDict):
    artifacts_bytes: int
    artifacts_files: int
    cache_bytes: int
    cache_files: int
    total_bytes: int
    total_files: int
    artifact_rows: int
    removable_rows: int
    table_rows: dict[str, int]


class SourceTreeStats(TypedDict):
    entries: int
    bytes: int


class MaintenanceResult(TypedDict, total=False):
    """Progress and outcome fields emitted by cleanup and source backup."""

    operation_id: str
    started_at: str
    finished_at: str
    completed_stage: str
    failed_stage: str
    error: str
    duration_ms: int
    roots: dict[str, str]
    deleted_rows: dict[str, int]
    deleted_row_total: int
    affected_row_total: int
    reclaimed_bytes: dict[str, int]
    total_reclaimed_bytes: int
    database_bytes_before: int
    database_bytes_after: int
    filesystem_bytes_before: dict[CleanupFilesystemClass, int]
    source_stats: dict[str, SourceTreeStats]
    archive_bytes: int


@runtime_checkable
class MaintenanceFailureDetails(Protocol):
    """Typed report attached by the operation runner to its original exception."""

    maintenance_result: MaintenanceResult
