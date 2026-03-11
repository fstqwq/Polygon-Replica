from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.main_util import safe_workspace_path
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    dumps_tests_spec,
    load_tests_spec,
    normalize_test_id,
    normalize_test_kind,
    payload_rel_path_for_test,
)


def tests_spec_workspace_path(workspace: Path) -> Path:
    return safe_workspace_path(workspace, TESTS_SPEC_REL.as_posix())


def tests_spec_payload_rel_path(test_id: str, kind: str) -> str:
    safe_id = normalize_test_id(test_id)
    safe_kind = normalize_test_kind(kind)
    return payload_rel_path_for_test(safe_id, safe_kind)


def tests_spec_payload_file_path(workspace: Path, test_id: str, kind: str) -> Path:
    return safe_workspace_path(workspace, tests_spec_payload_rel_path(test_id, kind))


def tests_spec_read_payload(workspace: Path, entry: dict) -> str:
    test_id = str(entry.get("id") or "").strip()
    if not test_id:
        return ""
    kind = str(entry.get("kind") or "").strip().lower()
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
    payload_path.write_text(str(content), encoding="utf-8")
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


def tests_spec_bool_flag(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def tests_spec_form_text(raw: object) -> str:
    if raw is None:
        return ""
    if raw.__class__.__module__.startswith("fastapi.params") and hasattr(raw, "default"):
        raw = getattr(raw, "default", "")
        if raw is None:
            return ""
    return str(raw)


def tests_spec_resolve_index(raw_index: str, total: int) -> int:
    count = max(0, int(total))
    if count <= 0:
        raise ValueError("no tests available")
    token = str(raw_index or "").strip()
    try:
        index = int(token)
    except Exception as exc:
        raise ValueError("invalid test index") from exc
    if index < 1 or index > count:
        raise ValueError("test index is out of range")
    return index


def read_tests_spec(workspace: Path) -> tuple[list[dict], Path]:
    path = tests_spec_workspace_path(workspace)
    try:
        entries = load_tests_spec(path)
    except ValueError as exc:
        raise ValueError(f"invalid tests/spec.json: {exc}") from exc
    return (entries, path)


def write_tests_spec(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_tests_spec(entries), encoding="utf-8")




