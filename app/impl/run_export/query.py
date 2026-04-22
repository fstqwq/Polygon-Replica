from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import quote_plus

from app.impl.runtime.config import config
from app.impl.workspace.context_operation import dedupe_preserve_order
from app.impl.workspace.context_verification import normalize_run_id_token
from app.main_util import normalize_optional_component_source_path_safe
from app.service.platform.git_process import run_git
from app.service.platform.process import is_canonical_artifact_id
from app.service.verification.task_store import VerificationTaskStore


class RuntimeProgress(TypedDict):
    detail: str
    log_href: str


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    count_value = max(0, int(count))
    token = singular if count_value == 1 else (plural if plural is not None else f"{singular}s")
    return f"{count_value} {token}"

def _verification_tests_total(details: dict[str, object]) -> int:
    selected_test_names = cast(list[object], details.get("selected_test_names") or [])
    return len([token for token in selected_test_names if str(token or "")])

def _build_validated_count_from_log(validate_log: Path) -> int:
    try:
        if (not validate_log.exists()) or (not validate_log.is_file()) or validate_log.is_symlink():
            return 0
    except OSError:
        return 0
    seen: set[str] = set()
    try:
        with validate_log.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if ": " not in line:
                    continue
                test_name, _rest = line.split(": ", 1)
                token = test_name.strip()
                if not token.lower().endswith(".in"):
                    continue
                seen.add(token)
    except Exception:
        return 0
    return max(0, int(len(seen)))

def _verification_runtime_progress(
    *,
    problem_id: int,
    problem_slug: str,
    username: str,
    verification_id: str,
    event_status: str,
) -> RuntimeProgress:
    result: RuntimeProgress = {
        "detail": "",
        "log_href": "",
    }
    if (not verification_id) or (not is_canonical_artifact_id(verification_id)):
        return result
    verification_row = config.verification_service.export_runtime_verification(int(problem_id), verification_id)
    verification_status = ""
    verification_detail: dict[str, object] = {}
    if verification_row is not None:
        status_value = cast(str | None, verification_row["status"])
        if status_value is not None:
            verification_status = status_value
            verification_detail = dict(cast(dict[str, object], verification_row["details"]))
    artifact_root = None
    if verification_id:
        try:
            root = config.fs_manager.resolve_verification_root(verification_id).resolve()
            base = config.fs_manager.cache_artifacts_root.resolve()
            if (root == base or base in root.parents) and root.exists() and root.is_dir() and (not root.is_symlink()):
                artifact_root = root
        except Exception:
            artifact_root = None
    if artifact_root is None:
        if event_status == "running":
            if verification_status in {"queued", "pending"}:
                result["detail"] = "verification queued"
            elif verification_status == "running":
                result["detail"] = "verification running"
            elif verification_status == "ok":
                result["detail"] = "packaging export bundle"
        return result

    logs_dir = artifact_root / "logs"
    generate_log = logs_dir / "generate.log"
    validate_log = logs_dir / "validate.log"
    solve_log = logs_dir / "solve.log"
    failure_log = logs_dir / "failure.log"
    compile_log = logs_dir / "compile.log"
    tests_total = _verification_tests_total(verification_detail)
    verification_rows = VerificationTaskStore(config.db).list_rows(verification_id)
    outputs_generated = len(
        [
            row
            for row in verification_rows
            if str(row["task_kind"] or "") == "main-correct" and str(row["status"] or "") == VerificationTaskStore.TASK_DONE
        ]
    )
    validated_count = _build_validated_count_from_log(validate_log)

    def _log_href(name: str) -> str:
        return f"/problems/{problem_slug}/artifacts/{verification_id}/logs/{name}"

    if event_status == "running":
        if verification_status in {"queued", "pending"}:
            result["detail"] = "verification queued"
            return result
        if verification_status == "ok":
            result["detail"] = "packaging export bundle"
            return result
        sanity_status = str(verification_detail.get("sanity_status") or "")
        if sanity_status in {"pending", "running"}:
            result["detail"] = "sanity checks running"
            if validate_log.exists() and validate_log.is_file() and (not validate_log.is_symlink()):
                result["log_href"] = _log_href("validate.log")
            return result
        if solve_log.exists() and solve_log.is_file() and (not solve_log.is_symlink()):
            if tests_total > 0:
                completed_outputs = min(outputs_generated, tests_total)
                if completed_outputs >= tests_total:
                    result["detail"] = f"generated outputs {completed_outputs}/{tests_total}"
                else:
                    result["detail"] = f"generate outputs {completed_outputs}/{tests_total}"
            else:
                result["detail"] = "generate outputs running"
            result["log_href"] = _log_href("solve.log")
            return result
        if validate_log.exists() and validate_log.is_file() and (not validate_log.is_symlink()):
            if tests_total > 0:
                result["detail"] = f"validate inputs {min(validated_count, tests_total)}/{tests_total}"
            else:
                result["detail"] = "validate inputs running"
            result["log_href"] = _log_href("validate.log")
            return result
        if generate_log.exists() and generate_log.is_file() and (not generate_log.is_symlink()):
            if tests_total > 0:
                result["detail"] = f"generate inputs {tests_total} prepared"
            else:
                result["detail"] = "generate inputs running"
            result["log_href"] = _log_href("generate.log")
            return result
        if compile_log.exists() and compile_log.is_file() and (not compile_log.is_symlink()):
            result["detail"] = "compile running"
            result["log_href"] = _log_href("compile.log")
            return result
        result["detail"] = "verification running"
        return result

    if event_status == "failed":
        record = config.verification_service.verification_record(verification_id)
        detail = str((record or {}).get("fail_reason") or verification_detail.get("error") or "").strip()
        if not detail:
            failed_step = cast(str | None, verification_detail.get("failed_step"))
            if failed_step is None:
                failed_step = ""
            failed_test = cast(str | None, verification_detail.get("failed_test"))
            if failed_test is None:
                failed_test = ""
            if failed_step and failed_test:
                detail = f"{failed_step} failed on {failed_test}"
            elif failed_step:
                detail = f"{failed_step} failed"
        if detail:
            result["detail"] = detail
        if failure_log.exists() and failure_log.is_file() and (not failure_log.is_symlink()):
            result["log_href"] = _log_href("failure.log")
    if event_status == "failed":
        record = config.verification_service.verification_record(verification_id)
        detail = str((record or {}).get("fail_reason") or verification_detail.get("error") or "").strip()
        if detail:
            result["detail"] = detail
    return result

def _verification_href(
    *,
    problem_id: int,
    problem_slug: str,
    username: str,
    verification_id: str,
) -> str:
    if (not verification_id) or (not is_canonical_artifact_id(verification_id)):
        return ""
    if not config.verification_service.has_export_detail_verification(int(problem_id), verification_id):
        return ""
    return f"/problems/{problem_slug}/run/details?verification_id={quote_plus(verification_id)}"

def _rerun_solution_paths_from_verification(
    *,
    problem_id: int,
    workspace_id: int,
    actor_user_id: int,
    workspace: Path,
    verification_id: str,
) -> list[str]:
    verification_id = normalize_run_id_token(verification_id)
    if not verification_id:
        return []
    out: list[str] = []
    for source_rel in config.verification_service.verification_source_paths(verification_id):
        solution_path = normalize_optional_component_source_path_safe(
            source_rel,
            "solutions",
            "solution path",
        )
        if not solution_path:
            continue
        candidate = (workspace / solution_path).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except Exception:
            continue
        if candidate.exists() and candidate.is_file() and (not candidate.is_symlink()):
            out.append(solution_path)
    return dedupe_preserve_order(out)

def _run_detail_use_compact_layout(detail_ctx: dict[str, object]) -> bool:
    columns = detail_ctx.get("detail_columns") or []
    return len(columns) >= 11

def _bare_repo_head_commit(bare_repo: Path) -> str:
    try:
        if not bare_repo.exists():
            return ""
        if bare_repo.is_symlink() or (not bare_repo.is_dir()):
            raise ValueError("import target bare repository path is invalid")
    except OSError:
        raise ValueError("import target bare repository path is invalid")
    proc = run_git(["git", "-C", str(bare_repo), "rev-parse", "--verify", "HEAD"])
    return proc.stdout.strip() if proc.returncode == 0 else ""
