"""ASGI entry point for the Polygon Replica web application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import app.impl.auth.internal.runtime as auth_runtime
import app.impl.auth.middleware as auth_http
from app.impl.auth.shared import _apply_security_headers
from app.route import (
    tests_route,
    preview_route,
    contest_route,
    agent_route,
    judgehost_route,
    problem_route,
    root_auth_route,
    run_export_route,
)
from app.service.platform.http_logging import install_uvicorn_access_filter


install_uvicorn_access_filter()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start and stop process-wide runtime helpers."""

    auth_runtime.startup()
    try:
        yield
    finally:
        auth_runtime.shutdown()


app = FastAPI(title="Polygonlike", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the browser favicon at the conventional root path."""
    return FileResponse("app/static/favicon.ico", media_type="image/x-icon")


@app.middleware("http")
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


app.include_router(root_auth_route.router)
app.include_router(contest_route.router)
app.include_router(agent_route.router)
app.include_router(problem_route.router)
app.include_router(tests_route.router)
app.include_router(preview_route.router)
app.include_router(run_export_route.router)
app.include_router(judgehost_route.router)
