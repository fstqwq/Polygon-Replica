from time import monotonic

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.impl.auth.session import session_user
from app.impl.auth.shared import (
    apply_security_headers,
    enforce_same_origin_state_change,
    login_redirect,
    template_timing,
)


def _requires_browser_session(path: str) -> bool:
    return (
        path == "/"
        or path in {"/problems", "/contests", "/settings", "/admin", "/agent", "/logout"}
        or (path.startswith("/agent/") and not path.startswith("/agent/v1/"))
        or path.startswith(("/problems/", "/contests/", "/settings/", "/admin/", "/switch-", "/sudo"))
    )


def _authentication_response(request: Request) -> Response | None:
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
                elapsed_ms = max(0.0, (monotonic() - started) * 1000)
                timing = template_timing(request)
                metrics = (
                    f"application;dur={elapsed_ms:.3f}, "
                    f"template;dur={timing.elapsed_ms:.3f}, "
                    f"template_cpu;dur={timing.cpu_ms:.3f}"
                )
                previous = headers.get("Server-Timing")
                headers["Server-Timing"] = f"{previous}, {metrics}" if previous else metrics
                headers["X-Backend-Render-Ms"] = str(int(round(elapsed_ms)))
            await send(message)

        try:
            response = (
                await run_in_threadpool(_authentication_response, request)
                if _requires_browser_session(scope["path"])
                else None
            )
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
