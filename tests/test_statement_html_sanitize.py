from unittest import TestCase

from app.service.statement.html_sanitize import sanitize_statement_html


class TestStatementHtmlSanitize(TestCase):
    def test_self_closing_html_preserves_authored_line_breaks(self) -> None:
        self.assertEqual(
            sanitize_statement_html(
                '<div class="center"><p><img src="board.png" /><br />'
                'First caption.<br />Second caption.</p></div><p>Body.</p>'
                '<math><mspace width="1em" /><mi>x</mi></math>',
            ),
            '<div class="center"><p><img src="board.png"><br>'
            'First caption.<br>Second caption.</p></div><p>Body.</p>'
            '<math><mspace width="1em"></mspace><mi>x</mi></math>',
        )

    def test_dimensions_survive_without_active_or_unbounded_css(self) -> None:
        result = sanitize_statement_html(
            '<img src="image.png" onerror="alert(1)" '
            'style="width:9cm;height:999999px;position:fixed;'
            'background:url(https://example.com/tracker)">'
            '<span style="font-size:10pt;line-height:11pt;'
            'color:expression(alert(1))">caption</span>'
        )
        self.assertEqual(
            result,
            '<img src="image.png" style="width:9cm">'
            '<span style="font-size:10pt;line-height:11pt">caption</span>',
        )

    def test_math_box_preserves_text_and_bounded_math_attributes(self) -> None:
        result = sanitize_statement_html(
            '<math><menclose notation="box"><mtext mathvariant="monospace">(</mtext>'
            '</menclose><mspace width="0.167em"></mspace>'
            '<mi mathvariant="normal">x</mi>'
            '<mspace width="url(https://example.com)"></mspace></math>'
        )
        self.assertEqual(
            result,
            '<math><mpadded class="statement-math-box">'
            '<mtext mathvariant="monospace">(</mtext></mpadded>'
            '<mspace width="0.167em"></mspace><mi mathvariant="normal">x</mi>'
            '<mspace></mspace></math>',
        )
