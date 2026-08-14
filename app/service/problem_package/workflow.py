"""Prepare one reusable verified published revision."""

import os
from pathlib import Path

from app.main_constant import SOLUTION_SOURCE_EXTENSIONS
from app.service.problem.build_config import load_build_config
from app.service.problem.solution_metadata import load_solution_desc
from app.service.problem_package.service import (
    ProblemPackageService,
    PublishedRevision,
    VerifiedRevision,
)
from app.service.verification.lifecycle import VerificationAdmission
from app.service.verification.service import VerificationService
from app.service.verification.types import Kind, VerificationStatus
from app.service.verification.workflow import VerificationWorkflow
from app.service.verification.workspace_fingerprint import (
    verification_sources_signature,
)


def build_full_verification_targets(
    snapshot: Path,
) -> tuple[list[dict[str, object]], str]:
    """Build the canonical full-verification target set from committed sources."""

    build = load_build_config(snapshot)
    accepted_source = build.get("accepted_solution_source", "")
    if not accepted_source:
        raise ValueError("main correct solution is required")

    solutions_root = snapshot / "solutions"
    sources: list[str] = []
    try:
        with os.scandir(solutions_root) as entries:
            for entry in entries:
                if Path(entry.name).suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
                    continue
                if entry.is_file(follow_symlinks=False):
                    sources.append(f"solutions/{entry.name}")
    except OSError as exc:
        raise ValueError("solutions directory cannot be read") from exc
    sources.sort()
    if accepted_source not in sources:
        raise ValueError("main correct solution source does not exist")

    targets: list[dict[str, object]] = []
    for source_path in sources:
        descriptor = load_solution_desc(snapshot, source_path)
        expected_behavior = descriptor["expected_behavior"]
        if source_path == accepted_source:
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
    solution_index = 0
    for target in targets:
        if target["path"] == accepted_source:
            target["program_id"] = "accepted"
        else:
            target["program_id"] = f"solution-{solution_index}"
            solution_index += 1
    return targets, accepted_source


class VerifiedRevisionWorkflow:
    """Run the one full verification that prepares a published revision."""

    def __init__(
        self,
        package_service: ProblemPackageService,
        verification_service: VerificationService,
        verification_workflow: VerificationWorkflow,
    ) -> None:
        self.package_service = package_service
        self.verification_service = verification_service
        self.verification_workflow = verification_workflow

    def ensure(
        self,
        *,
        revision: PublishedRevision,
        actor_user_id: int,
        actor_username: str,
    ) -> VerifiedRevision:
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
            signature = verification_sources_signature(snapshot)
            admission = self.verification_service.admit_verification(
                VerificationAdmission(
                    verification_id=verification_id,
                    problem_id=problem_id,
                    workspace_id=None,
                    signature=signature,
                    source_commit=commit,
                    kind=Kind.ALL.value,
                )
            )
            if admission.outcome != "admitted":
                raise RuntimeError("verified revision verification id already exists")
            self.verification_workflow.run(
                problem_slug,
                actor_username,
                actor_user_id=int(actor_user_id),
                problem_id=problem_id,
                workspace_id=None,
                workspace_head=commit,
                workspace_dirty=False,
                targets=targets,
                verification_id=verification_id,
                signature=signature,
                source_commit=commit,
                kind=Kind.ALL.value,
                snapshot_root_override=snapshot,
                retain_snapshot_override=True,
            )
            record = self.verification_service.verification_record(verification_id)
            if record is None or record["status"] != VerificationStatus.OK:
                error = (
                    "full verification failed"
                    if record is None
                    else record["fail_reason"] or "full verification failed"
                )
                raise ValueError(f"verified revision verification failed: {error}")
            return verification_id

        return self.package_service.ensure_verified_revision(revision, verify)
