import app.main_constant as _K

from fastapi import HTTPException

from app.impl.runtime.dependency import runtime



def count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{safe_count} {token}"


def _default_user_problem_selector(user_id: int, *, limit: int = 1) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for slug in runtime().workspace_service.accessible_problem_slugs(int(user_id), limit=max(1, int(limit))):
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
        user_ident_re = _K.USER_IDENT_RE
    safe_user = str(username or "").strip()
    if not user_ident_re.fullmatch(safe_user):
        return ""
    if user_problem_selector is None:
        return runtime().workspace_service.default_problem_slug_for_username(safe_user)
    user_id = runtime().workspace_service.known_user_id(safe_user)
    if user_id is None:
        return ""
    items = user_problem_selector(int(user_id), limit=1)
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
        user_ident_re = _K.USER_IDENT_RE
    if username_rule_message is None:
        username_rule_message = str(_K.USERNAME_RULE_MESSAGE)
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
    try:
        row = runtime().workspace_service.global_user_context(safe_user)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "user": {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "is_system_admin": int(row["is_system_admin"]),
        },
        "default_problem": selector(safe_user),
    }
