from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, Form

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_operation import audit
from app.impl.workspace.context_ui import page_ctx


def _workspace_redirect_href(problem: str, selected_path: str = "") -> str:
    base = f"/problems/{problem}/workspace"
    if not selected_path:
        return base
    return f"{base}?{urlencode({'path': selected_path})}"


def revision_commit(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    message: Annotated[str, Form()],
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx["workspace"]["path"])
    commit_head = ""
    try:
        with config.workspace_service.workspace_lock(workspace):
            if config.workspace_merge_service.published_revision_advanced(workspace):
                raise RuntimeError("a newer published revision is available; update the workspace before publishing")
            commit_head = config.git_service.commit(
                workspace, message, user, f"{user}@polygon-replica.local"
            )
            try:
                config.git_service.push(workspace, "main")
            except Exception as push_exc:
                try:
                    config.git_service.rollback_last_commit(workspace, expected_head=commit_head)
                except Exception as rollback_exc:
                    raise RuntimeError(f"{push_exc}; commit rollback failed: {rollback_exc}") from rollback_exc
                raise
            config.workspace_merge_service.clear_undo(workspace)
        audit(
            int(ctx["user"]["id"]),
            int(ctx["problem"]["id"]),
            "revision.commit",
            {"message": message, "head": commit_head},
        )
        msg = "revision published"
    except Exception as exc:
        err = str(exc)
        if any(token in err.lower() for token in ("non-fast-forward", "fetch first", "rejected")):
            msg = "a newer published revision is available; update the workspace before publishing"
        else:
            msg = err
    return redirect_response(f"/problems/{problem}/workspace", message=msg)


def git_discard_path(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()] = "",
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx["workspace"]["path"])
    selected_path = path.strip()
    next_path = selected_path
    msg = "discarded file changes"
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.discard_path(workspace, selected_path)
            changes = config.git_service.status_change_summary(workspace)
        rows = changes["rows"]
        link_paths = [str(row["link_path"]) for row in rows if row["link_path"]]
        if selected_path not in link_paths:
            next_path = link_paths[0] if link_paths else ""
        audit(
            int(ctx["user"]["id"]),
            int(ctx["problem"]["id"]),
            "workspace.discard_path",
            {"path": selected_path},
        )
    except Exception as exc:
        msg = str(exc)
    return redirect_response(_workspace_redirect_href(problem, next_path), message=msg)
