from __future__ import annotations

# ascii-lint: allow; reason=chinese-test

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.service.repository.merge_diff import (
    MAX_DIFF_BYTES,
    MergeDiffSide,
    compare_merge_files,
)


class TestMergeDiff(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="polygon-merge-diff-")
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    @staticmethod
    def _side(label: str, path: Path | None, *, executable: bool = False) -> MergeDiffSide:
        return MergeDiffSide(
            label=label,
            exists=path is not None,
            size=path.stat().st_size if path is not None else 0,
            executable=executable,
            open_side=label if path is not None else "",
        )

    def _compare(
        self,
        left: Path | None,
        right: Path | None,
        *,
        change_kind: str = "modified",
    ):
        return compare_merge_files(
            path="statement-sections/english/name.tex",
            change_kind=change_kind,
            left_path=left,
            left_side=self._side("current", left),
            right_path=right,
            right_side=self._side("latest", right),
        )

    def test_line_and_inline_changes_are_structured(self) -> None:
        left = self.root / "left.txt"
        right = self.root / "right.txt"
        left.write_text("alpha beta\nsame\nremoved", encoding="utf-8")
        right.write_text("alpha gamma\nsame\nadded\n", encoding="utf-8")

        comparison = self._compare(left, right)

        self.assertFalse(comparison.binary)
        self.assertFalse(comparison.truncated)
        self.assertEqual([row.operation for row in comparison.rows], ["replace", "equal", "replace"])
        first = comparison.rows[0]
        self.assertEqual(first.left.line_number, 1)
        self.assertEqual(first.right.line_number, 1)
        self.assertTrue(any(segment.changed for segment in first.left.segments))
        self.assertTrue(any(segment.changed for segment in first.right.segments))
        self.assertTrue(comparison.rows[2].left.no_newline)
        self.assertFalse(comparison.rows[2].right.no_newline)

    def test_added_and_deleted_files_keep_line_numbers(self) -> None:
        payload = self.root / "payload.txt"
        payload.write_text("first\nsecond\n", encoding="utf-8")

        added = self._compare(None, payload, change_kind="added")
        deleted = self._compare(payload, None, change_kind="deleted")

        self.assertEqual([row.operation for row in added.rows], ["insert", "insert"])
        self.assertEqual([row.right.line_number for row in added.rows], [1, 2])
        self.assertEqual([row.operation for row in deleted.rows], ["delete", "delete"])
        self.assertEqual([row.left.line_number for row in deleted.rows], [1, 2])

    def test_empty_unicode_and_tabs_remain_text(self) -> None:
        left = self.root / "left.txt"
        right = self.root / "right.txt"
        left.write_bytes(b"")
        right.write_text("\t中文\n", encoding="utf-8")

        comparison = self._compare(left, right)

        self.assertFalse(comparison.binary)
        self.assertEqual(len(comparison.rows), 1)
        self.assertEqual(
            comparison.rows[0].right.segments[0].text,
            "\t中文",
        )

    def test_binary_and_invalid_utf8_use_restricted_preview(self) -> None:
        binary = self.root / "binary.dat"
        invalid = self.root / "invalid.dat"
        binary.write_bytes(b"a\0b")
        invalid.write_bytes(b"\xff\xfe")

        for path in (binary, invalid):
            with self.subTest(path=path.name):
                comparison = self._compare(path, None)
                self.assertTrue(comparison.binary)
                self.assertEqual(comparison.rows, ())
                self.assertIn("cannot be compared as text", comparison.message)

    def test_size_limit_does_not_open_file(self) -> None:
        payload = self.root / "large.txt"
        payload.write_text("small", encoding="utf-8")
        oversized = MergeDiffSide("current", True, MAX_DIFF_BYTES + 1, False, "current")
        missing = MergeDiffSide("latest", False, 0, False, "")

        with patch.object(Path, "read_bytes", side_effect=AssertionError("file was opened")):
            comparison = compare_merge_files(
                path="large.txt",
                change_kind="deleted",
                left_path=payload,
                left_side=oversized,
                right_path=None,
                right_side=missing,
            )

        self.assertTrue(comparison.truncated)
        self.assertEqual(comparison.rows, ())

    def test_line_limit_uses_restricted_preview(self) -> None:
        payload = self.root / "many-lines.txt"
        payload.write_text("x\n" * 5_001, encoding="utf-8")

        comparison = self._compare(payload, None)

        self.assertTrue(comparison.truncated)
        self.assertEqual(comparison.rows, ())
        self.assertIn("5,000 lines", comparison.message)


if __name__ == "__main__":
    unittest.main()
