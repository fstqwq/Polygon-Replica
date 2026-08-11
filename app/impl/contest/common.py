from __future__ import annotations

import re

import app.main_constant as _K
from app.impl.runtime.config import config

_C = config.config_values


def _contest_idx_label(seq: int) -> str:
    value = max(1, int(seq))
    chars: list[str] = []
    while value > 0:
        value -= 1
        chars.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(chars))


def _normalize_contest_problem_idx_required(raw: object) -> str:
    token = str(raw or "").strip().upper()
    if not token:
        raise ValueError("problem index is required")
    if len(token) > 16:
        raise ValueError("problem index is too long")
    if not _K.CONTEST_IDENT_RE.fullmatch(token):
        raise ValueError("invalid problem index")
    return token


def _normalize_transferable_contest_member_role_required(raw: object) -> str:
    role = str(raw or "").strip().lower()
    if role in {"write", "read"}:
        return role
    if role == "owner":
        raise ValueError("owner access is fixed and cannot be transferred")
    raise ValueError("invalid role")


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


def _contest_problem_slug_file_token(problem_slug: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(problem_slug or "").strip()).strip("-")
    return token or "problem"
