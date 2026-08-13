import shlex
from pathlib import Path

from app.service.problem.test_spec import (
    TestSpecEntry,
    generator_source_paths,
    load_tests_spec,
    payload_rel_path_for_test,
    resolve_generator_source,
)
from app.service.problem.source_file import resolve_source


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

def prepare_tests_spec_runtime(
    snapshot: Path,
    tests_spec_entries: list[TestSpecEntry],
    *,
    parse_gen_command_tokens_fn,
) -> tuple[list[dict], list[tuple[str, Path]]]:
    runtime_entries: list[dict] = []
    generator_targets: list[tuple[str, Path]] = []
    by_source_rel: dict[str, str] = {}
    try:
        generator_sources = tuple(generator_source_paths(snapshot))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
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
        try:
            tokens = parse_gen_command_tokens_fn(command)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            source_rel = resolve_generator_source(tokens[0], generator_sources)
            source_path = resolve_source(snapshot, source_rel)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
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
                "command_payload": " ".join(
                    [
                        '"$SUBMISSION_BIN"',
                        *[shlex.quote(item) for item in tokens[1:]],
                    ]
                ),
                "source_rel": source_rel,
                "payload_rel": payload_rel,
                "target_name": compiled,
            }
        )
    return runtime_entries, generator_targets
