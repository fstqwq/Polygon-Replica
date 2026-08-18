"""Declarative inventory of cleanup-safe derived database state."""

from typing import Literal, TypedDict


ARTIFACT_TABLES = (
    "statement_previews",
    "previews",
    "contest_build_items",
    "contest_artifacts",
    "export_jobs",
    "exports",
    "problem_package_builds",
    "problem_package_materializations",
    "contest_jobs",
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
