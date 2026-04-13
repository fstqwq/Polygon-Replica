from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.main_util import enforce_textarea_max_bytes
from app.runtime_value import RuntimeValues, build_runtime_values

TESTS_SPEC_MAX_ITEMS: int = 4096
TESTS_SPEC_GEN_COMMAND_MAX_CHARS: int = 1024
TESTS_SPEC_ID_RE = None


def apply_runtime_values(values: RuntimeValues) -> None:
    global TESTS_SPEC_MAX_ITEMS
    global TESTS_SPEC_GEN_COMMAND_MAX_CHARS
    global TESTS_SPEC_ID_RE
    TESTS_SPEC_MAX_ITEMS = int(values.TESTS_SPEC_MAX_ITEMS)
    TESTS_SPEC_GEN_COMMAND_MAX_CHARS = int(values.TESTS_SPEC_GEN_COMMAND_MAX_CHARS)
    TESTS_SPEC_ID_RE = values.TESTS_SPEC_ID_RE

apply_runtime_values(build_runtime_values())

TESTS_SPEC_REL = Path("tests/spec.json")
TESTS_SPEC_MANUAL_DIR_REL = Path("tests/manual")
TESTS_SPEC_GENERATOR_DIR_REL = Path("tests/generator")


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def normalize_test_id(raw: object) -> str:
    value = str(raw or "").strip()
    if not TESTS_SPEC_ID_RE.fullmatch(value):
        raise ValueError("test id must be 3-12 digits (example: 001)")
    return value


def next_test_id(entries: list[dict]) -> str:
    max_value = 0
    for row in entries:
        raw_id = row.get("id")
        if not isinstance(raw_id, str):
            continue
        raw = raw_id.strip()
        if not raw.isdigit():
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > max_value:
            max_value = value
    width = max(3, len(str(max_value + 1)))
    return str(max_value + 1).zfill(width)


def spec_data_filename(test_id: str) -> str:
    return f"{normalize_test_id(test_id)}.in"


def normalize_test_kind(raw: object) -> str:
    kind = str(raw or "").strip().lower()
    if kind not in {"manual", "gen"}:
        raise ValueError("test kind must be manual or gen")
    return kind


def payload_dir_rel_for_kind(kind: str) -> Path:
    safe_kind = normalize_test_kind(kind)
    return TESTS_SPEC_MANUAL_DIR_REL if safe_kind == "manual" else TESTS_SPEC_GENERATOR_DIR_REL


def payload_rel_path_for_test(test_id: str, kind: str) -> str:
    return f"{payload_dir_rel_for_kind(kind).as_posix()}/{spec_data_filename(test_id)}"


def normalize_file_manual_input(raw: object) -> str:
    return _normalize_manual_input_text(raw)


def normalize_manual_input(raw: object) -> str:
    normalized = normalize_file_manual_input(raw)
    return enforce_textarea_max_bytes(normalized, label="manual test input")


def _normalize_manual_input_text(raw: object) -> str:
    value = _normalize_newlines(str(raw or ""))
    # Canonical manual input format:
    # - LF newlines only
    # - no trailing spaces/tabs on each line
    # - exactly one trailing newline at EOF
    lines = value.split("\n")
    stripped_lines = [line.rstrip(" \t") for line in lines]
    return "\n".join(stripped_lines).rstrip("\n") + "\n"


def parse_gen_command_tokens(command: str) -> list[str]:
    text = str(command or "").strip()
    if not text:
        raise ValueError("generator command is required")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid generator command: {exc}") from exc
    if not tokens:
        raise ValueError("generator command is required")
    return tokens


def normalize_gen_command(raw: object) -> str:
    value = str(raw or "").strip()
    if len(value) > TESTS_SPEC_GEN_COMMAND_MAX_CHARS:
        raise ValueError("generator command is too long")
    parse_gen_command_tokens(value)
    return value


def _normalize_sample_flag(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _normalize_sample_output_validate_flag(raw: object) -> bool:
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if not text:
        return True
    return text in {"1", "true", "yes", "on"}


def normalize_sample_input(raw: object) -> str:
    value = _normalize_newlines(str(raw or ""))
    return enforce_textarea_max_bytes(value, label="sample input")


def normalize_sample_output(raw: object) -> str:
    value = _normalize_newlines(str(raw or ""))
    return enforce_textarea_max_bytes(value, label="sample output")


def normalize_tests_spec_entry(raw: object, *, index: int = 0) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"tests[{index}] must be an object")
    raw_kind = raw.get("kind")
    if not isinstance(raw_kind, str):
        raise ValueError(f"tests[{index}] kind must be manual or gen")
    kind = raw_kind.strip().lower()
    if kind not in {"manual", "gen"}:
        raise ValueError(f"tests[{index}] kind must be manual or gen")
    sample = _normalize_sample_flag(raw.get("sample", False))
    sample_input = normalize_sample_input(raw.get("sample_input", ""))
    sample_output = normalize_sample_output(raw.get("sample_output", ""))
    sample_output_validate = _normalize_sample_output_validate_flag(
        raw.get("sample_output_validate", True)
    )
    if not isinstance(raw_id_obj := raw.get("id"), str):
        raise ValueError(f"tests[{index}] id is required")
    raw_id = raw_id_obj.strip()
    if not raw_id:
        raise ValueError(f"tests[{index}] id is required")
    return {
        "id": normalize_test_id(raw_id),
        "kind": kind,
        "sample": sample,
        "sample_input": sample_input,
        "sample_output": sample_output,
        "sample_output_validate": sample_output_validate,
    }


def normalize_tests_spec_entries(raw: object) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tests must be an array")
    entries: list[dict] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw, start=1):
        row = normalize_tests_spec_entry(item, index=idx)
        row_id = row["id"]
        if row_id in seen_ids:
            raise ValueError(f"tests[{idx}] duplicated id: {row_id}")
        seen_ids.add(row_id)
        entries.append(row)
        if len(entries) > TESTS_SPEC_MAX_ITEMS:
            raise ValueError("too many tests in tests/spec.json")
    return entries


def loads_tests_spec(text: str) -> list[dict]:
    raw_text = str(text or "").strip()
    if not raw_text:
        return []
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if isinstance(payload, list):
        return normalize_tests_spec_entries(payload)
    if not isinstance(payload, dict):
        raise ValueError("tests/spec.json must be an object")
    tests = payload.get("tests")
    if tests is None:
        return []
    return normalize_tests_spec_entries(tests)


def load_tests_spec(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError("tests/spec.json is invalid")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read tests/spec.json: {exc}") from exc
    return loads_tests_spec(text)


def dumps_tests_spec(entries: list[dict]) -> str:
    normalized = normalize_tests_spec_entries(entries)
    dumped_tests: list[dict] = []
    for idx, row in enumerate(normalized, start=1):
        row_payload: dict[str, object] = {
            "id": row.get("id"),
            "kind": row.get("kind"),
            "sample": row.get("sample", False),
        }
        sample_input = normalize_sample_input(row.get("sample_input", ""))
        sample_output = normalize_sample_output(row.get("sample_output", ""))
        sample_output_validate = _normalize_sample_output_validate_flag(
            row.get("sample_output_validate", True)
        )
        if sample_input:
            row_payload["sample_input"] = sample_input
        if sample_output:
            row_payload["sample_output"] = sample_output
        if not sample_output_validate:
            row_payload["sample_output_validate"] = False
        normalized_row = normalize_tests_spec_entry(row_payload, index=idx)
        dumped_row: dict[str, object] = {
            "id": normalized_row["id"],
            "kind": normalized_row["kind"],
            "sample": normalized_row["sample"],
        }
        normalized_sample_input = normalized_row["sample_input"]
        normalized_sample_output = normalized_row["sample_output"]
        normalized_sample_validate = _normalize_sample_output_validate_flag(
            normalized_row.get("sample_output_validate", True)
        )
        if normalized_sample_input:
            dumped_row["sample_input"] = normalized_sample_input
        if normalized_sample_output:
            dumped_row["sample_output"] = normalized_sample_output
        if not normalized_sample_validate:
            dumped_row["sample_output_validate"] = False
        dumped_tests.append(dumped_row)
    payload = {"tests": dumped_tests}
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def summarize_tests_spec(entries: list[dict]) -> dict:
    manual = 0
    gen = 0
    sample = 0
    for row in entries:
        if row["kind"] == "manual":
            manual += 1
        elif row["kind"] == "gen":
            gen += 1
        if row["sample"]:
            sample += 1
    return {
        "total": len(entries),
        "manual": manual,
        "gen": gen,
        "sample": sample,
    }
