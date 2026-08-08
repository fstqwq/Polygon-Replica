from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict, cast
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from fastapi import Depends, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.impl.auth.session import require_session_user
from app.impl.runtime.config import config


ProblemSection = Literal[
    "statement",
    "checker",
    "interactor",
    "validator",
    "generators",
    "solutions",
    "tests",
    "run",
    "export",
    "access",
    "files",
    "history",
    "workspace",
]

ContestWorkspaceBlockReason = Literal[
    "problem_access_denied",
    "section_access_denied",
]


class ContestWorkspaceProblem(TypedDict):
    contest_problem_id: int
    ordinal: int
    idx: str
    problem_id: int
    problem_slug: str
    active: bool
    can_open: bool
    block_reason: ContestWorkspaceBlockReason | None
    href: str | None


class ContestWorkspaceContext(TypedDict):
    contest_id: int
    contest_slug: str
    contest_title: str
    contest_href: str
    problem_count: int
    active_contest_problem_id: int
    active_idx: str
    section: ProblemSection
    exit_href: str
    problems: list[ContestWorkspaceProblem]


@dataclass(frozen=True)
class ContestWorkspaceScope:
    contest_id: int
    contest_slug: str
    problem_slugs: frozenset[str]
    context: ContestWorkspaceContext


@dataclass(frozen=True)
class ProblemHrefBuilder:
    request: Request
    problem_slug: str
    contest_slug: str | None

    def __call__(
        self,
        route_name: str,
        *,
        query: Mapping[str, str | int] | None = None,
        fragment: str = "",
        **path_params: str | int,
    ) -> str:
        if "problem" in path_params:
            raise ValueError("problem path parameter is managed by the builder")
        encoded_path = _encoded_route_path(
            self.request,
            route_name=route_name,
            path_params={
                "problem": self.problem_slug,
                **{key: str(value) for key, value in path_params.items()},
            },
        )
        if not unquote(encoded_path).startswith("/problems/"):
            raise ValueError("problem URL builder only accepts Problem routes")

        query_items: list[tuple[str, str]] = []
        for key, value in (query or {}).items():
            if key == "contest":
                raise ValueError("contest query parameter is managed by the builder")
            query_items.append((key, str(value)))
        if self.contest_slug is not None:
            query_items.append(("contest", self.contest_slug))
        query_string = urlencode(query_items)
        encoded_fragment = quote(fragment, safe="-._~") if fragment else ""
        return urlunsplit(("", "", encoded_path, query_string, encoded_fragment))


_PROBLEM_ROUTE_PREFIX = "/problems/{problem:path}/"
_PROBLEM_SECTION_ROUTE_NAMES: dict[ProblemSection, str] = {
    "statement": "problem_statement",
    "checker": "problem_checker",
    "interactor": "problem_interactor",
    "validator": "problem_validator",
    "generators": "problem_generators",
    "solutions": "problem_solutions",
    "tests": "problem_tests",
    "run": "problem_run",
    "export": "problem_export",
    "access": "problem_access",
    "files": "problem_files",
    "history": "problem_history",
    "workspace": "problem_workspace",
}
_CANONICAL_SECTIONS = frozenset(_PROBLEM_SECTION_ROUTE_NAMES)
_CONTEST_SCOPE_STATE_KEY = "contest_workspace_scope"


def problem_section_for_route(route_path: str) -> ProblemSection:
    if not route_path.startswith(_PROBLEM_ROUTE_PREFIX):
        return "statement"
    tail = route_path.removeprefix(_PROBLEM_ROUTE_PREFIX)
    segment = tail.partition("/")[0]
    if segment in _CANONICAL_SECTIONS:
        return cast(ProblemSection, segment)
    if segment == "preview":
        return "statement"
    if segment in {"artifacts", "verification"}:
        return "run"
    if segment == "exports":
        return "export"
    if segment == "revision":
        return "history"
    if segment in {"git", "merge", "problem"}:
        return "workspace"
    return "statement"


def problem_page_target_for_route(route_path: str) -> str:
    if not route_path.startswith(_PROBLEM_ROUTE_PREFIX):
        return "statement"
    tail = route_path.removeprefix(_PROBLEM_ROUTE_PREFIX)
    segment = tail.partition("/")[0]
    if segment == "preview":
        return "preview"
    return problem_section_for_route(route_path)


def _encoded_route_path(
    request: Request,
    *,
    route_name: str,
    path_params: dict[str, str],
) -> str:
    raw_path = str(request.app.url_path_for(route_name, **path_params))
    return quote(raw_path, safe="/:@-._~")


def build_contest_problem_href(
    request: Request,
    *,
    problem_slug: str,
    contest_slug: str,
    section: ProblemSection,
) -> str:
    return ProblemHrefBuilder(
        request=request,
        problem_slug=problem_slug,
        contest_slug=contest_slug,
    )(_PROBLEM_SECTION_ROUTE_NAMES[section])


def build_problem_exit_href(
    request: Request,
    *,
    problem_slug: str,
    section: ProblemSection,
) -> str:
    return _encoded_route_path(
        request,
        route_name=_PROBLEM_SECTION_ROUTE_NAMES[section],
        path_params={"problem": problem_slug},
    )


def problem_href_builder(request: Request, problem_slug: str) -> ProblemHrefBuilder:
    scope = contest_workspace_scope_from_request(request)
    return ProblemHrefBuilder(
        request=request,
        problem_slug=problem_slug,
        contest_slug=None if scope is None else scope.contest_slug,
    )


def problem_template_navigation(
    request: Request,
    problem_slug: str,
) -> dict[str, object]:
    route = request.scope.get("route")
    route_path = str(getattr(route, "path", ""))
    if not route_path.startswith(_PROBLEM_ROUTE_PREFIX):
        concrete_path = unquote(request.url.path)
        concrete_prefix = f"/problems/{problem_slug}/"
        if concrete_path.startswith(concrete_prefix):
            route_path = (
                _PROBLEM_ROUTE_PREFIX
                + concrete_path.removeprefix(concrete_prefix)
            )
    return {
        "problem_href": problem_href_builder(request, problem_slug),
        "problem_section": problem_section_for_route(route_path),
        "problem_page_target": problem_page_target_for_route(route_path),
    }


def add_contest_problem_hrefs(
    request: Request,
    *,
    contest_slug: str,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["href"] = (
            build_contest_problem_href(
                request,
                problem_slug=str(row["problem_slug"]),
                contest_slug=contest_slug,
                section="statement",
            )
            if bool(row["can_problem_read"])
            else None
        )
        result.append(item)
    return result


def _normalize_contest_query(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("invalid contest query parameter")
    if not config.constants.CONTEST_IDENT_RE.fullmatch(value):
        raise ValueError("invalid contest query parameter")
    return value


def _contest_overview_href(request: Request, contest_slug: str) -> str:
    return _encoded_route_path(
        request,
        route_name="contest_overview",
        path_params={"contest": contest_slug},
    )


def resolve_problem_contest_scope(
    request: Request,
    problem: str,
    user: str = Depends(require_session_user),
) -> ContestWorkspaceScope | None:
    contest_values = request.query_params.getlist("contest")
    if not contest_values:
        setattr(request.state, _CONTEST_SCOPE_STATE_KEY, None)
        return None
    if len(contest_values) != 1:
        raise HTTPException(status_code=400, detail="contest must be specified once")
    try:
        contest_slug = _normalize_contest_query(contest_values[0])
        problem_id, user_id = config.workspace_service.page_identity(problem, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    contest = config.contest_service.contest_context(contest_slug)
    if contest is None:
        raise HTTPException(status_code=404, detail="contest not found")
    contest_id = int(contest["id"])
    contest_access = config.contest_service.access_context(contest_id, user_id)
    if not contest_access["can_read"]:
        raise HTTPException(
            status_code=403,
            detail=contest_access["read_block_reason"] or "contest access required",
        )

    contest_problems = config.contest_service.contest_problems(contest_id)
    active = next(
        (row for row in contest_problems if int(row["problem_id"]) == problem_id),
        None,
    )
    if active is None:
        raise HTTPException(status_code=404, detail="problem is not part of this contest")

    problem_ids = [int(row["problem_id"]) for row in contest_problems]
    access_by_problem = config.workspace_service.access_contexts(problem_ids, user_id)
    active_access = access_by_problem[problem_id]
    if not bool(active_access["can_read"]):
        raise HTTPException(
            status_code=403,
            detail=str(active_access["read_block_reason"]),
        )

    route = request.scope.get("route")
    route_path = str(getattr(route, "path", request.url.path))
    section = problem_section_for_route(route_path)
    problem_rows: list[ContestWorkspaceProblem] = []
    for ordinal, row in enumerate(contest_problems, start=1):
        row_problem_id = int(row["problem_id"])
        row_problem_slug = str(row["problem_slug"])
        can_open = bool(access_by_problem[row_problem_id]["can_read"])
        problem_rows.append(
            {
                "contest_problem_id": int(row["contest_problem_id"]),
                "ordinal": ordinal,
                "idx": str(row["idx"]),
                "problem_id": row_problem_id,
                "problem_slug": row_problem_slug,
                "active": row_problem_id == problem_id,
                "can_open": can_open,
                "block_reason": None if can_open else "problem_access_denied",
                "href": (
                    build_contest_problem_href(
                        request,
                        problem_slug=row_problem_slug,
                        contest_slug=contest_slug,
                        section=section,
                    )
                    if can_open
                    else None
                ),
            }
        )

    context: ContestWorkspaceContext = {
        "contest_id": contest_id,
        "contest_slug": contest_slug,
        "contest_title": str(contest["title"]),
        "contest_href": _contest_overview_href(request, contest_slug),
        "problem_count": len(problem_rows),
        "active_contest_problem_id": int(active["contest_problem_id"]),
        "active_idx": str(active["idx"]),
        "section": section,
        "exit_href": build_problem_exit_href(
            request,
            problem_slug=problem,
            section=section,
        ),
        "problems": problem_rows,
    }
    scope = ContestWorkspaceScope(
        contest_id=contest_id,
        contest_slug=contest_slug,
        problem_slugs=frozenset(str(row["problem_slug"]) for row in contest_problems),
        context=context,
    )
    setattr(request.state, _CONTEST_SCOPE_STATE_KEY, scope)
    return scope


def contest_workspace_scope_from_request(request: Request) -> ContestWorkspaceScope | None:
    return cast(
        ContestWorkspaceScope | None,
        getattr(request.state, _CONTEST_SCOPE_STATE_KEY, None),
    )


def contest_workspace_context_from_request(
    request: Request,
) -> ContestWorkspaceContext | None:
    scope = contest_workspace_scope_from_request(request)
    return None if scope is None else scope.context


def _problem_redirect_with_scope(url: str, scope: ContestWorkspaceScope) -> str:
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        return url
    decoded_path = unquote(parsed.path)
    targets_contest_problem = any(
        decoded_path.startswith(f"/problems/{problem_slug}/")
        for problem_slug in scope.problem_slugs
    )
    if not targets_contest_problem:
        return url
    query = parse_qsl(parsed.query, keep_blank_values=True)
    existing = [value for key, value in query if key == "contest"]
    if existing:
        if existing == [scope.contest_slug]:
            return url
        raise RuntimeError("problem redirect contains a conflicting contest scope")
    query.append(("contest", scope.contest_slug))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def apply_problem_contest_scope(
    response: Response,
    scope: ContestWorkspaceScope | None,
) -> Response:
    if scope is None:
        return response
    if isinstance(response, RedirectResponse):
        location = response.headers.get("location")
        if location:
            response.headers["location"] = _problem_redirect_with_scope(location, scope)
        return response
    if not isinstance(response, JSONResponse):
        return response
    try:
        payload = json.loads(response.body)
    except (TypeError, ValueError):
        return response
    if not isinstance(payload, dict) or not isinstance(payload.get("redirect"), str):
        return response
    payload["redirect"] = _problem_redirect_with_scope(payload["redirect"], scope)
    response.body = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    response.headers["content-length"] = str(len(response.body))
    return response
