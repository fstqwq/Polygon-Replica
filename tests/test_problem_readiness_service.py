from tests.common import E2ETestBase, runtime
from tests.db_helpers import admit_test_verification, db_execute, db_fetch_one
from tests.identity_helpers import canonical_test_verification_id

from app.db import now_iso
from app.service.problem.readiness import WorkspaceReadinessSubject
from app.service.verification.types import VerificationStatus


class TestProblemReadinessService(E2ETestBase):
    seed_default_workspace = False

    def setUp(self) -> None:
        super().setUp()
        workspace = self._workspace_path()
        head = runtime.git_service.commit(
            workspace, "Published readiness source", self.user, f"{self.user}@example.com",
        )
        runtime.git_service.push(workspace, "main")
        context = runtime.workspace_service.workspace_context(self.problem, self.user)
        self.subject: WorkspaceReadinessSubject = {
            "problem_id": context["problem"]["id"],
            "workspace_id": context["workspace"]["id"],
            "workspace_path": workspace,
            "head_commit": head,
            "dirty": False,
            "local_revision": 1,
            "upstream_revision": 1,
            "needs_update": False,
        }

    def _historical_result(
        self, name: str, *, status: VerificationStatus, source_commit: str,
        published: bool = False, reason: str = "",
    ) -> str:
        verification_id = canonical_test_verification_id(f"{self.test_id}-{name}")
        admitted = admit_test_verification(
            verification_id=verification_id,
            problem_id=self.subject["problem_id"],
            workspace_id=None if published else self.subject["workspace_id"],
            source_commit=source_commit,
        )
        self.assertEqual(admitted.outcome, "admitted")
        db_execute(
            "UPDATE verifications SET status=?, fail_reason=?, finished_at=? WHERE id=?",
            [status.value, reason, now_iso(), verification_id],
        )
        return verification_id

    def test_active_published_build_is_queued_without_mutating_its_state(self) -> None:
        build_id = f"build-{self.test_id}"
        db_execute(
            """INSERT INTO problem_package_builds
               (id,problem_id,source_commit,phase,status,created_at)
               VALUES(?,?,?,'queued','queued',?)""",
            [build_id, self.subject["problem_id"], self.subject["head_commit"], now_iso()],
        )
        before = db_fetch_one("SELECT * FROM problem_package_builds WHERE id=?", [build_id])
        readiness = runtime.problem_readiness_service.readiness(self.subject)
        self.assertEqual(readiness["package"]["state"], "queued")
        self.assertEqual(readiness["package"]["published_commit"], self.subject["head_commit"])
        after = db_fetch_one("SELECT * FROM problem_package_builds WHERE id=?", [build_id])
        self.assertEqual(tuple(after), tuple(before))

    def test_batch_and_workspace_reads_share_failure_but_only_workspace_explains_it(self) -> None:
        verification_id = self._historical_result(
            "failed", status=VerificationStatus.FAILED,
            source_commit=self.subject["head_commit"], reason="checker exited with code 1",
        )
        single = runtime.problem_readiness_service.readiness(self.subject)["verification"]
        batch = runtime.problem_readiness_service.readiness_many([self.subject])[
            self.subject["problem_id"]
        ]["verification"]
        for result in (single, batch):
            self.assertEqual(result["verification_id"], verification_id)
            self.assertEqual(result["result"], "failed")
            self.assertFalse(result["stale"])
        self.assertEqual(single["reason_short"], "checker exited with code 1")
        self.assertEqual(batch["reason_short"], "")

    def test_current_published_result_wins_over_newer_stale_workspace_failure(self) -> None:
        published_id = self._historical_result(
            "published", status=VerificationStatus.OK,
            source_commit=self.subject["head_commit"], published=True,
        )
        self._historical_result(
            "stale", status=VerificationStatus.FAILED, source_commit="c" * 40,
        )
        projected = runtime.problem_readiness_service.readiness(self.subject)["verification"]
        self.assertEqual(projected["verification_id"], published_id)
        self.assertEqual(projected["result"], "ok")
        self.assertFalse(projected["stale"])
