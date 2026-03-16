from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import quote_plus

from app.impl.runtime.config import config
from app.impl.workspace.context_operation import dedupe_preserve_order
from app.impl.workspace.context_verification import normalize_run_id_token
from app.main_util import normalize_optional_component_source_path_safe
from app.service.verification.store import load_verification_summary, verification_source_paths
from app.service.platform.process import is_canonical_artifact_id, run_cmd


class RuntimeProgress(TypedDict):
    detail: str
    log_href: str


class VerificationDetailSummary(TypedDict, total=False):
    error: str
    failed_step: str
    failed_test: str


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    count_value = max(0, int(count))
    token = singular if count_value == 1 else (plural if plural is not None else f"{singular}s")
    return f"{count_value} {token}"

def _summary_object(raw: str | None) -> dict[str, object]:
    if raw is None:
        return {}
    text = raw.strip()
    if not text:
        return {}
    try:
        return cast(dict[str, object], json.loads(text))
    except Exception:
        return {}

def _count_files_with_suffix(directory: Path, suffix: str) -> int:
    count = 0
    suffix_token = suffix.lower()
    if not suffix_token:
        return 0
    try:
        if (not directory.exists()) or (not directory.is_dir()) or directory.is_symlink():
            return 0
    except OSError:
        return 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if not name.lower().endswith(suffix_token):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                count += 1
    except Exception:
        return 0
    return count

def _build_tests_total_from_artifacts(artifact_root: Path) -> int:
    logs_meta = artifact_root / "logs" / "tests_meta.json"
    try:
        if logs_meta.exists() and logs_meta.is_file() and (not logs_meta.is_symlink()):
            payload = cast(list[object], json.loads(logs_meta.read_text(encoding="utf-8", errors="replace")))
            return max(0, len(payload))
    except Exception:
        pass
    tests_dir = artifact_root / "tests"
    return _count_files_with_suffix(tests_dir, ".in")

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
    verification_row = config.db.fetch_one(
        "SELECT status,summary_json,artifact_path FROM verifications WHERE id=? AND problem_id=?",
        [verification_id, int(problem_id)],
    )
    verification_status = ""
    verification_summary: VerificationDetailSummary = {}
    artifact_path = ""
    if verification_row is not None:
        status_value = cast(str | None, verification_row["status"])
        if status_value is not None:
            verification_status = status_value
        verification_summary = cast(VerificationDetailSummary, _summary_object(cast(str | None, verification_row["summary_json"])))
        artifact_path_value = cast(str | None, verification_row["artifact_path"])
        if artifact_path_value is not None:
            artifact_path = artifact_path_value
    artifact_root = None
    if artifact_path:
        try:
            root = Path(artifact_path).resolve()
            base = config.settings.artifacts_root.resolve()
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
    tests_total = _build_tests_total_from_artifacts(artifact_root)
    outputs_generated = _count_files_with_suffix(artifact_root / "ans", ".ans")
    validated_count = _build_validated_count_from_log(validate_log)

    def _log_href(name: str) -> str:
        return f"/problems/{problem_slug}/{username}/artifacts/{verification_id}/logs/{name}"

    if event_status == "running":
        if verification_status in {"queued", "pending"}:
            result["detail"] = "verification queued"
            return result
        if verification_status == "ok":
            result["detail"] = "packaging export bundle"
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
        detail = verification_summary.get("error")
        if detail is None:
            detail = ""
        else:
            detail = detail.strip()
        if not detail:
            failed_step = verification_summary.get("failed_step")
            if failed_step is None:
                failed_step = ""
            failed_test = verification_summary.get("failed_test")
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
    row = config.db.fetch_one(
        "SELECT id,status FROM verifications WHERE id=? AND problem_id=?",
        [verification_id, int(problem_id)],
    )
    if row is None:
        return ""
    status_token = row["status"]
    if status_token not in {"queued", "pending", "running", "ok", "failed"}:
        return ""
    return f"/problems/{problem_slug}/{username}/run/details?verification_id={quote_plus(verification_id)}"

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
    verification_summary = load_verification_summary(config.db, verification_id)
    if not verification_summary:
        return []
    out: list[str] = []
    for source_rel in verification_source_paths(verification_summary):
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
    return len(columns) >= 12

def _bare_repo_head_commit(bare_repo: Path) -> str:
    try:
        if not bare_repo.exists():
            return ""
        if bare_repo.is_symlink() or (not bare_repo.is_dir()):
            raise ValueError("import target bare repository path is invalid")
    except OSError:
        raise ValueError("import target bare repository path is invalid")
    proc = run_cmd(["git", "-C", str(bare_repo), "rev-parse", "--verify", "HEAD"])
    return proc.stdout.strip() if proc.returncode == 0 else ""

def _workspace_problem_mode(workspace: Path) -> str:
    cfg_path = workspace / "config" / "problem.json"
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8")).get("mode", "pass-fail").strip()
    except Exception:
        pass
    return "pass-fail"
