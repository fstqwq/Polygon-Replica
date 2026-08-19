import unittest

from app.service.statement.constant import (
    DEFAULT_OLYMP_STY,
    DEFAULT_STATEMENT_TEMPLATE,
)
from app.service.statement.ftl.renderer import render_ftl_template


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

    def test_statement_template_projects_blank_page_property(self) -> None:
        context: dict[str, object] = {
            "title": "",
            "location": "",
            "date": "",
            "statements": [],
        }
        disabled = render_ftl_template(
            DEFAULT_STATEMENT_TEMPLATE,
            {**context, "insertBlankPage": False},
        )
        enabled = render_ftl_template(
            DEFAULT_STATEMENT_TEMPLATE,
            {**context, "insertBlankPage": True},
        )
        self.assertNotIn(r"\intentionallyblankpagestrue", disabled)
        self.assertIn(r"\intentionallyblankpagestrue", enabled)

    def test_statement_template_projects_the_banner_property(self) -> None:
        rendered = render_ftl_template(
            DEFAULT_STATEMENT_TEMPLATE,
            {
                "banner": r"\includegraphics[width=2cm]{contest-logo.pdf}",
                "title": "",
                "location": "",
                "date": "",
                "statements": [],
            },
        )

        self.assertIn(r"\renewcommand{\StatementBanner}", rendered)
        self.assertIn(
            r"\includegraphics[width=2cm]{contest-logo.pdf}",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
