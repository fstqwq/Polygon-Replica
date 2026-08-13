import json
import shlex
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict

from app.main_constant import (
    SOLUTION_SOURCE_EXTENSIONS,
    TESTS_SPEC_GEN_COMMAND_MAX_CHARS,
    TESTS_SPEC_ID_RE,
    TESTS_SPEC_MAX_ITEMS,
)
from app.main_util import enforce_textarea_max_bytes
from app.service.problem.json_codec import (
    loads_object,
    reject_unknown_keys,
    require_keys,
)
from app.service.problem.source_file import require_regular_source_file

TESTS_SPEC_REL = Path("tests/spec.json")
TESTS_SPEC_MANUAL_DIR_REL = Path("tests/manual")
TESTS_SPEC_GENERATOR_DIR_REL = Path("tests/generator")
_TESTS_SPEC_KEYS = frozenset({"tests"})
_TEST_ENTRY_KEYS = frozenset(
    {
        "id",
        "kind",
        "sample",
        "sample_input",
        "sample_output",
        "sample_output_validate",
    }
)
_TEST_ENTRY_REQUIRED_KEYS = frozenset({"id", "kind"})


TestKind = Literal["manual", "gen"]


class TestSpecEntry(TypedDict):
    id: str
    kind: TestKind
    sample: bool
    sample_input: str
    sample_output: str
    sample_output_validate: bool


def dumps_default_tests_spec() -> str:
    """Construct the canonical source written for a newly-created problem."""

    return json.dumps({"tests": []}, ensure_ascii=False, indent=2) + "\n"


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def normalize_test_id(raw: object) -> str:
    value = str(raw or "").strip()
    if not TESTS_SPEC_ID_RE.fullmatch(value):
        raise ValueError("test id must be 3-12 digits (example: 001)")
    return value


def next_test_id(entries: list[TestSpecEntry]) -> str:
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
    return (
        TESTS_SPEC_MANUAL_DIR_REL
        if safe_kind == "manual"
        else TESTS_SPEC_GENERATOR_DIR_REL
    )


def payload_rel_path_for_test(test_id: str, kind: str) -> str:
    return (
        f"{payload_dir_rel_for_kind(kind).as_posix()}/"
        f"{spec_data_filename(test_id)}"
    )


def normalize_file_manual_input(raw: object) -> str:
    return _normalize_manual_input_text(raw)


def normalize_manual_input(raw: object, *, max_bytes: int) -> str:
    normalized = normalize_file_manual_input(raw)
    return enforce_textarea_max_bytes(
        normalized,
        label="manual test input",
        max_bytes=max_bytes,
    )


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


def generator_source_paths(root: Path) -> list[str]:
    directory = root / "generators"
    if directory.is_symlink():
        raise ValueError("generators: must not be a symbolic link")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError("generators: must be a directory")
    sources: list[str] = []
    try:
        for path in directory.rglob("*"):
            if path.suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
                continue
            relative = path.relative_to(root).as_posix()
            require_regular_source_file(root, relative)
            sources.append(relative)
    except OSError as exc:
        raise ValueError(f"generators: cannot list directory: {exc}") from exc
    return sorted(sources)


def resolve_generator_source(token: str, source_paths: tuple[str, ...]) -> str:
    raw = token.replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise ValueError(f"invalid generator command source '{token}'")
    token_path = PurePosixPath(raw)
    matches: list[str] = []
    for source in source_paths:
        source_path = PurePosixPath(source)
        without_root = source.removeprefix("generators/")
        suffix_length = len(source_path.suffix)
        source_without_suffix = source[:-suffix_length] if suffix_length else source
        without_root_suffix = (
            without_root[:-suffix_length] if suffix_length else without_root
        )
        if raw in {
            source,
            without_root,
            source_without_suffix,
            without_root_suffix,
        }:
            matches.append(source)
            continue
        if "/" not in raw and (
            token_path.name == source_path.name
            or (not token_path.suffix and token_path.name == source_path.stem)
        ):
            matches.append(source)
    unique = list(dict.fromkeys(matches))
    if not unique:
        raise ValueError(f"generator source is not selected: {token}")
    if len(unique) > 1:
        raise ValueError(f"generator source is ambiguous: {token}")
    return unique[0]


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


def normalize_sample_input(raw: object, *, max_bytes: int) -> str:
    value = _normalize_newlines(str(raw or ""))
    return enforce_textarea_max_bytes(
        value,
        label="sample input",
        max_bytes=max_bytes,
    )


def normalize_sample_output(raw: object, *, max_bytes: int) -> str:
    value = _normalize_newlines(str(raw or ""))
    return enforce_textarea_max_bytes(
        value,
        label="sample output",
        max_bytes=max_bytes,
    )


def read_statement_sample_text(path: Path, *, max_bytes: int) -> str:
    """Read one generated sample payload without exceeding its remaining budget."""

    cap = int(max_bytes)
    if cap < 0:
        raise ValueError("statement sample exceeds byte limit")
    if path.is_symlink() or not path.is_file():
        raise ValueError("statement sample payload is not a regular file")
    try:
        if path.stat().st_size > cap:
            raise ValueError("statement sample exceeds byte limit")
        with path.open("rb") as source:
            payload = source.read(cap + 1)
    except OSError as exc:
        raise ValueError("statement sample payload is unavailable") from exc
    if len(payload) > cap:
        raise ValueError("statement sample exceeds byte limit")
    text = payload.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) > cap:
        raise ValueError("statement sample exceeds byte limit")
    return text


def normalize_tests_spec_entry(
    raw: object,
    *,
    index: int = 0,
    sample_max_bytes: int,
) -> TestSpecEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"tests[{index}] must be an object")
    raw_kind = raw.get("kind")
    if not isinstance(raw_kind, str):
        raise ValueError(f"tests[{index}] kind must be manual or gen")
    kind = raw_kind.strip().lower()
    if kind not in {"manual", "gen"}:
        raise ValueError(f"tests[{index}] kind must be manual or gen")
    sample = _normalize_sample_flag(raw.get("sample", False))
    sample_input = normalize_sample_input(
        raw.get("sample_input", ""),
        max_bytes=sample_max_bytes,
    )
    sample_output = normalize_sample_output(
        raw.get("sample_output", ""),
        max_bytes=sample_max_bytes,
    )
    if len(sample_input.encode("utf-8")) + len(
        sample_output.encode("utf-8")
    ) > max(1, int(sample_max_bytes)):
        raise ValueError(
            f"tests[{index}] sample input and output exceed statement sample byte limit"
        )
    sample_output_validate = _normalize_sample_output_validate_flag(
        raw.get("sample_output_validate", True)
    )
    if not isinstance(raw_id_obj := raw.get("id"), str):
        raise ValueError(f"tests[{index}] id is required")
    raw_id = raw_id_obj.strip()
    if not raw_id:
        raise ValueError(f"tests[{index}] id is required")
    return TestSpecEntry(
        id=normalize_test_id(raw_id),
        kind=kind,
        sample=sample,
        sample_input=sample_input,
        sample_output=sample_output,
        sample_output_validate=sample_output_validate,
    )


def normalize_tests_spec_entries(
    raw: object,
    *,
    sample_max_bytes: int,
) -> list[TestSpecEntry]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tests must be an array")
    entries: list[TestSpecEntry] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw, start=1):
        row = normalize_tests_spec_entry(
            item,
            index=idx,
            sample_max_bytes=sample_max_bytes,
        )
        row_id = row["id"]
        if row_id in seen_ids:
            raise ValueError(f"tests[{idx}] duplicated id: {row_id}")
        seen_ids.add(row_id)
        entries.append(row)
        if len(entries) > TESTS_SPEC_MAX_ITEMS:
            raise ValueError("too many tests in tests/spec.json")
    return entries


def loads_tests_spec(
    text: str,
    *,
    document_max_bytes: int,
    sample_max_bytes: int,
) -> list[TestSpecEntry]:
    bounded_text = enforce_textarea_max_bytes(
        text,
        label="tests/spec.json",
        max_bytes=document_max_bytes,
    )
    payload = loads_object(bounded_text, label="tests/spec.json")
    reject_unknown_keys(payload, allowed=_TESTS_SPEC_KEYS, label="tests/spec.json")
    require_keys(payload, required=_TESTS_SPEC_KEYS, label="tests/spec.json")
    tests = payload["tests"]
    if not isinstance(tests, list):
        raise ValueError("tests/spec.json.tests: must be an array")
    entries: list[TestSpecEntry] = []
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(tests, start=1):
        label = f"tests/spec.json.tests[{index}]"
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{label}: must be an object")
        entry_payload = dict(raw_entry)
        reject_unknown_keys(entry_payload, allowed=_TEST_ENTRY_KEYS, label=label)
        require_keys(entry_payload, required=_TEST_ENTRY_REQUIRED_KEYS, label=label)
        test_id = entry_payload["id"]
        if not isinstance(test_id, str) or not TESTS_SPEC_ID_RE.fullmatch(test_id):
            raise ValueError(f"{label}.id: must be 3-12 decimal digits")
        if test_id in seen_ids:
            raise ValueError(f"{label}.id: duplicate id '{test_id}'")
        seen_ids.add(test_id)
        raw_kind = entry_payload["kind"]
        if not isinstance(raw_kind, str) or raw_kind not in {"manual", "gen"}:
            raise ValueError(f"{label}.kind: must be 'manual' or 'gen'")
        kind: TestKind = raw_kind
        sample = entry_payload.get("sample", False)
        if not isinstance(sample, bool):
            raise ValueError(f"{label}.sample: must be a boolean")
        entry = TestSpecEntry(
            id=test_id,
            kind=kind,
            sample=sample,
            sample_input="",
            sample_output="",
            sample_output_validate=True,
        )
        if "sample_input" in entry_payload:
            sample_input_value = entry_payload["sample_input"]
            if not isinstance(sample_input_value, str):
                raise ValueError(f"{label}.sample_input: must be a string")
            if _normalize_newlines(sample_input_value) != sample_input_value:
                raise ValueError(f"{label}.sample_input: must use LF newlines")
            entry["sample_input"] = sample_input_value
        if "sample_output" in entry_payload:
            sample_output_value = entry_payload["sample_output"]
            if not isinstance(sample_output_value, str):
                raise ValueError(f"{label}.sample_output: must be a string")
            if _normalize_newlines(sample_output_value) != sample_output_value:
                raise ValueError(f"{label}.sample_output: must use LF newlines")
            entry["sample_output"] = sample_output_value
        if "sample_output_validate" in entry_payload:
            validate = entry_payload["sample_output_validate"]
            if not isinstance(validate, bool):
                raise ValueError(
                    f"{label}.sample_output_validate: must be a boolean"
                )
            entry["sample_output_validate"] = validate
        sample_input = entry.get("sample_input", "")
        sample_output = entry.get("sample_output", "")
        if len(sample_input.encode("utf-8")) + len(sample_output.encode("utf-8")) > max(
            1, int(sample_max_bytes)
        ):
            raise ValueError(
                f"{label}: sample input and output exceed statement sample byte limit"
            )
        entries.append(entry)
        if len(entries) > TESTS_SPEC_MAX_ITEMS:
            raise ValueError("too many tests in tests/spec.json")
    return entries


def load_tests_spec(
    path: Path,
    *,
    document_max_bytes: int,
    sample_max_bytes: int,
) -> list[TestSpecEntry]:
    root = path.parent.parent
    path = require_regular_source_file(root, TESTS_SPEC_REL.as_posix())
    cap = max(1, int(document_max_bytes))
    try:
        if path.stat().st_size > cap:
            raise ValueError("tests/spec.json is too long")
        with path.open("rb") as source:
            payload = source.read(cap + 1)
    except OSError as exc:
        raise ValueError(f"cannot read tests/spec.json: {exc}") from exc
    if len(payload) > cap:
        raise ValueError("tests/spec.json is too long")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("tests/spec.json must be UTF-8") from exc
    return loads_tests_spec(
        text,
        document_max_bytes=document_max_bytes,
        sample_max_bytes=sample_max_bytes,
    )


def dumps_tests_spec(
    entries: list[TestSpecEntry],
    *,
    document_max_bytes: int,
    sample_max_bytes: int,
) -> str:
    normalized = normalize_tests_spec_entries(
        entries,
        sample_max_bytes=sample_max_bytes,
    )
    dumped_tests: list[dict] = []
    for idx, row in enumerate(normalized, start=1):
        row_payload: dict[str, object] = {
            "id": row.get("id"),
            "kind": row.get("kind"),
        }
        if row.get("sample", False):
            row_payload["sample"] = True
        sample_input = normalize_sample_input(
            row.get("sample_input", ""),
            max_bytes=sample_max_bytes,
        )
        sample_output = normalize_sample_output(
            row.get("sample_output", ""),
            max_bytes=sample_max_bytes,
        )
        sample_output_validate = _normalize_sample_output_validate_flag(
            row.get("sample_output_validate", True)
        )
        if sample_input:
            row_payload["sample_input"] = sample_input
        if sample_output:
            row_payload["sample_output"] = sample_output
        if not sample_output_validate:
            row_payload["sample_output_validate"] = False
        normalized_row = normalize_tests_spec_entry(
            row_payload,
            index=idx,
            sample_max_bytes=sample_max_bytes,
        )
        dumped_row: dict[str, object] = {
            "id": normalized_row["id"],
            "kind": normalized_row["kind"],
        }
        if normalized_row["sample"]:
            dumped_row["sample"] = True
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
    return enforce_textarea_max_bytes(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        label="tests/spec.json",
        max_bytes=document_max_bytes,
    )


def summarize_tests_spec(entries: list[TestSpecEntry]) -> dict[str, int]:
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
