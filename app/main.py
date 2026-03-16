from __future__ import annotations

from time import monotonic

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.impl.auth.api import auth_middleware as auth_http_middleware
from app.impl.auth.internal.runtime import shutdown as auth_shutdown, startup as auth_startup
from app.route import (
    tests_route,
    preview_route,
    contest_route,
    judgehost_route,
    problem_route,
    root_auth_route,
    run_export_route,
)

app = FastAPI(title="Polygonlike")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def startup() -> None:
    auth_startup()


@app.on_event("shutdown")
def shutdown() -> None:
    auth_shutdown()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.request_started_at = monotonic()
    response = await auth_http_middleware(request, call_next)
    started = getattr(request.state, "request_started_at", None)
    if started is not None:
        response.headers["X-Backend-Render-Ms"] = str(max(0, int(round((monotonic() - started) * 1000))))
    return response


app.include_router(root_auth_route.router)
app.include_router(contest_route.router)
app.include_router(problem_route.router)
app.include_router(tests_route.router)
app.include_router(preview_route.router)
app.include_router(run_export_route.router)
app.include_router(judgehost_route.router)


