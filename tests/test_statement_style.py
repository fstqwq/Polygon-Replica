import unittest

from app.service.statement.constant import DEFAULT_STATEMENT_TEMPLATE
from app.service.statement.ftl.renderer import render_ftl_template


class TestCanonicalStatementStyle(unittest.TestCase):
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
