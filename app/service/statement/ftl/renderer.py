from __future__ import annotations

import re

from app.service.statement.ftl.evaluator import (
    UNDEFINED,
    _eval_expr,
    _eval_interpolation,
    _iter_values,
    _truthy,
)
from app.service.statement.ftl.parser import _parse_nodes, _strip_standalone_directive_lines


FTL_COMMENT_RE = re.compile(r"<#--.*?-->", re.DOTALL)


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


def render_ftl_template(template_text: str, context: dict[str, object]) -> str:
    stripped = FTL_COMMENT_RE.sub("", str(template_text or ""))
    stripped = _strip_standalone_directive_lines(stripped)
    nodes, _pos, _stop_tag, _stop_arg = _parse_nodes(stripped, 0, set(), set())
    return _render_nodes(nodes, dict(context))

