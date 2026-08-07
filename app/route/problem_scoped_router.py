from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, Depends, params
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import Response

from app.impl.contest.workspace_scope import (
    apply_problem_contest_scope,
    contest_workspace_scope_from_request,
    resolve_problem_contest_scope,
)


_PROBLEM_ROUTE_PREFIX = "/problems/{problem:path}/"


class ProblemScopedRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Any]:
        original_handler = super().get_route_handler()

        async def scoped_handler(request: Request) -> Response:
            response = await original_handler(request)
            return apply_problem_contest_scope(
                response,
                contest_workspace_scope_from_request(request),
            )

        return scoped_handler


class ProblemScopedRouter(APIRouter):
    def __init__(self) -> None:
        super().__init__(route_class=ProblemScopedRoute)

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        dependencies: Sequence[params.Depends] | None = None,
        **kwargs: Any,
    ) -> None:
        route_dependencies = list(dependencies or [])
        if path.startswith(_PROBLEM_ROUTE_PREFIX):
            route_dependencies.append(Depends(resolve_problem_contest_scope))
        super().add_api_route(
            path,
            endpoint,
            dependencies=route_dependencies,
            **kwargs,
        )
