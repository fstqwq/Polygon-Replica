from __future__ import annotations

from pathlib import Path

from app.impl.runtime.config import config
from app.impl.workspace.context_operation import normalize_page_target
from app.impl.workspace.test_spec import (
    read_tests_spec,
    tests_spec_bool_flag,
    tests_spec_form_text,
    tests_spec_read_payload,
    tests_spec_write_payload,
    write_tests_spec,
)
from app.service.problem.test_spec import (
    next_test_id,
    normalize_gen_command,
    normalize_sample_input,
    normalize_sample_output,
    normalize_test_id,
    normalize_test_kind,
    normalize_tests_spec_entry,
)
_C = config.config_values


def tests_spec_gen_script_context(workspace: Path) -> dict[str, object]:
    limits = _C.snapshot()
    lines: list[str] = []
    with config.workspace_service.workspace_lock(workspace):
        entries, _spec_path = read_tests_spec(
            workspace,
            document_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
        )
        for entry in entries:
            kind = kind.strip().lower() if isinstance(kind := entry.get("kind"), str) else ""
            if kind != "gen":
                continue
            command = str(tests_spec_read_payload(workspace, entry) or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if not command:
                continue
            lines.append(command)
    return {"text": "\n".join(lines), "count": len(lines)}


def parse_gen_script_lines(raw: object) -> list[str]:
    normalized = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    commands: list[str] = []
    for line in normalized.split("\n"):
        cmd = str(line or "").strip()
        if not cmd:
            continue
        commands.append(normalize_gen_command(cmd))
    return commands


def tests_spec_sample_input_value(raw: object | None, fallback: object = "") -> str:
    value = fallback if raw is None else tests_spec_form_text(raw)
    return normalize_sample_input(
        value,
        max_bytes=int(_C.STATEMENT_SAMPLE_MAX_BYTES),
    )


def tests_spec_sample_output_value(raw: object | None, fallback: object = "") -> str:
    value = fallback if raw is None else tests_spec_form_text(raw)
    return normalize_sample_output(
        value,
        max_bytes=int(_C.STATEMENT_SAMPLE_MAX_BYTES),
    )


def tests_spec_sample_output_validate_value(raw: object | list[object] | None, fallback: object = True) -> bool:
    if isinstance(raw, list):
        if not raw:
            return tests_spec_bool_flag(fallback)
        return tests_spec_bool_flag(tests_spec_form_text(raw[-1]))
    if raw is None:
        return tests_spec_bool_flag(fallback)
    return tests_spec_bool_flag(tests_spec_form_text(raw))


def tests_spec_row(
    *,
    test_id: str,
    kind: str,
    sample: bool,
    sample_input: str = "",
    sample_output: str = "",
    sample_output_validate: bool = True,
    index: int = 0,
) -> dict:
    payload: dict[str, object] = {
        "id": normalize_test_id(test_id),
        "kind": normalize_test_kind(kind),
        "sample": bool(sample),
    }
    safe_sample_input = tests_spec_sample_input_value(sample_input)
    safe_sample_output = tests_spec_sample_output_value(sample_output)
    if safe_sample_input:
        payload["sample_input"] = safe_sample_input
    if safe_sample_output:
        payload["sample_output"] = safe_sample_output
    if bool(sample) and safe_sample_output and (not bool(sample_output_validate)):
        payload["sample_output_validate"] = False
    return normalize_tests_spec_entry(
        payload,
        index=index,
        sample_max_bytes=int(_C.STATEMENT_SAMPLE_MAX_BYTES),
    )


def tests_spec_add_single_entry(
    workspace: Path,
    *,
    requested_id: str,
    kind: str,
    sample: bool,
    payload: str,
    sample_input: str,
    sample_output: str,
    sample_output_validate: bool,
) -> tuple[int, str]:
    limits = _C.snapshot()
    entries, spec_path = read_tests_spec(
        workspace,
        document_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
        sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
    )
    safe_test_id = normalize_test_id(requested_id) if requested_id else next_test_id(entries)
    if any((row.get("id") == safe_test_id for row in entries)):
        raise ValueError(f"test id already exists: {safe_test_id}")
    entries.append(
        tests_spec_row(
            test_id=safe_test_id,
            kind=kind,
            sample=sample,
            sample_input=sample_input,
            sample_output=sample_output,
            sample_output_validate=sample_output_validate,
            index=len(entries) + 1,
        )
    )
    write_tests_spec(
        spec_path,
        entries,
        document_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
        sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
    )
    tests_spec_write_payload(workspace, safe_test_id, kind, payload)
    return len(entries), safe_test_id


def normalize_verification_target_page(page: str) -> str:
    target_page = normalize_page_target(page)
    if target_page in {"problems", "contests", "settings"}:
        return "statement"
    if target_page == "git":
        return "workspace"
    return target_page
