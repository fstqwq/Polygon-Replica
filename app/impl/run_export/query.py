from __future__ import annotations

from app.impl.run_export.context import (
    Path,
    dedupe_preserve_order,
    normalize_optional_component_source_path_safe,
    normalize_run_id_token,
    config,
    is_canonical_artifact_id,
    json,
    os,
    quote_plus,
    run_cmd,
)
from app.impl.workspace.context_operation import parse_summary_json
from app.service.verification import load_verification_summary, verification_source_paths
def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{safe_count} {token}"

def _summary_object(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}

def _count_files_with_suffix(directory: Path, suffix: str) -> int:
    count = 0
    safe_suffix = str(suffix or "").lower()
    if not safe_suffix:
        return 0
    try:
        if (not directory.exists()) or (not directory.is_dir()) or directory.is_symlink():
            return 0
    except OSError:
        return 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = str(entry.name or "")
                if not name.lower().endswith(safe_suffix):
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
            payload = json.loads(logs_meta.read_text(encoding="utf-8", errors="replace"))
            if isinstance(payload, list):
                return max(0, int(len(payload)))
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
                line = str(raw or "").strip()
                if ": " not in line:
                    continue
                test_name, _rest = line.split(": ", 1)
                token = str(test_name or "").strip()
                if not token.lower().endswith(".in"):
                    continue
                seen.add(token)
    except Exception:
        return 0
    return max(0, int(len(seen)))

def _build_runtime_progress(
    *,
    problem_id: int,
    problem_slug: str,
    username: str,
    build_id: str,
    event_status: str,
) -> dict[str, str]:
    from app.impl.run_export.artifact import _build_artifact_root

    result = {
        "detail": "",
        "log_href": "",
    }
    safe_build_id = str(build_id or "").strip()
    if (not safe_build_id) or (not is_canonical_artifact_id(safe_build_id)):
        return result
    build_row = config.db.fetch_one(
        "SELECT status,summary_json FROM builds WHERE id=? AND problem_id=?",
        [safe_build_id, int(problem_id)],
    )
    build_status = str(build_row["status"] or "").strip().lower() if build_row is not None else ""
    build_summary = _summary_object(build_row["summary_json"] if build_row is not None else None)
    artifact_root = _build_artifact_root(int(problem_id), safe_build_id)
    if artifact_root is None:
        if event_status == "running":
            if build_status in {"queued", "pending"}:
                result["detail"] = "build queued"
            elif build_status == "running":
                result["detail"] = "build running"
            elif build_status == "ok":
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
        return f"/problems/{problem_slug}/{username}/artifacts/{safe_build_id}/logs/{name}"

    if event_status == "running":
        if build_status in {"queued", "pending"}:
            result["detail"] = "build queued"
            return result
        if build_status == "ok":
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
        result["detail"] = "build running"
        return result

    if event_status == "failed":
        detail = str(build_summary.get("error") or "").strip()
        if not detail:
            failed_step = str(build_summary.get("failed_step") or "").strip()
            failed_test = str(build_summary.get("failed_test") or "").strip()
            if failed_step and failed_test:
                detail = f"{failed_step} failed on {failed_test}"
            elif failed_step:
                detail = f"{failed_step} failed"
        if detail:
            result["detail"] = detail
        if failure_log.exists() and failure_log.is_file() and (not failure_log.is_symlink()):
            result["log_href"] = _log_href("failure.log")
    return result

def _verification_href_for_build(
    *,
    problem_id: int,
    problem_slug: str,
    username: str,
    build_id: str,
) -> str:
    safe_build_id = str(build_id or "").strip()
    if (not safe_build_id) or (not is_canonical_artifact_id(safe_build_id)):
        return ""
    verification_rows = config.db.fetch_all(
        """
        SELECT id,status
        FROM verifications
        WHERE problem_id=? AND build_id=?
        ORDER BY created_at DESC
        LIMIT 80
        """,
        [int(problem_id), safe_build_id],
    )
    for row in verification_rows:
        status_token = str(row["status"] or "").strip().lower()
        if status_token not in {"queued", "pending", "running", "ok", "failed"}:
            continue
        verification_id = normalize_run_id_token(row["id"])
        if verification_id:
            return f"/problems/{problem_slug}/{username}/run/details?verification_id={quote_plus(verification_id)}"
    return ""

def _detail_verification_id(detail_ctx: dict[str, object]) -> str:
    return normalize_run_id_token(detail_ctx.get("verification_id"))

def _rerun_solution_paths_from_verification(
    *,
    problem_id: int,
    workspace_id: int,
    actor_user_id: int,
    workspace: Path,
    verification_id: str,
) -> list[str]:
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return []
    verification_summary = load_verification_summary(config.db, safe_verification_id)
    if not isinstance(verification_summary, dict) or not verification_summary:
        return []
    out: list[str] = []
    for source_rel in verification_source_paths(verification_summary):
        safe_solution = normalize_optional_component_source_path_safe(
            source_rel,
            "solutions",
            "solution path",
        )
        if not safe_solution:
            continue
        candidate = (workspace / safe_solution).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except Exception:
            continue
        if candidate.exists() and candidate.is_file() and (not candidate.is_symlink()):
            out.append(safe_solution)
    return dedupe_preserve_order(out)

def _run_detail_use_compact_layout(detail_ctx: dict[str, object]) -> bool:
    columns = detail_ctx.get("detail_columns")
    if not isinstance(columns, list):
        return False
    column_count = len(columns)
    if column_count >= 12:
        return True
    if column_count <= 8:
        return False
    max_title_len = 0
    for col in columns:
        if not isinstance(col, dict):
            continue
        title = str(col.get("title") or col.get("source") or "").strip()
        if len(title) > max_title_len:
            max_title_len = len(title)
    return max_title_len >= 28

def _bare_repo_head_commit(bare_repo: Path) -> str:
    try:
        if not bare_repo.exists():
            return ""
        if bare_repo.is_symlink() or (not bare_repo.is_dir()):
            raise ValueError("import target bare repository path is invalid")
    except OSError:
        raise ValueError("import target bare repository path is invalid")
    proc = run_cmd(["git", "-C", str(bare_repo), "rev-parse", "--verify", "HEAD"])
    if proc.returncode != 0:
        return ""
    return str(proc.stdout or "").strip()

def _workspace_problem_mode(workspace: Path) -> str:
    cfg_path = workspace / "config" / "problem.json"
    try:
        if cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()):
            payload = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                mode = str(payload.get("mode") or "").strip().lower()
                if mode:
                    return mode
    except Exception:
        return "pass-fail"
    return "pass-fail"


