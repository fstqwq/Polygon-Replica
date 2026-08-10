from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = ROOT / "tests"
MANIFEST_PATH = TEST_ROOT / "resource_groups.json"
GROUP_ORDER = ("unit", "db", "workspace", "executor", "large-fixture", "e2e")


def load_manifest() -> dict[str, list[str]]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if set(payload) != set(GROUP_ORDER):
        raise RuntimeError("test resource manifest must define exactly the canonical groups")
    groups = {group: list(payload[group]) for group in GROUP_ORDER}
    listed = [name for names in groups.values() for name in names]
    duplicates = sorted({name for name in listed if listed.count(name) > 1})
    discovered = sorted(path.name for path in TEST_ROOT.glob("test_*.py"))
    if duplicates:
        raise RuntimeError(f"test modules assigned more than once: {', '.join(duplicates)}")
    missing = sorted(set(discovered) - set(listed))
    stale = sorted(set(listed) - set(discovered))
    if missing or stale:
        parts = []
        if missing:
            parts.append(f"unassigned: {', '.join(missing)}")
        if stale:
            parts.append(f"missing files: {', '.join(stale)}")
        raise RuntimeError("invalid test resource manifest: " + "; ".join(parts))
    return groups


def _direct_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if node.level and module in {"common", "db_fixture"}:
                module = f"tests.{module}"
            imported.add(module)
    return imported


def validate_resource_contracts(groups: dict[str, list[str]]) -> None:
    unit_forbidden = {
        "app.impl.runtime.config",
        "subprocess",
        "tests.common",
        "tests.db_fixture",
    }
    db_forbidden = {"subprocess", "tests.common"}
    violations: list[str] = []
    for filename in groups["unit"]:
        imports = _direct_imports(TEST_ROOT / filename)
        matched = sorted(imports & unit_forbidden)
        if matched:
            violations.append(f"unit/{filename}: {', '.join(matched)}")
    for filename in groups["db"]:
        imports = _direct_imports(TEST_ROOT / filename)
        matched = sorted(imports & db_forbidden)
        if matched:
            violations.append(f"db/{filename}: {', '.join(matched)}")
    if violations:
        raise RuntimeError("test resource contract violations: " + "; ".join(violations))


class TimingResult(unittest.TextTestResult):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._started_at = 0.0
        self.durations: list[tuple[float, str]] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self._started_at = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test: unittest.case.TestCase) -> None:
        self.durations.append((time.perf_counter() - self._started_at, test.id()))
        super().stopTest(test)


def _module_name(filename: str) -> str:
    return f"tests.{Path(filename).stem}"


def _write_timing(group: str, elapsed: float, result: TimingResult) -> None:
    timing_root = ROOT / ".test-results"
    timing_root.mkdir(parents=True, exist_ok=True)
    rows = [
        {"seconds": round(duration, 6), "test": test_id}
        for duration, test_id in sorted(result.durations, reverse=True)
    ]
    payload = {
        "group": group,
        "elapsed_seconds": round(elapsed, 6),
        "test_count": int(result.testsRun),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "tests": rows,
    }
    (timing_root / f"{group}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\n[{group}] slowest tests:")
    for row in rows[:10]:
        print(f"  {row['seconds']:8.3f}s  {row['test']}")


def run_group(group: str, filenames: list[str]) -> bool:
    from app.service.platform.worker_queue import WorkerQueueService

    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromNames([_module_name(name) for name in filenames])
    if group == "unit" and "app.impl.runtime.config" in sys.modules:
        raise RuntimeError("unit tests imported app.impl.runtime.config")
    print(f"Running {group}: {suite.countTestCases()} tests from {len(filenames)} modules")
    started = time.perf_counter()
    runner = unittest.TextTestRunner(verbosity=2, resultclass=TimingResult)
    with contextlib.ExitStack() as guards:
        if group in {"unit", "db"}:
            guards.enter_context(
                patch.object(
                    subprocess,
                    "run",
                    side_effect=AssertionError(f"{group} tests may not run subprocesses"),
                )
            )
            guards.enter_context(
                patch.object(
                    WorkerQueueService,
                    "submit",
                    side_effect=AssertionError(f"{group} tests may not submit worker jobs"),
                )
            )
            guards.enter_context(
                patch.object(
                    subprocess,
                    "Popen",
                    side_effect=AssertionError(f"{group} tests may not start subprocesses"),
                )
            )
        if group == "unit":
            guards.enter_context(
                patch.object(
                    sqlite3,
                    "connect",
                    side_effect=AssertionError("unit tests may not open SQLite"),
                )
            )
        result = runner.run(suite)
    elapsed = time.perf_counter() - started
    _write_timing(group, elapsed, result)
    return result.wasSuccessful()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run resource-classified unittest groups")
    parser.add_argument(
        "--check-manifest",
        action="store_true",
        help="validate test resource assignments without loading tests",
    )
    parser.add_argument("groups", nargs="*")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_manifest()
    validate_resource_contracts(manifest)
    if args.check_manifest:
        return 0
    requested = list(args.groups)
    if not requested:
        raw = os.environ.get("POLYGON_REPLICA_TEST_GROUPS", "").strip()
        requested = [token.strip() for token in raw.split(",") if token.strip()]
    if not requested:
        requested = list(GROUP_ORDER)
    unknown = sorted(set(requested) - set(GROUP_ORDER))
    if unknown:
        raise RuntimeError(f"unknown test resource groups: {', '.join(unknown)}")
    ok = True
    for group in GROUP_ORDER:
        if group in requested:
            ok = run_group(group, manifest[group]) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
