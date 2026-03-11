from __future__ import annotations


def verification_workspace_key(problem_id: int, workspace_id: int) -> str:
    return f'{int(problem_id)}:{int(workspace_id)}'


