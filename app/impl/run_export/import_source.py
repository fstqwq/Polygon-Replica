from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import TypedDict, cast

from fastapi import File, Form, UploadFile
from fastapi.responses import JSONResponse

from .artifact import _is_safe_regular_file
from app.impl.auth.shared import redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_job import page_ctx
from app.impl.workspace.context_operation import audit
from app.impl.run_export.query import (
    _bare_repo_head_commit,
    _count_label,
    _summary_object,
    _workspace_problem_mode,
)
from app.service.importing.icpc import ICPCPackageImportService
from app.service.importing.polygon import PolygonPackageImportService
from app.service.problem.test_spec import TESTS_SPEC_REL, load_tests_spec
from app.service.platform.process import run_cmd

_C = config.constants
_POLYGON_IMPORTER = PolygonPackageImportService()
_ICPC_IMPORTER = ICPCPackageImportService()
_POLYGON_LINUX_PACKAGE_SUFFIX_RE = re.compile(r"-\d+\$linux$", re.IGNORECASE)
_PROBLEM_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


ImportedTestsSummary = TypedDict(
    "ImportedTestsSummary",
    {
        "total": int,
        "answers": int,
        "sample_answers_built": int,
        "sample_answers_missing": int,
        "sample_manual_total": int,
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
        "sample_answers": dict[str, object],
    },
    total=False,
)


def _select_importer(package_format: str):
    token = package_format.strip().lower()
    if token == "polygon":
        return _POLYGON_IMPORTER
    if token == "icpc":
        return _ICPC_IMPORTER
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
    while config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [_problem_full_slug(owner, candidate)]) is not None:
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
        exists = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [full_requested]) is not None
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
        exists = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [full_requested])
        if exists is not None:
            suggestion = _next_available_problem_slug(owner, normalized)
            raise ValueError(f"problem already exists: {full_requested} (try: {_problem_full_slug(owner, suggestion)})")
        return full_requested

    base = _import_slug_base_from_package_name(package_name)
    return _problem_full_slug(owner, _next_available_problem_slug(owner, base))

def _sample_manual_rows_missing_answers(workspace: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    try:
        entries = cast(list[dict[str, object]], load_tests_spec(workspace / TESTS_SPEC_REL))
    except Exception as exc:
        raise ValueError(f"invalid tests/spec.json after import: {exc}") from exc

    rows: list[tuple[int, str]] = []
    missing: list[tuple[int, str]] = []
    for index, row in enumerate(entries, start=1):
        if not row["sample"]:
            continue
        if row["kind"] != "manual":
            continue
        test_id = row["id"]
        rows.append((index, test_id))
        answer_path = workspace / "tests" / "answers" / f"{test_id}.ans"
        if not _is_safe_regular_file(answer_path):
            missing.append((index, test_id))
    return rows, missing

def _build_polygon_sample_answers(problem: str, user: str, workspace: Path) -> dict[str, object]:
    sample_rows, missing_rows = _sample_manual_rows_missing_answers(workspace)
    if not sample_rows:
        return {"sample_manual_total": 0, "sample_answers_missing": 0, "sample_answers_built": 0, "verification_id": ""}
    if not missing_rows:
        return {"sample_manual_total": len(sample_rows), "sample_answers_missing": 0, "sample_answers_built": 0, "verification_id": ""}
    mode = _workspace_problem_mode(workspace)
    if mode != "pass-fail":
        return {
            "sample_manual_total": len(sample_rows),
            "sample_answers_missing": len(missing_rows),
            "sample_answers_built": 0,
            "verification_id": "",
            "skipped_mode": mode,
        }

    verification_id = config.verification_service.run_verification(problem, user)
    verification_row = config.db.fetch_one(
        "SELECT status,summary_json,artifact_path FROM verifications WHERE id=?",
        [verification_id],
    )
    if verification_row is None:
        raise ValueError(f"sample answer verification missing: {verification_id}")
    verification_status = verification_row["status"]
    if verification_status != "ok":
        summary = _summary_object(verification_row["summary_json"])
        error_text = cast(str | None, summary.get("error")) or ""
        if error_text:
            raise ValueError(f"sample answer verification failed ({verification_id}): {error_text}")
        raise ValueError(f"sample answer verification failed ({verification_id})")
    artifact_path = verification_row["artifact_path"]
    if not artifact_path:
        raise ValueError(f"sample answer verification has no artifact path: {verification_id}")
    try:
        artifact_root = Path(artifact_path).resolve()
    except Exception as exc:
        raise ValueError(f"sample answer verification has invalid artifact_path: {verification_id}") from exc
    ans_dir = artifact_root / "ans"
    if not ans_dir.exists() or not ans_dir.is_dir() or ans_dir.is_symlink():
        raise ValueError(f"sample answer verification missing ans directory: {verification_id}")

    built = 0
    for index, test_id in missing_rows:
        source_name = f"{int(index):03d}.ans"
        source_answer = ans_dir / source_name
        if not _is_safe_regular_file(source_answer):
            raise ValueError(
                f"sample answer missing from verification output for test id {test_id} (case {source_name})"
            )
        target_answer = workspace / "tests" / "answers" / f"{test_id}.ans"
        target_answer.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_answer, target_answer)
        built += 1

    _sample_rows_after, still_missing = _sample_manual_rows_missing_answers(workspace)
    if still_missing:
        first_idx, first_id = still_missing[0]
        raise ValueError(f"sample answer still missing after verification: test id {first_id} (spec row {first_idx})")

    return {
        "sample_manual_total": len(sample_rows),
        "sample_answers_missing": len(missing_rows),
        "sample_answers_built": built,
        "verification_id": verification_id,
    }

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
    has_problem_xml = _is_package_marker(names, "problem.xml")
    has_problem_yaml = _is_package_marker(names, "problem.yaml")
    if has_problem_xml:
        return "polygon"
    if has_problem_yaml:
        return "icpc"
    raise ValueError("unsupported package format: expected problem.xml (Polygon) or problem.yaml (ICPC)")

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
    target_segment = target_problem.split("/", 1)[1] if "/" in target_problem else target_problem
    config.workspace_service.ensure_problem(target_problem, f"{target_segment.title()} Problem")
    config.workspace_service.grant_repo_access(target_problem, safe_actor_user, "owner")
    target_workspace = Path(config.workspace_service.ensure_workspace(target_problem, safe_actor_user))
    workspace_head = run_cmd(["git", "-C", str(target_workspace), "rev-parse", "--verify", "HEAD"])
    if workspace_head.returncode == 0 and workspace_head.stdout.strip():
        raise ValueError(f"import target already has revision history: {target_problem}")
    sample_answer_summary: dict[str, object] = {}
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
        imported_title = cast(str | None, result.get("title"))
        if imported_title is None:
            imported_title = ""
        if imported_title:
            config.workspace_service.set_problem_name(target_problem, imported_title)
    if package_format == "polygon":
        sample_answer_summary = _build_polygon_sample_answers(target_problem, safe_actor_user, target_workspace)
        tests_summary = result.get("tests")
        if tests_summary is not None:
            tests_summary = cast(dict[str, object], tests_summary)
            tests_summary["sample_answers_built"] = int(sample_answer_summary.get("sample_answers_built", 0))
            tests_summary["sample_answers_missing"] = int(sample_answer_summary.get("sample_answers_missing", 0))
            tests_summary["sample_manual_total"] = int(sample_answer_summary.get("sample_manual_total", 0))
            current_answers = int(tests_summary.get("answers", 0))
            tests_summary["answers"] = current_answers + int(sample_answer_summary.get("sample_answers_built", 0))
    if sample_answer_summary:
        result["sample_answers"] = sample_answer_summary
    details = {
        "package": safe_package_name,
        "package_format": package_format,
        "source_problem": source_problem.strip(),
        "target_problem": target_problem,
        "statement": result.get("statement"),
        "tests": result.get("tests"),
        "components": result.get("components"),
        "solutions": result.get("solutions"),
    }
    target_problem_row = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [target_problem])
    if target_problem_row is not None:
        audit(actor_user_id, int(target_problem_row["id"]), "export.import", details)
    tests_info = result.get("tests")
    total_tests = int(cast(dict[str, object], tests_info).get("total", 0)) if tests_info is not None else 0
    return {"target_problem": target_problem, "total_tests": total_tests, "result": result, "package_format": package_format}

def import_statement_language_warning(import_result: dict[str, object] | None) -> str:
    if import_result is None:
        return ""
    result = cast(ImportedPackageResult | None, import_result.get("result"))
    if result is None:
        return ""
    statement = result.get("statement")
    if statement is None:
        return ""
    warning = cast(str | None, statement.get("language_warning"))
    if warning is None:
        return ""
    return warning

def export_import(problem: str, user: str, package_upload: UploadFile | None=File(None), problem_slug: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    try:
        if package_upload is None:
            raise ValueError('package file is required')
        package_name = package_upload.filename or ""
        package_name = package_name.strip()
        if not package_name:
            raise ValueError('package filename is required')
        package_content = package_upload.file.read()
        actor_user = ctx["user"]["username"]
        imported = import_package_as_new_problem(
            actor_user_id=int(ctx['user']['id']),
            actor_user=actor_user,
            package_name=package_name,
            package_content=package_content,
            requested_slug=problem_slug.strip(),
            source_problem=problem.strip(),
        )
        target_problem = cast(str, imported["target_problem"])
        total_tests = int(imported["total_tests"])
        package_format = cast(str, imported["package_format"])
        msg = f"{package_format} package imported as {target_problem} ({_count_label(total_tests, 'test')})"
        language_warning = import_statement_language_warning(imported)
        if language_warning:
            msg = f"{msg}; warning: {language_warning}"
        return redirect_response(f'/problems/{target_problem}/{actor_user}/statement', status_code=303, message=msg)
    except ValueError as exc:
        msg = str(exc)
    except Exception as exc:
        msg = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return redirect_response(f'/problems/{problem}/{user}/export', status_code=303, message=msg)

def export_import_slug_hint(problem: str, user: str, filename: str = "", requested_slug: str = ""):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    actor_user = ctx["user"]["username"]
    payload = build_import_slug_hint(actor_user, filename, requested_slug)
    return JSONResponse(payload)
