from pathlib import Path

from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_operation import dedupe_preserve_order
from app.impl.workspace.context_verification import normalize_run_id_token
from app.main_util import normalize_optional_component_source_path_safe
from app.service.platform.git_process import run_git
from app.service.platform.process import is_canonical_artifact_id


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    count_value = max(0, int(count))
    token = singular if count_value == 1 else (plural if plural is not None else f"{singular}s")
    return f"{count_value} {token}"

def _verification_detail_available(
    *,
    problem_id: int,
    verification_id: str,
) -> bool:
    if (not verification_id) or (not is_canonical_artifact_id(verification_id)):
        return False
    return runtime().verification_service.has_export_detail_verification(
        int(problem_id),
        verification_id,
    )

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
    for source_rel in runtime().verification_service.verification_source_paths(verification_id):
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
    columns = detail_ctx.get("detail_columns")
    if not isinstance(columns, list):
        return False
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
