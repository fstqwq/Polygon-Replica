import unittest

from app.impl.workspace.run_display import (
    generation_status_text,
    rewrite_failure_reason_with_source,
)


class TestRunDisplay(unittest.TestCase):
    def test_failure_reason_rewrites_generic_reason_with_source(self) -> None:
        generic_reason = "required=[AC], allowed=[AC], got=[TL]"

        reason = rewrite_failure_reason_with_source(
            generic_reason,
            [
                {
                    "source": "solutions/ac_python.py",
                    "match_reason": generic_reason,
                    "error": "",
                }
            ],
            limit_bytes=2048,
        )

        self.assertEqual(reason, "ac_python.py: required=[AC], allowed=[AC], got=[TL]")

    def test_failure_reason_prefers_source_error_over_incomplete_summary(self) -> None:
        reason = rewrite_failure_reason_with_source(
            "required=[WA, TL, RE, CE], allowed=[AC, WA, TL, RE, CE], "
            "got=[]: cancelled on service startup",
            [
                {
                    "source": "solutions/luangao.cpp",
                    "match_reason": "",
                    "error": "cancelled on service startup",
                }
            ],
            limit_bytes=2048,
        )

        self.assertEqual(reason, "luangao.cpp: cancelled on service startup")

    def test_failure_reason_ignores_transient_running_state(self) -> None:
        reason = rewrite_failure_reason_with_source(
            "",
            [
                {
                    "source": "solutions/std.cpp",
                    "match_reason": "running",
                    "error": "",
                }
            ],
            limit_bytes=2048,
        )

        self.assertEqual(reason, "")

    def test_generation_status_uses_generation_specific_labels(self) -> None:
        expectations = {
            ("done", "AC"): "OK",
            ("done", "OK"): "OK",
            ("failed", "WA"): "validation failed",
            ("failed", "TL"): "generator TL",
            ("failed", "TLX"): "generator TL",
            ("failed", "RE"): "generator RE",
            ("failed", "CE"): "generator CE",
            ("failed", "FL"): "validator failed",
            ("pending", "WA"): "pending",
            ("leased", "WA"): "running",
            ("cancelled", "WA"): "cancelled",
        }

        for (status, verdict), expected in expectations.items():
            with self.subTest(status=status, verdict=verdict):
                self.assertEqual(generation_status_text(status, verdict), expected)
