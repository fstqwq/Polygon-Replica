from __future__ import annotations
from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated

from fastapi import Request, Depends

from app.impl.auth.shared import template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_ui import page_ctx

_C = config.constants


def history_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    commits: list[dict] = []
    message = ''
    selected_revision = request.query_params.get('revision', '')
    selected_commit = ''
    selected_subject = ''
    selected_diff = ''
    selected_diff_truncated = False
    selected_diff_lines: list[dict[str, str]] = []
    try:
        commits = config.git_service.history(workspace, limit=_C.WORKSPACE_HISTORY_LIMIT)
        revision_top = int(ctx['workspace_version']) if ctx.get('workspace_version') is not None else None
        for idx, row in enumerate(commits):
            if revision_top is None:
                row['version'] = None
            else:
                row['version'] = max(1, revision_top - idx)
        if selected_revision:
            selected_row = next((row for row in commits if row.get('commit') == selected_revision), None)
            if selected_row is None:
                raise ValueError('selected revision is not in visible history')
            selected_commit = selected_row['commit']
            selected_subject = selected_row['subject']
            selected_diff, selected_diff_truncated = config.git_service.diff_for_revision(workspace, selected_commit)
            for line in selected_diff.splitlines():
                if (
                    line.startswith('diff --git ')
                    or line.startswith('index ')
                    or line.startswith('new file mode ')
                    or line.startswith('deleted file mode ')
                    or line.startswith('--- ')
                    or line.startswith('+++ ')
                ):
                    continue
                kind = 'ctx'
                if line.startswith('@@'):
                    kind = 'hunk'
                elif line.startswith('+'):
                    kind = 'add'
                elif line.startswith('-'):
                    kind = 'del'
                selected_diff_lines.append({'text': line, 'kind': kind})
    except Exception as exc:
        if not message:
            message = str(exc)
    return template_response(
        request,
        'history.html',
        {
            'ctx': ctx,
            'commits': commits,
            'message': message,
            'selected_commit': selected_commit,
            'selected_subject': selected_subject,
            'selected_diff': selected_diff,
            'selected_diff_truncated': bool(selected_diff_truncated),
            'selected_diff_lines': selected_diff_lines,
            'diff_char_limit': int(config.git_service.DIFF_MAX_CHARS),
        },
    )



