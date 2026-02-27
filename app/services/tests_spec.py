from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.runtime_values import RuntimeValues, build_runtime_values

TESTS_SPEC_VERSION: int = 2
TESTS_SPEC_MAX_ITEMS: int = 4096
TESTS_SPEC_MANUAL_MAX_CHARS: int = 262144
TESTS_SPEC_GEN_COMMAND_MAX_CHARS: int = 1024
TESTS_SPEC_ID_RE = None


def _apply_runtime_values(values: RuntimeValues) -> None:
    global TESTS_SPEC_VERSION
    global TESTS_SPEC_MAX_ITEMS
    global TESTS_SPEC_MANUAL_MAX_CHARS
    global TESTS_SPEC_GEN_COMMAND_MAX_CHARS
    global TESTS_SPEC_ID_RE
    TESTS_SPEC_VERSION = int(values.TESTS_SPEC_VERSION)
    TESTS_SPEC_MAX_ITEMS = int(values.TESTS_SPEC_MAX_ITEMS)
    TESTS_SPEC_MANUAL_MAX_CHARS = int(values.TESTS_SPEC_MANUAL_MAX_CHARS)
    TESTS_SPEC_GEN_COMMAND_MAX_CHARS = int(values.TESTS_SPEC_GEN_COMMAND_MAX_CHARS)
    TESTS_SPEC_ID_RE = values.TESTS_SPEC_ID_RE


def configure_runtime_values(values: RuntimeValues) -> None:
    _apply_runtime_values(values)


_apply_runtime_values(build_runtime_values())

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
        raw = str(row.get("id") or "").strip()
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


def normalize_manual_input(raw: object) -> str:
    value = _normalize_newlines(str(raw or ""))
    # Canonical manual input format:
    # - LF newlines only
    # - no trailing spaces/tabs on each line
    # - exactly one trailing newline at EOF
    lines = value.split("\n")
    stripped_lines = [line.rstrip(" \t") for line in lines]
    normalized = "\n".join(stripped_lines).rstrip("\n") + "\n"
    if len(normalized) > TESTS_SPEC_MANUAL_MAX_CHARS:
        raise ValueError("manual test input is too long")
    return normalized


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


def normalize_tests_spec_entry(raw: object, *, index: int = 0) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"tests[{index}] must be an object")
    kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
    if kind not in {"manual", "gen"}:
        raise ValueError(f"tests[{index}] kind must be manual or gen")
    sample = _normalize_sample_flag(raw.get("sample", False))
    raw_id = str(raw.get("id") or "").strip()
    if not raw_id:
        raise ValueError(f"tests[{index}] id is required")
    return {
        "id": normalize_test_id(raw_id),
        "kind": kind,
        "sample": sample,
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
        row_id = str(row.get("id") or "")
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
        dumped_tests.append(
            normalize_tests_spec_entry(
                {
                    "id": row.get("id"),
                    "kind": row.get("kind"),
                    "sample": row.get("sample", False),
                },
                index=idx,
            )
        )
    payload = {
        "version": TESTS_SPEC_VERSION,
        "tests": dumped_tests,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def summarize_tests_spec(entries: list[dict]) -> dict:
    manual = 0
    gen = 0
    sample = 0
    for row in entries:
        if str(row.get("kind") or "") == "manual":
            manual += 1
        elif str(row.get("kind") or "") == "gen":
            gen += 1
        if bool(row.get("sample")):
            sample += 1
    return {
        "total": len(entries),
        "manual": manual,
        "gen": gen,
        "sample": sample,
    }
