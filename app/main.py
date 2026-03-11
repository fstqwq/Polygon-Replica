from __future__ import annotations

from time import monotonic

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.impl.auth import public
from app.route import (
    build_route,
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
    public.startup()


@app.on_event("shutdown")
def shutdown() -> None:
    public.shutdown()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.request_started_at = monotonic()
    response = await public.auth_middleware(request, call_next)
    started = getattr(request.state, "request_started_at", None)
    if isinstance(started, (int, float)):
        elapsed_ms = max(0, int(round((monotonic() - started) * 1000)))
        response.headers.setdefault("X-Backend-Render-Ms", str(elapsed_ms))
    return response


app.include_router(root_auth_route.router)
app.include_router(contest_route.router)
app.include_router(problem_route.router)
app.include_router(build_route.router)
app.include_router(preview_route.router)
app.include_router(run_export_route.router)
app.include_router(judgehost_route.router)


