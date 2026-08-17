import unittest

from app.service.contest.problem_index import (
    contest_problem_idx_sort_key,
    normalize_contest_problem_idx,
)


class TestContestProblemIndex(unittest.TestCase):
    def test_letter_indices_use_excel_column_order(self) -> None:
        indices = ["AB", "Z", "B", "AA", "A"]

        self.assertEqual(
            sorted(indices, key=contest_problem_idx_sort_key),
            ["A", "B", "Z", "AA", "AB"],
        )

    def test_custom_indices_use_natural_numeric_order(self) -> None:
        indices = ["A10", "10", "A2", "2", "A1", "1"]

        self.assertEqual(
            sorted(indices, key=contest_problem_idx_sort_key),
            ["1", "2", "10", "A1", "A2", "A10"],
        )

    def test_letter_family_precedes_custom_indices(self) -> None:
        indices = ["1", "A2", "AA", "B", "A"]

        self.assertEqual(
            sorted(indices, key=contest_problem_idx_sort_key),
            ["A", "B", "AA", "1", "A2"],
        )

    def test_normalization_is_uppercase_and_bounded(self) -> None:
        self.assertEqual(normalize_contest_problem_idx(" ab-10 "), "AB-10")
        with self.assertRaisesRegex(ValueError, "required"):
            normalize_contest_problem_idx(" ")
        with self.assertRaisesRegex(ValueError, "too long"):
            normalize_contest_problem_idx("A" * 17)


if __name__ == "__main__":
    unittest.main()
