from __future__ import annotations

from fastapi import Form, HTTPException, Request

from app.impl.agent.shared import current_web_user
from app.impl.auth.shared import json_redirect_response, redirect_response, template_response
from app.impl.runtime.config import config


def _request_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def agent_sessions_page(request: Request):
    user = current_web_user(request)
    sessions = config.agent_service.list_user_sessions(user_id=int(user["user_id"]))
    return template_response(
        request,
        "agent_sessions.html",
        {
            "user": {"id": int(user["user_id"]), "username": str(user["username"] or "")},
            "active_main": "settings",
            "sessions": sessions,
        },
    )


def agent_connect(request: Request):
    user = current_web_user(request)
    payload = config.agent_service.create_registration_code(user_id=int(user["user_id"]))
    code = str(payload["code"])
    register_url = f"{_request_base_url(request)}/agent/v1/register/{code}"
    return json_redirect_response(
        "/agent/sessions",
        register_url=register_url,
        expires_at=payload["expires_at"],
        expires_in=payload["expires_in"],
    )


def agent_approve_page(request: Request, request_id: str):
    user = current_web_user(request)
    try:
        access_request = config.agent_service.access_request_for_user(actor_user_id=int(user["user_id"]), request_id=request_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return template_response(
        request,
        "agent_approve.html",
        {
            "user": {"id": int(user["user_id"]), "username": str(user["username"] or "")},
            "active_main": "settings",
            "access_request": access_request,
            "default_scope": "readonly",
            "default_ttl": "86400",
        },
    )


def agent_approve_submit(
    request: Request,
    request_id: str,
    decision: str = Form("approve"),
    scope: str = Form("readonly"),
    ttl: str = Form("86400"),
):
    user = current_web_user(request)
    try:
        result = config.agent_service.resolve_access_request(
            actor_user_id=int(user["user_id"]),
            request_id=request_id,
            decision=decision,
            scope=scope,
            ttl=ttl,
        )
        status = str(result.get("status") or "approved")
        if status == "denied":
            message = "agent access denied"
        else:
            message = "agent access approved"
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        message = str(exc)
    except ValueError as exc:
        message = str(exc)
    return redirect_response("/agent/sessions", status_code=303, message=message)


def agent_revoke_token(request: Request, token_id: str):
    user = current_web_user(request)
    try:
        config.agent_service.revoke_token(actor_user_id=int(user["user_id"]), token_id=token_id)
        return redirect_response("/agent/sessions", status_code=303, message="agent token revoked")
    except LookupError as exc:
        return redirect_response("/agent/sessions", status_code=303, message=str(exc))


def agent_disconnect_session(request: Request, session_id: str):
    user = current_web_user(request)
    try:
        config.agent_service.disconnect_session(actor_user_id=int(user["user_id"]), session_id=session_id)
        return redirect_response("/agent/sessions", status_code=303, message="agent disconnected")
    except LookupError as exc:
        return redirect_response("/agent/sessions", status_code=303, message=str(exc))
