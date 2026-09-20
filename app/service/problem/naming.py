"""Problem identifiers shared by individual and Contest package imports."""

import re

from app.main_constant import (
    PROBLEM_ID_MAX_LEN,
    PROBLEM_ID_RULE_MESSAGE,
    USER_IDENT_RE,
    USERNAME_RULE_MESSAGE,
)


PROBLEM_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def problem_slug_segment_max_len(owner: str) -> int:
    safe_owner = owner.strip().lower()
    if not USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(USERNAME_RULE_MESSAGE)
    return max(1, PROBLEM_ID_MAX_LEN - len(safe_owner) - 1)


def slugify_problem_id(raw: str, *, max_len: int) -> str:
    token = raw.strip().lower()
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token[:max_len].rstrip("-")


def normalize_problem_slug_segment_required(owner: str, raw: str) -> str:
    token = slugify_problem_id(raw, max_len=problem_slug_segment_max_len(owner))
    if not token or not PROBLEM_SEGMENT_RE.fullmatch(token):
        raise ValueError(PROBLEM_ID_RULE_MESSAGE)
    return token


def problem_full_slug(owner: str, slug_segment: str) -> str:
    safe_owner = owner.strip().lower()
    if not USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(USERNAME_RULE_MESSAGE)
    safe_segment = normalize_problem_slug_segment_required(safe_owner, slug_segment)
    full_slug = f"{safe_owner}/{safe_segment}"
    if len(full_slug) > PROBLEM_ID_MAX_LEN:
        raise ValueError(PROBLEM_ID_RULE_MESSAGE)
    return full_slug
