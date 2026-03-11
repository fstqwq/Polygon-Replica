from __future__ import annotations

from fastapi import HTTPException

from app.impl.runtime.config import config

_C = config.constants


def count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{safe_count} {token}"


def _default_user_problem_selector(user_id: int, *, limit: int = 1) -> list[dict[str, object]]:
    uid = int(user_id)
    cap = max(1, int(limit))
    rows = config.db.fetch_all(
        """
        SELECT p.slug
        FROM repo_acl a
        JOIN problems p ON p.id=a.problem_id
        LEFT JOIN workspaces w ON w.problem_id=p.id AND w.user_id=?
        WHERE a.user_id=?
        ORDER BY COALESCE(NULLIF(w.updated_at, ''), p.created_at) DESC, p.slug ASC
        LIMIT ?
        """,
        [uid, uid, cap],
    )
    out: list[dict[str, object]] = []
    for row in rows:
        slug = str(row["slug"] or "").strip()
        if slug:
            out.append({"slug": slug})
    return out


def default_problem_slug_for_user(
    username: str,
    *,
    user_ident_re=None,
    user_problem_selector=None,
) -> str:
    if user_ident_re is None:
        user_ident_re = _C.USER_IDENT_RE
    if user_problem_selector is None:
        user_problem_selector = _default_user_problem_selector
    safe_user = str(username or "").strip()
    if not user_ident_re.fullmatch(safe_user):
        return ""
    row = config.db.fetch_one("SELECT id FROM users WHERE username=?", [safe_user])
    if row is None:
        return ""
    items = user_problem_selector(int(row["id"]), limit=1)
    if items:
        return str(items[0]["slug"])
    return ""


def global_user_ctx(
    username: str,
    *,
    user_ident_re=None,
    username_rule_message: str | None = None,
    default_problem_selector=None,
) -> dict:
    if user_ident_re is None:
        user_ident_re = _C.USER_IDENT_RE
    if username_rule_message is None:
        username_rule_message = str(_C.USERNAME_RULE_MESSAGE)
    selector = default_problem_selector
    if selector is None:
        selector = lambda token: default_problem_slug_for_user(
            token,
            user_ident_re=user_ident_re,
            user_problem_selector=_default_user_problem_selector,
        )
    safe_user = str(username or "").strip()
    if not user_ident_re.fullmatch(safe_user):
        raise HTTPException(status_code=400, detail=username_rule_message)
    row = config.db.fetch_one("SELECT id,username FROM users WHERE username=?", [safe_user])
    if row is None:
        try:
            ensured = config.workspace_service.ensure_user(safe_user)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        row = {"id": ensured["id"], "username": ensured["username"]}
    return {
        "user": {"id": int(row["id"]), "username": str(row["username"])},
        "default_problem": selector(safe_user),
    }


