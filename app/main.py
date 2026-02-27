from __future__ import annotations

from time import monotonic

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.impl.auth import (
    auth_middleware as auth_http_middleware,
    shutdown as app_shutdown,
    startup as app_startup,
)
from app.routes.build_preview_routes import router as build_preview_router
from app.routes.problem_editor_routes import router as problem_editor_router
from app.routes.root_auth_routes import router as root_auth_router
from app.routes.run_export_routes import router as run_export_router

app = FastAPI(title="Polygonlike")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def startup() -> None:
    app_startup()


@app.on_event("shutdown")
def shutdown() -> None:
    app_shutdown()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.request_started_at = monotonic()
    response = await auth_http_middleware(request, call_next)
    started = getattr(request.state, "request_started_at", None)
    if isinstance(started, (int, float)):
        elapsed_ms = max(0, int(round((monotonic() - started) * 1000)))
        response.headers.setdefault("X-Backend-Render-Ms", str(elapsed_ms))
    return response


app.include_router(root_auth_router)
app.include_router(problem_editor_router)
app.include_router(build_preview_router)
app.include_router(run_export_router)
__all__ = ["app"]
