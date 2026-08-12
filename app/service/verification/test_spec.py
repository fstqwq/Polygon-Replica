from __future__ import annotations

from pathlib import Path
import os

from app.service.problem.test_spec import (
    TestSpecEntry,
    load_tests_spec,
    payload_rel_path_for_test,
    resolve_configured_generator_source,
)
from app.service.verification.source import resolve_source


def load_tests_spec_entries(
    snapshot: Path,
    *,
    document_max_bytes: int,
    sample_max_bytes: int,
) -> list[TestSpecEntry]:
    spec_path = snapshot / "tests" / "spec.json"
    try:
        return load_tests_spec(
            spec_path,
            document_max_bytes=document_max_bytes,
            sample_max_bytes=sample_max_bytes,
        )
    except ValueError as exc:
        raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc


def manual_test_sources(snapshot: Path) -> list[Path]:
    manual_root = snapshot / "tests" / "manual"
    if not manual_root.exists():
        return []
    try:
        manual_root_resolved = manual_root.resolve()
    except OSError:
        return []

    def _is_in_name(name: str) -> bool:
        return Path(name).suffix.lower() == ".in"

    def _collect_safe_entries(
        dir_root: Path,
        names: list[str],
        rel_prefix: str,
    ) -> list[tuple[str, Path, bool]]:
        safe_entries: list[tuple[str, Path, bool]] = []
        for name in names:
            p = dir_root / name
            if p.is_symlink() or not p.exists() or not p.is_file():
                continue
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            safe_entries.append((rel, p, _is_in_name(name)))
        return safe_entries

    in_files: list[tuple[str, Path]] = []
    all_files: list[tuple[str, Path]] | None = []
    for dirpath, dirnames, filenames in os.walk(manual_root, topdown=True, followlinks=False):
        dir_root = Path(dirpath)
        try:
            dir_root_resolved = dir_root.resolve()
        except OSError:
            dirnames[:] = []
            continue
        if manual_root_resolved not in dir_root_resolved.parents and manual_root_resolved != dir_root_resolved:
            dirnames[:] = []
            continue
        try:
            rel_root = dir_root.relative_to(manual_root)
        except ValueError:
            dirnames[:] = []
            continue
        rel_prefix = "" if rel_root == Path(".") else rel_root.as_posix()
        keep_dirs: list[str] = []
        for name in dirnames:
            d = dir_root / name
            if d.is_symlink():
                continue
            keep_dirs.append(name)
        dirnames[:] = sorted(keep_dirs)

        in_candidates = [name for name in filenames if _is_in_name(name)]
        has_in_file = False
        if in_candidates:
            safe_entries = _collect_safe_entries(dir_root, in_candidates, rel_prefix)
            has_in_file = bool(safe_entries)
            if not has_in_file and all_files is not None:
                safe_entries = _collect_safe_entries(dir_root, filenames, rel_prefix)
                has_in_file = any(is_in for _, _, is_in in safe_entries)
        elif all_files is None:
            safe_entries = []
        else:
            safe_entries = _collect_safe_entries(dir_root, filenames, rel_prefix)

        if has_in_file:
            if all_files is not None:
                all_files.clear()
                all_files = None
            for rel, p, is_in in safe_entries:
                if is_in:
                    in_files.append((rel, p))
        elif all_files is not None:
            for rel, p, _ in safe_entries:
                all_files.append((rel, p))

    if in_files:
        return [p for _, p in sorted(in_files)]
    return []


def tests_spec_payload_text(
    snapshot: Path,
    row: TestSpecEntry,
    index: int,
) -> tuple[str, str]:
    test_id = row["id"]
    kind = row["kind"]
    rel = payload_rel_path_for_test(test_id, kind)
    payload_path = snapshot / rel
    try:
        if payload_path.exists() and payload_path.is_file() and not payload_path.is_symlink():
            return rel, payload_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read tests payload for id {test_id}: {exc}") from exc
    raise RuntimeError(f"missing tests payload file for id {test_id}: {rel}")

def generator_source_catalog(
    snapshot: Path,
    generator_sources: list[str],
    generator_source_extensions: tuple[str, ...],
) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for relative in generator_sources:
        source = resolve_source(snapshot, relative)
        if source.suffix.lower() not in generator_source_extensions:
            raise RuntimeError(
                f"unsupported generator source language: {relative}"
            )
        rows.append((relative, source))
    return rows


def prepare_tests_spec_runtime(
    snapshot: Path,
    tests_spec_entries: list[TestSpecEntry],
    *,
    generator_sources: list[str],
    generator_source_extensions: tuple[str, ...],
    parse_gen_command_tokens_fn,
) -> tuple[list[dict], list[tuple[str, Path]]]:
    runtime_entries: list[dict] = []
    generator_targets: list[tuple[str, Path]] = []
    by_source_rel: dict[str, str] = {}
    generator_catalog = generator_source_catalog(
        snapshot,
        generator_sources,
        generator_source_extensions,
    )
    generator_path_by_source = dict(generator_catalog)
    for index, row in enumerate(tests_spec_entries, start=1):
        kind = row["kind"]
        test_id = row["id"]
        sample = row["sample"]
        sample_input = row.get("sample_input", "")
        sample_output = row.get("sample_output", "")
        sample_output_validate = row.get("sample_output_validate", True)
        payload_rel, payload = tests_spec_payload_text(snapshot, row, index)
        if kind == "manual":
            runtime_entries.append(
                {
                    "index": index,
                    "id": test_id,
                    "kind": "manual",
                    "sample": sample,
                    "sample_input": sample_input,
                    "sample_output": sample_output,
                    "sample_output_validate": sample_output_validate,
                    "source_rel": payload_rel,
                    "input": payload,
                }
            )
            continue
        if kind != "gen":
            raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}")
        command = str(payload or "").strip()
        tokens = parse_gen_command_tokens_fn(command)
        try:
            source_rel = resolve_configured_generator_source(
                tokens[0],
                list(generator_path_by_source),
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        source_path = generator_path_by_source[source_rel]
        compiled = by_source_rel.get(source_rel)
        if compiled is None:
            gen_index = len(by_source_rel) + 1
            target_name = f"generator_spec_{gen_index}"
            by_source_rel[source_rel] = target_name
            generator_targets.append((target_name, source_path))
            compiled = target_name
        runtime_entries.append(
            {
                "index": index,
                "id": test_id,
                "kind": "gen",
                "sample": sample,
                "sample_input": sample_input,
                "sample_output": sample_output,
                "sample_output_validate": sample_output_validate,
                "cmd": command,
                "args": [str(x) for x in tokens[1:]],
                "source_rel": source_rel,
                "payload_rel": payload_rel,
                "target_name": compiled,
            }
        )
    return runtime_entries, generator_targets
