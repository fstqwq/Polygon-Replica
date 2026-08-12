from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Annotated, TypedDict, cast

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context import count_label
from app.impl.workspace.context_ui import page_ctx
from app.service.importing.upload import spool_fileobj
from app.service.importing.archive import ArchiveView, problem_archive_policy

_C = config.config_values
_REVISION_TOKEN_RE = re.compile(r"v[1-9][0-9]*")


class RevisionHistoryRow(TypedDict):
    commit: str
    short: str
    author: str
    date: str
    subject: str
    version: int | None


def _revision_history_rows(ctx: dict[str, object]) -> tuple[Path, list[RevisionHistoryRow]]:
    workspace_context = cast(dict[str, object], ctx["workspace"])
    workspace = Path(cast(str, workspace_context["path"]))
    raw_rows = config.git_service.history(workspace, limit=_C.WORKSPACE_HISTORY_LIMIT)
    revision_top = cast(int | None, ctx.get("workspace_version"))
    rows: list[RevisionHistoryRow] = []
    for index, raw in enumerate(raw_rows):
        version = None if revision_top is None else revision_top - index
        rows.append(
            {
                "commit": cast(str, raw["commit"]),
                "short": cast(str, raw["short"]),
                "author": cast(str, raw["author"]),
                "date": cast(str, raw["date"]),
                "subject": cast(str, raw["subject"]),
                "version": version if version is not None and version > 0 else None,
            }
        )
    return workspace, rows


def _selected_revision(
    rows: list[RevisionHistoryRow],
    revision: str,
) -> RevisionHistoryRow:
    if not _REVISION_TOKEN_RE.fullmatch(revision):
        raise ValueError("selected revision is not in visible history")
    selected = next(
        (row for row in rows if row["version"] is not None and f"v{row['version']}" == revision),
        None,
    )
    if selected is None:
        raise ValueError("selected revision is not in visible history")
    return selected


def history_page(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    commits: list[RevisionHistoryRow] = []
    message = ""
    selected_revision = request.query_params.get("revision", "")
    selected_commit = ""
    selected_subject = ""
    selected_diff = ""
    selected_diff_truncated = False
    selected_diff_lines: list[dict[str, str]] = []
    try:
        workspace, commits = _revision_history_rows(ctx)
        if selected_revision:
            selected_row = _selected_revision(commits, selected_revision)
            selected_commit = selected_row["commit"]
            selected_subject = selected_row["subject"]
            selected_diff, selected_diff_truncated = config.git_service.diff_for_revision(
                workspace,
                selected_commit,
            )
            for line in selected_diff.splitlines():
                if (
                    line.startswith("diff --git ")
                    or line.startswith("index ")
                    or line.startswith("new file mode ")
                    or line.startswith("deleted file mode ")
                    or line.startswith("--- ")
                    or line.startswith("+++ ")
                ):
                    continue
                kind = "ctx"
                if line.startswith("@@"):
                    kind = "hunk"
                elif line.startswith("+"):
                    kind = "add"
                elif line.startswith("-"):
                    kind = "del"
                selected_diff_lines.append({"text": line, "kind": kind})
    except Exception as exc:
        message = str(exc)
    return template_response(
        request,
        "history.html",
        {
            "ctx": ctx,
            "commits": commits,
            "message": message,
            "selected_commit": selected_commit,
            "selected_subject": selected_subject,
            "selected_diff": selected_diff,
            "selected_diff_truncated": bool(selected_diff_truncated),
            "selected_diff_lines": selected_diff_lines,
            "diff_char_limit": int(config.git_service.DIFF_MAX_CHARS),
        },
    )


def history_snapshot(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    revision: str = Form(""),
):
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        include_recent=False,
        include_workspace_changes=False,
    )
    source_commit: str | None = None
    revision_number: int | None = None
    if revision:
        try:
            _workspace, rows = _revision_history_rows(ctx)
            selected = _selected_revision(rows, revision)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        source_commit = selected["commit"]
        revision_number = selected["version"]
    workspace_context = cast(dict[str, object], ctx["workspace"])
    archive = config.export_service.create_workspace_snapshot(
        problem,
        workspace_id=cast(int, workspace_context["id"]),
        source_commit=source_commit,
        revision_number=revision_number,
    )
    archive_root = archive.parent
    return FileResponse(
        archive,
        filename=archive.name,
        media_type="application/zip",
        background=BackgroundTask(lambda: shutil.rmtree(archive_root, ignore_errors=True)),
    )


def history_import(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    package_upload: UploadFile | None = File(None),
):
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=False,
    )
    require_write_access(ctx)
    try:
        if package_upload is None:
            raise ValueError("archive file is required")
        package_name = (package_upload.filename or "").strip()
        if not package_name:
            raise ValueError("archive filename is required")
        workspace_context = cast(dict[str, object], ctx["workspace"])
        workspace = Path(cast(str, workspace_context["path"]))
        snapshot = _C.snapshot()
        with spool_fileobj(
            package_upload.file,
            root=config.settings.cache_root / "archive-uploads",
            max_bytes=int(snapshot["UPLOAD_MAX_BYTES"]),
            label="archive file",
        ) as package_path:
            with ArchiveView(
                package_path,
                problem_archive_policy(
                    int(snapshot["PROBLEM_ZIP_MAX_EXPANDED_BYTES"])
                ),
            ) as package:
                rooted = package.rooted_at("config/problem.json")

                def apply_snapshot():
                    return config.workspace_archive_service.merge_zip(
                        workspace,
                        rooted,
                    )

                applied = config.workspace_mutation_service.write_locked(
                    workspace,
                    apply_snapshot,
                )
        message = (
            f"snapshot restored into your workspace "
            f"({count_label(len(applied.value.uploads), 'changed file')})"
        )
        return redirect_response(
            f"/problems/{problem}/workspace",
            status_code=303,
            message=message,
        )
    except ValueError as exc:
        message = str(exc)
    except Exception as exc:
        message = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return redirect_response(
        f"/problems/{problem}/history",
        status_code=303,
        message=message,
    )
