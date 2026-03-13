from __future__ import annotations

from app.impl.runtime.config import config
from app.service.verification import load_verification_summary, verification_run_ids as verification_summary_run_ids

from .run_view_detail import build_run_detail_context
from .run_view_list import run_list_rows


def verification_record_run_ids(problem_id: int, workspace_id: int, verification_id: str) -> list[str]:
    summary = load_verification_summary(config.db, verification_id)
    if not isinstance(summary, dict):
        return []
    return verification_summary_run_ids(summary)


def verification_run_ids(problem_id: int, workspace_id: int, verification_id: str) -> list[str]:
    return verification_record_run_ids(problem_id, workspace_id, verification_id)
