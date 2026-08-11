"""Canonical Native package manifest creation and validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypedDict, cast

from app.service.platform.hashing import sha256_file
from app.service.problem.test_spec import load_tests_spec
from app.service.verification.identity import canonical_verification_id


class NativeFileEntry(TypedDict):
    path: str
    sha256: str
    size: int


class NativeTestEntry(TypedDict):
    id: str
    kind: str
    sample: bool
    input: NativeFileEntry
    answer: NotRequired[NativeFileEntry]
    sample_input: NotRequired[NativeFileEntry]
    sample_output: NotRequired[NativeFileEntry]


class NativeManifest(TypedDict):
    source_commit: str
    revision_number: int
    source_digest: str
    mode: str
    pass_limit: int
    verification: dict[str, str]
    tests: list[NativeTestEntry]


def canonical_rel_path(raw: str) -> str:
    """Return a safe canonical package-relative POSIX path."""

    if "\\" in raw:
        raise ValueError(f"invalid Native package path: {raw}")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        raise ValueError(f"invalid Native package path: {raw}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid Native package path: {raw}")
    if path.as_posix() != raw:
        raise ValueError(f"non-canonical Native package path: {raw}")
    return path.as_posix()


def describe_file(path: Path, *, root: Path) -> NativeFileEntry:
    resolved_root = root.resolve()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Native payload is not a regular file: {path}")
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"Native payload escapes package root: {path}")
    return {
        "path": resolved.relative_to(resolved_root).as_posix(),
        "sha256": sha256_file(resolved),
        "size": int(resolved.stat().st_size),
    }


def source_digest(source_root: Path) -> str:
    """Hash the exact committed source tree, excluding only ``test_data``."""

    root = source_root.resolve()
    entries: list[dict[str, object]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        parent = Path(dirpath)
        rel_parent = parent.relative_to(root)
        if not rel_parent.parts:
            dirnames[:] = [name for name in sorted(dirnames) if name != "test_data"]
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
            if rel.parts and rel.parts[0] == "test_data":
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


def dumps_manifest(manifest: NativeManifest) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_manifest(path: Path) -> NativeManifest:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Native package manifest is missing")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Native package manifest must be an object")
    required = {
        "source_commit",
        "revision_number",
        "source_digest",
        "mode",
        "pass_limit",
        "verification",
        "tests",
    }
    if set(raw) != required:
        raise ValueError("Native package manifest has an unsupported shape")
    if not isinstance(raw["mode"], str) or raw["mode"] not in {
        "pass-fail",
        "interactive",
    }:
        raise ValueError("Native package mode is invalid")
    if not isinstance(raw["source_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", raw["source_commit"]
    ):
        raise ValueError("Native package source_commit is invalid")
    if (
        not isinstance(raw["revision_number"], int)
        or isinstance(raw["revision_number"], bool)
        or raw["revision_number"] < 1
    ):
        raise ValueError("Native package revision_number is invalid")
    if not isinstance(raw["source_digest"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", raw["source_digest"]
    ):
        raise ValueError("Native package source_digest is invalid")
    if (
        not isinstance(raw["pass_limit"], int)
        or isinstance(raw["pass_limit"], bool)
        or raw["pass_limit"] < 1
    ):
        raise ValueError("Native package pass_limit is invalid")
    verification = raw["verification"]
    if not isinstance(verification, dict) or set(verification) != {"id", "source"}:
        raise ValueError("Native package verification provenance is invalid")
    if not all(isinstance(verification[key], str) and verification[key] for key in verification):
        raise ValueError("Native package verification provenance is invalid")
    try:
        canonical_verification_id(verification["id"])
    except RuntimeError as exc:
        raise ValueError("Native package verification provenance is invalid") from exc
    tests = raw["tests"]
    if not isinstance(tests, list) or not tests:
        raise ValueError("Native package must contain tests")
    return cast(NativeManifest, raw)


def validate_manifest_files(
    package_root: Path,
    manifest: NativeManifest,
    *,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
) -> None:
    """Verify every declared payload and reject undeclared materialized files."""

    declared: set[str] = set()
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
            raise ValueError("Native manifest test entry must be an object")
        if not {"id", "kind", "sample", "input"}.issubset(test) or not set(
            test
        ).issubset(allowed_test_keys):
            raise ValueError("Native manifest test entry has an unsupported shape")
        if not isinstance(test["id"], str) or not isinstance(test["kind"], str):
            raise ValueError("Native manifest test identity is invalid")
        if not isinstance(test["sample"], bool):
            raise ValueError("Native manifest test sample flag is invalid")
        test_id = test["id"]
        if not test_id or test_id in test_ids:
            raise ValueError("Native manifest test IDs must be non-empty and unique")
        test_ids.add(test_id)
        manifest_shape.append((test_id, test["kind"], test["sample"]))
    spec_shape = [
        (str(test["id"]), str(test["kind"]), bool(test["sample"]))
        for test in spec
    ]
    if manifest_shape != spec_shape:
        raise ValueError("Native manifest tests do not match tests/spec.json order")
    for test in manifest["tests"]:
        test_id = test["id"]
        if manifest["mode"] != "interactive" and "answer" not in test:
            raise ValueError(f"Native manifest answer is required: {test_id}")
        for key in ("input", "answer", "sample_input", "sample_output"):
            descriptor = test.get(key)
            if descriptor is None:
                continue
            if not isinstance(descriptor, dict) or set(descriptor) != {
                "path",
                "sha256",
                "size",
            }:
                raise ValueError(f"Native payload descriptor is invalid: {test_id}/{key}")
            if not isinstance(descriptor["path"], str):
                raise ValueError(f"Native payload path is invalid: {test_id}/{key}")
            if not isinstance(descriptor["sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", descriptor["sha256"]
            ):
                raise ValueError(f"Native payload checksum is invalid: {test_id}/{key}")
            if (
                not isinstance(descriptor["size"], int)
                or isinstance(descriptor["size"], bool)
                or descriptor["size"] < 0
            ):
                raise ValueError(f"Native payload size is invalid: {test_id}/{key}")
            rel = canonical_rel_path(descriptor["path"])
            file_name = {
                "input": "input",
                "answer": "answer",
                "sample_input": "sample-input",
                "sample_output": "sample-output",
            }[key]
            expected_rel = f"test_data/tests/{test_id}/{file_name}"
            if rel != expected_rel:
                raise ValueError(f"Native manifest payload path is not canonical: {rel}")
            if rel in declared:
                raise ValueError(f"Native manifest declares a payload twice: {rel}")
            declared.add(rel)
            target = package_root / Path(*PurePosixPath(rel).parts)
            actual = describe_file(target, root=package_root)
            if (
                actual["sha256"] != descriptor["sha256"]
                or actual["size"] != descriptor["size"]
            ):
                raise ValueError(f"Native payload integrity check failed: {rel}")
        for display_key, judge_key in (("sample_input", "input"), ("sample_output", "answer")):
            display = test.get(display_key)
            judge = test.get(judge_key)
            if display is not None and judge is not None and display["sha256"] == judge["sha256"]:
                raise ValueError(f"Native manifest stores a redundant display override: {test_id}/{display_key}")
    data_root = package_root / "test_data" / "tests"
    actual_files = {
        path.relative_to(package_root).as_posix()
        for path in data_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_files != declared:
        raise ValueError("Native test_data contains undeclared or missing payload files")
    if source_digest(package_root) != manifest["source_digest"]:
        raise ValueError("Native source tree digest does not match its Git revision")
