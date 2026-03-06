from __future__ import annotations

import ast
import json
import os
import re
import shutil
from pathlib import Path

from app.services.hashing import quick_fp_digest
from app.services.tests_spec import TESTS_SPEC_REL, load_tests_spec, payload_rel_path_for_test


STATEMENT_DIR = Path("statement")
STATEMENT_TEMPLATE_REL = STATEMENT_DIR / "statements.ftl"
STATEMENT_PROBLEM_REL = STATEMENT_DIR / "problem.tex"
STATEMENT_STYLE_REL = STATEMENT_DIR / "olymp.sty"
STATEMENT_MAIN_REL = STATEMENT_DIR / "main.tex"
STATEMENT_LANGUAGE_REL = STATEMENT_DIR / "language.txt"
STATEMENT_RENDERED_DIR_REL = STATEMENT_DIR / "rendered"
STATEMENT_SECTIONS_DIR = Path("statement-sections")
TESTS_ANSWERS_DIR_REL = Path("tests/answers")
WF_STYLE_DIR = Path("third_party") / "Polygon-WF-Styles"
WF_STYLE_STATEMENTS_REL = WF_STYLE_DIR / "statements.ftl"
WF_STYLE_OLYMP_REL = WF_STYLE_DIR / "olymp.sty"
DEFAULT_PROBLEM_TITLE = "Sample Problem"
FTL_COMMENT_RE = re.compile(r"<#--.*?-->", re.DOTALL)
FTL_LIST_RE = re.compile(r"^list\s+(.+?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", re.DOTALL)
STANDALONE_OPEN_DIRECTIVE_PREFIXES = ("if ", "elseif ", "list ", "assign ")
STANDALONE_OPEN_DIRECTIVE_EXACT = {"else"}
STANDALONE_CLOSE_DIRECTIVES = {"if", "list"}

DEFAULT_STATEMENT_PROBLEM_TEMPLATE = r"""\begin{problem}{${problem.name}}{${problem.inputFile}}{${problem.outputFile}}{${(problem.timeLimit/1000)?c} seconds}{${(problem.memoryLimit/1048576)?c} megabytes}
${problem.legend}
<#if problem.input?? && (problem.input?length > 0)>
\InputFile
${problem.input}
</#if>
<#if problem.output?? && (problem.output?length > 0)>
\OutputFile
${problem.output}
</#if>
<#if problem.interaction?? && (problem.interaction?length > 0)>
\Interaction
${problem.interaction}
</#if>
<#if problem.scoring?? && (problem.scoring?length > 0)>
\Scoring
${problem.scoring}
</#if>
<#if (problem.sampleTests?size>0)>
\Example<#if (problem.sampleTests?size>1)>s</#if>
\begin{example}
<#list problem.sampleTests as test>
\exmpfile{${test.inputFile}}{${test.outputFile}}%
</#list>
\end{example}
</#if>
<#if (problem.notes??) && (problem.notes?length > 0)>
\ifdefined\Note
  \ifx\Note\empty
    \subsection*{Notes}
  \else
    \Note
  \fi
\else
  \subsection*{Notes}
\fi
${problem.notes}
</#if>
\end{problem}
"""

STATEMENT_RENDERER_SIGNATURE_VERSION = "2026-03-02-short-problem-title-tex-pass2-tests-sample-source"
UNDEFINED = object()


def _read_required_text(path: Path, *, label: str, allow_empty: bool = False) -> str:
    readable = path.as_posix()
    if path.is_symlink():
        raise RuntimeError(f"{label} must be a regular file: {readable}")
    if not path.exists():
        raise RuntimeError(f"{label} is missing: {readable}")
    if not path.is_file():
        raise RuntimeError(f"{label} is not a file: {readable}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{label} must be valid UTF-8: {readable}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to read {label}: {readable}: {exc}") from exc
    if (not allow_empty) and (not str(text).strip()):
        raise RuntimeError(f"{label} is empty: {readable}")
    return text


def _load_repo_required_text(rel_path: Path, *, label: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return _read_required_text(root / rel_path, label=label)


DEFAULT_STATEMENT_TEMPLATE = _load_repo_required_text(
    WF_STYLE_STATEMENTS_REL,
    label=f"canonical statement template ({WF_STYLE_STATEMENTS_REL.as_posix()})",
)
_DEFAULT_OLYMP_STY = _load_repo_required_text(
    WF_STYLE_OLYMP_REL,
    label=f"canonical olymp style ({WF_STYLE_OLYMP_REL.as_posix()})",
)


def default_olymp_sty_text() -> str:
    return _DEFAULT_OLYMP_STY


def _safe_read_text(path: Path, fallback: str) -> str:
    try:
        if path.exists() and path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return fallback


def _safe_read_json(path: Path) -> dict:
    try:
        if path.exists() and path.is_file() and (not path.is_symlink()):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        return {}
    return {}


def _tokenize_expr(expr: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    text = str(expr or "")
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("&&", i):
            tokens.append(("op", "&&"))
            i += 2
            continue
        if text.startswith("||", i):
            tokens.append(("op", "||"))
            i += 2
            continue
        if text.startswith("!=", i):
            tokens.append(("op", "!="))
            i += 2
            continue
        if text.startswith("<=", i):
            tokens.append(("op", "<="))
            i += 2
            continue
        if text.startswith(">=", i):
            tokens.append(("op", ">="))
            i += 2
            continue
        if text.startswith("??", i):
            tokens.append(("op", "??"))
            i += 2
            continue
        if ch in {'"', "'"}:
            quote = ch
            j = i + 1
            escaped = False
            while j < n:
                cj = text[j]
                if escaped:
                    escaped = False
                    j += 1
                    continue
                if cj == "\\":
                    escaped = True
                    j += 1
                    continue
                if cj == quote:
                    break
                j += 1
            if j >= n:
                raise ValueError("unterminated string")
            raw = text[i : j + 1]
            tokens.append(("string", str(ast.literal_eval(raw))))
            i = j + 1
            continue
        if ch.isdigit():
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] == ".":
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
                tokens.append(("number", float(text[i:j])))
            else:
                tokens.append(("number", int(text[i:j])))
            i = j
            continue
        if ch == "?" and i + 1 < n and (text[i + 1].isalpha() or text[i + 1] == "_"):
            j = i + 2
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("builtin", text[i + 1 : j]))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in {"_", "-"}):
                j += 1
            tokens.append(("ident", text[i:j]))
            i = j
            continue
        if ch in {"=", "<", ">", "+", "-", "*", "/", "%", "(", ")", ".", "!"}:
            tokens.append(("op", ch))
            i += 1
            continue
        raise ValueError(f"unsupported token: {ch}")
    return tokens


def _to_number(value: object) -> float | int:
    if value is UNDEFINED or value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return 0
    try:
        if "." in text:
            return float(text)
        return int(text)
    except Exception:
        return 0


def _num_to_text(value: object) -> str:
    if value is UNDEFINED or value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.12f}".rstrip("0").rstrip(".")
    return str(value)


def _truthy(value: object) -> bool:
    if value is UNDEFINED or value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _resolve_member(value: object, key: str) -> object:
    if value is UNDEFINED or value is None:
        return UNDEFINED
    if isinstance(value, dict):
        return value.get(key, UNDEFINED)
    try:
        if hasattr(value, key):
            return getattr(value, key)
    except Exception:
        return UNDEFINED
    return UNDEFINED


def _apply_builtin(value: object, builtin: str) -> object:
    token = str(builtin or "").strip().lower()
    if token in {"size", "length"}:
        if value is UNDEFINED or value is None:
            return 0
        try:
            return len(value)  # type: ignore[arg-type]
        except Exception:
            return len(str(value))
    if token == "c":
        return _num_to_text(value)
    if token == "string":
        if value is UNDEFINED or value is None:
            return ""
        return str(value)
    return value


def _compare_values(left: object, right: object, op: str) -> bool:
    if op == "=":
        if left is UNDEFINED and right is UNDEFINED:
            return True
        return left == right
    if op == "!=":
        if left is UNDEFINED and right is UNDEFINED:
            return False
        return left != right
    if left is UNDEFINED or right is UNDEFINED:
        return False
    l_num = _to_number(left)
    r_num = _to_number(right)
    if isinstance(left, (int, float, bool)) or isinstance(right, (int, float, bool)):
        if op == "<":
            return l_num < r_num
        if op == "<=":
            return l_num <= r_num
        if op == ">":
            return l_num > r_num
        if op == ">=":
            return l_num >= r_num
        return False
    l_text = str(left)
    r_text = str(right)
    if op == "<":
        return l_text < r_text
    if op == "<=":
        return l_text <= r_text
    if op == ">":
        return l_text > r_text
    if op == ">=":
        return l_text >= r_text
    return False


class _ExprParser:
    def __init__(self, tokens: list[tuple[str, object]], scope: dict[str, object]):
        self.tokens = tokens
        self.scope = scope
        self.index = 0

    def parse(self) -> object:
        return self._parse_or()

    def _peek(self) -> tuple[str, object]:
        if self.index >= len(self.tokens):
            return ("eof", "")
        return self.tokens[self.index]

    def _take(self) -> tuple[str, object]:
        token = self._peek()
        if self.index < len(self.tokens):
            self.index += 1
        return token

    def _accept_op(self, op: str) -> bool:
        kind, value = self._peek()
        if kind == "op" and value == op:
            self.index += 1
            return True
        return False

    def _accept_builtin(self) -> str:
        kind, value = self._peek()
        if kind == "builtin":
            self.index += 1
            return str(value)
        return ""

    def _expect_op(self, op: str) -> None:
        if not self._accept_op(op):
            raise ValueError(f"expected '{op}'")

    def _parse_or(self) -> object:
        left = self._parse_and()
        while self._accept_op("||"):
            right = self._parse_and()
            left = bool(_truthy(left) or _truthy(right))
        return left

    def _parse_and(self) -> object:
        left = self._parse_cmp()
        while self._accept_op("&&"):
            right = self._parse_cmp()
            left = bool(_truthy(left) and _truthy(right))
        return left

    def _parse_cmp(self) -> object:
        left = self._parse_add()
        while True:
            kind, value = self._peek()
            if kind != "op" or value not in {"=", "!=", "<", "<=", ">", ">="}:
                break
            self.index += 1
            right = self._parse_add()
            left = _compare_values(left, right, str(value))
        return left

    def _parse_add(self) -> object:
        left = self._parse_mul()
        while True:
            kind, value = self._peek()
            if kind != "op" or value not in {"+", "-"}:
                break
            self.index += 1
            right = self._parse_mul()
            if value == "+":
                if isinstance(left, str) or isinstance(right, str):
                    left = str("" if left is UNDEFINED else left) + str("" if right is UNDEFINED else right)
                else:
                    left = _to_number(left) + _to_number(right)
            else:
                left = _to_number(left) - _to_number(right)
        return left

    def _parse_mul(self) -> object:
        left = self._parse_unary()
        while True:
            kind, value = self._peek()
            if kind != "op" or value not in {"*", "/", "%"}:
                break
            self.index += 1
            right = self._parse_unary()
            if value == "*":
                left = _to_number(left) * _to_number(right)
            elif value == "/":
                divisor = _to_number(right)
                left = int(_to_number(left) / divisor) if divisor else 0
            else:
                divisor = _to_number(right)
                left = (_to_number(left) % divisor) if divisor else 0
        return left

    def _parse_unary(self) -> object:
        if self._accept_op("!"):
            return not _truthy(self._parse_unary())
        if self._accept_op("-"):
            return -_to_number(self._parse_unary())
        return self._parse_postfix()

    def _parse_postfix(self) -> object:
        value = self._parse_primary()
        while True:
            if self._accept_op("."):
                kind, token = self._take()
                if kind != "ident":
                    raise ValueError("member accessor expects identifier")
                value = _resolve_member(value, str(token))
                continue
            if self._accept_op("??"):
                value = value is not UNDEFINED and value is not None
                continue
            builtin = self._accept_builtin()
            if builtin:
                value = _apply_builtin(value, builtin)
                continue
            break
        return value

    def _parse_primary(self) -> object:
        kind, value = self._peek()
        if kind == "number":
            self.index += 1
            return value
        if kind == "string":
            self.index += 1
            return value
        if kind == "ident":
            self.index += 1
            name = str(value)
            if name == "true":
                return True
            if name == "false":
                return False
            if name == "null":
                return None
            return self.scope.get(name, UNDEFINED)
        if self._accept_op("("):
            inner = self._parse_or()
            self._expect_op(")")
            return inner
        raise ValueError("invalid expression")


def _eval_expr(expr: str, scope: dict[str, object]) -> object:
    parser = _ExprParser(_tokenize_expr(expr), scope)
    return parser.parse()


def _split_default_expr(expr: str) -> tuple[str, str | None]:
    text = str(expr or "")
    depth = 0
    quote = ""
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == quote:
                quote = ""
            continue
        if ch in {'"', "'"}:
            quote = ch
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            continue
        if depth == 0 and ch == "!" and (i + 1 >= len(text) or text[i + 1] != "="):
            left = text[:i].strip()
            right = text[i + 1 :].strip()
            if left:
                return (left, right if right else "")
    return (text.strip(), None)


def _eval_interpolation(expr: str, scope: dict[str, object]) -> str:
    left, fallback = _split_default_expr(expr)
    value = _eval_expr(left, scope)
    if value is UNDEFINED or value is None:
        if fallback is None:
            return ""
        if fallback == "":
            return ""
        return str(_eval_expr(fallback, scope))
    if isinstance(value, (int, float, bool)):
        return _num_to_text(value)
    return str(value)


def _find_directive_tag_end(text: str, start: int) -> int:
    """Locate the closing `>` for a `<#...>` tag while skipping expression `>` tokens."""
    n = len(text)
    i = int(start)
    depth = 0
    quote = ""
    escaped = False
    candidate = -1
    while i < n:
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {'"', "'"}:
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if ch == ">" and depth == 0:
            candidate = i
            i += 1
            continue
        if candidate >= 0:
            if ch in {"\n", "\r"}:
                return candidate
            if text.startswith("${", i) or text.startswith("<#", i) or text.startswith("</#", i):
                return candidate
        i += 1
    return candidate


def _strip_standalone_directive_lines(text: str) -> str:
    out: list[str] = []
    for line in str(text or "").splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        low = stripped.lower()
        if low.startswith("<#") and low.endswith(">"):
            body = low[2:-1].strip()
            if body in STANDALONE_OPEN_DIRECTIVE_EXACT or body.startswith(STANDALONE_OPEN_DIRECTIVE_PREFIXES):
                out.append(stripped)
                continue
        if low.startswith("</#") and low.endswith(">"):
            body = low[3:-1].strip()
            if body in STANDALONE_CLOSE_DIRECTIVES:
                out.append(stripped)
                continue
        out.append(line)
    return "".join(out)


def _parse_nodes(
    text: str,
    pos: int,
    stop_tags: set[str],
    stop_closing_tags: set[str],
) -> tuple[list[dict[str, object]], int, str, str]:
    nodes: list[dict[str, object]] = []
    n = len(text)
    while pos < n:
        next_expr = text.find("${", pos)
        next_dir = text.find("<#", pos)
        next_end = text.find("</#", pos)
        markers = [x for x in (next_expr, next_dir, next_end) if x >= 0]
        if not markers:
            if pos < n:
                nodes.append({"type": "text", "value": text[pos:]})
            return (nodes, n, "", "")
        hit = min(markers)
        if hit > pos:
            nodes.append({"type": "text", "value": text[pos:hit]})
            pos = hit
        if text.startswith("${", pos):
            end = text.find("}", pos + 2)
            if end < 0:
                nodes.append({"type": "text", "value": text[pos:]})
                return (nodes, n, "", "")
            nodes.append({"type": "expr", "expr": text[pos + 2 : end].strip()})
            pos = end + 1
            continue
        if text.startswith("<#--", pos):
            end = text.find("-->", pos + 4)
            if end < 0:
                return (nodes, n, "", "")
            pos = end + 3
            continue
        if text.startswith("</#", pos):
            end = text.find(">", pos + 3)
            if end < 0:
                return (nodes, n, "", "")
            closing = text[pos + 3 : end].strip().lower()
            pos = end + 1
            if closing in stop_closing_tags:
                return (nodes, pos, f"/{closing}", "")
            nodes.append({"type": "text", "value": f"</#{closing}>"})
            continue
        if text.startswith("<#", pos):
            end = _find_directive_tag_end(text, pos + 2)
            if end < 0:
                return (nodes, n, "", "")
            body = text[pos + 2 : end].strip()
            pos = end + 1
            low = body.lower()
            if low.startswith("assign "):
                assign_body = body[7:].strip()
                if assign_body.endswith("/"):
                    assign_body = assign_body[:-1].rstrip()
                if "=" in assign_body:
                    name, expr = assign_body.split("=", 1)
                    name = str(name or "").strip()
                    expr = str(expr or "").strip()
                    if name and expr:
                        nodes.append({"type": "assign", "name": name, "expr": expr})
                continue
            if low.startswith("if "):
                cond = body[3:].strip()
                branches: list[tuple[str | None, list[dict[str, object]]]] = []
                inner, pos, stop_tag, stop_arg = _parse_nodes(text, pos, {"elseif", "else"}, {"if"})
                branches.append((cond, inner))
                while stop_tag in {"elseif", "else"}:
                    if stop_tag == "elseif":
                        branch_cond = stop_arg
                        inner, pos, stop_tag, stop_arg = _parse_nodes(text, pos, {"elseif", "else"}, {"if"})
                        branches.append((branch_cond, inner))
                    else:
                        inner, pos, stop_tag, stop_arg = _parse_nodes(text, pos, set(), {"if"})
                        branches.append((None, inner))
                        break
                nodes.append({"type": "if", "branches": branches})
                continue
            if low.startswith("list "):
                m = FTL_LIST_RE.match(body)
                if m is None:
                    continue
                list_expr = str(m.group(1) or "").strip()
                item_name = str(m.group(2) or "").strip()
                inner, pos, _stop_tag, _stop_arg = _parse_nodes(text, pos, set(), {"list"})
                nodes.append({"type": "list", "expr": list_expr, "item": item_name, "children": inner})
                continue
            if low == "else":
                if "else" in stop_tags:
                    return (nodes, pos, "else", "")
                continue
            if low.startswith("elseif "):
                if "elseif" in stop_tags:
                    return (nodes, pos, "elseif", body[7:].strip())
                continue
            nodes.append({"type": "text", "value": f"<#{body}>"})
            continue
    return (nodes, pos, "", "")


def _iter_values(value: object) -> list[object]:
    if value is UNDEFINED or value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def _render_nodes(nodes: list[dict[str, object]], scope: dict[str, object]) -> str:
    out: list[str] = []
    for node in nodes:
        kind = str(node.get("type") or "")
        if kind == "text":
            out.append(str(node.get("value") or ""))
            continue
        if kind == "expr":
            try:
                out.append(_eval_interpolation(str(node.get("expr") or ""), scope))
            except Exception:
                out.append("")
            continue
        if kind == "assign":
            name = str(node.get("name") or "").strip()
            expr = str(node.get("expr") or "").strip()
            if not name or not expr:
                continue
            try:
                scope[name] = _eval_expr(expr, scope)
            except Exception:
                scope[name] = UNDEFINED
            continue
        if kind == "if":
            branches = node.get("branches")
            if not isinstance(branches, list):
                continue
            for item in branches:
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                cond, branch_nodes = item
                if cond is None:
                    out.append(_render_nodes(branch_nodes if isinstance(branch_nodes, list) else [], dict(scope)))
                    break
                try:
                    ok = _truthy(_eval_expr(str(cond), scope))
                except Exception:
                    ok = False
                if ok:
                    out.append(_render_nodes(branch_nodes if isinstance(branch_nodes, list) else [], dict(scope)))
                    break
            continue
        if kind == "list":
            expr = str(node.get("expr") or "")
            item_name = str(node.get("item") or "").strip()
            children = node.get("children")
            if not expr or not item_name or not isinstance(children, list):
                continue
            try:
                values = _iter_values(_eval_expr(expr, scope))
            except Exception:
                values = []
            for item in values:
                child_scope = dict(scope)
                child_scope[item_name] = item
                out.append(_render_nodes(children, child_scope))
            continue
    return "".join(out)


def _render_ftl_template(template_text: str, context: dict[str, object]) -> str:
    stripped = FTL_COMMENT_RE.sub("", str(template_text or ""))
    stripped = _strip_standalone_directive_lines(stripped)
    nodes, _pos, _stop_tag, _stop_arg = _parse_nodes(stripped, 0, set(), set())
    return _render_nodes(nodes, dict(context))


def _statement_languages(workspace: Path) -> list[str]:
    root = workspace / STATEMENT_SECTIONS_DIR
    if not root.exists() or not root.is_dir() or root.is_symlink():
        return []
    result: list[str] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.is_symlink() or not child.is_dir():
                continue
            token = str(child.name or "").strip()
            if token:
                result.append(token)
    except OSError:
        return []
    return result


def _read_statement_language(workspace: Path) -> str:
    marker = workspace / STATEMENT_LANGUAGE_REL
    try:
        if marker.exists() and marker.is_file() and (not marker.is_symlink()):
            token = str(marker.read_text(encoding="utf-8")).strip()
            if token:
                return token
    except OSError:
        return ""
    return ""


def _pick_statement_language(workspace: Path) -> str:
    configured = _read_statement_language(workspace)
    languages = _statement_languages(workspace)
    if configured and configured in languages:
        return configured
    if "english" in languages:
        return "english"
    if languages:
        return languages[0]
    return "english"


def _statement_section_text(workspace: Path, language: str, section_name: str, fallback: str = "") -> str:
    rel = STATEMENT_SECTIONS_DIR / language / section_name
    return _safe_read_text(workspace / rel, fallback)


def statement_editor_content_rel(workspace: Path) -> Path:
    language = _pick_statement_language(workspace)
    return STATEMENT_SECTIONS_DIR / language / "legend.tex"


def _safe_workspace_regular_file(workspace: Path, rel: Path) -> Path | None:
    try:
        workspace_resolved = workspace.resolve()
        candidate = (workspace / rel).resolve()
    except OSError:
        return None
    if workspace_resolved not in candidate.parents:
        return None
    try:
        if candidate.is_symlink() or not candidate.exists() or not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate


def _collect_sample_tests(workspace: Path, rendered_lang_root: Path) -> list[dict[str, str]]:
    spec_path = workspace / TESTS_SPEC_REL
    try:
        entries = load_tests_spec(spec_path)
    except Exception as exc:
        raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
    rows: list[dict[str, str]] = []
    for index, entry in enumerate(entries, start=1):
        if not bool(entry.get("sample")):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in {"manual", "gen"}:
            raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}: {kind or '(empty)'}")
        test_id = str(entry.get("id") or "").strip()
        if not test_id:
            continue
        sample_input_text = str(entry.get("sample_input") or "")
        sample_output_text = str(entry.get("sample_output") or "")
        try:
            input_rel = Path(payload_rel_path_for_test(test_id, kind))
        except Exception:
            continue
        answer_rel = TESTS_ANSWERS_DIR_REL / f"{test_id}.ans"
        input_source = None if sample_input_text else _safe_workspace_regular_file(workspace, input_rel)
        answer_source = None if sample_output_text else _safe_workspace_regular_file(workspace, answer_rel)
        if (not sample_input_text) and (input_source is None):
            continue
        if (not sample_output_text) and (answer_source is None):
            continue
        input_name = f"sample.{test_id}.in"
        output_name = f"sample.{test_id}.ans"
        input_target = rendered_lang_root / input_name
        output_target = rendered_lang_root / output_name
        try:
            if sample_input_text:
                input_target.write_text(sample_input_text, encoding="utf-8")
            else:
                shutil.copy2(input_source, input_target)
            if sample_output_text:
                output_target.write_text(sample_output_text, encoding="utf-8")
            else:
                shutil.copy2(answer_source, output_target)
        except OSError:
            continue
        rows.append({"inputFile": input_name, "outputFile": output_name})
    return rows


def _problem_context_for_language(
    workspace: Path,
    language: str,
    problem_title: str | None,
    *,
    sample_tests: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    cfg = _safe_read_json(workspace / "config" / "problem.json")
    input_file = str(cfg.get("input_file") or "stdin").strip() or "stdin"
    output_file = str(cfg.get("output_file") or "stdout").strip() or "stdout"
    try:
        time_limit_ms = int(cfg.get("time_limit_ms") or 2000)
    except Exception:
        time_limit_ms = 2000
    try:
        memory_limit_mb = int(cfg.get("memory_limit_mb") or 1024)
    except Exception:
        memory_limit_mb = 1024
    title_from_section = _statement_section_text(workspace, language, "name.tex", fallback="").strip()
    resolved_title = str(problem_title or "").strip() or title_from_section or DEFAULT_PROBLEM_TITLE
    return {
        "name": resolved_title,
        "inputFile": input_file,
        "outputFile": output_file,
        "timeLimit": time_limit_ms,
        "memoryLimit": max(1, memory_limit_mb) * 1024 * 1024,
        "legend": _statement_section_text(workspace, language, "legend.tex", fallback=""),
        "input": _statement_section_text(workspace, language, "input.tex", fallback=""),
        "output": _statement_section_text(workspace, language, "output.tex", fallback=""),
        "interaction": _statement_section_text(workspace, language, "interaction.tex", fallback=""),
        "scoring": _statement_section_text(workspace, language, "scoring.tex", fallback=""),
        "notes": _statement_section_text(workspace, language, "notes.tex", fallback=""),
        "sampleTests": list(sample_tests or []),
    }


def _copy_tree_without_symlinks(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir() or src.is_symlink():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
        current = Path(dirpath)
        safe_dirs: list[str] = []
        for name in dirnames:
            child = current / name
            if child.is_symlink():
                continue
            safe_dirs.append(name)
            rel = child.relative_to(src)
            (dst / rel).mkdir(parents=True, exist_ok=True)
        dirnames[:] = safe_dirs
        for name in filenames:
            source_file = current / name
            if source_file.is_symlink() or not source_file.is_file():
                continue
            rel = source_file.relative_to(src)
            target_file = dst / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)


def _seed_polygon_statement_sources(workspace: Path) -> None:
    statement_root = workspace / STATEMENT_DIR
    sections_root = workspace / STATEMENT_SECTIONS_DIR / "english"
    statement_root.mkdir(parents=True, exist_ok=True)
    sections_root.mkdir(parents=True, exist_ok=True)
    if not (workspace / STATEMENT_TEMPLATE_REL).exists():
        (workspace / STATEMENT_TEMPLATE_REL).write_text(DEFAULT_STATEMENT_TEMPLATE, encoding="utf-8")
    if not (workspace / STATEMENT_PROBLEM_REL).exists():
        (workspace / STATEMENT_PROBLEM_REL).write_text(DEFAULT_STATEMENT_PROBLEM_TEMPLATE, encoding="utf-8")
    if not (workspace / STATEMENT_STYLE_REL).exists():
        (workspace / STATEMENT_STYLE_REL).write_text(default_olymp_sty_text(), encoding="utf-8")
    defaults = {
        "name.tex": DEFAULT_PROBLEM_TITLE + "\n",
        "legend.tex": "",
        "input.tex": "",
        "output.tex": "",
        "notes.tex": "",
    }
    for rel, content in defaults.items():
        path = sections_root / rel
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def _render_polygon_statement(workspace: Path, statement_root: Path, problem_title: str | None = None) -> Path:
    template_text = _read_required_text(
        workspace / STATEMENT_TEMPLATE_REL,
        label=f"statement template ({STATEMENT_TEMPLATE_REL.as_posix()})",
    )
    problem_template_text = _safe_read_text(workspace / STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
    _read_required_text(
        workspace / STATEMENT_STYLE_REL,
        label=f"statement olymp style ({STATEMENT_STYLE_REL.as_posix()})",
    )

    language = _pick_statement_language(workspace)
    rendered_lang_root = workspace / STATEMENT_RENDERED_DIR_REL / language
    shutil.rmtree(rendered_lang_root, ignore_errors=True)
    rendered_lang_root.mkdir(parents=True, exist_ok=True)
    _copy_tree_without_symlinks(workspace / STATEMENT_SECTIONS_DIR / language, rendered_lang_root)
    sample_tests = _collect_sample_tests(workspace, rendered_lang_root)
    problem_ctx = _problem_context_for_language(
        workspace,
        language,
        problem_title,
        sample_tests=sample_tests,
    )
    rendered_problem_tex = _render_ftl_template(
        problem_template_text,
        {
            "problem": problem_ctx,
            "language": language,
            "contest": {"name": "", "location": "", "date": "", "language": language},
            "shortProblemTitle": False,
            "providedStatementsCommands": [],
            "statements": [],
        },
    )
    (rendered_lang_root / "problem.tex").write_text(rendered_problem_tex, encoding="utf-8")

    rendered_main = _render_ftl_template(
        template_text,
        {
            "contest": {"name": "", "location": "", "date": "", "language": language},
            "language": language,
            "shortProblemTitle": True,
            "providedStatementsCommands": [],
            "statements": [{"path": f"rendered/{language}/", "file": "problem.tex"}],
            "problem": problem_ctx,
        },
    )
    main_path = workspace / STATEMENT_MAIN_REL
    main_path.parent.mkdir(parents=True, exist_ok=True)
    main_path.write_text(rendered_main, encoding="utf-8")
    return main_path


def seed_statement_sources(workspace: Path) -> None:
    _seed_polygon_statement_sources(workspace)


def render_statement_main(statement_root: Path, problem_title: str | None = None) -> Path:
    workspace = statement_root.parent
    return _render_polygon_statement(workspace, statement_root, problem_title=problem_title)


def statement_sources_signature(workspace: Path, problem_title: str | None = None) -> str:
    """Stable signature of statement sources (excluding derived statement/main.tex)."""
    statement_root = workspace / STATEMENT_DIR
    entries: list[dict[str, object]] = [
        {"kind": "renderer-version", "value": STATEMENT_RENDERER_SIGNATURE_VERSION},
    ]
    if not statement_root.exists() or not statement_root.is_dir() or statement_root.is_symlink():
        entries.append({"kind": "statement-root", "state": "missing"})
        return quick_fp_digest(entries, schema="statement-signature.v2")

    files: list[tuple[str, Path]] = []
    for base in (workspace / STATEMENT_DIR, workspace / STATEMENT_SECTIONS_DIR):
        if not base.exists() or not base.is_dir() or base.is_symlink():
            continue
        for path in base.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(workspace).as_posix()
            except (OSError, ValueError):
                continue
            if rel == STATEMENT_MAIN_REL.as_posix():
                continue
            if rel.startswith(f"{STATEMENT_RENDERED_DIR_REL.as_posix()}/"):
                continue
            files.append((rel, path))
    files.sort(key=lambda item: item[0])

    for rel, path in files:
        try:
            stat_obj = path.stat()
            mtime_ns = int(getattr(stat_obj, "st_mtime_ns", int(float(stat_obj.st_mtime) * 1_000_000_000)))
            entries.append({"kind": "statement-file", "path": rel, "state": "ok", "size": int(stat_obj.st_size), "mtime_ns": mtime_ns})
        except OSError:
            entries.append({"kind": "statement-file", "path": rel, "state": "unreadable"})

    # Include sample source-of-truth so tests/sample changes mark statement preview stale.
    tests_spec_rel = TESTS_SPEC_REL.as_posix()
    tests_spec_path = _safe_workspace_regular_file(workspace, TESTS_SPEC_REL)
    if tests_spec_path is None:
        entries.append({"kind": "tests-spec", "path": tests_spec_rel, "state": "missing"})
    else:
        try:
            stat_obj = tests_spec_path.stat()
            mtime_ns = int(getattr(stat_obj, "st_mtime_ns", int(float(stat_obj.st_mtime) * 1_000_000_000)))
            entries.append({"kind": "tests-spec", "path": tests_spec_rel, "state": "ok", "size": int(stat_obj.st_size), "mtime_ns": mtime_ns})
        except OSError:
            entries.append({"kind": "tests-spec", "path": tests_spec_rel, "state": "unreadable"})

    spec_path = workspace / TESTS_SPEC_REL
    try:
        spec_rows = load_tests_spec(spec_path)
    except Exception as exc:
        raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
    sample_related_files: list[Path] = []
    for index, row in enumerate(spec_rows, start=1):
        if not isinstance(row, dict):
            continue
        if not bool(row.get("sample")):
            continue
        test_id = str(row.get("id") or "").strip()
        kind = str(row.get("kind") or "").strip().lower() or "manual"
        if kind not in {"manual", "gen"}:
            raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}: {kind or '(empty)'}")
        if not test_id:
            continue
        # Custom sample text already changes tests/spec.json hash.
        if not str(row.get("sample_input") or ""):
            sample_in = _safe_workspace_regular_file(workspace, payload_rel_path_for_test(test_id, kind))
            if sample_in is not None:
                sample_related_files.append(sample_in)
        if not str(row.get("sample_output") or ""):
            sample_ans = _safe_workspace_regular_file(workspace, TESTS_ANSWERS_DIR_REL / f"{test_id}.ans")
            if sample_ans is not None:
                sample_related_files.append(sample_ans)
    uniq_sample_files = sorted(
        {path.relative_to(workspace).as_posix(): path for path in sample_related_files}.items(),
        key=lambda item: item[0],
    )
    for rel, path in uniq_sample_files:
        try:
            stat_obj = path.stat()
            mtime_ns = int(getattr(stat_obj, "st_mtime_ns", int(float(stat_obj.st_mtime) * 1_000_000_000)))
            entries.append({"kind": "sample-file", "path": rel, "state": "ok", "size": int(stat_obj.st_size), "mtime_ns": mtime_ns})
        except OSError:
            entries.append({"kind": "sample-file", "path": rel, "state": "unreadable"})

    if problem_title is not None:
        entries.append({"kind": "problem-title", "value": str(problem_title or "").strip()})
    return quick_fp_digest(entries, schema="statement-signature.v2")
