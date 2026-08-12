from __future__ import annotations

import app.main_constant as _K


def _normalize_contest_problem_idx_required(raw: object) -> str:
    token = str(raw or "").strip().upper()
    if not token:
        raise ValueError("problem index is required")
    if len(token) > 16:
        raise ValueError("problem index is too long")
    if not _K.CONTEST_IDENT_RE.fullmatch(token):
        raise ValueError("invalid problem index")
    return token


def _dedupe_preserve(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result
