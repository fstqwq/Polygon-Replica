import app.main_constant as _K
from contextlib import contextmanager
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict, cast

from app.impl.runtime.dependency import runtime
from app.impl.run_export.query import (
    _bare_repo_head_commit,
)
from app.service.importing.icpc import ICPCPackageImportService
from app.service.importing.archive import (
    ArchiveView,
    ProblemImportPolicy,
)
from app.service.importing.polygon_replica import PolygonReplicaPackageImportService
from app.service.importing.polygon import PolygonPackageImportService
from app.service.problem.runtime_config import problem_config_limits
from app.service.platform.git_process import run_git

_POLYGON_IMPORTER = PolygonPackageImportService()
_ICPC_IMPORTER = ICPCPackageImportService()
_POLYGON_REPLICA_IMPORTER = PolygonReplicaPackageImportService()
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
    if token == "polygon-replica":
        return _POLYGON_REPLICA_IMPORTER
    raise ValueError(f"unsupported package format: {package_format}")


def _problem_slug_segment_max_len(owner: str) -> int:
    safe_owner = owner.strip().lower()
    if not _K.USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(_K.USERNAME_RULE_MESSAGE)
    return max(1, int(_K.PROBLEM_ID_MAX_LEN) - len(safe_owner) - 1)


def _slugify_problem_id(raw: str, *, max_len: int) -> str:
    token = raw.strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > max_len:
        token = token[:max_len].rstrip("-")
    return token

def _normalize_problem_slug_segment_required(owner: str, raw: str) -> str:
    token = _slugify_problem_id(raw, max_len=_problem_slug_segment_max_len(owner))
    if not token or (not _PROBLEM_SEGMENT_RE.fullmatch(token)):
        raise ValueError(_K.PROBLEM_ID_RULE_MESSAGE)
    return token

def _problem_full_slug(owner: str, slug_segment: str) -> str:
    safe_owner = owner.strip().lower()
    if not _K.USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(_K.USERNAME_RULE_MESSAGE)
    safe_segment = _normalize_problem_slug_segment_required(safe_owner, slug_segment)
    full_slug = f"{safe_owner}/{safe_segment}"
    if len(full_slug) > _K.PROBLEM_ID_MAX_LEN:
        raise ValueError(_K.PROBLEM_ID_RULE_MESSAGE)
    return full_slug

def _import_slug_base_from_package_name(owner: str, package_name: str) -> str:
    raw_stem = Path(package_name or "imported-problem.zip").stem.strip()
    normalized_stem = _POLYGON_LINUX_PACKAGE_SUFFIX_RE.sub("", raw_stem).strip()
    if not normalized_stem:
        normalized_stem = raw_stem
    stem = _slugify_problem_id(normalized_stem, max_len=_problem_slug_segment_max_len(owner))
    base = stem or "imported-problem"
    if not _PROBLEM_SEGMENT_RE.fullmatch(base):
        return "imported-problem"
    return base

def _next_available_problem_slug(owner: str, base: str) -> str:
    token = base.strip()
    if not token:
        token = "imported-problem"
    token = _normalize_problem_slug_segment_required(owner, token)
    max_len = _problem_slug_segment_max_len(owner)
    candidate = token
    idx = 2
    while runtime().workspace_service.known_problem_id(_problem_full_slug(owner, candidate)) is not None:
        suffix = f"-{idx}"
        prefix_len = max(1, max_len - len(suffix))
        prefix = token[:prefix_len].rstrip("-") or "p"
        candidate = f"{prefix}{suffix}"
        idx += 1
    return candidate

def build_import_slug_hint(owner: str, filename: str, requested_slug: str) -> dict[str, object]:
    package_name = filename.strip()
    requested = requested_slug.strip()
    base = _import_slug_base_from_package_name(owner, package_name)
    if requested:
        normalized = _slugify_problem_id(requested, max_len=_problem_slug_segment_max_len(owner))
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
                "message": _K.PROBLEM_ID_RULE_MESSAGE,
            }
        full_requested = _problem_full_slug(owner, normalized)
        exists = runtime().workspace_service.known_problem_id(full_requested) is not None
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
        normalized = _normalize_problem_slug_segment_required(owner, requested)
        full_requested = _problem_full_slug(owner, normalized)
        if runtime().workspace_service.known_problem_id(full_requested) is not None:
            suggestion = _next_available_problem_slug(owner, normalized)
            raise ValueError(f"problem already exists: {full_requested} (try: {_problem_full_slug(owner, suggestion)})")
        return full_requested

    base = _import_slug_base_from_package_name(owner, package_name)
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

def _detect_problem_package_format(package: ArchiveView) -> str:
    names = list(package.entries)
    if _is_package_marker(names, "problem.xml"):
        return "polygon"
    if _is_package_marker(names, "test_data/manifest.json"):
        return "polygon-replica"
    if _is_package_marker(names, "problem.yaml"):
        return "icpc"
    raise ValueError(
        "unsupported package format: expected problem.xml (Polygon), "
        "problem.yaml (ICPC), or test_data/manifest.json (Polygon Replica)"
    )


@contextmanager
def _open_problem_archive(
    package: Path | ArchiveView,
    policy: ProblemImportPolicy,
) -> Iterator[ArchiveView]:
    if isinstance(package, ArchiveView):
        yield package
        return
    try:
        with ArchiveView(Path(package), policy.archive) as archive:
            yield archive
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid zip package: {exc}") from exc


def _finalize_imported_problem(problem: str, actor_user: str, workspace: Path, package_format: str) -> str:
    commit_message = f"import {package_format} package"
    commit_head = runtime().git_service.commit(workspace, commit_message, actor_user, f"{actor_user}@polygonlike.local")
    runtime().git_service.push(workspace, "main")
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
    actor_user: str,
    target_problem: str,
    package_name: str,
    package: Path | ArchiveView,
    policy: ProblemImportPolicy,
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
    with _open_problem_archive(package, policy) as archive:
        package_format = _detect_problem_package_format(archive)
        target_workspace = Path(runtime().workspace_service.ensure_workspace(safe_target_problem, safe_actor_user, refresh_status=False))
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
                    archive,
                    normalize_test_data_newlines=bool(normalize_test_data_newlines),
                    text_limit_bytes=policy.text_limit_bytes,
                    statement_sample_max_bytes=policy.statement_sample_max_bytes,
                    problem_config_limits=problem_config_limits(
                        runtime().config_values
                    ),
                ),
            )
            with runtime().workspace_service.workspace_lock(target_workspace):
                _merge_imported_tree(staging_workspace, target_workspace)
            runtime().workspace_service.ensure_workspace(safe_target_problem, safe_actor_user, refresh_status=True)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    tests_info = result.get("tests")
    total_tests = int(cast(dict[str, object], tests_info).get("total", 0)) if tests_info is not None else 0
    return {
        "target_problem": safe_target_problem,
        "total_tests": total_tests,
        "result": result,
        "package_format": package_format,
    }

def import_package_as_new_problem(
    actor_user: str,
    package_name: str,
    package: Path | ArchiveView,
    policy: ProblemImportPolicy,
    requested_slug: str = "",
    normalize_test_data_newlines: bool = False,
) -> dict[str, object]:
    safe_actor_user = actor_user.strip()
    if not safe_actor_user:
        raise ValueError("actor user is required")
    safe_package_name = package_name.strip()
    if not safe_package_name:
        raise ValueError("package filename is required")
    target_problem = _resolve_import_problem_slug(safe_actor_user, requested_slug, safe_package_name)
    with _open_problem_archive(package, policy) as archive:
        package_format = _detect_problem_package_format(archive)
        target_bare = runtime().storage_layout.bare_repository(f"{target_problem}.git")
        existing_bare_head = _bare_repo_head_commit(target_bare)
        if existing_bare_head:
            raise ValueError(f"import target already has revision history: {target_problem}")
        created_problem = False
        try:
            runtime().workspace_service.ensure_problem(target_problem)
            created_problem = True
            runtime().workspace_service.grant_repo_access(target_problem, safe_actor_user, "owner")
            target_workspace = Path(runtime().workspace_service.ensure_workspace(target_problem, safe_actor_user))
            workspace_head = run_git(["git", "-C", str(target_workspace), "rev-parse", "--verify", "HEAD"])
            if workspace_head.returncode == 0 and workspace_head.stdout.strip():
                raise ValueError(f"import target already has revision history: {target_problem}")
            with runtime().workspace_service.workspace_lock(target_workspace):
                importer = _select_importer(package_format)
                result = cast(
                    ImportedPackageResult,
                    importer.import_package(
                        target_workspace,
                        safe_package_name,
                        archive,
                        normalize_test_data_newlines=bool(normalize_test_data_newlines),
                        text_limit_bytes=policy.text_limit_bytes,
                        statement_sample_max_bytes=policy.statement_sample_max_bytes,
                        problem_config_limits=problem_config_limits(
                            runtime().config_values
                        ),
                    ),
                )
            with runtime().workspace_service.workspace_lock(target_workspace):
                imported_commit = _finalize_imported_problem(target_problem, safe_actor_user, target_workspace, package_format)
            runtime().workspace_service.ensure_workspace(target_problem, safe_actor_user, refresh_status=True)
            result["commit"] = imported_commit
            tests_info = result.get("tests")
            total_tests = int(cast(dict[str, object], tests_info).get("total", 0)) if tests_info is not None else 0
            return {"target_problem": target_problem, "total_tests": total_tests, "result": result, "package_format": package_format}
        except Exception:
            if created_problem:
                try:
                    runtime().workspace_service.delete_problem(target_problem)
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
