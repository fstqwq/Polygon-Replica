import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_VARIADIC_APPLICATION_ADAPTERS = frozenset(
    {
        ("app.impl.judgehost.api", "_run_service_call"),
        ("app.impl.contest.workspace_scope", "__call__"),
        ("app.route.problem_scoped_router", "add_api_route"),
        ("app.service.repository.merge", "_git"),
    }
)


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    line: int
    importer: str
    target: str
    message: str


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root_name in ("app", "tests", "scripts"):
        root = ROOT / root_name
        if root.exists():
            files.extend(root.rglob("*.py"))
    return sorted(files)


def _module_name_for_path(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("").as_posix()
    if relative.endswith("/__init__"):
        relative = relative[: -len("/__init__")]
    return relative.replace("/", ".")


def _resolve_imported_module(importer_module: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return str(node.module or "")
    importer_parts = importer_module.split(".")[:-1]
    levels_up = node.level - 1
    if levels_up > len(importer_parts):
        return ""
    base_parts = importer_parts[: len(importer_parts) - levels_up]
    if node.module:
        base_parts.extend(str(node.module).split("."))
    return ".".join(part for part in base_parts if part)


def _import_targets(
    importer_module: str,
    node: ast.ImportFrom,
    module_set: set[str],
) -> set[str]:
    targets: set[str] = set()
    base = _resolve_imported_module(importer_module, node)
    if not base:
        return targets
    if base in module_set:
        targets.add(base)
    for alias in node.names:
        if alias.name == "*":
            continue
        candidate = f"{base}.{alias.name}"
        if candidate in module_set:
            targets.add(candidate)
    return targets


def _cycle_signatures(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        low_links[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)

        for target in sorted(graph.get(module, set())):
            if target not in indices:
                visit(target)
                low_links[module] = min(low_links[module], low_links[target])
            elif target in on_stack:
                low_links[module] = min(low_links[module], indices[target])

        if low_links[module] != indices[module]:
            return
        component: list[str] = []
        while stack:
            target = stack.pop()
            on_stack.remove(target)
            component.append(target)
            if target == module:
                break
        component.sort()
        if len(component) > 1 or (
            component and component[0] in graph.get(component[0], set())
        ):
            components.append(component)

    for module in sorted(graph):
        if module not in indices:
            visit(module)
    return sorted(components)


def _dynamic_reexport_detected(importer_module: str, source: str) -> bool:
    if not importer_module.startswith("app."):
        return False
    if "globals()" not in source and "globals()[" not in source:
        return False
    return (
        "for _module in" in source
        or "for name in dir(" in source
        or "setdefault(" in source
    )


def _imported_bindings(
    importer_module: str,
    tree: ast.Module,
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            source_module = _resolve_imported_module(importer_module, node)
            for alias in node.names:
                if alias.name == "*":
                    continue
                binding = alias.asname or alias.name
                target = (
                    f"{source_module}.{alias.name}"
                    if source_module
                    else alias.name
                )
                bindings[binding] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".", 1)[0]
                bindings[binding] = alias.name
    return bindings


def _static_all_names(value: ast.expr) -> tuple[str, ...] | None:
    if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return None
    names: list[str] = []
    for item in value.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        names.append(item.value)
    return tuple(names)


def _all_reexport_violations(
    *,
    relative: str,
    importer_module: str,
    tree: ast.Module,
) -> list[Violation]:
    """Reject imported symbols exposed through __all__ outside package initializers."""

    if relative.endswith("/__init__.py"):
        return []
    imported = _imported_bindings(importer_module, tree)
    violations: list[Violation] = []
    for node in tree.body:
        value: ast.expr | None = None
        is_all_assignment = False
        if isinstance(node, ast.Assign):
            is_all_assignment = any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            is_all_assignment = (
                isinstance(node.target, ast.Name) and node.target.id == "__all__"
            )
            value = node.value
        elif isinstance(node, ast.AugAssign):
            is_all_assignment = (
                isinstance(node.target, ast.Name) and node.target.id == "__all__"
            )
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "__all__"
        ):
            is_all_assignment = True
        if not is_all_assignment:
            continue
        names = _static_all_names(value) if value is not None else None
        if names is None:
            violations.append(
                Violation(
                    rule="REEXPORT_ALL_DYNAMIC",
                    file=relative,
                    line=int(node.lineno),
                    importer=importer_module,
                    target=importer_module,
                    message=(
                        "non-__init__.py modules must not construct __all__ "
                        "dynamically"
                    ),
                )
            )
            continue
        for name in names:
            target = imported.get(name)
            if target is None:
                continue
            violations.append(
                Violation(
                    rule="REEXPORT_ALL_IMPORTED",
                    file=relative,
                    line=int(node.lineno),
                    importer=importer_module,
                    target=target,
                    message=(
                        f"non-__init__.py module re-exports imported name "
                        f"through __all__: {name}"
                    ),
                )
            )
    return violations


def _discard_assignment_violations(
    *,
    relative: str,
    importer_module: str,
    tree: ast.Module,
) -> list[Violation]:
    """Reject assignments that manufacture usage by assigning to `_`."""

    violations: list[Violation] = []
    for node in ast.walk(tree):
        is_discard_assignment = (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_"
                for target in node.targets
            )
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_"
        )
        if not is_discard_assignment:
            continue
        violations.append(
            Violation(
                rule="DISCARD_ASSIGNMENT",
                file=relative,
                line=int(node.lineno),
                importer=importer_module,
                target="_",
                message=(
                    "do not assign expressions to `_` to manufacture symbol usage; "
                    "remove unused names or evaluate a required side effect directly"
                ),
            )
        )
    violations.sort(key=lambda violation: violation.line)
    return violations


def _variadic_business_signature_violations(
    *,
    relative: str,
    importer_module: str,
    tree: ast.Module,
) -> list[Violation]:
    """Reject variadic signatures on ordinary application operations."""

    if not _is_module_or_child(importer_module, "app"):
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.args.vararg is None and node.args.kwarg is None:
            continue
        identity = (importer_module, node.name)
        if identity in _VARIADIC_APPLICATION_ADAPTERS:
            continue
        parameters = ", ".join(
            token
            for token, present in (
                ("*args", node.args.vararg is not None),
                ("**kwargs", node.args.kwarg is not None),
            )
            if present
        )
        violations.append(
            Violation(
                rule="VARIADIC_BUSINESS_SIGNATURE",
                file=relative,
                line=int(node.lineno),
                importer=importer_module,
                target=node.name,
                message=(
                    f"application operation `{node.name}` declares {parameters}; "
                    "use an exact typed signature so unknown arguments fail"
                ),
            )
        )
    violations.sort(key=lambda violation: violation.line)
    return violations


def _is_module_or_child(module_name: str, owner: str) -> bool:
    return module_name == owner or module_name.startswith(f"{owner}.")


def _layer_violation(importer: str, target: str) -> str | None:
    """Derive layer direction from the canonical package topology."""

    layer = ""
    allowed: tuple[str, ...] | None = None
    if _is_module_or_child(importer, "app.route"):
        layer = "route"
        allowed = ("app.route", "app.impl")
    elif _is_module_or_child(importer, "app.impl"):
        layer = "implementation"
        allowed = (
            "app.impl",
            "app.config",
            "app.service",
            "app.db",
            "app.main_constant",
            "app.main_util",
            "app.runtime",
            "app.setting",
        )
    elif _is_module_or_child(importer, "app.service"):
        layer = "service"
        allowed = (
            "app.service",
            "app.config",
            "app.db",
            "app.main_constant",
            "app.main_util",
            "app.setting",
        )
    if allowed is None or any(_is_module_or_child(target, owner) for owner in allowed):
        return None
    return f"layer `{layer}` cannot import `{target}`"


def collect_audit() -> tuple[list[Violation], list[list[str]], dict[str, object]]:
    """Inspect every repository Python file and the complete application graph."""

    violations: list[Violation] = []
    python_files = _iter_python_files()
    module_by_path = {path: _module_name_for_path(path) for path in python_files}
    app_modules = {
        module for module in module_by_path.values() if _is_module_or_child(module, "app")
    }
    graph: dict[str, set[str]] = defaultdict(set)

    for path in python_files:
        relative = path.relative_to(ROOT).as_posix()
        source = _read_text(path)
        module = module_by_path[path]
        tree = ast.parse(source, filename=relative)

        violations.extend(
            _all_reexport_violations(
                relative=relative,
                importer_module=module,
                tree=tree,
            )
        )
        violations.extend(
            _discard_assignment_violations(
                relative=relative,
                importer_module=module,
                tree=tree,
            )
        )
        violations.extend(
            _variadic_business_signature_violations(
                relative=relative,
                importer_module=module,
                tree=tree,
            )
        )

        if _dynamic_reexport_detected(module, source):
            violations.append(
                Violation(
                    rule="REEXPORT_DYNAMIC",
                    file=relative,
                    line=1,
                    importer=module,
                    target=module,
                    message="dynamic re-export chain is prohibited in application modules",
                )
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve_imported_module(module, node)
                if any(alias.asname for alias in node.names):
                    violations.append(
                        Violation(
                            rule="ALIAS_FROM_IMPORT",
                            file=relative,
                            line=int(node.lineno),
                            importer=module,
                            target=target or str(node.module or ""),
                            message="`from X import Y as Z` is prohibited",
                        )
                    )
                if any(alias.name == "*" for alias in node.names):
                    violations.append(
                        Violation(
                            rule="WILDCARD_IMPORT",
                            file=relative,
                            line=int(node.lineno),
                            importer=module,
                            target=target or str(node.module or ""),
                            message="wildcard import is prohibited",
                        )
                    )
                if node.level > 0 and node.module:
                    violations.append(
                        Violation(
                            rule="MESH_RELATIVE_IMPORT",
                            file=relative,
                            line=int(node.lineno),
                            importer=module,
                            target=target or str(node.module or ""),
                            message="mesh-style relative import is prohibited",
                        )
                    )
                if module in app_modules and target.startswith("app."):
                    message = _layer_violation(module, target)
                    if message is not None:
                        violations.append(
                            Violation(
                                rule="BOUNDARY_LAYER_VIOLATION",
                                file=relative,
                                line=int(node.lineno),
                                importer=module,
                                target=target,
                                message=message,
                            )
                        )
                if module in app_modules:
                    graph[module].update(_import_targets(module, node, app_modules))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = str(alias.name or "")
                    if module in app_modules and target.startswith("app."):
                        message = _layer_violation(module, target)
                        if message is not None:
                            violations.append(
                                Violation(
                                    rule="BOUNDARY_LAYER_VIOLATION",
                                    file=relative,
                                    line=int(node.lineno),
                                    importer=module,
                                    target=target,
                                    message=message,
                                )
                            )
                        if target in app_modules:
                            graph[module].add(target)

    for module in app_modules:
        graph.setdefault(module, set())
    cycles = _cycle_signatures(graph)
    counts = Counter(violation.rule for violation in violations)
    metadata: dict[str, object] = {
        "pythonFileCount": len(python_files),
        "applicationModuleCount": len(app_modules),
        "summary": {
            "violations_total": len(violations),
            "cycles_total": len(cycles),
            "rule_counts": dict(sorted(counts.items())),
        },
    }
    return (
        sorted(violations, key=lambda item: (item.file, item.line, item.rule, item.target)),
        cycles,
        metadata,
    )


def _audit_payload(
    violations: list[Violation],
    cycles: list[list[str]],
    metadata: dict[str, object],
) -> dict[str, object]:
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": metadata["summary"],
        "meta": {key: value for key, value in metadata.items() if key != "summary"},
        "violations": [asdict(violation) for violation in violations],
        "cycles": [{"nodes": nodes} for nodes in cycles],
    }


def _print_text_report(payload: dict[str, object], *, show_details: bool) -> None:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    print(
        "Import policy audit: "
        f"violations={summary['violations_total']} cycles={summary['cycles_total']}"
    )
    rule_counts = summary["rule_counts"]
    assert isinstance(rule_counts, dict)
    for rule, count in sorted(rule_counts.items()):
        print(f"  {rule}: {count}")
    violations = payload["violations"]
    cycles = payload["cycles"]
    assert isinstance(violations, list)
    assert isinstance(cycles, list)
    if show_details and violations:
        print("\nViolations:")
        for violation in violations:
            assert isinstance(violation, dict)
            print(
                f"  - {violation['file']}:{violation['line']} [{violation['rule']}] "
                f"{violation['message']} "
                f"({violation['importer']} -> {violation['target']})"
            )
    if show_details and cycles:
        print("\nCycles:")
        for cycle in cycles:
            assert isinstance(cycle, dict)
            nodes = cycle["nodes"]
            assert isinstance(nodes, list)
            print(f"  - {' -> '.join(nodes)}")


def _run(args: argparse.Namespace, *, fail_on_findings: bool) -> int:
    violations, cycles, metadata = collect_audit()
    payload = _audit_payload(violations, cycles, metadata)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text_report(payload, show_details=bool(args.verbose))
    if fail_on_findings and (violations or cycles):
        print("\nImport policy check failed.", file=sys.stderr)
        for violation in violations:
            print(
                f"  {violation.file}:{violation.line} [{violation.rule}] "
                f"{violation.importer} -> {violation.target}",
                file=sys.stderr,
            )
        for cycle in cycles:
            print(f"  CYCLE {' -> '.join(cycle)}", file=sys.stderr)
        return 1
    if fail_on_findings:
        print("\nImport policy check passed: complete app graph is cycle-free.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit every Python import and the complete application graph"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser("audit", help="emit the complete import inventory")
    audit.add_argument("--output")
    audit.add_argument("--format", choices=("text", "json"), default="json")
    audit.add_argument("--verbose", action="store_true")
    audit.set_defaults(func=lambda args: _run(args, fail_on_findings=False))
    check = subparsers.add_parser("check", help="enforce the complete import policy")
    check.add_argument("--output")
    check.add_argument("--format", choices=("text", "json"), default="text")
    check.add_argument("--verbose", action="store_true")
    check.set_defaults(func=lambda args: _run(args, fail_on_findings=True))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
