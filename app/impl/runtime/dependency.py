"""Request-bound access to the application composition object."""

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, cast

from fastapi import FastAPI, Request

from app.runtime import ApplicationRuntime

_application: ContextVar[FastAPI | None] = ContextVar(
    "polygon_replica_application",
    default=None,
)


def runtime_from_request(request: Request) -> ApplicationRuntime:
    """Return the exact runtime installed on the request's application."""

    return cast(ApplicationRuntime, request.app.state.runtime)


def runtime() -> ApplicationRuntime:
    """Return the runtime of the currently executing HTTP request."""

    application = _application.get()
    if application is None:
        raise RuntimeError("application runtime is only available during a request")
    return cast(ApplicationRuntime, application.state.runtime)


@contextmanager
def bind_application(application: FastAPI) -> Iterator[None]:
    """Bind an ASGI application for the duration of one request."""

    token: Token[FastAPI | None] = _application.set(application)
    try:
        yield
    finally:
        _application.reset(token)
