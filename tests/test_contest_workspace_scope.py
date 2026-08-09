from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.impl.contest.overview import contest_overview_page
from app.impl.contest.workspace_scope import (
    ContestWorkspaceScope,
    ProblemHrefBuilder,
    apply_problem_contest_scope,
    build_contest_problem_href,
    problem_section_for_route,
    resolve_problem_contest_scope,
)
from app.route.problem_scoped_router import ProblemScopedRoute
from tests.contest_support import ContestActionBase
from tests.ui_support import AUTH_COOKIE_NAME, config, workspace_service


class _HtmlElements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append(
            (tag, {key: value or "" for key, value in attrs})
        )

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)


def _app_request(
    path: str,
    *,
    query: str = "",
    route_path: str = "",
) -> Request:
    from app.main import app

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("testserver", 80),
            "root_path": "",
            "app": app,
            "route": SimpleNamespace(path=route_path or path),
        }
    )


class TestContestWorkspaceScope(ContestActionBase):
    def _add_default_problem(
        self,
        contest_id: int,
        actor_user_id: int,
        *,
        idx: str = "A",
    ) -> int:
        problem_id = workspace_service.known_problem_id("alice/sample")
        self.assertIsNotNone(problem_id)
        config.contest_service.add_problem(
            contest_id,
            idx,
            int(problem_id),
            actor_user_id,
        )
        return int(problem_id)

    def _session_cookie(self, username: str) -> str:
        user_id = workspace_service.known_user_id(username)
        self.assertIsNotNone(user_id)
        token = config.auth_service.create_session_for_user(int(user_id))
        return f"{AUTH_COOKIE_NAME}={token}"

    def _resolve(
        self,
        problem: str,
        contest_slug: str,
        *,
        route_path: str = "/problems/{problem:path}/statement",
        user: str = "alice",
    ) -> ContestWorkspaceScope:
        request = _app_request(
            f"/problems/{problem}/statement",
            query=urlencode([("contest", contest_slug)]),
            route_path=route_path,
        )
        scope = resolve_problem_contest_scope(request, problem, user)
        self.assertIsNotNone(scope)
        return scope

    def test_missing_scope_has_no_contest_or_problem_lookup(self) -> None:
        request = _app_request(
            "/problems/alice/sample/statement",
            route_path="/problems/{problem:path}/statement",
        )
        with (
            patch.object(
                config.contest_service,
                "contest_context",
                side_effect=AssertionError("contest lookup is forbidden"),
            ),
            patch.object(
                config.workspace_service,
                "page_identity",
                side_effect=AssertionError("problem lookup is forbidden"),
            ),
        ):
            self.assertIsNone(
                resolve_problem_contest_scope(request, "alice/sample", "alice")
            )

    def test_scope_uses_db_order_batch_acl_and_never_provisions_peers(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("scope")
        active_problem_id = self._add_default_problem(
            contest_id,
            actor_user_id,
            idx="C",
        )
        _peer_contest_problem_id, peer_problem_id, peer_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "scope-peer",
        )
        locked_slug = f"alice/scope-locked-{self.test_id}"
        workspace_service.ensure_problem(locked_slug)
        locked_problem_id = workspace_service.known_problem_id(locked_slug)
        self.assertIsNotNone(locked_problem_id)
        config.contest_service.add_problem(
            contest_id,
            "BB",
            int(locked_problem_id),
            actor_user_id,
        )

        original_batch = config.workspace_service.access_contexts
        request = _app_request(
            "/problems/alice/sample/checker/view-standard",
            query=urlencode([("contest", contest_slug)]),
            route_path="/problems/{problem:path}/checker/view-standard",
        )
        with (
            patch.object(
                config.workspace_service,
                "access_context",
                side_effect=AssertionError("per-problem ACL lookup is forbidden"),
            ),
            patch.object(
                config.workspace_service,
                "ensure_workspace",
                side_effect=AssertionError("workspace provisioning is forbidden"),
            ),
            patch.object(
                config.workspace_service,
                "access_contexts",
                side_effect=original_batch,
            ) as batch_access,
        ):
            scope = resolve_problem_contest_scope(
                request,
                "alice/sample",
                "alice",
            )

        self.assertIsNotNone(scope)
        context = scope.context
        batch_access.assert_called_once()
        self.assertEqual(
            batch_access.call_args.args[0],
            [active_problem_id, peer_problem_id, int(locked_problem_id)],
        )
        self.assertEqual(context["problem_count"], 3)
        self.assertEqual([row["idx"] for row in context["problems"]], ["C", "A", "BB"])
        self.assertEqual(context["active_idx"], "C")
        self.assertEqual(context["section"], "checker")
        self.assertEqual(context["exit_href"], "/problems/alice/sample/checker")
        self.assertEqual(
            context["contest_href"],
            f"/contests/{contest_slug}/overview",
        )
        peer = context["problems"][1]
        self.assertEqual(peer["problem_slug"], peer_slug)
        self.assertEqual(
            peer["href"],
            f"/problems/{peer_slug}/checker?contest={contest_slug}",
        )
        contest_derived = context["problems"][2]
        self.assertTrue(contest_derived["can_open"])
        self.assertIsNone(contest_derived["block_reason"])
        self.assertEqual(
            contest_derived["href"],
            f"/problems/{locked_slug}/checker?contest={contest_slug}",
        )

    def test_url_builder_encodes_path_and_query_exactly_once(self) -> None:
        request = _app_request("/")
        problem_slug = "owner/题 +&?#%25"
        contest_slug = "夏 +&?#%25"
        href = build_contest_problem_href(
            request,
            problem_slug=problem_slug,
            contest_slug=contest_slug,
            section="checker",
        )
        parsed = urlsplit(href)
        self.assertEqual(unquote(parsed.path), f"/problems/{problem_slug}/checker")
        self.assertEqual(parse_qs(parsed.query), {"contest": [contest_slug]})
        self.assertEqual(parsed.query.count("contest="), 1)
        self.assertNotIn(" ", href)
        self.assertIn("%2525", href)

        scoped_builder = ProblemHrefBuilder(
            request=request,
            problem_slug=problem_slug,
            contest_slug=contest_slug,
        )
        detail_href = scoped_builder(
            "files_download",
            query={"path": "notes/a +&?#%.txt", "line": 7},
            fragment="selected row",
        )
        detail = urlsplit(detail_href)
        self.assertEqual(
            unquote(detail.path),
            f"/problems/{problem_slug}/files/download",
        )
        self.assertEqual(
            parse_qs(detail.query),
            {
                "contest": [contest_slug],
                "line": ["7"],
                "path": ["notes/a +&?#%.txt"],
            },
        )
        self.assertEqual(unquote(detail.fragment), "selected row")
        self.assertEqual(detail.query.count("contest="), 1)
        with self.assertRaisesRegex(ValueError, "managed by the builder"):
            scoped_builder(
                "problem_files",
                query={"contest": "different"},
            )

    def test_detail_section_mapping_and_unknown_fallback(self) -> None:
        expectations = {
            "/problems/{problem:path}/preview/status": "statement",
            "/problems/{problem:path}/verification/start": "run",
            "/problems/{problem:path}/artifacts/{verification_id}/{rel_path:path}": "run",
            "/problems/{problem:path}/solutions/editor": "solutions",
            "/problems/{problem:path}/files/download": "files",
            "/problems/{problem:path}/exports/{export_id}/{filename}": "export",
            "/problems/{problem:path}/merge/{preview_id}": "workspace",
            "/problems/{problem:path}/unknown/internal": "statement",
        }
        for route_path, expected in expectations.items():
            with self.subTest(route_path=route_path):
                self.assertEqual(problem_section_for_route(route_path), expected)

    def test_scope_http_errors_are_distinct(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("errors")
        self._add_default_problem(contest_id, actor_user_id)
        empty_contest_slug, _empty_contest_id, _actor = self.create_contest("empty")

        bob = self.random_id("bob")
        workspace_service.ensure_user(bob)
        workspace_service.grant_repo_access("alice/sample", bob, "read")
        bob_cookie = self._session_cookie(bob)

        carol = self.random_id("carol")
        workspace_service.ensure_user(carol)
        config.contest_service.grant_member_role(contest_id, carol, "read")
        carol_cookie = self._session_cookie(carol)
        alice_cookie = self._session_cookie("alice")

        from app.main import app

        cases = [
            ("?contest=", alice_cookie, 400),
            (f"?contest={contest_slug}&contest={contest_slug}", alice_cookie, 400),
            ("?contest=%20bad", alice_cookie, 400),
            ("?contest=missing-contest", alice_cookie, 404),
            (f"?contest={empty_contest_slug}", alice_cookie, 404),
            (f"?contest={contest_slug}", bob_cookie, 403),
            (f"?contest={contest_slug}", carol_cookie, 200),
        ]
        with TestClient(app) as client:
            for query, cookie, expected in cases:
                with self.subTest(query=query, expected=expected):
                    response = client.get(
                        f"/problems/alice/sample/statement{query}",
                        headers={"cookie": cookie},
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, expected, response.text)

    def test_post_validates_before_mutation_and_preserves_scope_in_redirect(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("post")
        self._add_default_problem(contest_id, actor_user_id)
        workspace = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        config_path = workspace / "config/problem.json"
        before = config_path.read_bytes()
        cookie = self._session_cookie("alice")
        payload = {
            "time_limit_ms": "2345",
            "memory_limit_mb": "768",
            "mode": "pass-fail",
            "pass_limit": "1",
            "language": "english",
        }

        from app.main import app

        with TestClient(app, base_url="https://testserver") as client:
            rejected = client.post(
                "/problems/alice/sample/statement/save?contest=missing-contest",
                data=payload,
                headers={
                    "cookie": cookie,
                    "origin": "https://testserver",
                },
                follow_redirects=False,
            )
            self.assertEqual(rejected.status_code, 404, rejected.text)
            self.assertEqual(config_path.read_bytes(), before)

            accepted = client.post(
                f"/problems/alice/sample/statement/save?contest={contest_slug}",
                data=payload,
                headers={
                    "cookie": cookie,
                    "origin": "https://testserver",
                },
                follow_redirects=False,
            )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        location = urlsplit(accepted.headers["location"])
        self.assertEqual(location.path, "/problems/alice/sample/statement")
        self.assertEqual(
            parse_qs(location.query),
            {"contest": [contest_slug], "language": ["english"]},
        )
        self.assertNotEqual(config_path.read_bytes(), before)

    def test_page_context_and_contest_overview_expose_final_hrefs(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("context")
        self._add_default_problem(contest_id, actor_user_id)
        locked_slug = f"alice/context-locked-{self.test_id}"
        workspace_service.ensure_problem(locked_slug)
        locked_problem_id = workspace_service.known_problem_id(locked_slug)
        self.assertIsNotNone(locked_problem_id)
        config.contest_service.add_problem(
            contest_id,
            "B",
            int(locked_problem_id),
            actor_user_id,
        )
        cookie = self._session_cookie("alice")

        def page_payload(_request: Request, _template: str, context: dict) -> JSONResponse:
            return JSONResponse(context["ctx"]["contest_workspace"])

        from app.main import app

        with (
            patch("app.impl.preview.preview.template_response", side_effect=page_payload),
            TestClient(app) as client,
        ):
            response = client.get(
                f"/problems/alice/sample/statement?contest={contest_slug}",
                headers={"cookie": cookie},
                follow_redirects=False,
            )
            unscoped_response = client.get(
                "/problems/alice/sample/statement",
                headers={"cookie": cookie},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 200, response.text)
        context = response.json()
        self.assertEqual(context["section"], "statement")
        self.assertEqual(
            context["problems"][0]["href"],
            f"/problems/alice/sample/statement?contest={contest_slug}",
        )
        self.assertEqual(unscoped_response.status_code, 200, unscoped_response.text)
        self.assertIsNone(unscoped_response.json())

        def overview_payload(
            _request: Request,
            _template: str,
            payload: dict,
        ) -> JSONResponse:
            return JSONResponse({"problem_rows": payload["problem_rows"]})

        request = _app_request(
            f"/contests/{contest_slug}/overview",
            route_path="/contests/{contest}/overview",
        )
        with patch(
            "app.impl.contest.overview.template_response",
            side_effect=overview_payload,
        ):
            overview = contest_overview_page(request, contest_slug, "alice")
        rows = json.loads(overview.body)["problem_rows"]
        self.assertEqual(
            rows[0]["href"],
            f"/problems/alice/sample/statement?contest={contest_slug}",
        )
        self.assertEqual(rows[1]["problem_slug"], locked_slug)
        self.assertTrue(rows[1]["can_problem_read"])
        self.assertEqual(
            rows[1]["href"],
            f"/problems/{locked_slug}/statement?contest={contest_slug}",
        )

    def test_scoped_problem_html_renders_contest_navigation_and_scoped_urls(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("ui")
        self._add_default_problem(contest_id, actor_user_id, idx="A")
        _contest_problem_id, _problem_id, peer_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "BB",
            "ui-peer",
        )
        locked_slug = f"alice/ui-locked-{self.test_id}"
        workspace_service.ensure_problem(locked_slug)
        locked_problem_id = workspace_service.known_problem_id(locked_slug)
        self.assertIsNotNone(locked_problem_id)
        config.contest_service.add_problem(
            contest_id,
            "CCC",
            int(locked_problem_id),
            actor_user_id,
        )
        cookie = self._session_cookie("alice")

        from app.main import app

        with TestClient(app) as client:
            scoped = client.get(
                f"/problems/alice/sample/statement?contest={contest_slug}",
                headers={"cookie": cookie},
            )
            unscoped = client.get(
                "/problems/alice/sample/statement",
                headers={"cookie": cookie},
            )

        self.assertEqual(scoped.status_code, 200, scoped.text)
        document = _HtmlElements()
        document.feed(scoped.text)

        contest_main = next(
            attrs
            for tag, attrs in document.elements
            if tag == "a" and attrs.get("data-main") == "contests"
        )
        problems_main = next(
            attrs
            for tag, attrs in document.elements
            if tag == "a" and attrs.get("data-main") == "problems"
        )
        self.assertEqual(contest_main.get("aria-current"), "page")
        self.assertIn("active", contest_main.get("class", "").split())
        self.assertNotIn("active", problems_main.get("class", "").split())

        cards = [
            attrs
            for tag, attrs in document.elements
            if tag == "section"
            and "contest-workspace-card" in attrs.get("class", "").split()
        ]
        self.assertEqual(len(cards), 1)
        self.assertTrue(
            any(
                attrs.get("href") == f"/contests/{contest_slug}/overview"
                for tag, attrs in document.elements
                if tag == "a"
            )
        )

        controls = [
            (tag, attrs)
            for tag, attrs in document.elements
            if "contest-problem-link" in attrs.get("class", "").split()
        ]
        self.assertEqual(len(controls), 3)
        active = next(
            attrs
            for _tag, attrs in controls
            if "active" in attrs.get("class", "").split()
        )
        self.assertEqual(active.get("aria-current"), "page")
        self.assertEqual(
            parse_qs(urlsplit(active["href"]).query),
            {"contest": [contest_slug]},
        )
        peer = next(
            attrs
            for tag, attrs in controls
            if tag == "a" and peer_slug in attrs.get("title", "")
        )
        self.assertEqual(
            peer["href"],
            f"/problems/{peer_slug}/statement?contest={contest_slug}",
        )
        locked = next(
            attrs
            for tag, attrs in controls
            if tag == "a" and locked_slug in attrs.get("title", "")
        )
        self.assertEqual(
            locked["href"],
            f"/problems/{locked_slug}/statement?contest={contest_slug}",
        )
        self.assertFalse(
            any(attrs.get("aria-disabled") == "true" for _tag, attrs in controls)
        )

        url_attributes = {
            "href",
            "action",
            "formaction",
            "src",
            "data-run-details-fragment",
            "data-compare-url",
        }
        for _tag, attrs in document.elements:
            for attribute in url_attributes:
                value = attrs.get(attribute, "")
                if not value.startswith("/problems/"):
                    continue
                with self.subTest(attribute=attribute, value=value):
                    self.assertEqual(
                        parse_qs(urlsplit(value).query).get("contest"),
                        [contest_slug],
                    )

        self.assertEqual(unscoped.status_code, 200, unscoped.text)
        self.assertNotIn("contest-workspace-card", unscoped.text)
        unscoped_document = _HtmlElements()
        unscoped_document.feed(unscoped.text)
        unscoped_problems_main = next(
            attrs
            for tag, attrs in unscoped_document.elements
            if tag == "a" and attrs.get("data-main") == "problems"
        )
        self.assertEqual(unscoped_problems_main.get("aria-current"), "page")

    def test_problem_templates_do_not_construct_problem_urls(self) -> None:
        template_root = Path("app/template")
        route_names: set[str] = set()
        for template_path in template_root.glob("*.html"):
            source = template_path.read_text(encoding="utf-8")
            route_names.update(re.findall(r"problem_href\('([^']+)'", source))
            with self.subTest(template=template_path.name):
                self.assertNotIn("/problems/{{ ctx.problem.slug }}", source)
                self.assertNotIn('"/problems/" ~ ctx.problem.slug', source)

        from app.main import app

        registered_names = {
            route.name
            for route in app.routes
            if isinstance(route, APIRoute)
        }
        self.assertTrue(route_names)
        self.assertEqual(route_names - registered_names, set())

    def test_json_redirect_and_route_contract(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("contract")
        self._add_default_problem(contest_id, actor_user_id)
        scope = self._resolve("alice/sample", contest_slug)
        response = JSONResponse(
            {
                "ok": True,
                "redirect": "/problems/alice/sample/solutions/editor?path=a%2Bb.cpp",
            }
        )
        scoped_response = apply_problem_contest_scope(response, scope)
        payload = json.loads(scoped_response.body)
        redirect = urlsplit(payload["redirect"])
        self.assertEqual(redirect.path, "/problems/alice/sample/solutions/editor")
        self.assertEqual(
            parse_qs(redirect.query),
            {"contest": [contest_slug], "path": ["a+b.cpp"]},
        )

        from app.main import app

        problem_routes = [
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and route.path.startswith("/problems/{problem:path}/")
        ]
        self.assertTrue(problem_routes)
        self.assertFalse(
            any(
                route.path.startswith("/contests/")
                and "/problems/" in route.path
                and route.path.endswith("/open")
                for route in app.routes
            )
        )
        self.assertNotIn(
            "/contests/{contest}/readiness",
            {route.path for route in app.routes},
        )
        for route in problem_routes:
            with self.subTest(path=route.path):
                self.assertIsInstance(route, ProblemScopedRoute)
                self.assertTrue(
                    any(
                        dependency.call is resolve_problem_contest_scope
                        for dependency in route.dependant.dependencies
                    ),
                    f"contest scope dependency missing: {route.path}",
                )
        expected_names = {
            "problem_statement",
            "problem_checker",
            "problem_interactor",
            "problem_validator",
            "problem_generators",
            "problem_solutions",
            "problem_tests",
            "problem_run",
            "problem_export",
            "problem_access",
            "problem_files",
            "problem_history",
            "problem_workspace",
        }
        self.assertTrue(expected_names.issubset({route.name for route in problem_routes}))
        problem_paths = {route.path for route in problem_routes}
        self.assertIn("/problems/{problem:path}/history/snapshot", problem_paths)
        self.assertIn("/problems/{problem:path}/history/import", problem_paths)
        self.assertNotIn("/problems/{problem:path}/export/snapshot", problem_paths)
        self.assertNotIn("/problems/{problem:path}/export/import", problem_paths)

    def test_same_problem_can_be_scoped_by_multiple_contests(self) -> None:
        first_slug, first_id, actor_user_id = self.create_contest("first")
        second_slug, second_id, second_actor_user_id = self.create_contest("second")
        self._add_default_problem(first_id, actor_user_id, idx="AA")
        self._add_default_problem(second_id, second_actor_user_id, idx="Z")

        first = self._resolve("alice/sample", first_slug)
        second = self._resolve("alice/sample", second_slug)
        self.assertEqual(first.context["contest_slug"], first_slug)
        self.assertEqual(first.context["active_idx"], "AA")
        self.assertEqual(second.context["contest_slug"], second_slug)
        self.assertEqual(second.context["active_idx"], "Z")

        config.contest_service.remove_problem(first_id, workspace_service.known_problem_id("alice/sample"))
        request = _app_request(
            "/problems/alice/sample/statement",
            query=urlencode([("contest", first_slug)]),
            route_path="/problems/{problem:path}/statement",
        )
        with self.assertRaises(HTTPException) as caught:
            resolve_problem_contest_scope(request, "alice/sample", "alice")
        self.assertEqual(caught.exception.status_code, 404)
