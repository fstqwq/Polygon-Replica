import app.main_constant as _K

import secrets
from typing import Annotated

from fastapi import Depends, Form, Request
from app.impl.auth.csrf import issue_password_form_csrf_token, verify_password_form_csrf_token
from app.impl.auth.password_envelope import password_envelope_store
from app.impl.auth.session import (
    create_session_for_user,
    require_session_user,
    revoke_sudo_sessions_for_user,
)
from app.impl.auth.shared import (
    dummy_password_salt_hex,
    lookup_user_auth,
    normalize_password_iters,
    normalize_password_salt_hex,
    redirect_response,
    set_user_password_verifier,
    template_response,
)
from app.impl.problem.shared import _settings_user_ctx
from app.impl.runtime.dependency import runtime
from app.main_util import form_text
from app.service.auth.password_hash import password_verifier_storage_hash




def settings_page(
    request: Request,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = _settings_user_ctx(user)
    user_row = dict(ctx["user"])
    auth_row = lookup_user_auth(str(user_row["username"]))
    current_salt = str(auth_row["password_salt"] or "").strip().lower() if auth_row is not None else ""
    try:
        current_iters = (
            int(auth_row["password_iters"] or 0)
            if auth_row is not None
            else runtime().config_values.integer("PASSWORD_HASH_ITERS")
        )
    except (TypeError, ValueError):
        current_iters = runtime().config_values.integer("PASSWORD_HASH_ITERS")
    if not _K.HEX_32_RE.fullmatch(current_salt):
        current_salt = dummy_password_salt_hex(str(user_row["username"]))
    if current_iters <= 0:
        current_iters = runtime().config_values.integer("PASSWORD_HASH_ITERS")
    return template_response(
        request,
        "settings.html",
        {
            "user": user_row,
            "active_main": "settings",
            "password_csrf_token": issue_password_form_csrf_token("settings-password"),
            "current_password_salt": current_salt,
            "current_password_iters": current_iters,
            "new_password_salt": secrets.token_hex(16),
            "new_password_iters": runtime().config_values.integer(
                "PASSWORD_HASH_ITERS"
            ),
        },
    )


def settings_password_update(
    user: Annotated[str, Depends(require_session_user)],
    current_password: str = Form(""),
    new_password: str = Form(""),
    current_password_key_id: str = Form(""),
    current_password_envelope_token: str = Form(""),
    current_password_encrypted_verifier: str = Form(""),
    new_password_key_id: str = Form(""),
    new_password_envelope_token: str = Form(""),
    new_password_encrypted_verifier: str = Form(""),
    csrf_token: str = Form(""),
    new_password_salt: str = Form(""),
    new_password_iters: str = Form(""),
):
    row = lookup_user_auth(user)
    message = "password updated"
    if row is None:
        return redirect_response("/settings", status_code=303, message="user not found")
    try:
        password_csrf = form_text(csrf_token).strip()
        if not verify_password_form_csrf_token(password_csrf, "settings-password"):
            raise ValueError("invalid password token")
        stored_hash = str(row["password_hash"] or "").strip().lower()
        if not _K.HEX_64_RE.fullmatch(stored_hash):
            raise ValueError("current password is incorrect")
        try:
            current_verifier = password_envelope_store.consume(
                scope="settings-password",
                purpose="settings-current",
                username=user,
                csrf_token=password_csrf,
                key_id=form_text(current_password_key_id),
                envelope_token=form_text(current_password_envelope_token),
                encrypted_verifier=form_text(current_password_encrypted_verifier),
            )
        except ValueError as exc:
            raise ValueError("current password is incorrect") from exc
        expected_current_hash = password_verifier_storage_hash(current_verifier)
        if not secrets.compare_digest(expected_current_hash, stored_hash):
            raise ValueError("current password is incorrect")
        try:
            new_verifier = password_envelope_store.consume(
                scope="settings-password",
                purpose="settings-new",
                username=user,
                csrf_token=password_csrf,
                key_id=form_text(new_password_key_id),
                envelope_token=form_text(new_password_envelope_token),
                encrypted_verifier=form_text(new_password_encrypted_verifier),
            )
        except ValueError as exc:
            raise ValueError("invalid new password envelope") from exc
        new_salt = normalize_password_salt_hex(form_text(new_password_salt))
        new_iters = normalize_password_iters(form_text(new_password_iters))
        if new_iters != runtime().config_values.integer("PASSWORD_HASH_ITERS"):
            raise ValueError("invalid password iterations")
        set_user_password_verifier(int(row["id"]), new_verifier, new_salt, new_iters)
        runtime().auth_service.revoke_auth_sessions_for_user(int(row["id"]))
        revoke_sudo_sessions_for_user(int(row["id"]))
        token = create_session_for_user(int(row["id"]))
        response = redirect_response("/settings", status_code=303, message=message)
        response.set_cookie(
            runtime().config_values.text("AUTH_COOKIE_NAME"),
            token,
            httponly=True,
            samesite="lax",
            secure=runtime().config_values.boolean("AUTH_COOKIE_SECURE"),
            max_age=runtime().config_values.integer("AUTH_COOKIE_MAX_AGE"),
            path="/",
        )
        return response
    except ValueError as exc:
        message = str(exc)
    return redirect_response("/settings", status_code=303, message=message)
