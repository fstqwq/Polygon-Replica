from __future__ import annotations

from pathlib import Path
import os

from app.service.problem.test_spec import load_tests_spec, payload_rel_path_for_test


def load_tests_spec_entries(snapshot: Path) -> list[dict] | None:
    spec_path = snapshot / "tests" / "spec.json"
    if not spec_path.exists():
        return None
    try:
        return load_tests_spec(spec_path)
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


def tests_spec_payload_text(snapshot: Path, row: dict, index: int) -> tuple[str, str]:
    test_id = row["id"].strip()
    if not test_id:
        raise RuntimeError(f"tests/spec.json entry {index} missing id")
    kind = row["kind"].strip().lower()
    if kind not in {"manual", "gen"}:
        raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}")
    rel = payload_rel_path_for_test(test_id, kind)
    payload_path = snapshot / rel
    try:
        if payload_path.exists() and payload_path.is_file() and not payload_path.is_symlink():
            return rel, payload_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read tests payload for id {test_id}: {exc}") from exc
    raise RuntimeError(f"missing tests payload file for id {test_id}: {rel}")


def tests_spec_answer_source(snapshot: Path, test_id: str) -> Path | None:
    safe_test_id = test_id.strip()
    if not safe_test_id:
        return None
    candidate = snapshot / "tests" / "answers" / f"{safe_test_id}.ans"
    try:
        resolved_snapshot = snapshot.resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved_snapshot not in resolved.parents:
        return None
    try:
        if resolved.is_symlink() or (not resolved.exists()) or (not resolved.is_file()):
            return None
    except OSError:
        return None
    return resolved


def generator_source_catalog(snapshot: Path, generator_source_extensions: tuple[str, ...]) -> list[tuple[str, Path]]:
    generators_root = snapshot / "generators"
    try:
        if not generators_root.exists() or not generators_root.is_dir() or generators_root.is_symlink():
            return []
    except OSError:
        return []
    try:
        generators_root_resolved = generators_root.resolve()
    except OSError:
        return []
    rows: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(generators_root, topdown=True, followlinks=False):
        dir_root = Path(dirpath)
        try:
            dir_root_resolved = dir_root.resolve()
        except OSError:
            dirnames[:] = []
            continue
        if generators_root_resolved not in dir_root_resolved.parents and generators_root_resolved != dir_root_resolved:
            dirnames[:] = []
            continue
        safe_dirs: list[str] = []
        for name in dirnames:
            p = dir_root / name
            try:
                if p.is_symlink() or not p.exists() or not p.is_dir():
                    continue
            except OSError:
                continue
            safe_dirs.append(name)
        dirnames[:] = sorted(safe_dirs)
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in generator_source_extensions:
                continue
            p = dir_root / name
            try:
                if p.is_symlink() or not p.exists() or not p.is_file():
                    continue
                rel = str(p.relative_to(snapshot)).replace("\\", "/")
            except (OSError, ValueError):
                continue
            rows.append((rel, p))
    rows.sort(key=lambda item: item[0])
    return rows


def resolve_generator_source_from_token(
    token: str,
    generator_catalog: list[tuple[str, Path]],
    generator_source_extensions: tuple[str, ...],
) -> tuple[str, Path]:
    raw = str(token or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        raise RuntimeError("generator command is empty")
    if any(part == ".." for part in raw.split("/")):
        raise RuntimeError(f"invalid generator command '{token}'")
    by_rel = {rel: path for rel, path in generator_catalog}
    candidates: list[str] = []
    token_path = Path(raw)
    suffix = token_path.suffix.lower()
    if raw.startswith("generators/"):
        if suffix in generator_source_extensions:
            candidates.append(raw)
        else:
            for ext in generator_source_extensions:
                candidates.append(f"{raw}{ext}")
    else:
        if suffix in generator_source_extensions:
            candidates.append(f"generators/{raw}")
        else:
            candidates.append(f"generators/{raw}")
            for ext in generator_source_extensions:
                candidates.append(f"generators/{raw}{ext}")
    seen: set[str] = set()
    for rel in candidates:
        rel_key = str(rel or "").strip()
        if not rel_key or rel_key in seen:
            continue
        seen.add(rel_key)
        hit = by_rel.get(rel_key)
        if hit is not None:
            return rel_key, hit
    name = token_path.name
    if suffix in generator_source_extensions:
        exact = [(rel, p) for rel, p in generator_catalog if Path(rel).name == name]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise RuntimeError(f"ambiguous generator source for command '{token}'")
    else:
        stem = token_path.name
        stem_matches = [(rel, p) for rel, p in generator_catalog if Path(rel).stem == stem]
        if len(stem_matches) == 1:
            return stem_matches[0]
        if len(stem_matches) > 1:
            raise RuntimeError(f"ambiguous generator source for command '{token}'")
    raise RuntimeError(f"cannot resolve generator source for command '{token}'")


def prepare_tests_spec_runtime(
    snapshot: Path,
    tests_spec_entries: list[dict],
    bin_dir: Path,
    *,
    generator_source_extensions: tuple[str, ...],
    parse_gen_command_tokens_fn,
) -> tuple[list[dict], list[tuple[str, Path, Path]]]:
    runtime_entries: list[dict] = []
    generator_targets: list[tuple[str, Path, Path]] = []
    by_source_rel: dict[str, tuple[str, Path]] = {}
    generator_catalog = generator_source_catalog(snapshot, generator_source_extensions)
    for index, row in enumerate(tests_spec_entries, start=1):
        kind = row["kind"].strip()
        test_id = row["id"].strip()
        sample = bool(row["sample"])
        sample_input = row["sample_input"]
        sample_output = row["sample_output"]
        sample_output_validate = bool(row.get("sample_output_validate", True))
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
        source_rel, source_path = resolve_generator_source_from_token(tokens[0], generator_catalog, generator_source_extensions)
        compiled = by_source_rel.get(source_rel)
        if compiled is None:
            gen_index = len(by_source_rel) + 1
            target_name = f"generator_spec_{gen_index}"
            target_bin = bin_dir / target_name
            by_source_rel[source_rel] = (target_name, target_bin)
            generator_targets.append((target_name, source_path, target_bin))
            compiled = (target_name, target_bin)
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
                "target_name": compiled[0],
            }
        )
    return runtime_entries, generator_targets


