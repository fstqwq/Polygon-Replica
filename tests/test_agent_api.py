from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from .common import SmokeBase
from .ui_support import AUTH_COOKIE_NAME, _cookie_value_from_response, _register_with_password_proof
from app.impl.runtime.config import config
from app.main import app

workspace_service = config.workspace_service


class TestAgentAPI(SmokeBase):
    def _issue_auth_cookie(self, username: str, password: str = "StrongPass123") -> tuple[str, str]:
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
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
        return payload

    def _register_agent(self, client: TestClient, register_url: str, *, desktop_id: str = "D-test") -> dict[str, object]:
        path = str(urlparse(register_url).path or "")
        resp = client.post(
            path,
            json={
                "agent_name": "cursor-polygon-skill",
                "desktop_id": desktop_id,
                "init_ts": "2026-04-12T10:00:00Z",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertRegex(str(payload.get("agent_session_id") or ""), r"^as-[0-9a-f]{16}$")
        self.assertRegex(str(payload.get("identity_hash") or ""), r"^[0-9a-f]{64}$")
        return payload

    def _approve_token(
        self,
        client: TestClient,
        *,
        auth_cookie: str,
        agent_session_id: str,
        identity_hash: str,
        scope: str = "readonly",
        ttl: str = "86400",
        problem: str | None = None,
    ) -> tuple[str, str]:
        problem_slug = problem or self.problem
        request_resp = client.post(
            "/agent/v1/auth/request-access",
            json={
                "agent_session_id": agent_session_id,
                "identity_hash": identity_hash,
                "problem": problem_slug,
            },
        )
        self.assertEqual(request_resp.status_code, 200, request_resp.text)
        request_payload = request_resp.json()
        request_id = str(request_payload.get("request_id") or "")
        self.assertRegex(request_id, r"^ar-[0-9a-f]{16}$")

        approve_page = client.get(
            f"/agent/approve/{request_id}",
            headers={"cookie": auth_cookie},
            follow_redirects=False,
        )
        self.assertEqual(approve_page.status_code, 200, approve_page.text)
        self.assertIn(problem_slug, approve_page.text)

        approve = client.post(
            f"/agent/approve/{request_id}",
            data={"decision": "approve", "scope": scope, "ttl": ttl},
            headers={"cookie": auth_cookie, "origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(approve.status_code, 303, approve.text)

        first_poll = client.get(
            f"/agent/v1/auth/poll/{request_id}",
            params={"agent_session_id": agent_session_id, "identity_hash": identity_hash},
        )
        self.assertEqual(first_poll.status_code, 200, first_poll.text)
        first_payload = first_poll.json()
        self.assertEqual(str(first_payload.get("status") or ""), "approved")
        raw_token = str(first_payload.get("token") or "")
        self.assertRegex(raw_token, r"^poly_")

        second_poll = client.get(
            f"/agent/v1/auth/poll/{request_id}",
            params={"agent_session_id": agent_session_id, "identity_hash": identity_hash},
        )
        self.assertEqual(second_poll.status_code, 200, second_poll.text)
        second_payload = second_poll.json()
        self.assertEqual(str(second_payload.get("status") or ""), "approved")
        self.assertNotIn("token", second_payload)
        return (request_id, raw_token)

    @staticmethod
    def _bearer(raw_token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {raw_token}"}

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
            self.assertIn("missing origin", cross_site.text)

            ok = client.get("/agent/sessions", headers={"cookie": auth_cookie}, follow_redirects=False)
            self.assertEqual(ok.status_code, 200)
            self.assertIn("Connected Agents", ok.text)

    def test_agent_register_code_is_one_time_and_reuses_session_identity(self) -> None:
        username = self.random_id("agent-reg")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            first_connect = self._connect_agent(client, auth_cookie)
            first_register = self._register_agent(client, str(first_connect["register_url"]))

            reused_connect = self._connect_agent(client, auth_cookie)
            reused_register = self._register_agent(client, str(reused_connect["register_url"]))
            self.assertEqual(str(first_register["agent_session_id"]), str(reused_register["agent_session_id"]))
            self.assertEqual(str(first_register["identity_hash"]), str(reused_register["identity_hash"]))

            reused_attempt = client.post(
                str(urlparse(str(first_connect["register_url"])).path or ""),
                json={
                    "agent_name": "cursor-polygon-skill",
                    "desktop_id": "D-test",
                    "init_ts": "2026-04-12T10:00:00Z",
                },
            )
            self.assertEqual(reused_attempt.status_code, 410)

    def test_agent_token_revocation_and_disconnect_invalidate_access(self) -> None:
        username = self.random_id("agent-revoke")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            identity_hash = str(register["identity_hash"])
            _request_id, raw_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="readonly",
            )

            before = client.get("/agent/v1/workspace/status", headers=self._bearer(raw_token))
            self.assertEqual(before.status_code, 200, before.text)

            token_identity = config.agent_service.token_identity(raw_token)
            self.assertIsNotNone(token_identity)
            revoke = client.post(
                f"/agent/revoke/{token_identity.token_id}",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(revoke.status_code, 303)

            after_revoke = client.get("/agent/v1/workspace/status", headers=self._bearer(raw_token))
            self.assertEqual(after_revoke.status_code, 401)

            _request_id2, raw_token2 = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="readonly",
            )
            before_disconnect = client.get("/agent/v1/workspace/status", headers=self._bearer(raw_token2))
            self.assertEqual(before_disconnect.status_code, 200, before_disconnect.text)

            disconnect = client.post(
                f"/agent/disconnect/{session_id}",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(disconnect.status_code, 303)

            after_disconnect = client.get("/agent/v1/workspace/status", headers=self._bearer(raw_token2))
            self.assertEqual(after_disconnect.status_code, 401)

    def test_agent_scope_enforcement_and_acl_downgrade(self) -> None:
        username = self.random_id("agent-scope")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace = self._grant_problem_owner(username)
        self.assertTrue(workspace.exists())

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            identity_hash = str(register["identity_hash"])

            _readonly_request, readonly_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="readonly",
            )
            readonly_upload = client.post(
                "/agent/v1/workspace/upload",
                headers=self._bearer(readonly_token),
                data={"path": "notes/readonly.txt"},
                files={"file": ("readonly.txt", b"blocked")},
            )
            self.assertEqual(readonly_upload.status_code, 403, readonly_upload.text)

            _workspace_request, workspace_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="workspace",
            )
            upload = client.post(
                "/agent/v1/workspace/upload",
                headers=self._bearer(workspace_token),
                data={"path": "notes/agent.txt"},
                files={"file": ("agent.txt", b"hello agent")},
            )
            self.assertEqual(upload.status_code, 200, upload.text)
            self.assertTrue((workspace / "notes/agent.txt").exists())

            read_back = client.get(
                "/agent/v1/workspace/file",
                headers=self._bearer(workspace_token),
                params={"path": "notes/agent.txt"},
            )
            self.assertEqual(read_back.status_code, 200, read_back.text)
            self.assertEqual(str(read_back.json().get("content") or ""), "hello agent")

            delete = client.delete(
                "/agent/v1/workspace/files/notes/agent.txt",
                headers=self._bearer(workspace_token),
            )
            self.assertEqual(delete.status_code, 200, delete.text)
            self.assertFalse((workspace / "notes/agent.txt").exists())

            workspace_service.grant_repo_access(self.problem, username, "read")
            downgraded_upload = client.post(
                "/agent/v1/workspace/upload",
                headers=self._bearer(workspace_token),
                data={"path": "notes/downgraded.txt"},
                files={"file": ("downgraded.txt", b"blocked")},
            )
            self.assertEqual(downgraded_upload.status_code, 403, downgraded_upload.text)

            still_readable = client.get("/agent/v1/workspace/status", headers=self._bearer(workspace_token))
            self.assertEqual(still_readable.status_code, 200, still_readable.text)

    def test_agent_verification_export_workspace_and_commit_endpoints(self) -> None:
        username = self.random_id("agent-api")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace = self._grant_problem_owner(username)
        solution = workspace / "solutions" / "main.cpp"
        solution.parent.mkdir(parents=True, exist_ok=True)
        solution.write_text("#include <bits/stdc++.h>\nint main(){return 0;}\n", encoding="utf-8")

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]), desktop_id="D-api")
            session_id = str(register["agent_session_id"])
            identity_hash = str(register["identity_hash"])

            access_denied = client.post(
                "/agent/v1/auth/request-access",
                json={
                    "agent_session_id": session_id,
                    "identity_hash": identity_hash,
                    "problem": self.default_problem,
                },
            )
            self.assertEqual(access_denied.status_code, 404)

            _readonly_request, readonly_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="readonly",
            )
            traversal = client.get(
                "/agent/v1/workspace/file",
                headers=self._bearer(readonly_token),
                params={"path": "../secrets.txt"},
            )
            self.assertEqual(traversal.status_code, 400)

            verify = client.post("/agent/v1/verification/start", headers=self._bearer(readonly_token), json={})
            self.assertEqual(verify.status_code, 200, verify.text)
            verify_payload = verify.json()
            verification_id = str(verify_payload.get("verification_id") or "")
            self.assertRegex(verification_id, r"^ver-[0-9a-f]{12}$")
            self.assertEqual(str(verify_payload.get("status") or ""), "queued")

            verify_status = client.get(f"/agent/v1/verification/{verification_id}/status", headers=self._bearer(readonly_token))
            self.assertEqual(verify_status.status_code, 200, verify_status.text)
            self.assertIn(str(verify_status.json().get("status") or ""), {"running", "queued", "failed", "ok"})

            native_export = client.post(
                "/agent/v1/export/start",
                headers=self._bearer(readonly_token),
                json={"export_type": "native"},
            )
            self.assertEqual(native_export.status_code, 200, native_export.text)
            self.assertRegex(str(native_export.json().get("export_id") or ""), r"^exp-api-")

            icpc_export = client.post(
                "/agent/v1/export/start",
                headers=self._bearer(readonly_token),
                json={"export_type": "icpc"},
            )
            self.assertEqual(icpc_export.status_code, 200, icpc_export.text)
            self.assertRegex(str(icpc_export.json().get("export_id") or ""), r"^exp-api-")

            _workspace_request, workspace_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="workspace",
            )
            workspace_commit = client.post(
                "/agent/v1/commit",
                headers=self._bearer(workspace_token),
                json={"message": "should fail"},
            )
            self.assertEqual(workspace_commit.status_code, 403)

            commit_file = workspace / "notes" / "commit.txt"
            commit_file.parent.mkdir(parents=True, exist_ok=True)
            commit_file.write_text("commit me\n", encoding="utf-8")

            _commit_request, commit_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="commit",
            )
            missing_message = client.post("/agent/v1/commit", headers=self._bearer(commit_token), json={})
            self.assertEqual(missing_message.status_code, 400)

            commit_resp = client.post(
                "/agent/v1/commit",
                headers=self._bearer(commit_token),
                json={"message": "agent commit"},
            )
            self.assertEqual(commit_resp.status_code, 200, commit_resp.text)
            commit_payload = commit_resp.json()
            self.assertEqual(str(commit_payload.get("status") or ""), "ok")
            head = str(commit_payload.get("head") or "")
            self.assertRegex(head, r"^[0-9a-f]{40}$")

            commit_status = client.get(f"/agent/v1/commit/{head}/status", headers=self._bearer(commit_token))
            self.assertEqual(commit_status.status_code, 200, commit_status.text)
            self.assertEqual(str(commit_status.json().get("status") or ""), "published")


