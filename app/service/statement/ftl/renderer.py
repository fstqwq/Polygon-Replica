import re

from app.service.statement.ftl.evaluator import (
    UNDEFINED,
    _eval_expr,
    _eval_interpolation,
    _iter_values,
    _truthy,
)
from app.service.statement.ftl.parser import (
    FtlNode,
    _parse_nodes,
    _strip_standalone_directive_lines,
)


FTL_COMMENT_RE = re.compile(r"<#--.*?-->", re.DOTALL)


def _render_nodes(nodes: list[FtlNode], scope: dict[str, object]) -> str:
    out: list[str] = []
    for node in nodes:
        if node["type"] == "text":
            out.append(node["value"])
            continue
        if node["type"] == "expr":
            try:
                out.append(_eval_interpolation(node["expr"], scope))
            except Exception:
                out.append("")
            continue
        if node["type"] == "assign":
            name = node["name"].strip()
            expr = node["expr"].strip()
            if not name or not expr:
                continue
            try:
                scope[name] = _eval_expr(expr, scope)
            except Exception:
                scope[name] = UNDEFINED
            continue
        if node["type"] == "if":
            for cond, branch_nodes in node["branches"]:
                if cond is None:
                    out.append(_render_nodes(branch_nodes, dict(scope)))
                    break
                try:
                    ok = _truthy(_eval_expr(cond, scope))
                except Exception:
                    ok = False
                if ok:
                    out.append(_render_nodes(branch_nodes, dict(scope)))
                    break
            continue
        if node["type"] == "list":
            expr = node["expr"]
            item_name = node["item"].strip()
            children = node["children"]
            if not expr or not item_name:
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
    stripped = FTL_COMMENT_RE.sub("", template_text)
    stripped = _strip_standalone_directive_lines(stripped)
    nodes, _pos, _stop_tag, _stop_arg = _parse_nodes(stripped, 0, set(), set())
    return _render_nodes(nodes, dict(context))
