from __future__ import annotations

from pathlib import Path

from app.impl.runtime.config import config
from app.impl.workspace.context_job_helper import allocate_run_id
from app.impl.workspace.context_operation import (
    run_solution_options_context,
    workspace_rel_file_exists,
)
from app.service.verification.workspace_fingerprint import verification_sources_signature
from app.impl.workspace.verification_dag import run_workspace_verification_dag
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.problem_package.service import MaterializationRow, PublishedRevision
from app.service.verification.types import Kind, Status


def build_full_verification_targets(
    snapshot: Path,
) -> tuple[list[dict[str, object]], str]:
    solution_options, accepted_source, _ = run_solution_options_context(snapshot)
    if not accepted_source:
        raise ValueError("main correct solution is required")
    if not workspace_rel_file_exists(snapshot, accepted_source):
        raise ValueError("main correct solution source does not exist")
    targets: list[dict[str, object]] = []
    for row in solution_options:
        source_path = str(row.get("path") or "")
        if not source_path:
            continue
        expected_behavior = normalize_expected_behavior(
            str(row.get("expected_behavior") or "unknown")
        )
        if source_path == accepted_source or bool(row.get("is_accepted")):
            expected_behavior = "accepted"
        targets.append(
            {
                "path": source_path,
                "expected_behavior": expected_behavior,
            }
        )
    if not targets:
        raise ValueError("at least one solution source is required")
    if not any(item["expected_behavior"] == "accepted" for item in targets):
        raise ValueError("accepted solution source is required")
    targets.sort(
        key=lambda item: (
            0 if item["expected_behavior"] == "accepted" else 1,
            str(item["path"]),
        )
    )
    for target in targets:
        target["run_id"] = allocate_run_id()
    return targets, accepted_source


def ensure_published_materialization(
    *,
    revision: PublishedRevision,
    actor_user_id: int,
    actor_username: str,
) -> MaterializationRow:
    problem_slug = revision.problem["slug"]
    problem_id = int(revision.problem["id"])

    def verify(
        snapshot: Path,
        commit: str,
        revision_number: int,
        verification_id: str,
    ) -> str:
        del revision_number
        targets, _accepted_source = build_full_verification_targets(snapshot)
        run_workspace_verification_dag(
            problem_slug,
            actor_username,
            actor_user_id=int(actor_user_id),
            problem_id=problem_id,
            workspace_id=None,
            workspace_head=commit,
            workspace_dirty=False,
            targets=targets,
            verification_id=verification_id,
            signature=verification_sources_signature(snapshot),
            source_commit=commit,
            kind=Kind.ALL.value,
            snapshot_root_override=snapshot,
            retain_snapshot_override=True,
        )
        record = config.verification_service.verification_record(verification_id) or {}
        if str(record.get("status") or "") != Status.OK.value:
            error = str(record.get("fail_reason") or "full verification failed")
            raise ValueError(f"Native materialization verification failed: {error}")
        return verification_id

    return config.problem_package_service.ensure_materialization(revision, verify)
