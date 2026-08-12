import tempfile
import unittest
from pathlib import Path
from typing import cast

from app.service.problem.readiness import (
    ProblemReadinessService,
    WorkspaceReadinessSubject,
)
from app.service.problem_package.service import (
    ProblemPackageService,
    PublishedPackageReadiness,
)
from app.service.verification.service import VerificationService
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.types import VerificationStatus, WorkspaceVerificationRow


def _verification_row(
    verification_id: str,
    *,
    source_commit: str,
    status: VerificationStatus,
    fail_reason: str = "",
) -> WorkspaceVerificationRow:
    return {
        "id": verification_id,
        "status": status,
        "signature": "",
        "source_commit": source_commit,
        "kind": "all",
        "fail_reason": fail_reason,
        "error": "",
        "sanity_status": "skipped",
        "created_at": "2026-08-10T00:00:00Z",
        "finished_at": "2026-08-10T00:00:01Z",
    }


def _missing_package(problem_id: int) -> PublishedPackageReadiness:
    return {
        "problem_id": problem_id,
        "published_commit": "a" * 40,
        "published_revision_number": 1,
        "materialized_revision_number": None,
        "materialization_id": "",
        "status": "none",
        "missing_reason": "Package not built",
    }


class _VerificationRows:
    def __init__(
        self,
        rows: dict[tuple[int, int], list[WorkspaceVerificationRow]],
    ) -> None:
        self.rows = rows
        self.single_calls = 0
        self.batch_calls = 0
        self.detail_calls = 0
        self.task_store = cast(VerificationTaskStore, None)

    def visible_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        **_kwargs: object,
    ) -> list[WorkspaceVerificationRow]:
        self.single_calls += 1
        return list(self.rows.get((problem_id, workspace_id), ()))

    def visible_verification_rows_many(
        self,
        subjects: list[tuple[int, int]],
        **_kwargs: object,
    ) -> dict[tuple[int, int], list[WorkspaceVerificationRow]]:
        self.batch_calls += 1
        return {subject: list(self.rows.get(subject, ())) for subject in subjects}

    def verification_detail(self, _verification_id: str) -> dict[str, object]:
        self.detail_calls += 1
        raise AssertionError("batch readiness must not read verification detail")


class _PackageRows:
    def __init__(self, rows: dict[int, PublishedPackageReadiness]) -> None:
        self.rows = rows

    def published_readiness(self, problem_id: int) -> PublishedPackageReadiness:
        return self.rows[problem_id]

    def published_readiness_many(
        self,
        problem_ids: list[int],
    ) -> dict[int, PublishedPackageReadiness]:
        return {problem_id: self.rows[problem_id] for problem_id in problem_ids}


class TestProblemReadinessService(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="readiness-service-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.subject: WorkspaceReadinessSubject = {
            "problem_id": 11,
            "workspace_id": 17,
            "workspace_path": self.workspace,
            "head_commit": "b" * 40,
            "dirty": False,
            "local_revision": 1,
            "upstream_revision": 1,
            "needs_update": False,
        }

    def _service(
        self,
        rows: list[WorkspaceVerificationRow],
    ) -> tuple[ProblemReadinessService, _VerificationRows]:
        verification = _VerificationRows(
            {(self.subject["problem_id"], self.subject["workspace_id"]): rows}
        )
        packages = _PackageRows(
            {self.subject["problem_id"]: _missing_package(self.subject["problem_id"])}
        )
        return (
            ProblemReadinessService(
                cast(VerificationService, verification),
                cast(ProblemPackageService, packages),
            ),
            verification,
        )

    def test_batch_projection_uses_bulk_rows_without_failure_details(self) -> None:
        service, verification = self._service(
            [
                _verification_row(
                    "ver-readiness-batch",
                    source_commit=self.subject["head_commit"],
                    status=VerificationStatus.FAILED,
                    fail_reason="checker exited with code 1",
                )
            ]
        )

        result = service.readiness_many([self.subject])

        self.assertEqual(verification.batch_calls, 1)
        self.assertEqual(verification.single_calls, 0)
        self.assertEqual(verification.detail_calls, 0)
        readiness = result[self.subject["problem_id"]]
        self.assertEqual(readiness["verification"]["result"], "failed")
        self.assertEqual(readiness["verification"]["reason_short"], "")
        self.assertEqual(readiness["package"]["state"], "none")

    def test_workspace_projection_explains_current_workspace_failure(self) -> None:
        verification_id = "ver-readiness-workspace"
        service, verification = self._service(
            [
                _verification_row(
                    verification_id,
                    source_commit=self.subject["head_commit"],
                    status=VerificationStatus.FAILED,
                    fail_reason="checker exited with code 1",
                )
            ]
        )

        readiness = service.readiness(self.subject)

        self.assertEqual(verification.single_calls, 1)
        projected = readiness["verification"]
        self.assertEqual(projected["verification_id"], verification_id)
        self.assertEqual(projected["result"], "failed")
        self.assertEqual(projected["reason_short"], "checker exited with code 1")

    def test_problem_level_verification_matching_workspace_head_is_current(self) -> None:
        published_id = "ver-readiness-published"
        service, _verification = self._service(
            [
                _verification_row(
                    "ver-readiness-newer-stale",
                    source_commit="c" * 40,
                    status=VerificationStatus.FAILED,
                ),
                _verification_row(
                    published_id,
                    source_commit=self.subject["head_commit"],
                    status=VerificationStatus.OK,
                ),
            ]
        )

        readiness = service.readiness(self.subject)

        projected = readiness["verification"]
        self.assertEqual(projected["verification_id"], published_id)
        self.assertEqual(projected["result"], "ok")
        self.assertFalse(projected["stale"])
