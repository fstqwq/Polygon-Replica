import unittest

from app.service.statement.constant import DEFAULT_OLYMP_STY


class TestCanonicalStatementStyle(unittest.TestCase):
    def test_xelatex_monospace_disables_common_ligatures(self) -> None:
        self.assertIn(
            r"\setmonofont{TeX Gyre Cursor}[Ligatures=NoCommon]",
            DEFAULT_OLYMP_STY,
        )

    def test_structured_sample_api_is_available(self) -> None:
        for command in (
            r"\NewDocumentEnvironment{StatementSamples}",
            r"\NewDocumentCommand{\StatementSampleFile}",
            r"\NewDocumentCommand{\StatementSamplePassFile}",
            r"\NewDocumentEnvironment{StatementSampleInteraction}",
            r"\NewDocumentCommand{\StatementSampleEventFile}",
            r"\newcommand{\StatementBanner}",
        ):
            with self.subTest(command=command):
                self.assertIn(command, DEFAULT_OLYMP_STY)
        self.assertIn("Unknown sample event source", DEFAULT_OLYMP_STY)

    def test_blank_pages_use_the_existing_olymp_signal(self) -> None:
        self.assertIn(r"\newif\ifintentionallyblankpages", DEFAULT_OLYMP_STY)
        self.assertIn(r"\ifintentionallyblankpages", DEFAULT_OLYMP_STY)


if __name__ == "__main__":
    unittest.main()
