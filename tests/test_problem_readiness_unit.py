from __future__ import annotations

import unittest

from app.service.problem.content_review import problem_content_review


class TestProblemReadinessUnit(unittest.TestCase):
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
            "3 solutions · no main correct",
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
