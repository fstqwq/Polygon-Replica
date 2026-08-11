from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.impl.runtime.config import config
from app.service.problem.content_review import problem_content_review
from app.service.problem.readiness import WorkspaceReadinessSubject
from tests.common import E2ETestBase
from tests.db_helpers import db_execute
from tests.identity_helpers import canonical_test_verification_id


class TestProblemReadiness(E2ETestBase):
    def test_content_review_returns_only_actionable_checks(self) -> None:
        review = problem_content_review(
            time_limit_ms=400,
            memory_limit_mb=128,
            test_count=0,
            tests_valid=True,
            solution_count=3,
            solutions_truncated=False,
            main_solution_ready=False,
            output_component_label="Checker",
            output_component_display="missing",
            output_component_ready=False,
            validator_display="validator.cpp",
            validator_ready=True,
            statement_language_names=[],
        )

        self.assertEqual(review["tone"], "danger")
        self.assertEqual(
            [warning["code"] for warning in review["warnings"]],
            [
                "tests",
                "solutions",
                "output_component",
                "languages",
                "time_limit",
                "memory_limit",
            ],
        )
        self.assertEqual(review["tests"]["display"], "0 tests")
        self.assertEqual(
            review["solutions"]["display"],
            "3 solutions \u00b7 no main correct",
        )
        self.assertEqual(review["time_limit"]["tone"], "warning")
        self.assertEqual(review["memory_limit"]["tone"], "warning")

    def test_content_review_marks_complete_content_ready(self) -> None:
        review = problem_content_review(
            time_limit_ms=2_000,
            memory_limit_mb=1_024,
            test_count=5,
            tests_valid=True,
            solution_count=2,
            solutions_truncated=False,
            main_solution_ready=True,
            output_component_label="Interactor",
            output_component_display="interactor.cpp",
            output_component_ready=True,
            validator_display="validator.cpp",
            validator_ready=True,
            statement_language_names=["english"],
        )

        self.assertEqual(review["tone"], "normal")
        self.assertEqual(review["warnings"], [])

    def _subject(self) -> WorkspaceReadinessSubject:
        context = config.workspace_service.workspace_context(
            self.problem,
            self.user,
            include_recent=False,
        )
        workspace = context["workspace"]
        return {
            "problem_id": int(context["problem"]["id"]),
            "workspace_id": int(workspace["id"]),
            "workspace_path": Path(str(workspace["path"])),
            "head_commit": str(workspace["head_commit"] or ""),
            "dirty": False,
            "local_revision": 1,
            "upstream_revision": 1,
            "needs_update": False,
        }

    @staticmethod
    def _none_package(problem_id: int) -> dict[str, object]:
        return {
            "problem_id": problem_id,
            "published_commit": "a" * 40,
            "published_revision_number": 1,
            "materialized_revision_number": None,
            "materialization_id": "",
            "status": "none",
            "missing_reason": "Package not built",
        }

    def test_batch_readiness_is_select_only_and_skips_failure_details(self) -> None:
        subject = self._subject()
        verification_id = canonical_test_verification_id("readiness-batch-failed")
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=subject["problem_id"],
            workspace_id=subject["workspace_id"],
            signature="",
            source_commit=subject["head_commit"],
            kind="all",
            status="failed",
        )
        db_execute(
            "UPDATE verifications SET fail_reason=? WHERE id=?",
            ["checker exited with code 1", verification_id],
        )
        batch_reader = config.verification_service.workspace_verification_rows_many
        with (
            patch.object(
                config.verification_service,
                "workspace_verification_rows",
                side_effect=AssertionError("batch readiness must not use per-workspace queries"),
            ),
            patch.object(
                config.verification_service,
                "workspace_verification_rows_many",
                wraps=batch_reader,
            ) as rows_many,
            patch.object(
                config.verification_service,
                "verification_detail",
                side_effect=AssertionError("contest readiness must not read details"),
            ),
            patch.object(
                config.verification_service.task_store,
                "list_rows",
                side_effect=AssertionError("contest readiness must not read tasks"),
            ),
            patch.object(
                config.problem_package_service,
                "published_readiness_many",
                return_value={
                    subject["problem_id"]: self._none_package(subject["problem_id"])
                },
            ),
            patch.object(
                config.db,
                "execute",
                side_effect=AssertionError("readiness must not write SQLite"),
            ),
            patch.object(
                config.db,
                "write_transaction",
                side_effect=AssertionError("readiness must not open write transactions"),
            ),
        ):
            result = config.problem_readiness_service.readiness_many([subject])

        rows_many.assert_called_once()
        readiness = result[subject["problem_id"]]
        self.assertEqual(readiness["verification"]["result"], "failed")
        self.assertEqual(readiness["verification"]["reason_short"], "")
        self.assertEqual(readiness["package"]["state"], "none")

    def test_workspace_readiness_explains_failure_and_ignores_package_verification(self) -> None:
        subject = self._subject()
        workspace_verification_id = canonical_test_verification_id(
            "readiness-workspace-failed"
        )
        config.verification_service.begin_verification_record(
            verification_id=workspace_verification_id,
            problem_id=subject["problem_id"],
            workspace_id=subject["workspace_id"],
            signature="",
            source_commit=subject["head_commit"],
            kind="all",
            status="failed",
        )
        db_execute(
            "UPDATE verifications SET fail_reason=? WHERE id=?",
            ["checker exited with code 1", workspace_verification_id],
        )
        config.verification_service.begin_verification_record(
            verification_id=canonical_test_verification_id("readiness-package-ok"),
            problem_id=subject["problem_id"],
            workspace_id=None,
            signature="package-signature",
            source_commit=subject["head_commit"],
            kind="all",
            status="ok",
        )

        with patch.object(
            config.problem_package_service,
            "published_readiness",
            return_value=self._none_package(subject["problem_id"]),
        ):
            readiness = config.problem_readiness_service.readiness(subject)

        verification = readiness["verification"]
        self.assertEqual(verification["verification_id"], workspace_verification_id)
        self.assertEqual(verification["result"], "failed")
        self.assertEqual(verification["reason_short"], "checker exited with code 1")

    def test_review_template_lists_upstream_first_and_marks_only_workspace(self) -> None:
        template = config.templates.env.get_template("_problem_review.html")
        module = template.make_module()
        html = str(
            module.problem_review(
                {
                    "workspace": {
                        "state": "behind",
                        "local_revision": 1,
                        "upstream_revision": 2,
                        "dirty": True,
                        "needs_update": True,
                        "tone": "danger",
                    },
                    "verification": {
                        "result": "ok",
                        "display": "ok",
                        "stale": False,
                        "sanity_status": "ok",
                        "tone": "normal",
                        "verification_id": "verification-template",
                        "reason_short": "",
                        "created_at": "",
                    },
                    "package": {
                        "state": "stale",
                        "revision_number": 1,
                        "tone": "warning",
                        "reason": "A newer revision has not been packaged",
                    },
                }
            )
        )

        upstream = "Upstream: <strong>v2</strong>"
        workspace = 'Workspace: <strong class="danger">v1</strong>'
        self.assertIn(upstream, html)
        self.assertIn(workspace, html)
        self.assertLess(html.index(upstream), html.index(workspace))
        self.assertNotIn('Upstream: <strong class="danger">', html)
        self.assertIn('Package: <span class="warn">v1 (stale)</span>', html)
