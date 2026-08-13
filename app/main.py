"""ASGI entry point for the Polygon Replica web application."""

from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

import app.impl.auth.middleware as auth_http
from app import runtime_lifecycle
from app.impl.auth.shared import (
    _apply_security_headers,
    install_template_filters,
)
from app.impl.runtime.dependency import bind_application
from app.route import (
    admin_route,
    tests_route,
    preview_route,
    contest_route,
    agent_route,
    judgehost_route,
    problem_route,
    root_auth_route,
    run_export_route,
    maintenance_route,
)
from app.service.platform.http_logging import install_uvicorn_access_filter
from app.runtime import ApplicationRuntime, build_runtime
from app.setting import Settings, load_settings


install_uvicorn_access_filter()


# ASGI middleware is a single-call protocol object by design.
class MaintenanceAdmissionMiddleware:  # pylint: disable=too-few-public-methods
    """Count admitted HTTP work until the complete ASGI response finishes."""

    def __init__(
        self,
        asgi_app: ASGIApp,
        application_runtime: ApplicationRuntime,
    ) -> None:
        self._asgi_app = asgi_app
        self._runtime = application_runtime

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._asgi_app(scope, receive, send)
            return
        schema_error = self._runtime.schema_error
        if schema_error is not None:
            response = PlainTextResponse(
                "Database schema upgrade required.\n"
                "The application runtime was not started.\n"
                f"{schema_error}\n"
                "Upgrade the database offline, then restart the application. "
                "No automatic schema changes were applied.\n",
                status_code=503,
                headers={
                    "Retry-After": "60",
                    "Cache-Control": "no-store",
                },
            )
            _apply_security_headers(response)
            await response(scope, receive, send)
            return
        admission = self._runtime.maintenance_admission_gate
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        if admission.is_exempt(path):
            await self._asgi_app(scope, receive, send)
            return
        counted = False
        if admission.is_drain_control(path, method):
            admitted, counted = admission.enter_control_request()
        else:
            admitted = admission.enter_request()
            counted = admitted
        if not admitted:
            response = PlainTextResponse(
                "The site is temporarily unavailable for maintenance. Retry shortly.\n",
                status_code=503,
                headers={
                    "Retry-After": "5",
                    "Cache-Control": "no-store",
                },
            )
            _apply_security_headers(response)
            await response(scope, receive, send)
            return
        try:
            await self._asgi_app(scope, receive, send)
        finally:
            if counted:
                admission.leave_request()


class RuntimeBindingMiddleware:  # pylint: disable=too-few-public-methods
    """Expose one installed application runtime for the complete ASGI request."""

    def __init__(self, asgi_app: ASGIApp, application: FastAPI) -> None:
        self._asgi_app = asgi_app
        self._application = application

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        with bind_application(self._application):
            await self._asgi_app(scope, receive, send)


async def favicon(request: Request):
    """Serve the browser favicon at the conventional root path."""
    application_runtime = request.app.state.runtime
    return FileResponse(
        application_runtime.STATIC_ROOT / "favicon.ico",
        media_type="image/x-icon",
    )


async def auth_middleware(request: Request, call_next):
    """Apply authentication and common response headers to every request."""

    request.state.request_started_at = monotonic()
    try:
        response = await auth_http.auth_middleware(request, call_next)
    except HTTPException as exc:
        response = PlainTextResponse(
            str(exc.detail or "request failed"),
            status_code=int(exc.status_code or 400),
        )
        _apply_security_headers(response)
    started = getattr(request.state, "request_started_at", None)
    if started is not None:
        elapsed_ms = max(0, int(round((monotonic() - started) * 1000)))
        response.headers["X-Backend-Render-Ms"] = str(elapsed_ms)
    return response


def create_app(application_runtime: ApplicationRuntime) -> FastAPI:
    """Create an ASGI application bound to one explicit runtime."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime_lifecycle.startup(application_runtime)
        try:
            yield
        finally:
            runtime_lifecycle.shutdown(application_runtime)

    install_template_filters(application_runtime.templates)
    application = FastAPI(title="Polygonlike", lifespan=lifespan)
    application.state.runtime = application_runtime
    application.mount(
        "/static",
        StaticFiles(directory=str(application_runtime.STATIC_ROOT)),
        name="static",
    )
    application.add_api_route(
        "/favicon.ico",
        favicon,
        include_in_schema=False,
    )
    application.middleware("http")(auth_middleware)
    application.add_middleware(
        MaintenanceAdmissionMiddleware,
        application_runtime=application_runtime,
    )
    application.add_middleware(RuntimeBindingMiddleware, application=application)
    for router in (
        root_auth_route.router,
        admin_route.router,
        contest_route.router,
        agent_route.router,
        problem_route.router,
        tests_route.router,
        preview_route.router,
        run_export_route.router,
        judgehost_route.router,
        maintenance_route.router,
    ):
        application.include_router(router)
    return application


settings: Settings = load_settings()
runtime: ApplicationRuntime = build_runtime(settings)
app: FastAPI = create_app(runtime)
