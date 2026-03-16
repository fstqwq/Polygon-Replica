from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOUNDARY_PATH = ROOT / "migration-gates" / "import-boundaries.json"
DEFAULT_FIRST_WAVE_PATH = ROOT / "migration-gates" / "import-policy-first-wave.txt"
DEFAULT_BASELINE_PATH = ROOT / "migration-gates" / "import-policy-baseline.json"
DEFAULT_NAMING_RULE_SCOPE_PREFIXES = (
    "app.impl.auth.",
    "app.impl.build_preview.",
    "app.impl.run_export.",
    "app.impl.workspace.",
    "app.service.statement.",
)
DEFAULT_NAMING_PLURAL_SEGMENT_EXCEPTIONS = {
    "fs",
    "status",
    "process",
    "news",
    "series",
}


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    line: int
    importer: str
    target: str
    message: str
    first_wave: bool

    @property
    def key(self) -> str:
        return "|".join(
            [
                self.rule,
                self.file,
                str(self.line),
                self.importer,
                self.target,
            ]
        )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root_name in ("app", "tests", "scripts"):
        root = ROOT / root_name
        if not root.exists():
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def _module_name_for_path(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _path_for_module(module_name: str) -> Path:
    return ROOT / (module_name.replace(".", "/") + ".py")


def _load_first_wave(path: Path) -> list[str]:
    if not path.exists():
        return []
    items: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        items.append(line)
    return items


def _load_boundaries(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "firstWave": [], "layers": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid boundaries config: {path}")
    if "layers" not in data or not isinstance(data["layers"], list):
        data["layers"] = []
    if "firstWave" not in data or not isinstance(data["firstWave"], list):
        data["firstWave"] = []
    return data


def _in_prefixes(module_name: str, prefixes: Iterable[str]) -> bool:
    for prefix in prefixes:
        p = str(prefix).strip()
        if not p:
            continue
        if module_name == p or module_name.startswith(f"{p}."):
            return True
    return False


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


def _layer_for_module(module_name: str, layers: list[dict]) -> dict | None:
    for layer in layers:
        for raw in layer.get("match", []):
            prefix = str(raw or "").strip()
            if not prefix:
                continue
            if module_name.startswith(prefix):
                return layer
    return None


def _target_allowed(target_module: str, allowed: list[str]) -> bool:
    for raw in allowed:
        prefix = str(raw or "").strip()
        if not prefix:
            continue
        if target_module == prefix.rstrip("."):
            return True
        if target_module.startswith(prefix):
            return True
    return False


def _import_targets(importer_module: str, node: ast.ImportFrom, module_set: set[str]) -> set[str]:
    targets: set[str] = set()
    base = _resolve_imported_module(importer_module, node)
    if not base:
        return targets
    if base in module_set:
        targets.add(base)
    for alias in node.names:
        if alias.name == "*":
            continue
        sub = f"{base}.{alias.name}"
        if sub in module_set:
            targets.add(sub)
    return targets


def _cycle_signatures(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)

        for w in sorted(graph.get(v, set())):
            if w not in indices:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])

        if low[v] == indices[v]:
            component: list[str] = []
            while stack:
                w = stack.pop()
                on_stack.remove(w)
                component.append(w)
                if w == v:
                    break
            component_sorted = sorted(component)
            if len(component_sorted) > 1:
                sccs.append(component_sorted)
            elif component_sorted and component_sorted[0] in graph.get(component_sorted[0], set()):
                sccs.append(component_sorted)

    for v in sorted(graph):
        if v not in indices:
            strongconnect(v)
    return sorted(sccs)


def _dynamic_reexport_detected(importer_module: str, source: str) -> bool:
    # Only enforce this in application modules; scripts/tests may legitimately
    # contain these tokens in policy logic or fixture text.
    if not importer_module.startswith("app."):
        return False
    if "globals()" not in source and "globals()[" not in source:
        return False
    if "for _module in" in source or "for name in dir(" in source or "setdefault(" in source:
        return True
    return False


def _scope_for_naming_rules(
    importer_module: str,
    first_wave: list[str],
    scope_prefixes: Iterable[str],
) -> bool:
    if not _in_prefixes(importer_module, first_wave):
        return False
    return _in_prefixes(importer_module, scope_prefixes)


def _module_segments_for_naming(importer_module: str) -> list[str]:
    parts = importer_module.split(".")
    if len(parts) < 4:
        return []
    # app.impl.<domain>... or app.service.<domain>...
    if parts[0] != "app" or parts[1] not in {"impl", "service"}:
        return []
    return [p for p in parts[2:] if p]


def _plural_name_violations_for_module(
    importer_module: str,
    plural_exceptions: Iterable[str] | None = None,
) -> list[str]:
    exceptions = {
        str(item or "").strip().lower()
        for item in (plural_exceptions or DEFAULT_NAMING_PLURAL_SEGMENT_EXCEPTIONS)
        if str(item or "").strip()
    }
    violations: list[str] = []
    for segment in _module_segments_for_naming(importer_module):
        safe_segment = segment.lower()
        if "_" in segment:
            # snake_case module names are validated by the anti-affix rule.
            continue
        if not segment.endswith("s"):
            continue
        if safe_segment in exceptions:
            continue
        violations.append(segment)
    return violations


def _affix_cluster_modules(package_leaf: str, module_stems: list[str]) -> set[str]:
    leaf = str(package_leaf or "").strip().lower()
    stems = [str(item or "").strip().lower() for item in module_stems if str(item or "").strip()]
    if not leaf or len(stems) < 3:
        return set()
    offenders: set[str] = set()
    prefix_cluster = [stem for stem in stems if stem.startswith(f"{leaf}_")]
    suffix_cluster = [stem for stem in stems if stem.endswith(f"_{leaf}")]
    service_cluster = [stem for stem in stems if stem.endswith("_service")]
    impl_cluster = [stem for stem in stems if stem.endswith("_impl")]
    if len(prefix_cluster) >= 3:
        offenders.update(prefix_cluster)
    if len(suffix_cluster) >= 3:
        offenders.update(suffix_cluster)
    if len(service_cluster) >= 3:
        offenders.update(service_cluster)
    if len(impl_cluster) >= 3:
        offenders.update(impl_cluster)
    return offenders


def collect_audit(
    *,
    boundaries: dict,
    first_wave: list[str],
) -> tuple[list[Violation], list[list[str]], dict]:
    violations: list[Violation] = []
    module_for_path: dict[str, str] = {}
    module_set: set[str] = set()
    graph: dict[str, set[str]] = defaultdict(set)
    layers = boundaries.get("layers", [])
    naming_policy = boundaries.get("namingPolicy", {})
    scope_prefixes = naming_policy.get("scopePrefixes", list(DEFAULT_NAMING_RULE_SCOPE_PREFIXES))
    plural_exceptions = naming_policy.get(
        "pluralSegmentExceptions",
        list(DEFAULT_NAMING_PLURAL_SEGMENT_EXCEPTIONS),
    )

    python_files = _iter_python_files()
    for path in python_files:
        module = _module_name_for_path(path)
        module_for_path[path.relative_to(ROOT).as_posix()] = module
        if module.startswith("app."):
            module_set.add(module)

    first_wave_package_files: dict[str, list[str]] = defaultdict(list)
    first_wave_package_stems: dict[str, list[str]] = defaultdict(list)

    for path in python_files:
        rel = path.relative_to(ROOT).as_posix()
        source = _read_text(path)
        module = module_for_path[rel]
        in_first_wave = _in_prefixes(module, first_wave)
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError:
            continue

        if _dynamic_reexport_detected(module, source):
            violations.append(
                Violation(
                    rule="REEXPORT_DYNAMIC",
                    file=rel,
                    line=1,
                    importer=module,
                    target=module,
                    message="dynamic re-export chain is prohibited in first-wave modules",
                    first_wave=in_first_wave,
                )
            )

        if _scope_for_naming_rules(module, first_wave, scope_prefixes):
            for segment in _plural_name_violations_for_module(module, plural_exceptions):
                violations.append(
                    Violation(
                        rule="NAMING_PLURAL_SEGMENT",
                        file=rel,
                        line=1,
                        importer=module,
                        target=segment,
                        message="plural module/package segment is prohibited in scoped modules",
                        first_wave=in_first_wave,
                    )
                )
            path_obj = Path(rel)
            if path_obj.name != "__init__.py":
                package_key = path_obj.parent.as_posix()
                stem = path_obj.stem
                first_wave_package_files[package_key].append(rel)
                first_wave_package_stems[package_key].append(stem)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base_module = _resolve_imported_module(module, node)

                if any(alias.asname for alias in node.names):
                    violations.append(
                        Violation(
                            rule="ALIAS_FROM_IMPORT",
                            file=rel,
                            line=int(node.lineno),
                            importer=module,
                            target=base_module or str(node.module or ""),
                            message="`from X import Y as Z` is prohibited",
                            first_wave=in_first_wave,
                        )
                    )

                if any(alias.name == "*" for alias in node.names):
                    violations.append(
                        Violation(
                            rule="WILDCARD_IMPORT",
                            file=rel,
                            line=int(node.lineno),
                            importer=module,
                            target=base_module or str(node.module or ""),
                            message="wildcard import is prohibited",
                            first_wave=in_first_wave,
                        )
                    )

                if node.level > 0 and node.module:
                    violations.append(
                        Violation(
                            rule="MESH_RELATIVE_IMPORT",
                            file=rel,
                            line=int(node.lineno),
                            importer=module,
                            target=base_module or str(node.module or ""),
                            message="mesh-style relative import is prohibited",
                            first_wave=in_first_wave,
                        )
                    )

                if module.startswith("app.") and base_module.startswith("app."):
                    layer = _layer_for_module(module, layers)
                    if layer is not None and not _target_allowed(base_module, list(layer.get("allow", []))):
                        violations.append(
                            Violation(
                                rule="BOUNDARY_LAYER_VIOLATION",
                                file=rel,
                                line=int(node.lineno),
                                importer=module,
                                target=base_module,
                                message=f"layer `{layer.get('name', 'unknown')}` cannot import `{base_module}`",
                                first_wave=in_first_wave,
                            )
                        )

                if module.startswith("app.") and _in_prefixes(module, first_wave):
                    for target in _import_targets(module, node, module_set):
                        if _in_prefixes(target, first_wave):
                            graph[module].add(target)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported = str(alias.name or "").strip()
                    if not imported:
                        continue
                    if module.startswith("app.") and _in_prefixes(module, first_wave):
                        candidate = imported
                        if candidate in module_set and _in_prefixes(candidate, first_wave):
                            graph[module].add(candidate)

    for module in sorted(module_set):
        if _in_prefixes(module, first_wave):
            graph.setdefault(module, set())

    for package_key, stems in first_wave_package_stems.items():
        package_leaf = Path(package_key).name
        offenders = _affix_cluster_modules(package_leaf, stems)
        if not offenders:
            continue
        for rel in sorted(first_wave_package_files.get(package_key, [])):
            stem = Path(rel).stem.lower()
            if stem not in offenders:
                continue
            importer = module_for_path.get(rel, "")
            in_first_wave = _in_prefixes(importer, first_wave)
            violations.append(
                Violation(
                    rule="NAMING_AFFIX_CLUSTER",
                    file=rel,
                    line=1,
                    importer=importer,
                    target=package_leaf,
                    message="module name participates in forbidden affix-heavy cluster",
                    first_wave=in_first_wave,
                )
            )

    cycles = _cycle_signatures(graph)
    counts = Counter(v.rule for v in violations)
    meta = {
        "summary": {
            "violations_total": len(violations),
            "cycles_total": len(cycles),
            "rule_counts": dict(sorted(counts.items())),
        },
        "moduleCount": len(module_set),
        "firstWaveModuleCount": len([m for m in module_set if _in_prefixes(m, first_wave)]),
    }
    return sorted(violations, key=lambda v: (v.file, v.line, v.rule, v.target)), cycles, meta


def _load_baseline(path: Path) -> tuple[set[str], set[str]]:
    if not path.exists():
        return set(), set()
    data = json.loads(path.read_text(encoding="utf-8"))
    violations = data.get("violations", [])
    cycles = data.get("cycles", [])
    violation_keys = {
        "|".join(
            [
                str(v.get("rule", "")),
                str(v.get("file", "")),
                str(v.get("line", "")),
                str(v.get("importer", "")),
                str(v.get("target", "")),
            ]
        )
        for v in violations
    }
    cycle_keys = {"|".join(sorted(map(str, c.get("nodes", [])))) for c in cycles}
    return violation_keys, cycle_keys


def _changed_python_files(base_ref: str) -> set[str]:
    try:
        proc = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=ACMR",
                f"{base_ref}...HEAD",
            ],
            check=True,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except Exception:
        return set()
    changed = {
        line.strip().replace("\\", "/")
        for line in proc.stdout.splitlines()
        if str(line).strip().endswith(".py")
    }
    return {p for p in changed if p.startswith(("app/", "tests/", "scripts/"))}


def _audit_payload(
    *,
    boundaries: dict,
    first_wave: list[str],
    violations: list[Violation],
    cycles: list[list[str]],
    meta: dict,
) -> dict:
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "boundariesPath": str(DEFAULT_BOUNDARY_PATH.relative_to(ROOT).as_posix()),
        "firstWavePath": str(DEFAULT_FIRST_WAVE_PATH.relative_to(ROOT).as_posix()),
        "firstWave": first_wave,
        "boundaries": boundaries,
        "summary": meta["summary"],
        "meta": {k: v for k, v in meta.items() if k != "summary"},
        "violations": [
            {
                "rule": v.rule,
                "file": v.file,
                "line": v.line,
                "importer": v.importer,
                "target": v.target,
                "message": v.message,
                "firstWave": v.first_wave,
            }
            for v in violations
        ],
        "cycles": [{"nodes": list(nodes)} for nodes in cycles],
    }


def _print_text_report(payload: dict, *, show_details: bool) -> None:
    summary = payload["summary"]
    print(
        f"Import policy audit: violations={summary['violations_total']} cycles={summary['cycles_total']}"
    )
    if summary["rule_counts"]:
        for rule, count in sorted(summary["rule_counts"].items()):
            print(f"  {rule}: {count}")
    if show_details and payload["violations"]:
        print("\nViolations:")
        for v in payload["violations"]:
            print(
                f"  - {v['file']}:{v['line']} [{v['rule']}] "
                f"{v['message']} ({v['importer']} -> {v['target']})"
            )
    if show_details and payload["cycles"]:
        print("\nCycles:")
        for cycle in payload["cycles"]:
            print(f"  - {' -> '.join(cycle['nodes'])}")


def cmd_audit(args: argparse.Namespace) -> int:
    boundaries = _load_boundaries(Path(args.boundaries))
    first_wave = list(boundaries.get("firstWave", [])) or _load_first_wave(Path(args.first_wave))
    violations, cycles, meta = collect_audit(boundaries=boundaries, first_wave=first_wave)
    payload = _audit_payload(
        boundaries=boundaries,
        first_wave=first_wave,
        violations=violations,
        cycles=cycles,
        meta=meta,
    )
    output_path = Path(args.output) if args.output else None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text_report(payload, show_details=bool(args.verbose))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    boundaries = _load_boundaries(Path(args.boundaries))
    first_wave = list(boundaries.get("firstWave", [])) or _load_first_wave(Path(args.first_wave))
    violations, cycles, meta = collect_audit(boundaries=boundaries, first_wave=first_wave)
    payload = _audit_payload(
        boundaries=boundaries,
        first_wave=first_wave,
        violations=violations,
        cycles=cycles,
        meta=meta,
    )
    baseline_keys, baseline_cycle_keys = _load_baseline(Path(args.baseline))
    changed_files = _changed_python_files(args.base_ref) if args.changed_only else set()

    new_violations = [v for v in violations if v.key not in baseline_keys]
    new_cycles = [nodes for nodes in cycles if "|".join(nodes) not in baseline_cycle_keys]

    if changed_files:
        changed_files_norm = {p.replace("\\", "/") for p in changed_files}
        new_violations = [v for v in new_violations if v.file in changed_files_norm]
        module_by_file = {
            _module_name_for_path(ROOT / f): f for f in changed_files_norm if (ROOT / f).exists()
        }
        changed_modules = set(module_by_file.keys())
        new_cycles = [
            nodes
            for nodes in new_cycles
            if any(node in changed_modules for node in nodes)
        ]

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text_report(payload, show_details=bool(args.verbose))

    if not Path(args.baseline).exists():
        print(f"\nskipping no-new gate; missing baseline: {args.baseline}", file=sys.stderr)
        return 0

    if new_violations or new_cycles:
        print("\nImport policy check failed: new violations detected.", file=sys.stderr)
        for v in new_violations:
            print(
                f"  NEW {v.file}:{v.line} [{v.rule}] {v.importer} -> {v.target}",
                file=sys.stderr,
            )
        for nodes in new_cycles:
            print(f"  NEW CYCLE {' -> '.join(nodes)}", file=sys.stderr)
        return 1

    if args.changed_only and not changed_files:
        print("\nNo changed python files detected; skipped no-new gate.")
    else:
        print("\nImport policy check passed: no new violations/cycles.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import architecture policy audit/check tool")
    parser.add_argument(
        "--boundaries",
        default=str(DEFAULT_BOUNDARY_PATH),
        help="Path to import boundary config JSON",
    )
    parser.add_argument(
        "--first-wave",
        default=str(DEFAULT_FIRST_WAVE_PATH),
        help="Path to first-wave module prefixes list",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    audit = sub.add_parser("audit", help="emit current policy violations and cycle inventory")
    audit.add_argument("--output", default=str(DEFAULT_BASELINE_PATH))
    audit.add_argument("--format", choices=("text", "json"), default="json")
    audit.add_argument("--verbose", action="store_true")
    audit.set_defaults(func=cmd_audit)

    check = sub.add_parser("check", help="enforce no-new violations/cycles against baseline")
    check.add_argument("--baseline", default=str(DEFAULT_BASELINE_PATH))
    check.add_argument("--changed-only", action="store_true")
    check.add_argument("--base-ref", default="HEAD~1")
    check.add_argument("--format", choices=("text", "json"), default="text")
    check.add_argument("--verbose", action="store_true")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
