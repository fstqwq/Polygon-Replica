import hashlib
import io
import zipfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from tests.common import E2ETestBase, configure_build_sources
from tests.archive_support import archive_view_from_bytes
from tests.db_helpers import (
    db_execute,
    db_fetch_one,
)
from tests.ui_support import (
    AUTH_COOKIE_NAME,
    _cookie_value_from_response,
    _register_with_password_envelope,
)
from app.main import runtime
from app.main import app

workspace_service = runtime.workspace_service


class AgentTestGrant(NamedTuple):
    headers: dict[str, str]
    grant_id: str


class TestAgentAPI(E2ETestBase):
    seed_default_workspace = True

    def _issue_auth_cookie(self, username: str, password: str = "StrongPass123") -> tuple[str, str]:
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        db_execute("UPDATE users SET is_system_admin=0 WHERE username=?", [username])
        auth_token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(auth_token)
        return (password, f"{AUTH_COOKIE_NAME}={auth_token}")

    def _grant_problem_owner(self, username: str) -> Path:
        workspace_service.grant_repo_access(self.problem, username, "owner")
        return Path(workspace_service.ensure_workspace(self.problem, username))

    def _connect_agent(self, client: TestClient, auth_cookie: str) -> dict[str, object]:
        resp = client.post(
            "/agent/connect",
            headers={"cookie": auth_cookie, "origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get("ok"))
        self.assertRegex(str(payload.get("register_url") or ""), r"^http://testserver/agent/v1/register/reg-[0-9a-f]{16}$")
        self.assertEqual(int(payload.get("expires_in") or 0), 900)
        return payload

    def _register_agent(
        self,
        client: TestClient,
        register_url: str,
        *,
        desktop_id: str = "D-test",
        existing_session_id: str = "",
    ) -> dict[str, object]:
        path = str(urlparse(register_url).path or "")
        request = {
            "agent_name": "cursor-polygon-skill",
            "desktop_id": desktop_id,
            "init_ts": "2026-04-12T10:00:00Z",
        }
        if existing_session_id:
            request["existing_session_id"] = existing_session_id
        resp = client.post(
            path,
            json=request,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertRegex(str(payload.get("agent_session_id") or ""), r"^as-[0-9a-f]{48}$")
        self.assertRegex(
            str(payload.get("credential") or ""),
            r"^polygon_agent_[A-Za-z0-9_-]{43}$",
        )
        self.assertNotIn("identity_hash", payload)
        return payload

    @staticmethod
    def _credential_headers(
        agent_session_id: str,
        credential: str,
    ) -> dict[str, str]:
        if not agent_session_id:
            raise AssertionError("test session id is required")
        return {"Authorization": f"Bearer {credential}"}

    def _set_general_scope(
        self,
        client: TestClient,
        *,
        auth_cookie: str,
        agent_session_id: str,
        scope: str,
    ) -> None:
        response = client.post(
            f"/agent/sessions/{agent_session_id}/general-scope",
            data={"general_scope": scope},
            headers={"cookie": auth_cookie, "origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)

    def _approve_grant(
        self,
        client: TestClient,
        *,
        auth_cookie: str,
        agent_session_id: str,
        credential: str,
        scope: str = "readonly",
        ttl: str = "86400",
        problem: str | None = None,
    ) -> tuple[str, AgentTestGrant]:
        problem_slug = problem or self.problem
        headers = self._credential_headers(agent_session_id, credential)
        request_resp = client.post(
            "/agent/v1/auth/request-access",
            json={
                "problem": problem_slug,
                "scope": scope,
            },
            headers=headers,
        )
        self.assertEqual(request_resp.status_code, 200, request_resp.text)
        request_payload = request_resp.json()
        request_id = str(request_payload.get("request_id") or "")
        self.assertRegex(request_id, r"^ar-[0-9a-f]{16}$")

        approve = client.post(
            f"/agent/approve/{request_id}",
            data={"decision": "approve", "scope": scope, "ttl": ttl},
            headers={"cookie": auth_cookie, "origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(approve.status_code, 303, approve.text)

        first_poll = client.get(
            f"/agent/v1/auth/poll/{request_id}",
            headers=headers,
        )
        self.assertEqual(first_poll.status_code, 200, first_poll.text)
        first_payload = first_poll.json()
        self.assertEqual(str(first_payload.get("status") or ""), "approved")
        grant_id = str(first_payload.get("grant_id") or "")
        self.assertRegex(grant_id, r"^ag-[0-9a-f]{16}$")
        self.assertEqual(first_payload.get("granted_scope"), scope)
        self.assertNotIn("token", first_payload)

        second_poll = client.get(
            f"/agent/v1/auth/poll/{request_id}",
            headers=headers,
        )
        self.assertEqual(second_poll.status_code, 200, second_poll.text)
        second_payload = second_poll.json()
        self.assertEqual(str(second_payload.get("status") or ""), "approved")
        self.assertEqual(second_payload.get("grant_id"), grant_id)
        self.assertNotIn("token", second_payload)
        return (request_id, AgentTestGrant(headers=headers, grant_id=grant_id))

    @staticmethod
    def _agent_headers(grant: AgentTestGrant) -> dict[str, str]:
        return grant.headers

    @staticmethod
    def _workspace_zip(files: dict[str, bytes | str], dirs: list[str] | None = None) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for dirname in dirs or []:
                archive.writestr(dirname.rstrip("/") + "/", b"")
            for rel, payload in files.items():
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                archive.writestr(rel, data)
        return buffer.getvalue()

    def test_registered_agent_creates_one_owned_problem(self) -> None:
        username = self.random_id("agent-create").lower()
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)
        problem = f"{username}/created"

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            headers = self._credential_headers(
                str(register["agent_session_id"]),
                str(register["credential"]),
            )
            self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=str(register["agent_session_id"]),
                credential=str(register["credential"]),
                scope="commit",
            )
            denied = client.post(
                "/agent/v1/problems",
                json={"problem": problem},
                headers=headers,
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(
                denied.json().get("error"),
                "agent_general_permission_required",
            )
            self._set_general_scope(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=str(register["agent_session_id"]),
                scope="commit",
            )

            created = client.post(
                "/agent/v1/problems",
                json={"problem": problem},
                headers=headers,
            )
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json(), {"problem": problem})

            problem_id = workspace_service.known_problem_id(problem)
            user_id = workspace_service.known_user_id(username)
            self.assertIsNotNone(problem_id)
            self.assertIsNotNone(user_id)
            access = runtime.access_query.problem_context(int(problem_id), int(user_id))
            self.assertEqual(str(access["role"]), "owner")
            workspace = workspace_service.workspace_context(
                problem,
                username,
                include_recent=False,
            )
            self.assertTrue(Path(str(workspace["workspace"]["path"])).is_dir())

            duplicate = client.post(
                "/agent/v1/problems",
                json={"problem": problem},
                headers=headers,
            )
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            foreign = client.post(
                "/agent/v1/problems",
                json={"problem": "someone-else/created"},
                headers=headers,
            )
            self.assertEqual(foreign.status_code, 422, foreign.text)
            bad_identity = client.post(
                "/agent/v1/problems",
                json={"problem": f"{username}/other"},
                headers=self._credential_headers(
                    str(register["agent_session_id"]),
                    "bad",
                ),
            )
            self.assertEqual(bad_identity.status_code, 401, bad_identity.text)

    def test_problem_deletion_removes_agent_grants_and_requests(self) -> None:
        username = self.random_id("agent-delete")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            request_id, grant = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=str(register["agent_session_id"]),
                credential=str(register["credential"]),
                scope="commit",
            )
            problem_id = workspace_service.known_problem_id(self.problem)
            self.assertIsNotNone(problem_id)
            self.assertIsNotNone(
                db_fetch_one("SELECT id FROM agent_problem_grants WHERE id=?", [grant.grant_id])
            )
            self.assertIsNotNone(
                db_fetch_one("SELECT id FROM agent_access_requests WHERE id=?", [request_id])
            )

            workspace_service.delete_problem(self.problem)

        self.assertIsNone(
            db_fetch_one("SELECT id FROM agent_problem_grants WHERE id=?", [grant.grant_id])
        )
        self.assertIsNone(
            db_fetch_one("SELECT id FROM agent_access_requests WHERE id=?", [request_id])
        )

    @staticmethod
    def _zip_entries(payload: bytes) -> dict[str, bytes]:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}

    def test_agent_pages_require_login_and_connect_enforces_same_origin(self) -> None:
        username = self.random_id("agent-ui")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            unauth = client.get("/agent/sessions", follow_redirects=False)
            self.assertEqual(unauth.status_code, 303)
            self.assertIn("/login?next=", unauth.headers.get("location", ""))

            approve_unauth = client.get("/agent/approve/ar-missing", follow_redirects=False)
            self.assertEqual(approve_unauth.status_code, 303)
            self.assertIn("/login?next=", approve_unauth.headers.get("location", ""))

            cross_site = client.post("/agent/connect", headers={"cookie": auth_cookie}, follow_redirects=False)
            self.assertEqual(cross_site.status_code, 403)

            ok = client.get("/agent/sessions", headers={"cookie": auth_cookie}, follow_redirects=False)
            self.assertEqual(ok.status_code, 200)

    def test_agent_register_code_is_one_time_and_reconnect_rotates_credential(self) -> None:
        username = self.random_id("agent-reg")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            first_connect = self._connect_agent(client, auth_cookie)
            first_register = self._register_agent(client, str(first_connect["register_url"]))
            first_session_id = str(first_register["agent_session_id"])
            first_credential = str(first_register["credential"])
            persisted = db_fetch_one(
                """
                SELECT identity_hash,credential_sha256
                FROM agent_sessions WHERE id=?
                """,
                [first_session_id],
            )
            self.assertEqual(
                str(persisted["credential_sha256"]),
                hashlib.sha256(first_credential.encode("utf-8")).hexdigest(),
            )
            self.assertNotEqual(str(persisted["credential_sha256"]), first_credential)
            self.assertNotEqual(str(persisted["identity_hash"]), first_credential)
            self._set_general_scope(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=first_session_id,
                scope="workspace",
            )

            reused_connect = self._connect_agent(client, auth_cookie)
            reused_register = self._register_agent(
                client,
                str(reused_connect["register_url"]),
                existing_session_id=str(first_register["agent_session_id"]),
            )
            self.assertEqual(str(first_register["agent_session_id"]), str(reused_register["agent_session_id"]))
            self.assertNotEqual(str(first_register["credential"]), str(reused_register["credential"]))
            old_credential = client.get(
                "/agent/v1/auth/status",
                headers=self._credential_headers(
                    str(first_register["agent_session_id"]),
                    str(first_register["credential"]),
                ),
            )
            self.assertEqual(old_credential.status_code, 401)
            current_credential = client.get(
                "/agent/v1/auth/status",
                headers=self._credential_headers(
                    str(reused_register["agent_session_id"]),
                    str(reused_register["credential"]),
                ),
            )
            self.assertEqual(current_credential.status_code, 200)
            self.assertEqual(
                current_credential.json().get("general_scope"),
                "workspace",
            )

            reused_attempt = client.post(
                str(urlparse(str(first_connect["register_url"])).path or ""),
                json={
                    "agent_name": "cursor-polygon-skill",
                    "desktop_id": "D-test",
                    "init_ts": "2026-04-12T10:00:00Z",
                },
            )
            self.assertEqual(reused_attempt.status_code, 410)

    def test_problem_routes_require_one_explicit_problem_before_provisioning(self) -> None:
        username = self.random_id("agent-explicit-problem")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace_service.grant_repo_access(self.problem, username, "read")
        user_id = workspace_service.known_user_id(username)
        self.assertIsNotNone(user_id)

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            _request_id, grant = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=str(register["agent_session_id"]),
                credential=str(register["credential"]),
            )
            before = db_fetch_one(
                "SELECT COUNT(*) AS n FROM workspaces WHERE user_id=?",
                [int(user_id)],
            )
            self.assertEqual(int(before["n"]), 0)

            missing_identity = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
            )
            self.assertEqual(missing_identity.status_code, 401, missing_identity.text)
            wrong_identity = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._credential_headers(
                    str(register["agent_session_id"]),
                    "wrong",
                ),
            )
            self.assertEqual(wrong_identity.status_code, 401, wrong_identity.text)

            missing = client.get(
                "/agent/v1/workspace/status",
                headers=self._agent_headers(grant),
            )
            self.assertEqual(missing.status_code, 400, missing.text)
            empty = client.get(
                "/agent/v1/workspace/status?problem=",
                headers=self._agent_headers(grant),
            )
            self.assertEqual(empty.status_code, 400, empty.text)
            repeated = client.get(
                "/agent/v1/workspace/status"
                f"?problem={self.problem}&problem={self.problem}",
                headers=self._agent_headers(grant),
            )
            self.assertEqual(repeated.status_code, 400, repeated.text)
            invalid = client.get(
                "/agent/v1/workspace/status",
                params={"problem": "not-a-canonical-slug"},
                headers=self._agent_headers(grant),
            )
            self.assertEqual(invalid.status_code, 400, invalid.text)
            unchanged = db_fetch_one(
                "SELECT COUNT(*) AS n FROM workspaces WHERE user_id=?",
                [int(user_id)],
            )
            self.assertEqual(int(unchanged["n"]), 0)

            valid = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._agent_headers(grant),
            )
            self.assertEqual(valid.status_code, 200, valid.text)

    def test_contest_roster_and_snapshot_use_general_permission(self) -> None:
        username = self.random_id("agent-contest")
        _password, auth_cookie = self._issue_auth_cookie(username)
        user_id = workspace_service.known_user_id(username)
        owner_id = workspace_service.known_user_id("alice")
        self.assertIsNotNone(user_id)
        self.assertIsNotNone(owner_id)
        contest_slug = self.random_id("agent-roster")
        contest_id = runtime.contest_service.create_contest_with_owner(
            slug=contest_slug,
            title="Agent Roster",
            owner_user_id=int(owner_id),
        )
        roster_items: list[tuple[int, str, str]] = []
        for label in ("B", "A", "C"):
            problem_slug = f"alice/{self.random_id(f'agent-contest-{label.lower()}')}"
            workspace_service.ensure_problem(problem_slug)
            problem_id = workspace_service.known_problem_id(problem_slug)
            self.assertIsNotNone(problem_id)
            runtime.contest_service.add_problem(
                contest_id,
                label,
                int(problem_id),
                int(owner_id),
            )
            row = db_fetch_one(
                "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
                [contest_id, int(problem_id)],
            )
            self.assertIsNotNone(row)
            roster_items.append((int(row["id"]), label, problem_slug))
        runtime.contest_service.grant_member_role(contest_id, username, "read")

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            headers = self._credential_headers(
                session_id,
                str(register["credential"]),
            )
            self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=str(register["credential"]),
                scope="readonly",
                problem=roster_items[0][2],
            )
            denied = client.get(
                f"/agent/v1/contests/{contest_slug}/problems",
                headers=headers,
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(
                denied.json()["detail"]["error"],
                "agent_general_permission_required",
            )
            self.assertEqual(
                denied.json()["detail"]["settings_url"],
                "http://testserver/agent/sessions",
            )
            self._set_general_scope(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                scope="readonly",
            )
            before = db_fetch_one(
                "SELECT COUNT(*) AS n FROM workspaces WHERE user_id=?",
                [int(user_id)],
            )
            roster = client.get(
                f"/agent/v1/contests/{contest_slug}/problems",
                headers=headers,
            )
            self.assertEqual(roster.status_code, 200, roster.text)
            roster_payload = roster.json()
            self.assertEqual(roster_payload["problem_count"], 3)
            self.assertEqual(
                [item["idx"] for item in roster_payload["problems"]],
                ["A", "B", "C"],
            )
            self.assertEqual(
                [item["problem"] for item in roster_payload["problems"]],
                [item[2] for item in sorted(roster_items, key=lambda item: item[1])],
            )
            self.assertTrue(
                all(
                    set(item) == {"contest_problem_id", "idx", "problem"}
                    for item in roster_payload["problems"]
                )
            )
            after_roster = db_fetch_one(
                "SELECT COUNT(*) AS n FROM workspaces WHERE user_id=?",
                [int(user_id)],
            )
            self.assertEqual(int(after_roster["n"]), int(before["n"]))

            generation = int(roster_payload["source_generation"])
            first_id = roster_items[0][0]
            stale = client.get(
                f"/agent/v1/contests/{contest_slug}/problems/{first_id}"
                "/workspace/snapshot",
                params={"source_generation": generation + 1},
                headers=headers,
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            after_stale = db_fetch_one(
                "SELECT COUNT(*) AS n FROM workspaces WHERE user_id=?",
                [int(user_id)],
            )
            self.assertEqual(int(after_stale["n"]), int(before["n"]))

            snapshot = client.get(
                f"/agent/v1/contests/{contest_slug}/problems/{first_id}"
                "/workspace/snapshot",
                params={"source_generation": generation},
                headers=headers,
            )
            self.assertEqual(snapshot.status_code, 200, snapshot.text)
            self.assertEqual(snapshot.headers["x-contest-problem-id"], str(first_id))

            runtime.contest_service.revoke_member(contest_id, int(user_id))
            revoked = client.get(
                f"/agent/v1/contests/{contest_slug}/problems",
                headers=headers,
            )
            self.assertEqual(revoked.status_code, 403, revoked.text)
            ordinary_hidden = client.get(
                "/agent/v1/workspace/status",
                params={"problem": roster_items[0][2]},
                headers=headers,
            )
            self.assertEqual(ordinary_hidden.status_code, 404, ordinary_hidden.text)
            workspace_service.grant_repo_access(
                roster_items[0][2],
                username,
                "read",
            )
            ordinary_direct = client.get(
                "/agent/v1/workspace/status",
                params={"problem": roster_items[0][2]},
                headers=headers,
            )
            self.assertEqual(ordinary_direct.status_code, 200, ordinary_direct.text)

    def test_agent_auth_status_reports_general_scope_and_individual_grants(self) -> None:
        username = self.random_id("agent-status")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)
        stale_seen = "2000-01-01T00:00:00+00:00"

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            credential = str(register["credential"])
            headers = self._credential_headers(session_id, credential)

            runtime.agent_service.store.touch_session(session_id, last_seen_at=stale_seen)
            empty_status = client.get(
                "/agent/v1/auth/status",
                headers=headers,
            )
            self.assertEqual(empty_status.status_code, 200, empty_status.text)
            empty_payload = empty_status.json()
            self.assertEqual(str(empty_payload.get("status") or ""), "ok")
            self.assertEqual(str(empty_payload.get("agent_session_id") or ""), session_id)
            self.assertEqual(str(empty_payload.get("user") or ""), username)
            self.assertEqual(empty_payload.get("general_scope"), "none")
            self.assertEqual(list(empty_payload.get("problem_grants") or []), [])
            touched_after_status = str(runtime.agent_service.store.session_by_id(session_id)["last_seen_at"])
            self.assertNotEqual(touched_after_status, stale_seen)
            self.assertEqual(touched_after_status, str(empty_payload.get("last_seen_at") or ""))

            runtime.agent_service.store.touch_session(session_id, last_seen_at=stale_seen)
            request_resp = client.post(
                "/agent/v1/auth/request-access",
                json={"problem": self.problem, "scope": "readonly"},
                headers=headers,
            )
            self.assertEqual(request_resp.status_code, 200, request_resp.text)
            request_id = str(request_resp.json().get("request_id") or "")
            self.assertRegex(request_id, r"^ar-[0-9a-f]{16}$")
            self.assertEqual(int(request_resp.json().get("expires_in") or 0), 900)
            self.assertNotEqual(str(runtime.agent_service.store.session_by_id(session_id)["last_seen_at"]), stale_seen)

            runtime.agent_service.store.touch_session(session_id, last_seen_at=stale_seen)
            pending_poll = client.get(
                f"/agent/v1/auth/poll/{request_id}",
                headers=headers,
            )
            self.assertEqual(pending_poll.status_code, 200, pending_poll.text)
            self.assertEqual(str(pending_poll.json().get("status") or ""), "pending")
            self.assertNotEqual(str(runtime.agent_service.store.session_by_id(session_id)["last_seen_at"]), stale_seen)

            approve = client.post(
                f"/agent/approve/{request_id}",
                data={"decision": "approve", "scope": "readonly", "ttl": "86400"},
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(approve.status_code, 303, approve.text)
            readonly_poll = client.get(
                f"/agent/v1/auth/poll/{request_id}",
                headers=headers,
            )
            self.assertEqual(readonly_poll.status_code, 200, readonly_poll.text)
            readonly_grant_id = str(readonly_poll.json().get("grant_id") or "")
            self.assertRegex(readonly_grant_id, r"^ag-[0-9a-f]{16}$")
            self.assertNotIn("token", readonly_poll.json())

            readonly_status = client.get(
                "/agent/v1/auth/status",
                headers=headers,
            )
            self.assertEqual(readonly_status.status_code, 200, readonly_status.text)
            readonly_items = list(readonly_status.json().get("problem_grants") or [])
            self.assertEqual(len(readonly_items), 1)
            self.assertEqual(str(readonly_items[0].get("problem") or ""), self.problem)
            self.assertEqual(str(readonly_items[0].get("scope") or ""), "readonly")
            self.assertTrue(str(readonly_items[0].get("expires_at") or ""))

            _workspace_request_id, workspace_grant = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="workspace",
            )
            workspace_status = client.get(
                "/agent/v1/auth/status",
                headers=headers,
            )
            self.assertEqual(workspace_status.status_code, 200, workspace_status.text)
            workspace_items = list(workspace_status.json().get("problem_grants") or [])
            self.assertEqual(len(workspace_items), 2)
            self.assertEqual(
                {str(item.get("scope") or "") for item in workspace_items},
                {"readonly", "workspace"},
            )

            workspace_service.grant_repo_access(self.problem, username, "read")
            downgraded_status = client.get(
                "/agent/v1/auth/status",
                headers=headers,
            )
            self.assertEqual(downgraded_status.status_code, 200, downgraded_status.text)
            downgraded_items = list(downgraded_status.json().get("problem_grants") or [])
            self.assertEqual(len(downgraded_items), 2)
            self.assertEqual(
                {str(item.get("effective_scope") or "") for item in downgraded_items},
                {"readonly"},
            )

            workspace_service.grant_repo_access(self.problem, username, "owner")
            revoke_readonly = client.post(
                f"/agent/grants/{readonly_grant_id}/revoke",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(revoke_readonly.status_code, 303)
            revoke_workspace = client.post(
                f"/agent/grants/{workspace_grant.grant_id}/revoke",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(revoke_workspace.status_code, 303)

            revoked_status = client.get(
                "/agent/v1/auth/status",
                headers=headers,
            )
            self.assertEqual(revoked_status.status_code, 200, revoked_status.text)
            self.assertEqual(list(revoked_status.json().get("problem_grants") or []), [])

            self._set_general_scope(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                scope="workspace",
            )
            general_status = client.get("/agent/v1/auth/status", headers=headers)
            self.assertEqual(general_status.status_code, 200, general_status.text)
            self.assertEqual(general_status.json().get("general_scope"), "workspace")

            bad_credential = client.get(
                "/agent/v1/auth/status",
                headers=self._credential_headers(session_id, "bad"),
            )
            self.assertEqual(bad_credential.status_code, 401)

            missing_credential = client.get("/agent/v1/auth/status")
            self.assertEqual(missing_credential.status_code, 401)

            disconnect = client.post(
                f"/agent/disconnect/{session_id}",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(disconnect.status_code, 303)
            self.assertIsNone(runtime.agent_service.store.session_by_id(session_id))
            grant_count_row = db_fetch_one(
                "SELECT COUNT(*) AS n FROM agent_problem_grants WHERE agent_session_id=?",
                [session_id],
            )
            self.assertEqual(int(grant_count_row["n"]), 0)
            request_count_row = db_fetch_one(
                "SELECT COUNT(*) AS n FROM agent_access_requests WHERE agent_session_id=?",
                [session_id],
            )
            self.assertEqual(int(request_count_row["n"]), 0)
            disconnected_status = client.get(
                "/agent/v1/auth/status",
                headers=headers,
            )
            self.assertEqual(disconnected_status.status_code, 401)
            disconnected_request = client.post(
                "/agent/v1/auth/request-access",
                json={"problem": self.problem, "scope": "readonly"},
                headers=headers,
            )
            self.assertEqual(disconnected_request.status_code, 401)
            disconnected_poll = client.get(
                f"/agent/v1/auth/poll/{request_id}",
                headers=headers,
            )
            self.assertEqual(disconnected_poll.status_code, 401)
    def test_approval_access_recheck_returns_a_controlled_failure(self) -> None:
        username = self.random_id("agent-approval-recheck")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            headers = self._credential_headers(
                session_id,
                str(register["credential"]),
            )
            requested = client.post(
                "/agent/v1/auth/request-access",
                json={"problem": self.problem, "scope": "commit"},
                headers=headers,
            )
            self.assertEqual(requested.status_code, 200, requested.text)
            request_id = str(requested.json().get("request_id") or "")

            workspace_service.grant_repo_access(self.problem, username, "read")
            approved = client.post(
                f"/agent/approve/{request_id}",
                data={"decision": "approve", "scope": "commit", "ttl": "86400"},
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )

            self.assertEqual(approved.status_code, 303, approved.text)
            access_request = runtime.agent_service.store.access_request_by_id(
                request_id
            )
            self.assertIsNotNone(access_request)
            assert access_request is not None
            self.assertEqual(access_request["status"], "pending")
            self.assertEqual(
                runtime.agent_service.store.list_session_grants(session_id),
                [],
            )

    def test_problem_grants_keep_independent_scopes_expiries_and_revocation(self) -> None:
        username = self.random_id("agent-grant-lifetimes")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            credential = str(register["credential"])
            headers = self._credential_headers(session_id, credential)
            grants: list[AgentTestGrant] = []
            for ttl in ("3600", "86400", "604800", "2592000", "forever"):
                _request_id, grant = self._approve_grant(
                    client,
                    auth_cookie=auth_cookie,
                    agent_session_id=session_id,
                    credential=credential,
                    scope="readonly",
                    ttl=ttl,
                )
                grants.append(grant)
            rows = runtime.agent_service.store.list_session_grants(session_id)
            self.assertEqual(len(rows), 5)
            self.assertEqual(sum(1 for row in rows if not row["expires_at"]), 1)

            request_id, commit_grant = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="commit",
                ttl="3600",
            )
            duplicate_approval = client.post(
                f"/agent/approve/{request_id}",
                data={
                    "decision": "approve",
                    "scope": "workspace",
                    "ttl": "2592000",
                },
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(duplicate_approval.status_code, 303)
            after_duplicate = runtime.agent_service.store.list_session_grants(
                session_id
            )
            self.assertEqual(len(after_duplicate), 6)
            approved_request = runtime.agent_service.store.access_request_by_id(
                request_id
            )
            self.assertIsNotNone(approved_request)
            assert approved_request is not None
            self.assertEqual(approved_request["grant_id"], commit_grant.grant_id)
            self.assertEqual(approved_request["granted_scope"], "commit")

            db_execute(
                "UPDATE agent_problem_grants SET expires_at=? WHERE id=?",
                ["2000-01-01T00:00:00+00:00", commit_grant.grant_id],
            )
            commit_after_expiry = client.post(
                "/agent/v1/commit",
                params={"problem": self.problem},
                headers=headers,
                json={"message": "must not commit"},
            )
            self.assertEqual(commit_after_expiry.status_code, 403)
            self.assertEqual(
                commit_after_expiry.json()["detail"]["error"],
                "agent_permission_required",
            )
            readable = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=headers,
            )
            self.assertEqual(readable.status_code, 200, readable.text)

            revoke = client.post(
                f"/agent/grants/{grants[0].grant_id}/revoke",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(revoke.status_code, 303)
            active_status = client.get("/agent/v1/auth/status", headers=headers)
            self.assertEqual(active_status.status_code, 200, active_status.text)
            active_ids = {
                str(item["grant_id"])
                for item in active_status.json()["problem_grants"]
            }
            self.assertNotIn(grants[0].grant_id, active_ids)
            self.assertIn(grants[1].grant_id, active_ids)

    def test_agent_grant_revocation_and_disconnect_invalidate_access(self) -> None:
        username = self.random_id("agent-revoke")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            credential = str(register["credential"])
            _request_id, grant = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="readonly",
            )

            before = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._agent_headers(grant),
            )
            self.assertEqual(before.status_code, 200, before.text)

            revoke = client.post(
                f"/agent/grants/{grant.grant_id}/revoke",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(revoke.status_code, 303)

            after_revoke = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._agent_headers(grant),
            )
            self.assertEqual(after_revoke.status_code, 403)

            _request_id2, grant2 = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="readonly",
            )
            before_disconnect = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._agent_headers(grant2),
            )
            self.assertEqual(before_disconnect.status_code, 200, before_disconnect.text)

            disconnect = client.post(
                f"/agent/disconnect/{session_id}",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(disconnect.status_code, 303)
            self.assertIsNone(runtime.agent_service.store.session_by_id(session_id))

            after_disconnect = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._agent_headers(grant2),
            )
            self.assertEqual(after_disconnect.status_code, 401)

    def test_agent_scope_enforcement_and_acl_downgrade(self) -> None:
        username = self.random_id("agent-scope")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace = self._grant_problem_owner(username)
        self.assertTrue(workspace.exists())
        workspace_ctx = workspace_service.workspace_context(self.problem, username, include_recent=False)
        workspace_id = int(workspace_ctx["workspace"]["id"])

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            credential = str(register["credential"])

            _readonly_request, readonly_token = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="readonly",
            )
            readonly_upload = client.post(
                "/agent/v1/workspace/upload",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
                data={"path": "solutions/readonly.txt"},
                files={"file": ("readonly.txt", b"blocked")},
            )
            self.assertEqual(readonly_upload.status_code, 403, readonly_upload.text)

            _workspace_request, workspace_token = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="workspace",
            )
            upload = client.post(
                "/agent/v1/workspace/upload",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                data={"path": "solutions/agent.txt"},
                files={"file": ("agent.txt", b"hello\r\nagent\r\n")},
            )
            self.assertEqual(upload.status_code, 200, upload.text)
            self.assertTrue((workspace / "solutions/agent.txt").exists())
            self.assertEqual((workspace / "solutions/agent.txt").read_bytes(), b"hello\r\nagent\r\n")
            upload_status = workspace_service.read_workspace_status(workspace)
            upload_row = db_fetch_one("SELECT dirty FROM workspaces WHERE id=?", [workspace_id])
            self.assertIsNotNone(upload_row)
            self.assertEqual(int(upload_row["dirty"] or 0), int(upload_status.get("dirty") or 0))

            hidden_root = workspace / ".env"
            hidden_nested = workspace / "solutions" / ".cache" / "secret.txt"
            hidden_root.write_text("token=secret\n", encoding="utf-8")
            hidden_nested.parent.mkdir(parents=True, exist_ok=True)
            hidden_nested.write_text("nested\n", encoding="utf-8")

            hidden_read = client.get(
                "/agent/v1/workspace/file",
                headers=self._agent_headers(workspace_token),
                params={"problem": self.problem, "path": ".env"},
            )
            self.assertEqual(hidden_read.status_code, 400, hidden_read.text)

            hidden_list = client.get(
                "/agent/v1/workspace/files",
                headers=self._agent_headers(workspace_token),
                params={"problem": self.problem},
            )
            self.assertEqual(hidden_list.status_code, 200, hidden_list.text)
            listed_paths = {str(item.get("path") or "") for item in hidden_list.json().get("entries") or []}
            self.assertNotIn(".env", listed_paths)
            self.assertNotIn("solutions/.cache", listed_paths)
            self.assertNotIn("solutions/.cache/secret.txt", listed_paths)

            invalid_root_upload = client.post(
                "/agent/v1/workspace/upload",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                data={"path": "README.md"},
                files={"file": ("README.md", b"blocked")},
            )
            self.assertEqual(invalid_root_upload.status_code, 400, invalid_root_upload.text)

            hidden_upload = client.post(
                "/agent/v1/workspace/upload",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                data={"path": ".env"},
                files={"file": ("env.txt", b"blocked")},
            )
            self.assertEqual(hidden_upload.status_code, 400, hidden_upload.text)

            hidden_delete = client.delete(
                "/agent/v1/workspace/files/.env",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
            )
            self.assertEqual(hidden_delete.status_code, 400, hidden_delete.text)

            read_back = client.get(
                "/agent/v1/workspace/file",
                headers=self._agent_headers(workspace_token),
                params={"problem": self.problem, "path": "solutions/agent.txt"},
            )
            self.assertEqual(read_back.status_code, 200, read_back.text)
            self.assertEqual(str(read_back.json().get("content") or ""), "hello\r\nagent\r\n")

            delete = client.delete(
                "/agent/v1/workspace/files/solutions/agent.txt",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
            )
            self.assertEqual(delete.status_code, 200, delete.text)
            self.assertFalse((workspace / "solutions/agent.txt").exists())
            delete_status = workspace_service.read_workspace_status(workspace)
            delete_row = db_fetch_one("SELECT dirty FROM workspaces WHERE id=?", [workspace_id])
            self.assertIsNotNone(delete_row)
            self.assertEqual(int(delete_row["dirty"] or 0), int(delete_status.get("dirty") or 0))

            workspace_service.grant_repo_access(self.problem, username, "read")
            downgraded_upload = client.post(
                "/agent/v1/workspace/upload",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                data={"path": "solutions/downgraded.txt"},
                files={"file": ("downgraded.txt", b"blocked")},
            )
            self.assertEqual(downgraded_upload.status_code, 403, downgraded_upload.text)

            still_readable = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
            )
            self.assertEqual(still_readable.status_code, 200, still_readable.text)

    def test_agent_workspace_snapshot_compare_and_apply_full_zip(self) -> None:
        username = self.random_id("agent-sync")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace = self._grant_problem_owner(username)
        main_source = workspace / "solutions" / "main.cpp"
        main_source.parent.mkdir(parents=True, exist_ok=True)
        main_source.write_bytes(b"int main(){return 0;}\r\n")
        test_input = workspace / "tests" / "manual" / "001.in"
        test_input.parent.mkdir(parents=True, exist_ok=True)
        test_input.write_text("1\n", encoding="utf-8")
        legacy_answer = workspace / "tests" / "answers" / "001.ans"
        legacy_answer.parent.mkdir(parents=True, exist_ok=True)
        legacy_answer.write_text("legacy\n", encoding="utf-8")
        (workspace / "README.md").write_text("private\n", encoding="utf-8")
        (workspace / ".env").write_text("secret\n", encoding="utf-8")
        (workspace / "temp").mkdir(exist_ok=True)
        (workspace / "temp" / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        (workspace / "draft").mkdir(exist_ok=True)
        (workspace / "draft" / "draft.txt").write_text("draft\n", encoding="utf-8")

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]), desktop_id="D-sync")
            session_id = str(register["agent_session_id"])
            credential = str(register["credential"])
            _readonly_request, readonly_token = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="readonly",
            )
            snapshot = client.get(
                "/agent/v1/workspace/snapshot",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
            )
            self.assertEqual(snapshot.status_code, 200, snapshot.text)
            self.assertIn("application/zip", snapshot.headers.get("content-type", ""))
            self.assertEqual(snapshot.headers.get("x-problem"), self.problem)
            snapshot_entries = self._zip_entries(snapshot.content)
            self.assertEqual(snapshot_entries["solutions/main.cpp"], b"int main(){return 0;}\n")
            self.assertEqual(snapshot_entries["tests/manual/001.in"], b"1\n")
            self.assertNotIn("tests/answers/001.ans", snapshot_entries)
            self.assertNotIn("README.md", snapshot_entries)
            self.assertNotIn(".env", snapshot_entries)
            self.assertNotIn("temp/scratch.txt", snapshot_entries)
            self.assertNotIn("draft/draft.txt", snapshot_entries)

            local_files = dict(snapshot_entries)
            local_files["solutions/main.cpp"] = b"int main(){return 0;}\r\n"
            local_files["solutions/new.cpp"] = b"int main(){return 1;}\r\n"
            del local_files["tests/manual/001.in"]
            local_zip = self._workspace_zip(local_files)

            compare = client.post(
                "/agent/v1/workspace/compare",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
                files={"archive": ("workspace.zip", local_zip, "application/zip")},
            )
            self.assertEqual(compare.status_code, 200, compare.text)
            compare_payload = compare.json()
            self.assertTrue(bool(compare_payload.get("changed")))
            self.assertIn("solutions/new.cpp", compare_payload.get("uploads") or [])
            self.assertIn("tests/manual/001.in", compare_payload.get("deletes") or [])
            self.assertIn("solutions/main.cpp", compare_payload.get("same") or [])

            readonly_apply = client.post(
                "/agent/v1/workspace/apply",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
                files={"archive": ("workspace.zip", local_zip, "application/zip")},
            )
            self.assertEqual(readonly_apply.status_code, 403, readonly_apply.text)

            _workspace_request, workspace_token = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="workspace",
            )

            conflict = client.post(
                "/agent/v1/workspace/apply",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                data={"base_head_commit": "not-current-head"},
                files={"archive": ("workspace.zip", local_zip, "application/zip")},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)

            invalid_zip = self._workspace_zip({"README.md": "bad\n"})
            invalid_compare = client.post(
                "/agent/v1/workspace/compare",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
                files={"archive": ("workspace.zip", invalid_zip, "application/zip")},
            )
            self.assertEqual(invalid_compare.status_code, 400, invalid_compare.text)
            answer_zip = self._workspace_zip({"tests/answers/001.ans": "bad\n"})
            invalid_answer_compare = client.post(
                "/agent/v1/workspace/compare",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
                files={"archive": ("workspace.zip", answer_zip, "application/zip")},
            )
            self.assertEqual(invalid_answer_compare.status_code, 400, invalid_answer_compare.text)

            apply = client.post(
                "/agent/v1/workspace/apply",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                files={"archive": ("workspace.zip", local_zip, "application/zip")},
            )
            self.assertEqual(apply.status_code, 200, apply.text)
            apply_payload = apply.json()
            self.assertTrue(bool(apply_payload.get("applied")))
            self.assertIn("solutions/new.cpp", apply_payload.get("uploads") or [])
            self.assertIn("tests/manual/001.in", apply_payload.get("deletes") or [])
            self.assertEqual(main_source.read_bytes(), b"int main(){return 0;}\n")
            self.assertEqual((workspace / "solutions" / "new.cpp").read_bytes(), b"int main(){return 1;}\n")
            self.assertFalse(test_input.exists())
            self.assertEqual((workspace / "README.md").read_text(encoding="utf-8"), "private\n")

    def test_workspace_archive_service_rejects_large_unzipped_zip_payload(self) -> None:
        username = self.random_id("agent-zip-cap")
        workspace = self._grant_problem_owner(username)
        sentinel = workspace / "solutions" / "keep.cpp"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("keep\n", encoding="utf-8")

        oversized_entry_zip = self._workspace_zip({"solutions/bomb.txt": "x" * 17})
        oversized_total_zip = self._workspace_zip({"solutions/a.txt": "1234567890", "solutions/b.txt": "abcdefghij"})
        with archive_view_from_bytes(oversized_entry_zip, max_expanded_bytes=16) as archive:
            with self.assertRaisesRegex(ValueError, "expanded zip payload is too large"):
                runtime.workspace_archive_service.compare_zip(workspace, archive)
        with archive_view_from_bytes(oversized_total_zip, max_expanded_bytes=16) as archive:
            with self.assertRaisesRegex(ValueError, "expanded zip payload is too large"):
                runtime.workspace_archive_service.compare_zip(workspace, archive)
        with archive_view_from_bytes(oversized_total_zip, max_expanded_bytes=16) as archive:
            with self.assertRaisesRegex(ValueError, "expanded zip payload is too large"):
                runtime.workspace_archive_service.apply_zip(workspace, archive)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_agent_verification_export_workspace_and_commit_endpoints(self) -> None:
        username = self.random_id("agent-api")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace = self._grant_problem_owner(username)
        solution = workspace / "solutions" / "main.cpp"
        solution.parent.mkdir(parents=True, exist_ok=True)
        solution.write_text("#include <bits/stdc++.h>\nint main(){return 0;}\n", encoding="utf-8")
        Path(f"{solution}.desc").write_text(
            "expected: accepted\n",
            encoding="utf-8",
        )
        configure_build_sources(
            workspace,
            accepted_solution_source="solutions/main.cpp",
        )
        stale_head = str(workspace_service.read_workspace_status(workspace).get("head_commit") or "")
        workspace_ctx = workspace_service.workspace_context(self.problem, username, include_recent=False)
        workspace_id = int(workspace_ctx["workspace"]["id"])

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]), desktop_id="D-api")
            session_id = str(register["agent_session_id"])
            credential = str(register["credential"])

            access_denied = client.post(
                "/agent/v1/auth/request-access",
                json={"problem": self.default_problem, "scope": "readonly"},
                headers=self._credential_headers(session_id, credential),
            )
            self.assertEqual(access_denied.status_code, 404)

            _readonly_request, readonly_token = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="readonly",
            )
            traversal = client.get(
                "/agent/v1/workspace/file",
                headers=self._agent_headers(readonly_token),
                params={"problem": self.problem, "path": "../secrets.txt"},
            )
            self.assertEqual(traversal.status_code, 400)

            verify = client.post(
                "/agent/v1/verification/start",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
                json={},
            )
            self.assertEqual(verify.status_code, 200, verify.text)
            verify_payload = verify.json()
            verification_id = str(verify_payload.get("verification_id") or "")
            self.assertRegex(verification_id, r"^ver-[0-9a-f]+$")
            self.assertEqual(str(verify_payload.get("status") or ""), "queued")

            verify_status = client.get(
                f"/agent/v1/verification/{verification_id}/status",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
            )
            self.assertEqual(verify_status.status_code, 200, verify_status.text)
            self.assertIn(
                str(verify_status.json().get("status") or ""),
                {"running", "queued", "failed", "ok", "cancelled"},
            )

            domjudge_export = client.post(
                "/agent/v1/export/start",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
                json={"format": "domjudge"},
            )
            self.assertEqual(domjudge_export.status_code, 403, domjudge_export.text)

            _workspace_request, workspace_token = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="workspace",
            )
            missing_format = client.post(
                "/agent/v1/export/start",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                json={},
            )
            self.assertEqual(missing_format.status_code, 400, missing_format.text)

            icpc_export = client.post(
                "/agent/v1/export/start",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                json={"format": "icpc-2025-09"},
            )
            self.assertEqual(icpc_export.status_code, 400, icpc_export.text)
            self.assertIn("no published main revision", icpc_export.text)

            qoj_export = client.post(
                "/agent/v1/export/start",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                json={"format": "qoj"},
            )
            self.assertEqual(qoj_export.status_code, 400, qoj_export.text)
            self.assertIn("no published main revision", qoj_export.text)

            workspace_commit = client.post(
                "/agent/v1/commit",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                json={"message": "should fail"},
            )
            self.assertEqual(workspace_commit.status_code, 403)

            commit_file = workspace / "notes" / "commit.txt"
            commit_file.parent.mkdir(parents=True, exist_ok=True)
            commit_file.write_text("commit me\n", encoding="utf-8")

            _commit_request, commit_token = self._approve_grant(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                credential=credential,
                scope="commit",
            )
            missing_message = client.post(
                "/agent/v1/commit",
                params={"problem": self.problem},
                headers=self._agent_headers(commit_token),
                json={},
            )
            self.assertEqual(missing_message.status_code, 400)

            commit_resp = client.post(
                "/agent/v1/commit",
                params={"problem": self.problem},
                headers=self._agent_headers(commit_token),
                json={"message": "agent commit"},
            )
            self.assertEqual(commit_resp.status_code, 200, commit_resp.text)
            commit_payload = commit_resp.json()
            self.assertEqual(str(commit_payload.get("status") or ""), "ok")
            head = str(commit_payload.get("head") or "")
            self.assertRegex(head, r"^[0-9a-f]{40}$")
            commit_row = db_fetch_one("SELECT head_commit,dirty FROM workspaces WHERE id=?", [workspace_id])
            self.assertIsNotNone(commit_row)
            self.assertEqual(str(commit_row["head_commit"] or ""), head)
            self.assertEqual(int(commit_row["dirty"] or 0), 0)

            commit_status = client.get(
                f"/agent/v1/commit/{head}/status",
                params={"problem": self.problem},
                headers=self._agent_headers(commit_token),
            )
            self.assertEqual(commit_status.status_code, 200, commit_status.text)
            self.assertEqual(str(commit_status.json().get("status") or ""), "published")
            foreign_commit_status = client.get(
                f"/agent/v1/commit/{'f' * 40}/status",
                params={"problem": self.problem},
                headers=self._agent_headers(commit_token),
            )
            self.assertEqual(
                foreign_commit_status.status_code,
                404,
                foreign_commit_status.text,
            )
            self.assertNotEqual(stale_head, head)

            db_execute(
                "UPDATE workspaces SET head_commit=?, dirty=1 WHERE id=?",
                [stale_head, workspace_id],
            )

            live_status = client.get(
                "/agent/v1/workspace/status",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
            )
            self.assertEqual(live_status.status_code, 200, live_status.text)
            self.assertEqual(str(live_status.json().get("head_commit") or ""), head)
            self.assertFalse(bool(live_status.json().get("dirty")))

            fresh_icpc_export = client.post(
                "/agent/v1/export/start",
                params={"problem": self.problem},
                headers=self._agent_headers(workspace_token),
                json={"format": "icpc-2025-09"},
            )
            self.assertEqual(fresh_icpc_export.status_code, 200, fresh_icpc_export.text)
            fresh_export_job_id = str(fresh_icpc_export.json().get("job_id") or "")
            self.assertRegex(fresh_export_job_id, r"^exp-api-")
            fresh_export_status = client.get(
                f"/agent/v1/export/{fresh_export_job_id}/status",
                params={"problem": self.problem},
                headers=self._agent_headers(readonly_token),
            )
            self.assertEqual(fresh_export_status.status_code, 200, fresh_export_status.text)
            fresh_status_payload = fresh_export_status.json()
            self.assertEqual(str(fresh_status_payload.get("format") or ""), "icpc-2025-09")
            self.assertIn(
                str(fresh_status_payload.get("phase") or ""),
                {"queued", "verifying", "packaging", "complete"},
            )
            self.assertEqual(str(fresh_status_payload.get("source_commit") or ""), head)
