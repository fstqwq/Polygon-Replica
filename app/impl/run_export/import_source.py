from __future__ import annotations
from app.impl.auth.session import require_session_user

import io
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import TypedDict, cast, Annotated, Annotated

from fastapi import File, Form, UploadFile, Depends
from fastapi.responses import JSONResponse

from app.impl.auth.shared import redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_operation import audit
from app.impl.run_export.query import (
    _bare_repo_head_commit,
    _count_label,
)
from app.main_util import read_fileobj_bytes_limited
from app.service.importing.icpc import ICPCPackageImportService
from app.service.importing.native import NATIVE_PACKAGE_ANCHOR, NativePackageImportService
from app.service.importing.polygon import PolygonPackageImportService
from app.service.platform.git_process import run_git

_C = config.constants
_POLYGON_IMPORTER = PolygonPackageImportService()
_ICPC_IMPORTER = ICPCPackageImportService()
_NATIVE_IMPORTER = NativePackageImportService()
_POLYGON_LINUX_PACKAGE_SUFFIX_RE = re.compile(r"-\d+\$linux$", re.IGNORECASE)
_PROBLEM_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


ImportedTestsSummary = TypedDict(
    "ImportedTestsSummary",
    {
        "total": int,
        "answers": int,
    },
    total=False,
)

ImportedStatementSummary = TypedDict(
    "ImportedStatementSummary",
    {
        "language_warning": str,
    },
    total=False,
)

ImportedPackageResult = TypedDict(
    "ImportedPackageResult",
    {
        "title": str,
        "tests": ImportedTestsSummary,
        "statement": ImportedStatementSummary,
        "components": dict[str, object],
        "solutions": dict[str, object],
        "warnings": list[str],
    },
    total=False,
)


def _select_importer(package_format: str):
    token = package_format.strip().lower()
    if token == "polygon":
        return _POLYGON_IMPORTER
    if token == "icpc":
        return _ICPC_IMPORTER
    if token == "native":
        return _NATIVE_IMPORTER
    raise ValueError(f"unsupported package format: {package_format}")
def _slugify_problem_id(raw: str) -> str:
    token = raw.strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > 64:
        token = token[:64].rstrip("-")
    return token

def _normalize_problem_slug_segment_required(raw: str) -> str:
    token = _slugify_problem_id(raw)
    if not token or (not _PROBLEM_SEGMENT_RE.fullmatch(token)):
        raise ValueError(_C.PROBLEM_ID_RULE_MESSAGE)
    return token

def _problem_full_slug(owner: str, slug_segment: str) -> str:
    safe_owner = owner.strip().lower()
    if not _C.USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(_C.USERNAME_RULE_MESSAGE)
    safe_segment = _normalize_problem_slug_segment_required(slug_segment)
    return f"{safe_owner}/{safe_segment}"

def _import_slug_base_from_package_name(package_name: str) -> str:
    raw_stem = Path(package_name or "imported-problem.zip").stem.strip()
    normalized_stem = _POLYGON_LINUX_PACKAGE_SUFFIX_RE.sub("", raw_stem).strip()
    if not normalized_stem:
        normalized_stem = raw_stem
    stem = _slugify_problem_id(normalized_stem)
    base = stem or "imported-problem"
    if not _PROBLEM_SEGMENT_RE.fullmatch(base):
        return "imported-problem"
    return base

def _next_available_problem_slug(owner: str, base: str) -> str:
    token = base.strip()
    if not token:
        token = "imported-problem"
    token = _normalize_problem_slug_segment_required(token)
    candidate = token
    idx = 2
    while config.workspace_service.known_problem_id(_problem_full_slug(owner, candidate)) is not None:
        suffix = f"-{idx}"
        prefix_len = max(1, 64 - len(suffix))
        prefix = token[:prefix_len].rstrip("-") or "p"
        candidate = f"{prefix}{suffix}"
        idx += 1
    return candidate

def build_import_slug_hint(owner: str, filename: str, requested_slug: str) -> dict[str, object]:
    package_name = filename.strip()
    requested = requested_slug.strip()
    base = _import_slug_base_from_package_name(package_name)
    if requested:
        normalized = _slugify_problem_id(requested)
        valid = bool(normalized and _PROBLEM_SEGMENT_RE.fullmatch(normalized))
        if not valid:
            return {
                "ok": True,
                "filename": package_name,
                "requested_slug": requested,
                "valid": False,
                "exists": False,
                "base": base,
                "suggested": _next_available_problem_slug(owner, base),
                "message": _C.PROBLEM_ID_RULE_MESSAGE,
            }
        full_requested = _problem_full_slug(owner, normalized)
        exists = config.workspace_service.known_problem_id(full_requested) is not None
        suggested = _next_available_problem_slug(owner, normalized) if exists else normalized
        message = ""
        if exists:
            message = f"problem already exists: {full_requested}"
        return {
            "ok": True,
            "filename": package_name,
            "requested_slug": normalized,
            "valid": True,
            "exists": bool(exists),
            "base": base,
            "suggested": suggested,
            "message": message,
        }

    suggested = _next_available_problem_slug(owner, base)
    return {
        "ok": True,
        "filename": package_name,
        "requested_slug": "",
        "valid": True,
        "exists": bool(suggested != base),
        "base": base,
        "suggested": suggested,
        "message": "",
    }

def _resolve_import_problem_slug(owner: str, requested_slug: str, package_name: str) -> str:
    requested = requested_slug.strip()
    if requested:
        normalized = _normalize_problem_slug_segment_required(requested)
        full_requested = _problem_full_slug(owner, normalized)
        if config.workspace_service.known_problem_id(full_requested) is not None:
            suggestion = _next_available_problem_slug(owner, normalized)
            raise ValueError(f"problem already exists: {full_requested} (try: {_problem_full_slug(owner, suggestion)})")
        return full_requested

    base = _import_slug_base_from_package_name(package_name)
    return _problem_full_slug(owner, _next_available_problem_slug(owner, base))

def _is_package_marker(names: list[str], marker: str) -> bool:
    safe_marker = marker.replace("\\", "/").strip().strip("/")
    if not safe_marker:
        return False
    suffix = "/" + safe_marker
    for raw in names:
        token = raw.replace("\\", "/").strip().strip("/")
        if not token:
            continue
        if token == safe_marker or token.endswith(suffix):
            return True
    return False

def _detect_problem_package_format(package_payload: bytes) -> str:
    raw = bytes(package_payload or b"")
    if not raw:
        raise ValueError("package file is empty")
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            names = zf.namelist()
    except Exception as exc:
        raise ValueError(f"invalid zip package: {exc}") from exc
    if _is_package_marker(names, "problem.xml"):
        return "polygon"
    if _is_package_marker(names, "problem.yaml"):
        return "icpc"
    if _is_package_marker(names, NATIVE_PACKAGE_ANCHOR):
        return "native"
    raise ValueError("unsupported package format: expected problem.xml (Polygon), problem.yaml (ICPC), or config/problem.json (native)")


def _finalize_imported_problem(problem: str, actor_user: str, workspace: Path, package_format: str) -> str:
    commit_message = f"import {package_format} package"
    commit_head = config.git_service.commit(workspace, commit_message, actor_user, f"{actor_user}@polygonlike.local")
    config.git_service.push(workspace, "main")
    return commit_head


def _remove_tree_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=False)


def _merge_imported_tree(source_root: Path, target_root: Path) -> None:
    for child in source_root.iterdir():
        if child.name == ".git":
            continue
        target = target_root / child.name
        if child.is_symlink():
            child.unlink(missing_ok=True)
            continue
        if child.is_dir():
            if target.is_symlink() or target.is_file():
                _remove_tree_path(target)
            if not target.exists():
                shutil.move(str(child), str(target))
                continue
            _merge_imported_tree(child, target)
            child.rmdir()
            continue
        if target.exists():
            _remove_tree_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(child), str(target))


def import_package_into_workspace(
    actor_user_id: int,
    actor_user: str,
    target_problem: str,
    package_name: str,
    package_content: bytes,
    source_problem: str = "",
    normalize_test_data_newlines: bool = False,
) -> dict[str, object]:
    safe_actor_user = actor_user.strip()
    if not safe_actor_user:
        raise ValueError("actor user is required")
    safe_target_problem = target_problem.strip()
    if not safe_target_problem:
        raise ValueError("target problem is required")
    safe_package_name = package_name.strip()
    if not safe_package_name:
        raise ValueError("package filename is required")
    payload = bytes(package_content or b"")
    if not payload:
        raise ValueError("package file is empty")

    package_format = _detect_problem_package_format(payload)
    target_workspace = Path(config.workspace_service.ensure_workspace(safe_target_problem, safe_actor_user, refresh_status=False))
    importer = _select_importer(package_format)
    staging_root = target_workspace.parent / f".workspace-import-{uuid.uuid4().hex}"
    staging_workspace = staging_root / "workspace"
    result: ImportedPackageResult
    try:
        staging_workspace.mkdir(parents=True, exist_ok=False)
        result = cast(
            ImportedPackageResult,
            importer.import_package(
                staging_workspace,
                safe_package_name,
                payload,
                normalize_test_data_newlines=bool(normalize_test_data_newlines),
            ),
        )
        with config.workspace_service.workspace_lock(target_workspace):
            _merge_imported_tree(staging_workspace, target_workspace)
        config.workspace_service.ensure_workspace(safe_target_problem, safe_actor_user, refresh_status=True)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    problem_id = config.workspace_service.known_problem_id(safe_target_problem)
    if problem_id is not None:
        audit(
            actor_user_id,
            int(problem_id),
            "export.import_workspace",
            {
                "package": safe_package_name,
                "package_format": package_format,
                "source_problem": source_problem.strip(),
                "target_problem": safe_target_problem,
                "statement": result.get("statement"),
                "tests": result.get("tests"),
                "components": result.get("components"),
                "solutions": result.get("solutions"),
                "merge_mode": "overwrite_matching_paths_keep_missing",
            },
        )
    tests_info = result.get("tests")
    total_tests = int(cast(dict[str, object], tests_info).get("total", 0)) if tests_info is not None else 0
    return {
        "target_problem": safe_target_problem,
        "total_tests": total_tests,
        "result": result,
        "package_format": package_format,
    }

def import_package_as_new_problem(
    actor_user_id: int,
    actor_user: str,
    package_name: str,
    package_content: bytes,
    requested_slug: str = "",
    source_problem: str = "",
    normalize_test_data_newlines: bool = False,
) -> dict[str, object]:
    safe_actor_user = actor_user.strip()
    if not safe_actor_user:
        raise ValueError("actor user is required")
    safe_package_name = package_name.strip()
    if not safe_package_name:
        raise ValueError("package filename is required")
    payload = bytes(package_content or b"")
    if not payload:
        raise ValueError("package file is empty")

    target_problem = _resolve_import_problem_slug(safe_actor_user, requested_slug, safe_package_name)
    package_format = _detect_problem_package_format(payload)
    target_bare = (config.settings.bare_root / f"{target_problem}.git").resolve()
    existing_bare_head = _bare_repo_head_commit(target_bare)
    if existing_bare_head:
        raise ValueError(f"import target already has revision history: {target_problem}")
    created_problem = False
    try:
        config.workspace_service.ensure_problem(target_problem)
        created_problem = True
        config.workspace_service.grant_repo_access(target_problem, safe_actor_user, "owner")
        target_workspace = Path(config.workspace_service.ensure_workspace(target_problem, safe_actor_user))
        workspace_head = run_git(["git", "-C", str(target_workspace), "rev-parse", "--verify", "HEAD"])
        if workspace_head.returncode == 0 and workspace_head.stdout.strip():
            raise ValueError(f"import target already has revision history: {target_problem}")
        with config.workspace_service.workspace_lock(target_workspace):
            importer = _select_importer(package_format)
            result = cast(
                ImportedPackageResult,
                importer.import_package(
                    target_workspace,
                    safe_package_name,
                    payload,
                    normalize_test_data_newlines=bool(normalize_test_data_newlines),
                ),
            )
        with config.workspace_service.workspace_lock(target_workspace):
            imported_commit = _finalize_imported_problem(target_problem, safe_actor_user, target_workspace, package_format)
        config.workspace_service.ensure_workspace(target_problem, safe_actor_user, refresh_status=True)
        result["commit"] = imported_commit
        details = {
            "package": safe_package_name,
            "package_format": package_format,
            "source_problem": source_problem.strip(),
            "target_problem": target_problem,
            "import_commit": imported_commit,
            "statement": result.get("statement"),
            "tests": result.get("tests"),
            "components": result.get("components"),
            "solutions": result.get("solutions"),
        }
        target_problem_id = config.workspace_service.known_problem_id(target_problem)
        if target_problem_id is not None:
            audit(actor_user_id, int(target_problem_id), "export.import", details)
        tests_info = result.get("tests")
        total_tests = int(cast(dict[str, object], tests_info).get("total", 0)) if tests_info is not None else 0
        return {"target_problem": target_problem, "total_tests": total_tests, "result": result, "package_format": package_format}
    except Exception:
        if created_problem:
            try:
                config.workspace_service.delete_problem(target_problem)
            except Exception:
                pass
        raise

def import_package_warnings(import_result: dict[str, object] | None) -> list[str]:
    if import_result is None:
        return []
    result = cast(ImportedPackageResult | None, import_result.get("result"))
    if result is None:
        return []
    warnings = [text for item in (result.get("warnings") or []) if (text := str(item).strip())]
    statement = result.get("statement")
    if statement is None:
        return warnings
    warning = cast(str | None, statement.get("language_warning"))
    if warning:
        warnings.append(warning)
    return warnings

def export_import(problem: str, user: Annotated[str, Depends(require_session_user)], package_upload: UploadFile | None=File(None)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    try:
        if package_upload is None:
            raise ValueError('package file is required')
        package_name = package_upload.filename or ""
        package_name = package_name.strip()
        if not package_name:
            raise ValueError('package filename is required')
        package_content = read_fileobj_bytes_limited(package_upload.file, label='package file')
        actor_user = ctx["user"]["username"]
        imported = import_package_into_workspace(
            actor_user_id=int(ctx['user']['id']),
            actor_user=actor_user,
            target_problem=problem.strip(),
            package_name=package_name,
            package_content=package_content,
            source_problem=problem.strip(),
        )
        target_problem = cast(str, imported["target_problem"])
        total_tests = int(imported["total_tests"])
        package_format = cast(str, imported["package_format"])
        msg = f"{package_format} package imported into working copy {target_problem} ({_count_label(total_tests, 'test')})"
        warnings = import_package_warnings(imported)
        if warnings:
            msg = f"{msg}; warning: {'; '.join(warnings)}"
        return redirect_response(f'/problems/{target_problem}/workspace', status_code=303, message=msg)
    except ValueError as exc:
        msg = str(exc)
    except Exception as exc:
        msg = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return redirect_response(f'/problems/{problem}/export', status_code=303, message=msg)

def export_import_slug_hint(problem: str, user: Annotated[str, Depends(require_session_user)], filename: str = "", requested_slug: str = ""):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    actor_user = ctx["user"]["username"]
    payload = build_import_slug_hint(actor_user, filename, requested_slug)
    return JSONResponse(payload)

