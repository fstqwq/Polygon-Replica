from __future__ import annotations

import re


FTL_LIST_RE = re.compile(r"^list\s+(.+?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", re.DOTALL)
STANDALONE_OPEN_DIRECTIVE_PREFIXES = ("if ", "elseif ", "list ", "assign ")
STANDALONE_OPEN_DIRECTIVE_EXACT = {"else"}
STANDALONE_CLOSE_DIRECTIVES = {"if", "list"}


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

