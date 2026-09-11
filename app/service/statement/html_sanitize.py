"""Preserve bounded presentation values at the untrusted HTML boundary."""

from html import escape
from html.parser import HTMLParser
from importlib import import_module
import re


nh3 = import_module("nh3")

MATHML_TAGS = {
    "annotation", "math", "mfrac", "mi", "mn", "mo", "mover", "mroot",
    "mrow", "mspace", "msqrt", "msub", "msubsup", "msup", "mtable",
    "mtd", "mtext", "mtr", "munder", "munderover", "semantics", "mpadded",
    "mphantom", "mstyle",
}
_LENGTH = re.compile(r"(?P<number>\d+(?:\.\d+)?|\.\d+)(?:px|pt|pc|cm|mm|in|em|ex|rem|%)?")
_HTML_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_STYLE_PROPERTIES = {
    "img": {"width", "height"},
    "span": {"font-size", "line-height", "width", "height", "margin-left"},
    "div": {"font-size", "line-height", "width", "text-align", "margin-top", "vertical-align"},
    "p": {"text-align"},
    "figure": {"text-align"},
    "td": {"text-align"},
    "th": {"text-align"},
    "mtd": {"text-align"},
}


def _length(value: str) -> bool:
    match = _LENGTH.fullmatch(value)
    return match is not None and float(match['number']) <= 1000


def _attribute(tag: str, name: str, value: str) -> str | None:
    if name == "style":
        accepted: dict[str, str] = {}
        for declaration in value.split(";"):
            key, separator, token = declaration.partition(":")
            key, token = key.strip().lower(), token.strip().lower()
            if not separator or key not in _STYLE_PROPERTIES.get(tag, set()):
                continue
            if key == "text-align":
                valid = token in {"left", "right", "center", "justify"}
            elif key == "vertical-align":
                valid = token in {"top", "middle", "bottom"}
            else:
                valid = _length(token)
            if valid:
                accepted[key] = token
        return ";".join(f"{key}:{token}" for key, token in accepted.items()) or None
    if tag in MATHML_TAGS:
        if name in {"width", "height", "depth", "lspace", "rspace", "mathsize"}:
            return value if _length(value) else None
        if name in {"accent", "accentunder", "stretchy", "symmetric", "largeop", "movablelimits", "displaystyle"}:
            return value if value in {"true", "false"} else None
        if name == "mathvariant":
            return value if value in {"normal", "bold", "italic", "bold-italic", "double-struck", "bold-fraktur", "script", "bold-script", "fraktur", "sans-serif", "bold-sans-serif", "sans-serif-italic", "sans-serif-bold-italic", "monospace"} else None
    return value


class _MathBoxes(HTMLParser):
    """Map TeX boxes to MathML Core nodes that Chromium also paints."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.boxes: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "menclose":
            boxed = dict(attrs).get("notation") == "box"
            self.boxes.append(boxed)
            tag = "mpadded" if boxed else "mrow"
            attrs = [("class", "statement-math-box")] if boxed else []
        attributes = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in attrs if value is not None)
        self.output.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_VOID_TAGS:
            return
        if tag == "menclose":
            tag = "mpadded" if self.boxes and self.boxes.pop() else "mrow"
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")


def sanitize_statement_html(fragment: str) -> str:
    boxes = _MathBoxes()
    boxes.feed(fragment)
    boxes.close()
    return nh3.clean(
        "".join(boxes.output),
        tags={
            "a", "article", "aside", "blockquote", "br", "code", "div", "em",
            "figure", "figcaption", "h2", "h3", "h4", "h5", "hr", "img",
            "li", "ol", "p", "pre", "section", "span", "strong", "sub", "sup",
            "table", "tbody", "td", "th", "thead", "tr", "ul", "var",
        } | MATHML_TAGS,
        attributes={
            "*": {"class", "id"},
            "a": {"href", "title", "role"},
            "aside": {"role"},
            "img": {"alt", "height", "src", "title", "width", "style"},
            "span": {"style"}, "div": {"style"}, "p": {"style"},
            "figure": {"style"},
            "td": {"style", "colspan", "rowspan"}, "th": {"style", "colspan", "rowspan"},
            "math": {"display", "xmlns"}, "annotation": {"encoding"},
            "mi": {"mathvariant"}, "mn": {"mathvariant"}, "mtext": {"mathvariant"},
            "mo": {"form", "stretchy", "accent", "symmetric", "largeop", "movablelimits", "lspace", "rspace"},
            "mover": {"accent"}, "munder": {"accentunder"}, "munderover": {"accent", "accentunder"},
            "mspace": {"width", "height", "depth"},
            "mpadded": {"width", "height", "depth", "lspace"},
            "mstyle": {"mathvariant", "mathsize", "displaystyle"},
            "mtable": {"columnalign", "columnspacing", "rowspacing"},
            "mtd": {"columnalign", "style", "rowspan", "columnspan"},
        },
        attribute_filter=_attribute,
        url_schemes=set(),
    )
