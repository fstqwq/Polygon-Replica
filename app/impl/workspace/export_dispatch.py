from __future__ import annotations


def export_workspace_key(problem_id: int, workspace_id: int, head_commit: str, export_type: str) -> str:
    return f"{int(problem_id)}:{int(workspace_id)}:{str(head_commit or '').strip()}:{str(export_type or '').strip().lower()}"


