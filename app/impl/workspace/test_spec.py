from pathlib import Path

from fastapi import HTTPException

from app.service.platform.workspace_path import safe_workspace_path
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    TestSpecEntry,
    dumps_tests_spec,
    load_tests_spec,
    normalize_test_id,
    normalize_test_kind,
    payload_rel_path_for_test,
)


def tests_spec_workspace_path(workspace: Path) -> Path:
    return safe_workspace_path(workspace, TESTS_SPEC_REL.as_posix())


def tests_spec_payload_rel_path(test_id: str, kind: str) -> str:
    return payload_rel_path_for_test(normalize_test_id(test_id), normalize_test_kind(kind))


def tests_spec_payload_file_path(workspace: Path, test_id: str, kind: str) -> Path:
    return safe_workspace_path(workspace, tests_spec_payload_rel_path(test_id, kind))


def tests_spec_read_payload(workspace: Path, entry: TestSpecEntry) -> str:
    test_id = entry.get("id") or ""
    if not test_id:
        return ""
    kind = entry.get("kind") or ""
    try:
        payload_path = tests_spec_payload_file_path(workspace, test_id, kind)
    except (HTTPException, ValueError):
        return ""
    try:
        if payload_path.exists() and payload_path.is_file() and (not payload_path.is_symlink()):
            return payload_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return ""


def tests_spec_write_payload(workspace: Path, test_id: str, kind: str, content: str) -> None:
    safe_kind = normalize_test_kind(kind)
    payload_path = tests_spec_payload_file_path(workspace, test_id, safe_kind)
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(content, encoding="utf-8")
    stale_kind = "gen" if safe_kind == "manual" else "manual"
    stale_payload_path = tests_spec_payload_file_path(workspace, test_id, stale_kind)
    stale_payload_path.unlink(missing_ok=True)


def tests_spec_remove_payload(workspace: Path, test_id: str) -> None:
    for kind in ("manual", "gen"):
        try:
            payload_path = tests_spec_payload_file_path(workspace, test_id, kind)
        except (HTTPException, ValueError):
            continue
        payload_path.unlink(missing_ok=True)


def tests_spec_bool_flag(raw: bool | str | None) -> bool:
    if raw is None:
        return False
    if raw is True:
        return True
    if raw is False:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def tests_spec_form_text(raw: str | None) -> str:
    if raw is None:
        return ""
    return raw


def tests_spec_resolve_index(raw_index: str, total: int) -> int:
    count = max(0, int(total))
    if count <= 0:
        raise ValueError("no tests available")
    try:
        index = int(raw_index)
    except Exception as exc:
        raise ValueError("invalid test index") from exc
    if index < 1 or index > count:
        raise ValueError("test index is out of range")
    return index


def read_tests_spec(
    workspace: Path,
    *,
    document_max_bytes: int,
    sample_max_bytes: int,
) -> tuple[list[TestSpecEntry], Path]:
    path = tests_spec_workspace_path(workspace)
    try:
        entries = load_tests_spec(
            path,
            document_max_bytes=document_max_bytes,
            sample_max_bytes=sample_max_bytes,
        )
    except ValueError as exc:
        raise ValueError(f"invalid tests/spec.json: {exc}") from exc
    return (entries, path)


def write_tests_spec(
    path: Path,
    entries: list[TestSpecEntry],
    *,
    document_max_bytes: int,
    sample_max_bytes: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dumps_tests_spec(
            entries,
            document_max_bytes=document_max_bytes,
            sample_max_bytes=sample_max_bytes,
        ),
        encoding="utf-8",
    )
