"""Verified revision manifest creation and validation."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Literal, NotRequired, TypedDict, cast

from app.service.platform.hashing import sha256_file
from app.service.problem.build_config import load_build_config
from app.service.problem.json_codec import loads_object
from app.service.problem.solution_metadata import (
    load_solution_desc,
    normalize_expected_behavior,
)
from app.service.problem.source_tree import solution_sources
from app.service.problem.runtime_config import ProblemMode
from app.service.problem.test_spec import load_tests_spec
from app.service.problem_package.layout import (
    PACKAGE_DERIVED_ROOT_NAMES,
    TEST_DATA_DIR,
)
from app.service.verification.identity import canonical_verification_id


class VerifiedFileEntry(TypedDict):
    path: str
    sha256: str
    size: int


class VerifiedTestEntry(TypedDict):
    id: str
    kind: str
    sample: bool
    input: VerifiedFileEntry
    answer: NotRequired[VerifiedFileEntry]
    sample_input: NotRequired[VerifiedFileEntry]
    sample_output: NotRequired[VerifiedFileEntry]


VerifiedSolutionVerdict = Literal["AC", "WA", "TLE", "RTE", "CE"]
VERIFIED_SOLUTION_VERDICT_ORDER: tuple[VerifiedSolutionVerdict, ...] = (
    "AC",
    "WA",
    "TLE",
    "RTE",
    "CE",
)


class VerifiedSolutionEntry(TypedDict):
    source_path: str
    expected_behavior: str
    verdicts: list[VerifiedSolutionVerdict]


class VerifiedRevisionManifest(TypedDict):
    source_commit: str
    revision_number: int
    source_digest: str
    mode: ProblemMode
    pass_limit: int
    verification: dict[str, str]
    solutions: list[VerifiedSolutionEntry]
    tests: list[VerifiedTestEntry]


def canonical_rel_path(raw: str) -> str:
    """Return a safe canonical package-relative POSIX path."""

    if "\\" in raw:
        raise ValueError(f"invalid verified revision path: {raw}")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        raise ValueError(f"invalid verified revision path: {raw}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid verified revision path: {raw}")
    if path.as_posix() != raw:
        raise ValueError(f"non-canonical verified revision path: {raw}")
    return path.as_posix()


def describe_file(path: Path, *, root: Path) -> VerifiedFileEntry:
    resolved_root = root.resolve()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"verified payload is not a regular file: {path}")
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"verified payload escapes package root: {path}")
    return {
        "path": resolved.relative_to(resolved_root).as_posix(),
        "sha256": sha256_file(resolved),
        "size": int(resolved.stat().st_size),
    }


def source_digest(source_root: Path) -> str:
    """Hash authored source while excluding package-local derived trees."""

    root = source_root.resolve()
    derived_roots = PACKAGE_DERIVED_ROOT_NAMES
    entries: list[dict[str, object]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        parent = Path(dirpath)
        rel_parent = parent.relative_to(root)
        if not rel_parent.parts:
            dirnames[:] = [
                name for name in sorted(dirnames) if name not in derived_roots
            ]
        else:
            dirnames[:] = sorted(dirnames)
        for dirname in dirnames:
            directory = parent / dirname
            if directory.is_symlink():
                raise ValueError(f"published source contains a symbolic link: {directory.relative_to(root)}")
            entries.append({"path": directory.relative_to(root).as_posix() + "/"})
        for filename in sorted(filenames):
            source = parent / filename
            rel = source.relative_to(root)
            if rel.parts and rel.parts[0] in derived_roots:
                continue
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"published source contains a non-regular file: {rel}")
            entries.append(
                {
                    "path": rel.as_posix(),
                    "mode": stat.S_IMODE(source.stat().st_mode),
                    "size": int(source.stat().st_size),
                    "sha256": sha256_file(source),
                }
            )
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dumps_manifest(manifest: VerifiedRevisionManifest) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_manifest(path: Path) -> VerifiedRevisionManifest:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Polygon Replica package test-data/manifest.json is missing")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Polygon Replica package manifest must be UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"cannot read Polygon Replica package manifest: {exc}") from exc
    raw = loads_object(text, label="Polygon Replica package manifest")
    required = {
        "source_commit",
        "revision_number",
        "source_digest",
        "mode",
        "pass_limit",
        "verification",
        "solutions",
        "tests",
    }
    if set(raw) != required:
        raise ValueError("Polygon Replica package manifest has an unsupported shape")
    if not isinstance(raw["mode"], str) or raw["mode"] not in {
        "pass-fail",
        "interactive",
    }:
        raise ValueError("Polygon Replica package mode is invalid")
    if not isinstance(raw["source_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", raw["source_commit"]
    ):
        raise ValueError("Polygon Replica package source_commit is invalid")
    if (
        not isinstance(raw["revision_number"], int)
        or isinstance(raw["revision_number"], bool)
        or raw["revision_number"] < 1
    ):
        raise ValueError("Polygon Replica package revision_number is invalid")
    if not isinstance(raw["source_digest"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", raw["source_digest"]
    ):
        raise ValueError("Polygon Replica package source_digest is invalid")
    if (
        not isinstance(raw["pass_limit"], int)
        or isinstance(raw["pass_limit"], bool)
        or raw["pass_limit"] < 1
    ):
        raise ValueError("Polygon Replica package pass_limit is invalid")
    verification = raw["verification"]
    if not isinstance(verification, dict) or set(verification) != {"id", "source"}:
        raise ValueError("Polygon Replica package verification provenance is invalid")
    if not all(isinstance(verification[key], str) and verification[key] for key in verification):
        raise ValueError("Polygon Replica package verification provenance is invalid")
    try:
        canonical_verification_id(verification["id"])
    except RuntimeError as exc:
        raise ValueError("Polygon Replica package verification provenance is invalid") from exc
    solutions = raw["solutions"]
    if not isinstance(solutions, list) or not solutions:
        raise ValueError("Polygon Replica package must contain solutions")
    tests = raw["tests"]
    if not isinstance(tests, list) or not tests:
        raise ValueError("Polygon Replica package must contain tests")
    return cast(VerifiedRevisionManifest, raw)


def validate_manifest_files(
    package_root: Path,
    manifest: VerifiedRevisionManifest,
    *,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
) -> None:
    """Verify every declared payload and reject undeclared materialized files."""

    declared: set[str] = set()
    committed_solution_paths = list(solution_sources(package_root))
    manifest_solution_paths: list[str] = []
    build = load_build_config(package_root, problem_mode=manifest["mode"])
    accepted_source = build.get("accepted_solution_source")
    expected_behaviors = {
        source_path: (
            "accepted"
            if source_path == accepted_source
            else load_solution_desc(package_root, source_path)["expected_behavior"]
        )
        for source_path in committed_solution_paths
    }
    for solution in manifest["solutions"]:
        if not isinstance(solution, dict) or set(solution) != {
            "source_path",
            "expected_behavior",
            "verdicts",
        }:
            raise ValueError("verified revision solution entry has an unsupported shape")
        source_path = solution["source_path"]
        if not isinstance(source_path, str):
            raise ValueError("verified revision solution path is invalid")
        canonical_rel_path(source_path)
        manifest_solution_paths.append(source_path)
        expected_behavior = solution["expected_behavior"]
        try:
            normalized_behavior = normalize_expected_behavior(expected_behavior)
        except ValueError as exc:
            raise ValueError(
                f"verified revision solution behavior is invalid: {source_path}"
            ) from exc
        if expected_behaviors.get(source_path) != normalized_behavior:
            raise ValueError(
                f"verified revision solution behavior does not match source: {source_path}"
            )
        verdicts = solution["verdicts"]
        if not isinstance(verdicts, list) or not verdicts:
            raise ValueError(
                f"verified revision solution verdicts are incomplete: {source_path}"
            )
        if any(
            not isinstance(verdict, str)
            or verdict not in VERIFIED_SOLUTION_VERDICT_ORDER
            for verdict in verdicts
        ):
            raise ValueError(
                f"verified revision solution verdict is invalid: {source_path}"
            )
        canonical_verdicts = [
            verdict
            for verdict in VERIFIED_SOLUTION_VERDICT_ORDER
            if verdict in verdicts
        ]
        if verdicts != canonical_verdicts:
            raise ValueError(
                f"verified revision solution verdicts are not canonical: {source_path}"
            )
    if manifest_solution_paths != committed_solution_paths:
        raise ValueError(
            "verified revision solutions do not match committed solution sources"
        )
    test_ids: set[str] = set()
    spec = load_tests_spec(
        package_root / "tests" / "spec.json",
        document_max_bytes=tests_spec_max_bytes,
        sample_max_bytes=statement_sample_max_bytes,
    )
    manifest_shape: list[tuple[str, str, bool]] = []
    allowed_test_keys = {
        "id",
        "kind",
        "sample",
        "input",
        "answer",
        "sample_input",
        "sample_output",
    }
    for test in manifest["tests"]:
        if not isinstance(test, dict):
            raise ValueError("verified revision test entry must be an object")
        if not {"id", "kind", "sample", "input"}.issubset(test) or not set(
            test
        ).issubset(allowed_test_keys):
            raise ValueError("verified revision test entry has an unsupported shape")
        if not isinstance(test["id"], str) or not isinstance(test["kind"], str):
            raise ValueError("verified revision test identity is invalid")
        if not isinstance(test["sample"], bool):
            raise ValueError("verified revision test sample flag is invalid")
        test_id = test["id"]
        if not test_id or test_id in test_ids:
            raise ValueError("verified revision test IDs must be non-empty and unique")
        test_ids.add(test_id)
        manifest_shape.append((test_id, test["kind"], test["sample"]))
    spec_shape = [
        (str(test["id"]), str(test["kind"]), bool(test["sample"]))
        for test in spec
    ]
    if manifest_shape != spec_shape:
        raise ValueError("verified revision tests do not match tests/spec.json order")
    for test in manifest["tests"]:
        test_id = test["id"]
        if manifest["mode"] != "interactive" and "answer" not in test:
            raise ValueError(f"verified answer is required: {test_id}")
        for key in ("input", "answer", "sample_input", "sample_output"):
            descriptor = test.get(key)
            if descriptor is None:
                continue
            if not isinstance(descriptor, dict) or set(descriptor) != {
                "path",
                "sha256",
                "size",
            }:
                raise ValueError(f"verified payload descriptor is invalid: {test_id}/{key}")
            if not isinstance(descriptor["path"], str):
                raise ValueError(f"verified payload path is invalid: {test_id}/{key}")
            if not isinstance(descriptor["sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", descriptor["sha256"]
            ):
                raise ValueError(f"verified payload checksum is invalid: {test_id}/{key}")
            if (
                not isinstance(descriptor["size"], int)
                or isinstance(descriptor["size"], bool)
                or descriptor["size"] < 0
            ):
                raise ValueError(f"verified payload size is invalid: {test_id}/{key}")
            rel = canonical_rel_path(descriptor["path"])
            file_name = {
                "input": "input",
                "answer": "answer",
                "sample_input": "sample-input",
                "sample_output": "sample-output",
            }[key]
            expected_rel = f"{TEST_DATA_DIR.name}/tests/{test_id}/{file_name}"
            if rel != expected_rel:
                raise ValueError(f"verified payload path is not canonical: {rel}")
            if rel in declared:
                raise ValueError(f"verified revision declares a payload twice: {rel}")
            declared.add(rel)
            target = package_root / Path(*PurePosixPath(rel).parts)
            actual = describe_file(target, root=package_root)
            if (
                actual["sha256"] != descriptor["sha256"]
                or actual["size"] != descriptor["size"]
            ):
                raise ValueError(f"verified payload integrity check failed: {rel}")
        display_pairs: tuple[
            tuple[
                Literal["sample_input", "sample_output"],
                Literal["input", "answer"],
            ],
            ...,
        ] = (("sample_input", "input"), ("sample_output", "answer"))
        for display_key, judge_key in display_pairs:
            display = test.get(display_key)
            judge = test.get(judge_key)
            if display is not None and judge is not None and display["sha256"] == judge["sha256"]:
                raise ValueError(
                    "verified revision stores a redundant display override: "
                    f"{test_id}/{display_key}"
                )
    data_root = package_root / TEST_DATA_DIR
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for path in data_root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            actual_directories.add(path.relative_to(package_root).as_posix())
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Polygon Replica package test-data contains a non-regular entry")
        actual_files.add(path.relative_to(package_root).as_posix())
    expected_files = {*declared, f"{TEST_DATA_DIR.name}/manifest.json"}
    if actual_files != expected_files:
        raise ValueError(
            "Polygon Replica package test-data contains undeclared or missing payload files"
        )
    expected_directories = {f"{TEST_DATA_DIR.name}/tests"}
    expected_directories.update(
        f"{TEST_DATA_DIR.name}/tests/{test['id']}" for test in manifest["tests"]
    )
    if actual_directories != expected_directories:
        raise ValueError(
            "Polygon Replica package test-data contains undeclared or missing directories"
        )
    if source_digest(package_root) != manifest["source_digest"]:
        raise ValueError("Polygon Replica package source tree does not match its Git revision")
