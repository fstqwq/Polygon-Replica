import unittest

from app.impl.workspace.context_model import (
    package_published_revision_pair,
    workspace_published_revision_pair,
    workspace_revision_notice,
)
from app.service.problem.content_review import problem_content_review
from app.service.problem.readiness import PackageReadinessState, ProblemReadiness


def _readiness(
    *,
    package_state: PackageReadinessState = "ready",
    package_revision: int | None = 7,
    published_revision: int | None = 7,
    workspace_revision: int | None = 7,
    dirty: bool = False,
    needs_update: bool = False,
) -> ProblemReadiness:
    return {
        "workspace": {
            "state": "behind" if needs_update else "current",
            "local_revision": workspace_revision,
            "upstream_revision": published_revision,
            "dirty": dirty,
            "needs_update": needs_update,
            "tone": "danger" if needs_update else "normal",
        },
        "verification": {
            "result": "ok",
            "display": "ok",
            "stale": False,
            "sanity_status": "ok",
            "tone": "normal",
            "verification_id": "ver-presentation",
            "reason_short": "",
            "created_at": "2026-08-17T00:00:00Z",
        },
        "package": {
            "state": package_state,
            "revision_number": package_revision,
            "tone": "normal",
            "reason": "",
            "verified_revision_id": "vr-presentation" if package_revision else None,
            "published_commit": "a" * 40 if published_revision is not None else "",
            "published_revision_number": published_revision,
        },
    }


class TestProblemReadinessUnit(unittest.TestCase):
    def test_package_revision_pair_state_matrix(self) -> None:
        cases: tuple[
            tuple[
                PackageReadinessState,
                int | None,
                int | None,
                str,
                str,
                str,
                str,
            ],
            ...,
        ] = (
            ("ready", 7, 7, "v7", "v7", "current", "normal"),
            ("stale", 6, 7, "v6", "v7", "stale", "warning"),
            ("queued", None, 7, "queued", "v7", "queued", "normal"),
            ("none", None, 7, "none", "v7", "none", "danger"),
            ("none", None, None, "none", "none", "none", "danger"),
        )
        for (
            state,
            package_revision,
            published_revision,
            left_display,
            right_display,
            status,
            tone,
        ) in cases:
            with self.subTest(state=state, published=published_revision):
                pair = package_published_revision_pair(
                    _readiness(
                        package_state=state,
                        package_revision=package_revision,
                        published_revision=published_revision,
                    )
                )
                self.assertEqual(pair["left_label"], "Package")
                self.assertEqual(pair["right_label"], "Published")
                self.assertEqual(pair["left_display"], left_display)
                self.assertEqual(pair["left_meta"], "")
                self.assertEqual(pair["right_display"], right_display)
                self.assertEqual(pair["status"], status)
                self.assertEqual(pair["left_tone"], tone)
                self.assertIn("package is", pair["aria_label"])
                self.assertNotIn("revision v", pair["aria_label"])

    def test_workspace_revision_notice_only_reports_actionable_state(self) -> None:
        self.assertIsNone(workspace_revision_notice(_readiness()))

        dirty = workspace_revision_notice(_readiness(dirty=True))
        self.assertIsNotNone(dirty)
        assert dirty is not None
        self.assertEqual(dirty["display"], "v7")
        self.assertEqual(dirty["meta"], "local changes")
        self.assertEqual(dirty["tone"], "warning")

        behind = workspace_revision_notice(
            _readiness(workspace_revision=6, published_revision=7)
        )
        self.assertIsNotNone(behind)
        assert behind is not None
        self.assertEqual(behind["display"], "v6")
        self.assertEqual(behind["meta"], "sync required")
        self.assertEqual(behind["tone"], "danger")

        pair = workspace_published_revision_pair(6, 7)
        self.assertEqual(pair["left_label"], "Workspace")
        self.assertEqual(pair["right_label"], "Published")
        self.assertEqual(pair["left_display"], "v6")
        self.assertEqual(pair["left_meta"], "sync required")
        self.assertEqual(pair["right_display"], "v7")
        self.assertEqual(pair["status"], "stale")
        self.assertEqual(pair["left_tone"], "danger")

        dirty_pair = workspace_published_revision_pair(0, 0, dirty=True)
        self.assertEqual(dirty_pair["status"], "current")
        self.assertEqual(dirty_pair["left_meta"], "")
        self.assertEqual(dirty_pair["left_tone"], "normal")
        self.assertIn("workspace has local changes", dirty_pair["aria_label"])

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
