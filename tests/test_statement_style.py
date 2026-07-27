from __future__ import annotations

import re
import unittest

from app.service.statement.constant import DEFAULT_OLYMP_STY


class TestCanonicalStatementStyle(unittest.TestCase):
    def test_xelatex_monospace_disables_common_ligatures(self) -> None:
        self.assertIn(
            r"\setmonofont{TeX Gyre Cursor}[Ligatures=NoCommon]",
            DEFAULT_OLYMP_STY,
        )

    def test_interactzigzag_sets_body_text_formatting(self) -> None:
        self._assert_environment_body_formatting("interactzigzag")

    def test_interactzigzagtwice_sets_body_text_formatting(self) -> None:
        self._assert_environment_body_formatting("interactzigzagtwice")

    def _assert_environment_body_formatting(self, environment: str) -> None:
        opening = re.search(
            rf"\\newenvironment\{{{re.escape(environment)}\}}\{{(?P<body>.*?)"
            r"\\ifdefined\\NoExamples",
            DEFAULT_OLYMP_STY,
            re.DOTALL,
        )
        self.assertIsNotNone(opening)
        body = opening.group("body")
        self.assertIn(r"\ttfamily", body)
        self.assertIn(r"\obeylines", body)
        self.assertIn(r"\obeyspaces", body)
        self.assertIn(r"\frenchspacing", body)


if __name__ == "__main__":
    unittest.main()
