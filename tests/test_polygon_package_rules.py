from __future__ import annotations

import unittest

from app.service.importing.polygon import polygon_solution_expected_from_tag


class TestPolygonPackageRules(unittest.TestCase):
    def test_solution_tags_map_to_canonical_expected_behaviors(self) -> None:
        expectations = {
            "main": "accepted",
            "accepted": "accepted",
            "wrong-answer": "wrong_answer",
            "presentation-error": "wrong_answer",
            "time-limit-exceeded": "time_limit_exceeded",
            "time-limit-exceeded-or-accepted": "tle_or_correct",
            "time-limit-exceeded-or-memory-limit-exceeded": "tle_or_re",
            "memory-limit-exceeded": "run_time_error",
            "rejected": "rejected",
            "failed": "rejected",
            "do-not-run": "unknown",
        }

        for tag, expected in expectations.items():
            with self.subTest(tag=tag):
                self.assertEqual(polygon_solution_expected_from_tag(tag), expected)
