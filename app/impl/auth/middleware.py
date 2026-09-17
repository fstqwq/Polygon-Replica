from time import monotonic

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.impl.auth.session import session_user
from app.impl.auth.shared import (
    apply_security_headers,
    enforce_same_origin_state_change,
    login_redirect,
)


def _authentication_response(request: Request) -> Response | None:
    path = request.scope["path"]
    protected = (
        path == "/"
        or path in {"/problems", "/contests", "/settings", "/admin", "/agent", "/logout"}
        or (path.startswith("/agent/") and not path.startswith("/agent/v1/"))
        or path.startswith(("/problems/", "/contests/", "/settings/", "/admin/", "/switch-", "/sudo"))
    )
    if not protected:
        return None
    if not session_user(request):
        return login_redirect(request)
    enforce_same_origin_state_change(request)
    return None


# ASGI middleware is a single-call protocol object by design.
class AuthenticationMiddleware:  # pylint: disable=too-few-public-methods
    """Authorize HTTP requests and add headers at the ASGI response boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        started = request.state.request_started_at = monotonic()
        response_started = False

        async def send_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                apply_security_headers(headers)
                headers["X-Backend-Render-Ms"] = str(
                    max(0, int(round((monotonic() - started) * 1000)))
                )
            await send(message)

        try:
            response = _authentication_response(request)
            if response is None:
                await self._app(scope, receive, send_response)
            else:
                await response(scope, receive, send_response)
        except HTTPException as exc:
            if response_started:
                raise
            response = PlainTextResponse(
                str(exc.detail or "request failed"),
                status_code=int(exc.status_code or 400),
            )
            await response(scope, receive, send_response)
