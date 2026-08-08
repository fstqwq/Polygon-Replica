from __future__ import annotations

import io
import hashlib
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from tests.common import E2ETestBase
from tests.db_helpers import db_execute, db_fetch_one
from tests.ui_support import AUTH_COOKIE_NAME, _cookie_value_from_response, _register_with_password_envelope
from app.impl.runtime.config import config
from app.main import app
from app.service.verification.task_store import VerificationTaskStore

workspace_service = config.workspace_service


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

    def test_export_status_and_download_resolve_export_job_directly(self) -> None:
        username = self.random_id("agent-export-job")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)
        ctx = workspace_service.workspace_context(
            self.problem,
            username,
            include_recent=False,
        )
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        job_id = "agent-export-job-direct"
        export_id = "e-agent-export-direct"
        filename = "agent-package.zip"
        archive = config.export_service._export_path(
            self.problem,
            export_id,
            filename,
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"agent export payload")
        db_execute(
            """
            INSERT INTO problem_package_materializations(
                id,problem_id,source_commit,revision_number,source_digest,
                archive_rel_path,archive_sha256,archive_size_bytes,verification_id,
                status,created_at,checked_at,unavailable_reason
            ) VALUES(?,?,?,1,?,?,?,?,?,'available',?,?,'')
            """,
            [
                "pm-agent-export-direct",
                problem_id,
                "c" * 40,
                "0" * 64,
                archive.relative_to(config.settings.artifacts_root).as_posix(),
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                archive.stat().st_size,
                "pv-agent-export-direct",
                "2026-08-08T00:00:00Z",
                "2026-08-08T00:00:00Z",
            ],
        )
        db_execute(
            """
            INSERT INTO exports(
                id,problem_id,materialization_id,export_type,options_hash,
                filename,archive_rel_path,sha256,size_bytes,source_commit,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                "pm-agent-export-direct",
                "icpc",
                "0" * 64,
                filename,
                archive.relative_to(config.settings.artifacts_root).as_posix(),
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                archive.stat().st_size,
                "c" * 40,
                "2026-08-08T00:00:00Z",
            ],
        )
        config.export_service.create_export_job(
            job_id=job_id,
            problem_id=problem_id,
            actor_user_id=actor_user_id,
            export_type="icpc",
            source_commit="c" * 40,
        )
        config.export_service.mark_export_job_running(
            job_id,
            source_commit="c" * 40,
        )
        config.export_service.mark_export_job_succeeded(
            job_id,
            materialization_id="pm-agent-export-direct",
            export_id=export_id,
        )
        db_execute(
            """
            INSERT INTO audit_log(
                actor_user_id,problem_id,action,details_json,created_at
            ) VALUES(?,?,?,?,?)
            """,
            [
                actor_user_id,
                problem_id,
                "export.create",
                '{"status":"failed","error":"must-not-be-read"}',
                "2026-08-08T00:00:01Z",
            ],
        )

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(
                client,
                str(connect["register_url"]),
                desktop_id="D-export-job",
            )
            _request_id, token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=str(register["agent_session_id"]),
                identity_hash=str(register["identity_hash"]),
            )
            status = client.get(
                f"/agent/v1/export/{job_id}/status",
                headers=self._bearer(token),
            )
            self.assertEqual(status.status_code, 200, status.text)
            payload = status.json()
            self.assertEqual(str(payload.get("job_id") or ""), job_id)
            self.assertEqual(str(payload.get("status") or ""), "succeeded")
            self.assertEqual(str(payload.get("filename") or ""), filename)
            self.assertNotIn("must-not-be-read", status.text)

            download = client.get(
                f"/agent/v1/export/{job_id}/download",
                headers=self._bearer(token),
            )
            self.assertEqual(download.status_code, 200, download.text)
            self.assertEqual(download.content, b"agent export payload")

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
            self.assertIn("missing origin", cross_site.text)

            ok = client.get("/agent/sessions", headers={"cookie": auth_cookie}, follow_redirects=False)
            self.assertEqual(ok.status_code, 200)
            self.assertIn("Connected Agents", ok.text)
            self.assertNotIn("Disconnected at", ok.text)

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

    def test_agent_auth_status_reports_authorized_problems_and_updates_last_seen(self) -> None:
        username = self.random_id("agent-status")
        _password, auth_cookie = self._issue_auth_cookie(username)
        self._grant_problem_owner(username)
        stale_seen = "2000-01-01T00:00:00+00:00"

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]))
            session_id = str(register["agent_session_id"])
            identity_hash = str(register["identity_hash"])

            config.agent_service.store.touch_session(session_id, last_seen_at=stale_seen)
            empty_status = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(empty_status.status_code, 200, empty_status.text)
            empty_payload = empty_status.json()
            self.assertEqual(str(empty_payload.get("status") or ""), "ok")
            self.assertEqual(str(empty_payload.get("agent_session_id") or ""), session_id)
            self.assertEqual(str(empty_payload.get("user") or ""), username)
            self.assertEqual(list(empty_payload.get("authorized_problems") or []), [])
            touched_after_status = str(config.agent_service.store.session_by_id(session_id)["last_seen_at"])
            self.assertNotEqual(touched_after_status, stale_seen)
            self.assertEqual(touched_after_status, str(empty_payload.get("last_seen_at") or ""))

            config.agent_service.store.touch_session(session_id, last_seen_at=stale_seen)
            request_resp = client.post(
                "/agent/v1/auth/request-access",
                json={
                    "agent_session_id": session_id,
                    "identity_hash": identity_hash,
                    "problem": self.problem,
                },
            )
            self.assertEqual(request_resp.status_code, 200, request_resp.text)
            request_id = str(request_resp.json().get("request_id") or "")
            self.assertRegex(request_id, r"^ar-[0-9a-f]{16}$")
            self.assertEqual(int(request_resp.json().get("expires_in") or 0), 900)
            self.assertNotEqual(str(config.agent_service.store.session_by_id(session_id)["last_seen_at"]), stale_seen)

            config.agent_service.store.touch_session(session_id, last_seen_at=stale_seen)
            pending_poll = client.get(
                f"/agent/v1/auth/poll/{request_id}",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(pending_poll.status_code, 200, pending_poll.text)
            self.assertEqual(str(pending_poll.json().get("status") or ""), "pending")
            self.assertNotEqual(str(config.agent_service.store.session_by_id(session_id)["last_seen_at"]), stale_seen)

            approve = client.post(
                f"/agent/approve/{request_id}",
                data={"decision": "approve", "scope": "readonly", "ttl": "86400"},
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(approve.status_code, 303, approve.text)
            readonly_poll = client.get(
                f"/agent/v1/auth/poll/{request_id}",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(readonly_poll.status_code, 200, readonly_poll.text)
            readonly_token = str(readonly_poll.json().get("token") or "")
            self.assertRegex(readonly_token, r"^poly_")

            readonly_status = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(readonly_status.status_code, 200, readonly_status.text)
            readonly_items = list(readonly_status.json().get("authorized_problems") or [])
            self.assertEqual(len(readonly_items), 1)
            self.assertEqual(str(readonly_items[0].get("problem") or ""), self.problem)
            self.assertEqual(str(readonly_items[0].get("scope") or ""), "readonly")
            self.assertTrue(str(readonly_items[0].get("expires_at") or ""))

            _workspace_request_id, workspace_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="workspace",
            )
            workspace_status = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(workspace_status.status_code, 200, workspace_status.text)
            workspace_items = list(workspace_status.json().get("authorized_problems") or [])
            self.assertEqual(len(workspace_items), 1)
            self.assertEqual(str(workspace_items[0].get("problem") or ""), self.problem)
            self.assertEqual(str(workspace_items[0].get("scope") or ""), "workspace")

            workspace_service.grant_repo_access(self.problem, username, "read")
            downgraded_status = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(downgraded_status.status_code, 200, downgraded_status.text)
            downgraded_items = list(downgraded_status.json().get("authorized_problems") or [])
            self.assertEqual(len(downgraded_items), 1)
            self.assertEqual(str(downgraded_items[0].get("problem") or ""), self.problem)
            self.assertEqual(str(downgraded_items[0].get("scope") or ""), "readonly")

            workspace_service.grant_repo_access(self.problem, username, "owner")
            readonly_identity = config.agent_service.token_identity(readonly_token)
            workspace_identity = config.agent_service.token_identity(workspace_token)
            self.assertIsNotNone(readonly_identity)
            self.assertIsNotNone(workspace_identity)

            revoke_readonly = client.post(
                f"/agent/revoke/{readonly_identity.token_id}",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(revoke_readonly.status_code, 303)
            revoke_workspace = client.post(
                f"/agent/revoke/{workspace_identity.token_id}",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(revoke_workspace.status_code, 303)

            revoked_status = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(revoked_status.status_code, 200, revoked_status.text)
            self.assertEqual(list(revoked_status.json().get("authorized_problems") or []), [])

            bad_identity = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": session_id, "identity_hash": "bad"},
            )
            self.assertEqual(bad_identity.status_code, 401)

            bad_session = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": "as-missing", "identity_hash": identity_hash},
            )
            self.assertEqual(bad_session.status_code, 401)

            disconnect = client.post(
                f"/agent/disconnect/{session_id}",
                headers={"cookie": auth_cookie, "origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(disconnect.status_code, 303)
            self.assertIsNone(config.agent_service.store.session_by_id(session_id))
            token_count_row = db_fetch_one(
                "SELECT COUNT(*) AS n FROM agent_tokens WHERE agent_session_id=?",
                [session_id],
            )
            self.assertEqual(int(token_count_row["n"]), 0)
            request_count_row = db_fetch_one(
                "SELECT COUNT(*) AS n FROM agent_access_requests WHERE agent_session_id=?",
                [session_id],
            )
            self.assertEqual(int(request_count_row["n"]), 0)
            disconnected_status = client.get(
                "/agent/v1/auth/status",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(disconnected_status.status_code, 401)
            disconnected_request = client.post(
                "/agent/v1/auth/request-access",
                json={
                    "agent_session_id": session_id,
                    "identity_hash": identity_hash,
                    "problem": self.problem,
                },
            )
            self.assertEqual(disconnected_request.status_code, 401)
            disconnected_poll = client.get(
                f"/agent/v1/auth/poll/{request_id}",
                params={"agent_session_id": session_id, "identity_hash": identity_hash},
            )
            self.assertEqual(disconnected_poll.status_code, 401)
            sessions_page = client.get("/agent/sessions", headers={"cookie": auth_cookie}, follow_redirects=False)
            self.assertEqual(sessions_page.status_code, 200)
            self.assertIn("No connected agents.", sessions_page.text)
            self.assertNotIn("Disconnected at", sessions_page.text)

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
            self.assertIsNone(config.agent_service.store.session_by_id(session_id))

            after_disconnect = client.get("/agent/v1/workspace/status", headers=self._bearer(raw_token2))
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
                data={"path": "solutions/readonly.txt"},
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
                headers=self._bearer(workspace_token),
                params={"path": ".env"},
            )
            self.assertEqual(hidden_read.status_code, 400, hidden_read.text)

            hidden_list = client.get(
                "/agent/v1/workspace/files",
                headers=self._bearer(workspace_token),
            )
            self.assertEqual(hidden_list.status_code, 200, hidden_list.text)
            listed_paths = {str(item.get("path") or "") for item in hidden_list.json().get("entries") or []}
            self.assertNotIn(".env", listed_paths)
            self.assertNotIn("solutions/.cache", listed_paths)
            self.assertNotIn("solutions/.cache/secret.txt", listed_paths)

            invalid_root_upload = client.post(
                "/agent/v1/workspace/upload",
                headers=self._bearer(workspace_token),
                data={"path": "README.md"},
                files={"file": ("README.md", b"blocked")},
            )
            self.assertEqual(invalid_root_upload.status_code, 400, invalid_root_upload.text)

            hidden_upload = client.post(
                "/agent/v1/workspace/upload",
                headers=self._bearer(workspace_token),
                data={"path": ".env"},
                files={"file": ("env.txt", b"blocked")},
            )
            self.assertEqual(hidden_upload.status_code, 400, hidden_upload.text)

            hidden_delete = client.delete(
                "/agent/v1/workspace/files/.env",
                headers=self._bearer(workspace_token),
            )
            self.assertEqual(hidden_delete.status_code, 400, hidden_delete.text)

            read_back = client.get(
                "/agent/v1/workspace/file",
                headers=self._bearer(workspace_token),
                params={"path": "solutions/agent.txt"},
            )
            self.assertEqual(read_back.status_code, 200, read_back.text)
            self.assertEqual(str(read_back.json().get("content") or ""), "hello\r\nagent\r\n")

            delete = client.delete(
                "/agent/v1/workspace/files/solutions/agent.txt",
                headers=self._bearer(workspace_token),
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
                headers=self._bearer(workspace_token),
                data={"path": "solutions/downgraded.txt"},
                files={"file": ("downgraded.txt", b"blocked")},
            )
            self.assertEqual(downgraded_upload.status_code, 403, downgraded_upload.text)

            still_readable = client.get("/agent/v1/workspace/status", headers=self._bearer(workspace_token))
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
            identity_hash = str(register["identity_hash"])
            _readonly_request, readonly_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="readonly",
            )
            _workspace_request, workspace_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="workspace",
            )

            snapshot = client.get("/agent/v1/workspace/snapshot", headers=self._bearer(readonly_token))
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
                headers=self._bearer(readonly_token),
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
                headers=self._bearer(readonly_token),
                files={"archive": ("workspace.zip", local_zip, "application/zip")},
            )
            self.assertEqual(readonly_apply.status_code, 403, readonly_apply.text)

            conflict = client.post(
                "/agent/v1/workspace/apply",
                headers=self._bearer(workspace_token),
                data={"base_head_commit": "not-current-head"},
                files={"archive": ("workspace.zip", local_zip, "application/zip")},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)

            invalid_zip = self._workspace_zip({"README.md": "bad\n"})
            invalid_compare = client.post(
                "/agent/v1/workspace/compare",
                headers=self._bearer(readonly_token),
                files={"archive": ("workspace.zip", invalid_zip, "application/zip")},
            )
            self.assertEqual(invalid_compare.status_code, 400, invalid_compare.text)
            answer_zip = self._workspace_zip({"tests/answers/001.ans": "bad\n"})
            invalid_answer_compare = client.post(
                "/agent/v1/workspace/compare",
                headers=self._bearer(readonly_token),
                files={"archive": ("workspace.zip", answer_zip, "application/zip")},
            )
            self.assertEqual(invalid_answer_compare.status_code, 400, invalid_answer_compare.text)

            apply = client.post(
                "/agent/v1/workspace/apply",
                headers=self._bearer(workspace_token),
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
        with self.assertRaisesRegex(ValueError, "workspace archive entry is too large"):
            config.workspace_archive_service.compare_zip(workspace, oversized_entry_zip, max_bytes=16)
        with self.assertRaisesRegex(ValueError, "workspace archive payload is too large"):
            config.workspace_archive_service.compare_zip(workspace, oversized_total_zip, max_bytes=16)
        with self.assertRaisesRegex(ValueError, "workspace archive payload is too large"):
            config.workspace_archive_service.apply_zip(workspace, oversized_total_zip, max_bytes=16)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_agent_verification_export_workspace_and_commit_endpoints(self) -> None:
        username = self.random_id("agent-api")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace = self._grant_problem_owner(username)
        solution = workspace / "solutions" / "main.cpp"
        solution.parent.mkdir(parents=True, exist_ok=True)
        solution.write_text("#include <bits/stdc++.h>\nint main(){return 0;}\n", encoding="utf-8")
        stale_head = str(workspace_service.read_workspace_status(workspace).get("head_commit") or "")
        workspace_ctx = workspace_service.workspace_context(self.problem, username, include_recent=False)
        workspace_id = int(workspace_ctx["workspace"]["id"])

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
            self.assertEqual(native_export.status_code, 400, native_export.text)
            self.assertIn("no committed revision", native_export.text)

            icpc_export = client.post(
                "/agent/v1/export/start",
                headers=self._bearer(readonly_token),
                json={"export_type": "icpc"},
            )
            self.assertEqual(icpc_export.status_code, 400, icpc_export.text)
            self.assertIn("no committed revision", icpc_export.text)

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
            commit_row = db_fetch_one("SELECT head_commit,dirty FROM workspaces WHERE id=?", [workspace_id])
            self.assertIsNotNone(commit_row)
            self.assertEqual(str(commit_row["head_commit"] or ""), head)
            self.assertEqual(int(commit_row["dirty"] or 0), 0)

            commit_status = client.get(f"/agent/v1/commit/{head}/status", headers=self._bearer(commit_token))
            self.assertEqual(commit_status.status_code, 200, commit_status.text)
            self.assertEqual(str(commit_status.json().get("status") or ""), "published")
            self.assertNotEqual(stale_head, head)

            db_execute(
                "UPDATE workspaces SET head_commit=?, dirty=1 WHERE id=?",
                [stale_head, workspace_id],
            )

            live_status = client.get("/agent/v1/workspace/status", headers=self._bearer(readonly_token))
            self.assertEqual(live_status.status_code, 200, live_status.text)
            self.assertEqual(str(live_status.json().get("head_commit") or ""), head)
            self.assertFalse(bool(live_status.json().get("dirty")))

            fresh_icpc_export = client.post(
                "/agent/v1/export/start",
                headers=self._bearer(readonly_token),
                json={"export_type": "icpc"},
            )
            self.assertEqual(fresh_icpc_export.status_code, 200, fresh_icpc_export.text)
            fresh_export_job_id = str(fresh_icpc_export.json().get("job_id") or "")
            self.assertRegex(fresh_export_job_id, r"^exp-api-")
            fresh_export_status = client.get(f"/agent/v1/export/{fresh_export_job_id}/status", headers=self._bearer(readonly_token))
            self.assertEqual(fresh_export_status.status_code, 200, fresh_export_status.text)
            self.assertEqual(str(fresh_export_status.json().get("source_commit") or ""), head)

            fresh_native_export = client.post(
                "/agent/v1/export/start",
                headers=self._bearer(readonly_token),
                json={"export_type": "native"},
            )
            self.assertEqual(fresh_native_export.status_code, 200, fresh_native_export.text)
            fresh_native_job_id = str(fresh_native_export.json().get("job_id") or "")
            self.assertRegex(fresh_native_job_id, r"^exp-api-")
            fresh_native_status = client.get(f"/agent/v1/export/{fresh_native_job_id}/status", headers=self._bearer(readonly_token))
            self.assertEqual(fresh_native_status.status_code, 200, fresh_native_status.text)
            self.assertEqual(str(fresh_native_status.json().get("source_commit") or ""), head)

    def test_agent_verification_detail_returns_yaml_table_and_zoom(self) -> None:
        username = self.random_id("agent-detail")
        _password, auth_cookie = self._issue_auth_cookie(username)
        workspace = self._grant_problem_owner(username)
        source = workspace / "solutions" / "ac_python.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('ok')\n", encoding="utf-8")

        with TestClient(app, raise_server_exceptions=False) as client:
            connect = self._connect_agent(client, auth_cookie)
            register = self._register_agent(client, str(connect["register_url"]), desktop_id="D-detail")
            session_id = str(register["agent_session_id"])
            identity_hash = str(register["identity_hash"])
            _readonly_request, readonly_token = self._approve_token(
                client,
                auth_cookie=auth_cookie,
                agent_session_id=session_id,
                identity_hash=identity_hash,
                scope="readonly",
            )

            ctx = workspace_service.workspace_context(self.problem, username, include_recent=False)
            problem_id = int(ctx["problem"]["id"])
            workspace_id = int(ctx["workspace"]["id"])
            verification_id = config.verification_service.allocate_verification_id()
            config.verification_service.begin_verification_record(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                signature="",
                kind="all",
                status="failed",
                detail={
                    "mode": "pass-fail",
                    "selected_test_names": ["001.in"],
                    "source_paths": ["solutions/ac_python.py"],
                    "sanity_status": "warning",
                    "sanity_checked_count": 3,
                    "sanity_checks": ["empty_output_stability", "unicode_output_stability", "boundary_coverage"],
                    "validation_status": "warning",
                    "validated_count": 3,
                    "failed_step": "sanity",
                    "failed_check": "boundary_coverage",
                    "error": "Test data did not hit: n max=3",
                    "sanity_check_results": [
                        {"name": "empty_output_stability", "status": "passed", "checked_count": 1, "messages": []},
                        {"name": "unicode_output_stability", "status": "passed", "checked_count": 1, "messages": []},
                        {
                            "name": "boundary_coverage",
                            "status": "warning",
                            "checked_count": 1,
                            "messages": [
                                {"severity": "warning", "test_name": "", "message": "Test data did not hit: n max=3"},
                                {"severity": "warning", "test_name": "", "message": "Test data did not hit: m min=1"},
                            ],
                        },
                    ],
                },
            )
            task_store = config.verification_task_store
            task_store.replace_graph(
                verification_id,
                tasks=[
                    {
                        "id": task_store.allocate_id(),
                        "task_kind": "solution-run",
                        "source_path": "solutions/ac_python.py",
                        "logical_run_id": "run-ac-python",
                        "test_name": "001.in",
                        "expected_behavior": "accepted",
                        "status": VerificationTaskStore.TASK_DONE,
                        "verdict": "TL",
                        "runtime_sec": 1.5,
                        "cpu_sec": 1.4,
                        "wall_sec": 1.6,
                        "memory_kb": 65536,
                        "compile_log": "",
                        "diagnostics_json": '[{"kind":"runtime","message":"time limit exceeded"}]',
                        "error_text": "required=[AC], allowed=[AC], got=[TL]",
                        "feedback_text": "time limit exceeded",
                        "output_ref": "blob-output",
                    }
                ],
                edges=[],
            )
            config.verification_service.update_verification_record_status(
                verification_id,
                status="failed",
                fail_reason="required=[AC], allowed=[AC], got=[TL]",
                finished=True,
            )

            detail_resp = client.get(
                f"/agent/v1/verification/{verification_id}/detail",
                headers=self._bearer(readonly_token),
            )
            self.assertEqual(detail_resp.status_code, 200, detail_resp.text)
            self.assertIn("text/plain", detail_resp.headers.get("content-type", ""))
            self.assertIn(f"verification: {verification_id}", detail_resp.text)
            self.assertIn("status: failed", detail_resp.text)
            self.assertIn("tasks:", detail_resp.text)
            self.assertIn("sanity:", detail_resp.text)
            self.assertIn("status: warning", detail_resp.text)
            self.assertIn('reason: "Test data did not hit: n max=3"', detail_resp.text)
            self.assertIn("ran: 3", detail_resp.text)
            self.assertIn("total: 3", detail_resp.text)
            self.assertIn("name: boundary_coverage", detail_resp.text)
            self.assertIn("label: Boundary coverage", detail_resp.text)
            self.assertIn("messages:", detail_resp.text)
            self.assertIn('- "Test data did not hit: n max=3"', detail_resp.text)
            self.assertIn('- "Test data did not hit: m min=1"', detail_resp.text)
            self.assertNotIn("sanity_status:", detail_resp.text)
            self.assertNotIn("sanity_checks:", detail_resp.text)
            self.assertIn("columns:", detail_resp.text)
            self.assertIn("ac_python.py:", detail_resp.text)
            self.assertIn("source: solutions/ac_python.py", detail_resp.text)
            self.assertIn("result: TL 1500ms 64MB", detail_resp.text)
            self.assertIn("001.in: TL 1500ms 64MB", detail_resp.text)
            self.assertNotIn("runtime_summary", detail_resp.text)
            self.assertNotIn("task_rows", detail_resp.text)
            self.assertNotIn("runs:", detail_resp.text)

            zoom_resp = client.get(
                f"/agent/v1/verification/{verification_id}/detail",
                headers=self._bearer(readonly_token),
                params={"test_name": "001.in"},
            )
            self.assertEqual(zoom_resp.status_code, 200, zoom_resp.text)
            self.assertIn("test: 001.in", zoom_resp.text)
            self.assertIn("ac_python.py:", zoom_resp.text)
            self.assertIn("result: TL 1400ms (1600ms wall) 64MB", zoom_resp.text)
            self.assertIn("feedback: time limit exceeded", zoom_resp.text)
            self.assertIn("diagnostics:", zoom_resp.text)

            source_zoom_resp = client.get(
                f"/agent/v1/verification/{verification_id}/detail",
                headers=self._bearer(readonly_token),
                params={"test_name": "001.in", "source": "solutions/ac_python.py"},
            )
            self.assertEqual(source_zoom_resp.status_code, 200, source_zoom_resp.text)
            self.assertIn("cell:", source_zoom_resp.text)
            self.assertIn("title: ac_python.py", source_zoom_resp.text)
            self.assertIn("result: TL 1400ms (1600ms wall) 64MB", source_zoom_resp.text)

            missing_source_resp = client.get(
                f"/agent/v1/verification/{verification_id}/detail",
                headers=self._bearer(readonly_token),
                params={"test_name": "001.in", "source": "solutions/missing.cpp"},
            )
            self.assertEqual(missing_source_resp.status_code, 404, missing_source_resp.text)
            self.assertEqual(missing_source_resp.text, "source detail not found\n")

            removed_text_resp = client.get(
                f"/agent/v1/verification/{verification_id}/detail/text",
                headers=self._bearer(readonly_token),
            )
            self.assertEqual(removed_text_resp.status_code, 404)
