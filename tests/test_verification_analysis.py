from __future__ import annotations

import unittest
import hashlib
from pathlib import Path

from app.impl.workspace.boundary_coverage import boundary_coverage_from_feedback
from app.impl.workspace.runtime_threshold import evaluate_summary_runtime_threshold
from app.service.verification.plan import VerificationTestPlan
from app.service.platform.runtime_blob_store import PayloadFile


def _payload(name: str, content: bytes) -> PayloadFile:
    return PayloadFile(
        path=Path("/tmp") / name,
        size=len(content),
        identity=hashlib.sha256(content).hexdigest(),
    )


def _test_plan(test_name: str = "001.in") -> VerificationTestPlan:
    return VerificationTestPlan(
        test_name=test_name,
        source_kind="manual",
        display_source_path="manual_validate.cpp",
        execution_source_name="manual_validate.cpp",
        execution_source_file=_payload("manual_validate.cpp", b"int main(){return 0;}\n"),
        execution_input_file=_payload("001.in", b"1\n"),
        extra_source_files={},
        tests_meta={},
        sample=False,
        sample_input_custom=False,
        sample_input_text="",
        uses_custom_sample_input=False,
        sample_output_text="",
        sample_output_validate=True,
    )


class TestVerificationAnalysis(unittest.TestCase):
    def test_runtime_threshold_marks_answer_correct_points(self) -> None:
        report = evaluate_summary_runtime_threshold(
            summary={
                "tests": [
                    {
                        "test": "001.in",
                        "verdict": "OK",
                        "time_user_ms": 600,
                        "answer_correct": True,
                    },
                    {
                        "test": "002.in",
                        "verdict": "TL",
                        "time_user_ms": 1200,
                        "answer_correct": True,
                    },
                    {
                        "test": "003.in",
                        "verdict": "WA",
                        "time_user_ms": 700,
                        "answer_correct": False,
                    },
                ]
            },
            source="solutions/slow.cpp",
            time_limit_ms=1000,
        )
        self.assertEqual(report.highlighted_tests, frozenset({"001.in", "002.in"}))
        self.assertIsNone(report.warning_hit)

        all_correct_report = evaluate_summary_runtime_threshold(
            summary={
                "tests": [
                    {
                        "test": "001.in",
                        "verdict": "OK",
                        "time_user_ms": 600,
                        "answer_correct": True,
                    },
                    {
                        "test": "002.in",
                        "verdict": "OK",
                        "time_user_ms": 1200,
                        "answer_correct": True,
                    },
                ]
            },
            source="solutions/accepted.cpp",
            time_limit_ms=1000,
        )
        self.assertIsNotNone(all_correct_report.warning_hit)

    def test_boundary_coverage_aggregates_testlib_overview_logs(self) -> None:
        first = '"n": min-value-hit\nconstant-bounds "n": 1 3\nvariable "n"\n'
        second = '"n": max-value-hit\nconstant-bounds "n": 1 3\nvariable "n"\n'

        result = boundary_coverage_from_feedback(
            feedback_by_test={"001.in": first, "002.in": second},
            test_plans=[_test_plan("001.in"), _test_plan("002.in")],
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.checked_count, 1)
        self.assertEqual(result.missing, [])

    def test_boundary_coverage_warns_for_missing_hits(self) -> None:
        feedback = (
            '"n": min-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
            '"~T~": min-value-hit max-value-hit\n'
            'constant-bounds "~T~": 0 10\n'
            'variable "~T~"\n'
            'constant-bounds "x": ? 9\n'
            'variable "x"\n'
        )

        result = boundary_coverage_from_feedback(
            feedback_by_test={"001.in": feedback},
            test_plans=[_test_plan()],
        )

        self.assertEqual(result.status, "warning")
        self.assertEqual(result.checked_count, 3)
        self.assertEqual(result.missing, ["n max=3", "x max=9"])
        self.assertEqual(result.error, "Test data did not hit: n max=3, x max=9")

    def test_boundary_coverage_ignores_wrapped_or_plain_messages(self) -> None:
        wrapped = (
            "__POLYGON_TESTLIB_OVERVIEW_BEGIN__\n"
            '"n": min-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
            "__POLYGON_TESTLIB_OVERVIEW_END__\n"
        )

        result = boundary_coverage_from_feedback(
            feedback_by_test={"001.in": wrapped, "002.in": "validator accepted\n"},
            test_plans=[_test_plan("001.in"), _test_plan("002.in")],
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.checked_count, 0)
        self.assertEqual(result.missing, [])
