from __future__ import annotations

# ascii-lint: allow; reason=chinese-test

import json
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.impl.contest.workspace_scope import (
    ContestWorkspaceScope,
    ProblemHrefBuilder,
    apply_problem_contest_scope,
    build_contest_problem_href,
    problem_section_for_route,
    resolve_problem_contest_scope,
)
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

    def test_scoped_problem_page_preserves_contest_urls(self) -> None:
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
        hrefs = {
            attrs.get("href", "")
            for tag, attrs in document.elements
            if tag == "a"
        }
        self.assertIn(f"/contests/{contest_slug}/overview", hrefs)
        self.assertIn(
            f"/problems/{peer_slug}/statement?contest={contest_slug}",
            hrefs,
        )
        self.assertIn(
            f"/problems/{locked_slug}/statement?contest={contest_slug}",
            hrefs,
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
        unscoped_document = _HtmlElements()
        unscoped_document.feed(unscoped.text)
        for tag, attrs in unscoped_document.elements:
            if tag == "a" and attrs.get("href", "").startswith("/problems/"):
                self.assertNotIn("contest=", attrs["href"])

    def test_scoped_files_navigation_and_mutation_keep_browser_state(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("files")
        self._add_default_problem(contest_id, actor_user_id, idx="A")
        cookie = self._session_cookie("alice")
        workspace = Path(
            config.workspace_service.ensure_workspace("alice/sample", "alice")
        )
        (workspace / "notes").mkdir(exist_ok=True)

        from app.main import app

        with TestClient(app, base_url="https://testserver") as client:
            page = client.get(
                f"/problems/alice/sample/files?path=config%2Fproblem.json"
                f"&contest={contest_slug}",
                headers={"cookie": cookie},
            )
            directory = client.get(
                f"/problems/alice/sample/files?path=config&dir=config"
                f"&contest={contest_slug}",
                headers={"cookie": cookie},
            )
            created = client.post(
                f"/problems/alice/sample/files/new?contest={contest_slug}",
                data={
                    "name": "scoped + file.txt",
                    "dir": "notes",
                },
                headers={
                    "cookie": cookie,
                    "origin": "https://testserver",
                },
                follow_redirects=False,
            )
            created_directory = client.post(
                f"/problems/alice/sample/files/new-directory"
                f"?contest={contest_slug}",
                data={
                    "name": "scoped + directory",
                    "dir": "notes",
                },
                headers={
                    "cookie": cookie,
                    "origin": "https://testserver",
                },
                follow_redirects=False,
            )
            uploaded = client.post(
                f"/problems/alice/sample/files/upload?contest={contest_slug}",
                data={"dir": "config"},
                files={"upload": ("scoped + payload.txt", b"payload\n")},
                headers={
                    "cookie": cookie,
                    "origin": "https://testserver",
                },
                follow_redirects=False,
            )

        self.assertEqual(page.status_code, 200, page.text)
        document = _HtmlElements()
        document.feed(page.text)
        config_href = next(
            attrs["href"]
            for tag, attrs in document.elements
            if tag == "a"
            and parse_qs(urlsplit(attrs.get("href", "")).query).get("dir")
            == ["config"]
        )
        self.assertEqual(urlsplit(config_href).path, "/problems/alice/sample/files")
        self.assertEqual(
            parse_qs(urlsplit(config_href).query),
            {
                "path": ["config/problem.json"],
                "dir": ["config"],
                "contest": [contest_slug],
            },
        )
        for tag, attrs in document.elements:
            for attribute in ("href", "action", "src"):
                value = attrs.get(attribute, "")
                if value.startswith("/problems/"):
                    with self.subTest(tag=tag, attribute=attribute, value=value):
                        self.assertEqual(
                            parse_qs(urlsplit(value).query).get("contest"),
                            [contest_slug],
                        )

        self.assertEqual(directory.status_code, 200, directory.text)
        self.assertNotIn('data-code-editor="1"', directory.text)

        self.assertEqual(created.status_code, 303, created.text)
        created_location = urlsplit(created.headers["location"])
        self.assertEqual(created_location.path, "/problems/alice/sample/files")
        self.assertEqual(
            parse_qs(created_location.query),
            {
                "path": ["notes/scoped + file.txt"],
                "dir": ["notes"],
                "contest": [contest_slug],
            },
        )

        self.assertEqual(
            created_directory.status_code,
            303,
            created_directory.text,
        )
        created_directory_location = urlsplit(
            created_directory.headers["location"]
        )
        self.assertEqual(
            created_directory_location.path,
            "/problems/alice/sample/files",
        )
        self.assertEqual(
            parse_qs(created_directory_location.query),
            {
                "dir": ["notes/scoped + directory"],
                "contest": [contest_slug],
            },
        )
        self.assertTrue((workspace / "notes/scoped + file.txt").is_file())
        self.assertTrue((workspace / "notes/scoped + directory").is_dir())

        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        uploaded_location = urlsplit(uploaded.headers["location"])
        self.assertEqual(uploaded_location.path, "/problems/alice/sample/files")
        self.assertEqual(
            parse_qs(uploaded_location.query),
            {
                "path": ["config/scoped + payload.txt"],
                "dir": ["config"],
                "contest": [contest_slug],
            },
        )

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

    def test_same_problem_can_be_scoped_by_multiple_contests(self) -> None:
        first_slug, first_id, actor_user_id = self.create_contest("first")
        second_slug, second_id, second_actor_user_id = self.create_contest("second")
        self._add_default_problem(first_id, actor_user_id, idx="AA")
        self._add_default_problem(second_id, second_actor_user_id, idx="Z")

        cookie = self._session_cookie("alice")
        from app.main import app

        with TestClient(app) as client:
            first = client.get(
                f"/problems/alice/sample/statement?contest={first_slug}",
                headers={"cookie": cookie},
            )
            second = client.get(
                f"/problems/alice/sample/statement?contest={second_slug}",
                headers={"cookie": cookie},
            )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)

        config.contest_service.remove_problem(first_id, workspace_service.known_problem_id("alice/sample"))
        request = _app_request(
            "/problems/alice/sample/statement",
            query=urlencode([("contest", first_slug)]),
            route_path="/problems/{problem:path}/statement",
        )
        with self.assertRaises(HTTPException) as caught:
            resolve_problem_contest_scope(request, "alice/sample", "alice")
        self.assertEqual(caught.exception.status_code, 404)
