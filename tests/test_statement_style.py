import unittest

from app.service.statement.constant import DEFAULT_STATEMENT_PROBLEM_TEMPLATE, DEFAULT_STATEMENT_TEMPLATE
from app.service.statement.ftl.renderer import render_ftl_template


class TestCanonicalStatementStyle(unittest.TestCase):
    def test_problem_template_formats_millisecond_time_limits(self) -> None:
        for language, unit in (("english", "seconds"), ("chinese", "秒")):
            for milliseconds, seconds in ((2077, "2.077"), (2000, "2"), (1, "0.001")):
                with self.subTest(language=language, milliseconds=milliseconds):
                    rendered = render_ftl_template(
                        DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
                        {"language": language, "problem": {"timeLimit": milliseconds}},
                    )
                    self.assertIn(f"{{{seconds} {unit}}}", rendered)

    def test_statement_template_selects_pdf_language(self) -> None:
        for language, package in (
            ("chinese", r"\usepackage[chinese]{olymp}"),
            ("english", r"\usepackage {olymp}"),
        ):
            with self.subTest(language=language):
                rendered = render_ftl_template(
                    DEFAULT_STATEMENT_TEMPLATE,
                    {"language": language, "statements": []},
                )
                self.assertIn(package, rendered)

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
