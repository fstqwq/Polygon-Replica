from __future__ import annotations

from tests.db_helpers import db_execute, db_fetch_one, write_verification_summary

import asyncio
import base64
import json
import re
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from tests.common import E2ETestBase, suite_root
from app.impl.runtime.config import config
from app.impl.problem.checker import checker_rename_source, checker_set_standard
from app.impl.problem.file import (
    files_create_template,
    files_delete,
    files_download,
    files_new,
    files_rename,
    files_save,
    files_upload,
)
from app.impl.problem.generator import generator_create_template, generator_rename_source
from app.impl.problem.interactor import interactor_create_template, interactor_rename_source, interactor_save_source
from app.impl.problem.solution import (
    solutions_create_template,
    solutions_delete,
    solutions_rename,
    solutions_save_source,
    solutions_set_tag,
)
from app.impl.problem.validator import validator_create_template, validator_rename_source, validator_save_source
from app.impl.run_export.artifact import artifact_file
from app.impl.run_export.run import run_cancel, run_execute
from app.impl.root.auth_pages import auth_password_meta, login_page
from app.main_util import TEXTAREA_MAX_BYTES
from app.service.verification.task_store import VerificationTaskStore
from tests.ui_support import _register_with_password_envelope

db = config.db
workspace_service = config.workspace_service

FLASH_COOKIE_NAME = config.constants.FLASH_COOKIE_NAME


def _request(
    path: str,
    query: str = "",
    *,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "headers": headers or [],
            "client": ("127.0.0.1", 0),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        }
    )


def _post_request(path: str, *, origin: str = "http://testserver") -> Request:
    return _request(path, method="POST", headers=[(b"origin", origin.encode("utf-8"))])


def _extract_hidden_input_value(html: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="([^"]*)"', html, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


def _response_set_cookie_headers(response) -> list[str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    values: list[str] = []
    try:
        values = [str(item or "") for item in headers.getlist("set-cookie")]
    except Exception:
        values = []
    if values:
        return values
    raw_headers = list(getattr(response, "raw_headers", []) or [])
    for key, value in raw_headers:
        if bytes(key).lower() == b"set-cookie":
            values.append(bytes(value).decode("latin-1", errors="ignore"))
    if values:
        return values
    single = str(headers.get("set-cookie", "") or "")
    return [single] if single else []


def _extract_cookie_value(set_cookie: str | list[str], cookie_name: str) -> str:
    prefix = f"{cookie_name}="
    headers = [str(item or "") for item in set_cookie] if isinstance(set_cookie, list) else [str(set_cookie or "")]
    for header in headers:
        first = str(header).split(";", 1)[0].strip()
        if first.startswith(prefix):
            return first[len(prefix) :]
    return ""


def _flash_messages_from_response(response) -> list[str]:
    token = _extract_cookie_value(_response_set_cookie_headers(response), FLASH_COOKIE_NAME)
    if not token:
        return []
    try:
        padded = token + ("=" * ((4 - (len(token) % 4)) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    result: list[str] = []
    for item in payload:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


class TestSecurity(E2ETestBase):
    seed_default_workspace = True

    class _FakeUpload:
        def __init__(self, data: bytes):
            self._buf = data
            self._offset = 0

        async def read(self, size: int = -1) -> bytes:
            if size is None or size < 0:
                size = len(self._buf) - self._offset
            if self._offset >= len(self._buf):
                return b""
            end = min(len(self._buf), self._offset + int(size))
            chunk = self._buf[self._offset : end]
            self._offset = end
            return chunk

        async def close(self) -> None:
            return None

    def _first_flash_message(self, response) -> str:
        messages = _flash_messages_from_response(response)
        self.assertTrue(messages)
        return str(messages[0] or "")

    def _fixture_verification_root(self, *, problem: str, workspace_id: int, verification_id: str) -> tuple[str, Path]:
        artifact_root = config.fs_manager.prepare_verification_root(str(verification_id or "").strip()).resolve()
        return "", artifact_root

    def test_auth_password_meta_ignores_sql_injection_style_username(self) -> None:
        username = self.random_id("secsql")
        password = "StrongPass123"
        registered = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(registered.status_code, 303)

        row = db_fetch_one("SELECT password_salt FROM users WHERE username=?", [username])
        self.assertIsNotNone(row)
        real_salt = str(row["password_salt"] or "").strip().lower()
        self.assertRegex(real_salt, r"^[0-9a-f]{32}$")

        login_resp = login_page(_request("/login"))
        self.assertEqual(login_resp.status_code, 200)
        login_html = login_resp.body.decode("utf-8", errors="replace")
        csrf = _extract_hidden_input_value(login_html, "csrf_token")
        self.assertTrue(csrf)

        normal_meta = auth_password_meta(username=username, csrf_token=csrf)
        self.assertEqual(str(normal_meta.get("salt") or "").strip().lower(), real_salt)

        injected_username = f"{username}' OR 1=1 --"
        injected_meta = auth_password_meta(username=injected_username, csrf_token=csrf)
        injected_salt = str(injected_meta.get("salt") or "").strip().lower()
        self.assertRegex(injected_salt, r"^[0-9a-f]{32}$")
        self.assertNotEqual(injected_salt, real_salt)

    def test_artifact_download_denies_cross_workspace_access(self) -> None:
        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        workspace_service.ensure_workspace("alice/sample", "bob")

        alice_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])

        verification_id = f"ver-sec-artifact-{uuid.uuid4().hex[:8]}"
        _build_ref, artifact_root = self._fixture_verification_root(
            problem="alice/sample",
            workspace_id=alice_workspace_id,
            verification_id=verification_id,
        )
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs" / "compile.log").write_text("ok\n", encoding="utf-8")
        db_execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,fail_reason,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                problem_id,
                alice_workspace_id,
                "",
                "all",
                "ok",
                "",
                "2026-02-25T00:00:00Z",
                "2026-02-25T00:00:01Z",
            ],
        )
        write_verification_summary(verification_id, {"status": "ok"})

        with self.assertRaises(HTTPException) as denied:
            artifact_file("alice/sample", "bob", verification_id, "logs/compile.log")
        self.assertEqual(denied.exception.status_code, 404)
        self.assertIn("workspace", str(denied.exception.detail))

    def test_artifact_download_rejects_path_traversal(self) -> None:
        alice_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])

        verification_id = f"ver-sec-path-{uuid.uuid4().hex[:8]}"
        _build_ref, artifact_root = self._fixture_verification_root(
            problem="alice/sample",
            workspace_id=alice_workspace_id,
            verification_id=verification_id,
        )
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs" / "compile.log").write_text("ok\n", encoding="utf-8")
        db_execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,fail_reason,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                problem_id,
                alice_workspace_id,
                "",
                "all",
                "ok",
                "",
                "2026-02-25T00:00:00Z",
                "2026-02-25T00:00:01Z",
            ],
        )
        write_verification_summary(verification_id, {"status": "ok"})

        with self.assertRaises(HTTPException) as denied:
            artifact_file("alice/sample", "alice", verification_id, "../outside.txt")
        self.assertEqual(denied.exception.status_code, 400)
        self.assertIn("invalid artifact path", str(denied.exception.detail))

    def test_run_cancel_rejects_foreign_workspace_verification_id(self) -> None:
        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        workspace_service.ensure_workspace("alice/sample", "bob")

        alice_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])

        verification_id = f"ver-sec-cancel-{uuid.uuid4().hex[:8]}"
        run_id = f"r-sec-cancel-{uuid.uuid4().hex[:8]}"
        db_execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,fail_reason,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                problem_id,
                alice_workspace_id,
                "",
                "all",
                "running",
                "",
                "2026-04-13T00:00:00Z",
                None,
            ],
        )
        config.verification_task_store.replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-sec-cancel-1",
                    "task_kind": "solution-run",
                    "source_path": "solutions/accepted.cpp",
                    "logical_run_id": run_id,
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_QUEUED,
                    "run_id": run_id,
                    "judgehost_task_id": "jt-sec-cancel-1",
                }
            ],
            edges=[],
        )

        with patch.object(
            config.judgehost_task_service,
            "request_verification_cancel",
            return_value={
                "cancelled_cases": 0,
                "awaiting_receipts": 0,
                "affected_tasks": 0,
                "affected_batches": 0,
            },
        ) as cancel_execution:
            resp = run_cancel("alice/sample", "bob", verification_id=verification_id)

        self.assertEqual(resp.status_code, 303)
        self.assertIn("verification not found", self._first_flash_message(resp).lower())
        cancel_execution.assert_not_called()
        verification_row = db_fetch_one("SELECT status,finished_at FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or "").lower(), "running")
        self.assertFalse(str(verification_row["finished_at"] or "").strip())
        task_rows = config.verification_task_store.list_rows(verification_id)
        self.assertEqual(len(task_rows), 1)
        self.assertEqual(str(task_rows[0]["status"] or ""), VerificationTaskStore.TASK_QUEUED)

    def test_artifact_blob_download_rejects_foreign_cache_token(self) -> None:
        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        workspace_service.ensure_workspace("alice/sample", "bob")

        alice_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        bob_ctx = workspace_service.workspace_context("alice/sample", "bob", include_recent=False)
        problem_id = int(alice_ctx["problem"]["id"])
        alice_workspace_id = int(alice_ctx["workspace"]["id"])
        bob_workspace_id = int(bob_ctx["workspace"]["id"])

        foreign_verification_id = f"ver-sec-blob-a-{uuid.uuid4().hex[:8]}"
        local_verification_id = f"ver-sec-blob-b-{uuid.uuid4().hex[:8]}"
        for verification_id, workspace_id in [
            (foreign_verification_id, alice_workspace_id),
            (local_verification_id, bob_workspace_id),
        ]:
            db_execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,fail_reason,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    problem_id,
                    workspace_id,
                    "",
                    "all",
                    "ok",
                    "",
                    "2026-04-13T00:00:00Z",
                    "2026-04-13T00:00:01Z",
                ],
            )

        token = config.verification_service.store_verification_blob(
            verification_id=foreign_verification_id,
            test_name="001.in",
            role="output",
            file_name="program.out",
            payload=b"secret-output\n",
        )
        encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")

        with self.assertRaises(HTTPException) as denied:
            artifact_file(
                "alice/sample",
                "bob",
                local_verification_id,
                f"blob/{encoded}/program.out",
            )
        self.assertEqual(denied.exception.status_code, 404)
        self.assertIn("artifact", str(denied.exception.detail).lower())
        self.assertIn("not found", str(denied.exception.detail).lower())

    def test_files_save_rejects_path_traversal_escape(self) -> None:
        marker = suite_root() / f"files-save-escape-{uuid.uuid4().hex[:8]}.txt"
        marker.unlink(missing_ok=True)
        resp = files_save(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
            content="pwned\n",
        )
        self.assertEqual(resp.status_code, 303)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("invalid path", messages[0].lower())
        self.assertFalse(marker.exists())

    def test_files_upload_rejects_path_traversal_escape(self) -> None:
        marker = suite_root() / f"files-upload-escape-{uuid.uuid4().hex[:8]}.txt"
        marker.unlink(missing_ok=True)
        upload = self._FakeUpload(b"owned\n")
        with self.assertRaises(HTTPException) as denied:
            asyncio.run(
                files_upload(
                    problem="alice/sample",
                    user="alice",
                    path="../../" + marker.name,
                    upload=upload,
                )
            )
        self.assertEqual(denied.exception.status_code, 400)
        self.assertIn("invalid path", str(denied.exception.detail).lower())
        self.assertFalse(marker.exists())

    def test_files_save_rejects_textarea_content_over_shared_limit(self) -> None:
        oversized = ("x" * (TEXTAREA_MAX_BYTES + 32)) + "\n"
        resp = files_save(
            problem="alice/sample",
            user="alice",
            path="notes/oversized.txt",
            content=oversized,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("file content is too long", self._first_flash_message(resp).lower())

    def test_files_upload_rejects_payload_over_shared_upload_limit(self) -> None:
        upload = self._FakeUpload(b"123456789")
        with patch("app.main_util.UPLOAD_MAX_BYTES", 8):
            with self.assertRaises(HTTPException) as denied:
                asyncio.run(
                    files_upload(
                        problem="alice/sample",
                        user="alice",
                        path="notes/upload-too-large.txt",
                        upload=upload,
                    )
                )
        self.assertEqual(denied.exception.status_code, 400)
        self.assertIn("uploaded file is too large", str(denied.exception.detail).lower())

    def test_files_new_rejects_path_traversal_escape(self) -> None:
        marker = suite_root() / f"files-new-escape-{uuid.uuid4().hex[:8]}.txt"
        marker.unlink(missing_ok=True)
        resp = files_new(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("invalid path", self._first_flash_message(resp).lower())
        self.assertFalse(marker.exists())

    def test_files_create_template_rejects_path_traversal_escape(self) -> None:
        marker = suite_root() / f"files-template-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = files_create_template(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
            kind="checker",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("template is only available", self._first_flash_message(resp).lower())
        self.assertFalse(marker.exists())

    def test_files_delete_rejects_path_traversal_escape(self) -> None:
        marker = suite_root() / f"files-delete-escape-{uuid.uuid4().hex[:8]}.txt"
        marker.unlink(missing_ok=True)
        resp = files_delete(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("invalid path", self._first_flash_message(resp).lower())
        self.assertFalse(marker.exists())

    def test_files_delete_redirect_preserves_query_delimiter_for_directory_only(self) -> None:
        resp = files_delete(
            problem="alice/sample",
            user="alice",
            path="notes/missing-delete-target.txt",
            dir="notes",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(
            resp.headers.get("location", ""),
            "/problems/alice/sample/files?dir=notes",
        )

    def test_files_rename_rejects_destination_path_traversal(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        old_rel = f"notes/security-rename-{uuid.uuid4().hex[:8]}.txt"
        old_abs = ws / old_rel
        old_abs.parent.mkdir(parents=True, exist_ok=True)
        old_abs.write_text("keep\n", encoding="utf-8")

        marker = suite_root() / f"files-rename-escape-{uuid.uuid4().hex[:8]}.txt"
        marker.unlink(missing_ok=True)
        resp = files_rename(
            problem="alice/sample",
            user="alice",
            old_path=old_rel,
            new_path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("invalid path", messages[0].lower())
        self.assertTrue(old_abs.exists())
        self.assertFalse(marker.exists())

    def test_files_rename_rejects_hidden_destination_path(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        old_rel = f"notes/security-hidden-rename-{uuid.uuid4().hex[:8]}.txt"
        old_abs = ws / old_rel
        old_abs.parent.mkdir(parents=True, exist_ok=True)
        old_abs.write_text("keep\n", encoding="utf-8")

        resp = files_rename(
            problem="alice/sample",
            user="alice",
            old_path=old_rel,
            new_path="notes/.env",
        )
        self.assertEqual(resp.status_code, 303)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("hidden path is not allowed", messages[0].lower())
        self.assertTrue(old_abs.exists())
        self.assertFalse((ws / "notes/.env").exists())

    def test_files_download_rejects_path_traversal_escape(self) -> None:
        with self.assertRaises(HTTPException) as denied:
            files_download("alice/sample", "alice", "../../outside.txt")
        self.assertEqual(denied.exception.status_code, 400)
        self.assertIn("invalid path", str(denied.exception.detail).lower())

    def test_generator_create_template_path_traversal_stays_in_workspace(self) -> None:
        marker = suite_root() / f"generator-template-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = generator_create_template(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(marker.exists())
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self.assertTrue((ws / "generators" / "generator.cpp").exists())

    def test_validator_create_template_path_traversal_stays_in_workspace(self) -> None:
        marker = suite_root() / f"validator-template-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = validator_create_template(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(marker.exists())
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self.assertTrue((ws / "validators" / "validator.cpp").exists())

    def test_validator_save_source_path_traversal_stays_in_workspace(self) -> None:
        marker = suite_root() / f"validator-save-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        content = "int main(){return 0;}\n"
        with patch("app.impl.problem.validator.judgehost_compile_check_error", return_value=""):
            resp = validator_save_source(
                problem="alice/sample",
                user="alice",
                path="../../" + marker.name,
                content=content,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(marker.exists())
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        target = ws / "validators" / "validator.cpp"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_interactor_create_template_path_traversal_stays_in_workspace(self) -> None:
        marker = suite_root() / f"interactor-template-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = interactor_create_template(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(marker.exists())
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self.assertTrue((ws / "interactors" / "interactor.cpp").exists())

    def test_interactor_save_source_path_traversal_stays_in_workspace(self) -> None:
        marker = suite_root() / f"interactor-save-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        content = "int main(){return 0;}\n"
        with patch("app.impl.problem.interactor.judgehost_compile_check_error", return_value=""):
            resp = interactor_save_source(
                problem="alice/sample",
                user="alice",
                path="../../" + marker.name,
                content=content,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(marker.exists())
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        target = ws / "interactors" / "interactor.cpp"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_component_source_rename_rejects_destination_path_traversal(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        marker = suite_root() / f"component-rename-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        cases = [
            (checker_rename_source, "checkers/security_checker.cpp"),
            (validator_rename_source, "validators/security_validator.cpp"),
            (interactor_rename_source, "interactors/security_interactor.cpp"),
            (generator_rename_source, "generators/security_generator.cpp"),
        ]
        for rename_func, old_rel in cases:
            old_abs = ws / old_rel
            old_abs.parent.mkdir(parents=True, exist_ok=True)
            old_abs.write_text("// keep\n", encoding="utf-8")
            resp = rename_func(
                problem=self.problem,
                user=self.user,
                old_path=old_rel,
                new_path="../../" + marker.name,
            )
            self.assertEqual(resp.status_code, 303)
            self.assertFalse(marker.exists())
            self.assertTrue(old_abs.exists())
            self.assertIn("new ", self._first_flash_message(resp).lower())

    def test_component_source_rename_rejects_existing_destination(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        old_rel = "validators/security_rename_old.cpp"
        new_rel = "validators/security_rename_new.cpp"
        (ws / old_rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / old_rel).write_text("// old\n", encoding="utf-8")
        (ws / new_rel).write_text("// existing\n", encoding="utf-8")

        resp = validator_rename_source(
            problem=self.problem,
            user=self.user,
            old_path=old_rel,
            new_path=new_rel,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertTrue((ws / old_rel).exists())
        self.assertEqual((ws / new_rel).read_text(encoding="utf-8"), "// existing\n")
        self.assertIn("destination source already exists", self._first_flash_message(resp).lower())

    def test_component_source_rename_rejects_source_path_traversal(self) -> None:
        ws = Path(workspace_service.ensure_workspace(self.problem, self.user))
        default_rel = "validators/validator.cpp"
        (ws / default_rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / default_rel).write_text("// default validator\n", encoding="utf-8")

        resp = validator_rename_source(
            problem=self.problem,
            user=self.user,
            old_path="../../escape.cpp",
            new_path="renamed_validator.cpp",
        )

        self.assertEqual(resp.status_code, 303)
        self.assertTrue((ws / default_rel).exists())
        self.assertFalse((ws / "validators/renamed_validator.cpp").exists())
        self.assertIn("validator source is required", self._first_flash_message(resp).lower())

    def test_solutions_create_template_path_traversal_stays_in_workspace(self) -> None:
        marker = suite_root() / f"solution-template-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = solutions_create_template(
            problem="alice/sample",
            user="alice",
            path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(marker.exists())
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self.assertTrue((ws / "solutions" / "accepted.cpp").exists())
        self.assertTrue((ws / "solutions" / "accepted.cpp.desc").exists())

    def test_solutions_save_source_rejects_path_traversal_escape(self) -> None:
        marker = suite_root() / f"solution-save-escape-{uuid.uuid4().hex[:8]}.py"
        marker.unlink(missing_ok=True)
        resp = solutions_save_source(
            request=_post_request("/problems/alice/sample/solutions/editor"),
            problem="alice/sample",
            user="alice",
            source_path="../../" + marker.name,
            content="print(0)\n",
            expected_behavior="accepted",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(self._first_flash_message(resp).strip())
        self.assertFalse(marker.exists())

    def test_solutions_set_tag_rejects_path_traversal_escape(self) -> None:
        marker = suite_root() / f"solution-tag-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = solutions_set_tag(
            problem="alice/sample",
            user="alice",
            source_path="../../" + marker.name,
            expected_behavior="accepted",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(self._first_flash_message(resp).strip())
        self.assertFalse(marker.exists())

    def test_solutions_rename_rejects_destination_path_traversal(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        old_rel = "solutions/rename-old.cpp"
        old_abs = ws / old_rel
        old_abs.parent.mkdir(parents=True, exist_ok=True)
        old_abs.write_text("int main(){return 0;}\n", encoding="utf-8")

        marker = suite_root() / f"solution-rename-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = solutions_rename(
            problem="alice/sample",
            user="alice",
            old_path=old_rel,
            new_path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(self._first_flash_message(resp).strip())
        self.assertTrue(old_abs.exists())
        self.assertFalse(marker.exists())

    def test_solutions_delete_rejects_path_traversal_escape(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        keep_rel = "solutions/keep.cpp"
        keep_abs = ws / keep_rel
        keep_abs.parent.mkdir(parents=True, exist_ok=True)
        keep_abs.write_text("int main(){return 0;}\n", encoding="utf-8")

        marker = suite_root() / f"solution-delete-escape-{uuid.uuid4().hex[:8]}.cpp"
        marker.unlink(missing_ok=True)
        resp = solutions_delete(
            problem="alice/sample",
            user="alice",
            source_path="../../" + marker.name,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(self._first_flash_message(resp).strip())
        self.assertTrue(keep_abs.exists())
        self.assertFalse(marker.exists())

    def test_run_execute_sanitizes_path_traversal_solution_paths_before_queue(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        captured: dict[str, object] = {}

        def _fake_start_verification_job(*args, **kwargs):
            captured["targets"] = list(kwargs.get("targets") or [])
            return True

        with patch("app.impl.run_export.run.start_verification_job", side_effect=_fake_start_verification_job):
            resp = run_execute(
                problem="alice/sample",
                user="alice",
                artifact_verification_id="",
                solution_paths=["../../escape.cpp"],
                test_names=[],
                submission_upload=None,
            )
        self.assertEqual(resp.status_code, 303)
        location = str(resp.headers.get("location", "") or "")
        self.assertIn("/problems/alice/sample/run/details?verification_id=", location)
        targets = captured.get("targets")
        self.assertIsInstance(targets, list)
        self.assertTrue(targets)
        for row in targets:
            self.assertIsInstance(row, dict)
            submission_path = str(row.get("path") or "")
            self.assertNotIn("..", submission_path)
            self.assertFalse(submission_path.startswith("/"))
            self.assertFalse(submission_path.startswith("\\"))

    def test_checker_set_standard_rejects_escape_name(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        cfg = ws / "config" / "build.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        from app.service.verification.standard_checker import copy_standard_checker
        copy_standard_checker("wcmp.cpp", ws)
        cfg.write_text(json.dumps({"checker_source": "checkers/wcmp.cpp"}, indent=2) + "\n", encoding="utf-8")
        resp = checker_set_standard(problem="alice/sample", user="alice", checker_name="../../evil")
        self.assertEqual(resp.status_code, 303)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("invalid standard checker name", messages[0].lower())
        payload = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("checker_source"), "checkers/wcmp.cpp")
