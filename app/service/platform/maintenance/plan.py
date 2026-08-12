"""Declarative inventory of cleanup-safe derived database state."""

from __future__ import annotations

from typing import Literal, TypedDict


ARTIFACT_TABLES = (
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
