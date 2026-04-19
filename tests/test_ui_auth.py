from __future__ import annotations

from .db_helpers import db_execute, db_fetch_one

import asyncio
import sqlite3
from unittest.mock import patch

from fastapi import HTTPException
from starlette.responses import PlainTextResponse

from .ui_support import (
    ADMIN_CONFIG_DEFAULTS,
    AUTH_COOKIE_NAME,
    Request,
    UIBaseSuite,
    _cookie_value_from_response,
    _extract_hidden_input_value,
    _flash_messages_from_response,
    issue_password_form_csrf_token,
    _login_with_password_proof,
    _password_verifier_hex,
    _post_form_request,
    _post_request,
    _register_with_password_proof,
    _request,
    _request_with_cookie,
    _response_set_cookie_blob,
    session_user,
    _settings_password_update_with_proof,
    _sha256_hex,
    _setup_with_password_proof,
    _sudo_with_password_proof,
    auth_middleware,
    auth_password_meta,
    config,
    json,
    login_page,
    login_submit,
    register_page,
    register_submit,
    settings_page,
    settings_judgehost_snapshot,
    settings_config_category_page,
    settings_config_category_update,
    settings_password_update,
    settings_system_config_reset,
    settings_worker_queue_snapshot,
    setup_page,
    setup_submit,
    sudo_page,
    switch_workspace,
    uuid,
    workspace_service,
)
SUDO_COOKIE_NAME = config.constants.SUDO_COOKIE_NAME
SUDO_COOKIE_MAX_AGE = int(config.constants.SUDO_COOKIE_MAX_AGE)


class TestUIAuth(UIBaseSuite):
    def test_sudo_password_proof_flow_sets_short_lived_token(self) -> None:
        username = f"sudo-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        auth_token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(auth_token)
        cookie_header = f"{AUTH_COOKIE_NAME}={auth_token}"
        next_path = f"/settings"

        page = sudo_page(_request_with_cookie("/sudo", cookie_header, query=f"next={next_path}"))
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Sudo Mode", html)
        self.assertIn("Enable Sudo Mode", html)

        enabled = _sudo_with_password_proof(cookie_header, password, next_path=next_path)
        self.assertEqual(enabled.status_code, 303)
        self.assertEqual(next_path, enabled.headers.get("location", ""))
        sudo_set_cookie = _response_set_cookie_blob(enabled)
        self.assertIn(f"{SUDO_COOKIE_NAME}=", sudo_set_cookie)
        self.assertIn(f"Max-Age={SUDO_COOKIE_MAX_AGE}", sudo_set_cookie)

        denied = _sudo_with_password_proof(cookie_header, "WrongPass123", next_path=next_path)
        self.assertEqual(denied.status_code, 303)
        self.assertIn("/sudo?next=", denied.headers.get("location", ""))
        denied_messages = _flash_messages_from_response(denied)
        self.assertTrue(any("invalid password proof" in item for item in denied_messages))

    def test_register_login_and_password_update(self) -> None:
        username = f"user-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        updated = "UpdatedPass456"

        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        self.assertIn("/problems", reg.headers.get("location", ""))
        reg_set_cookie = _response_set_cookie_blob(reg)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", reg_set_cookie)
        self.assertIn("Secure", reg_set_cookie)

        user_row = db_fetch_one(
            "SELECT id,password_hash,password_salt,password_iters FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(user_row)
        self.assertTrue(str(user_row["password_hash"] or ""))
        self.assertTrue(str(user_row["password_salt"] or ""))
        self.assertGreater(int(user_row["password_iters"] or 0), 0)

        bad = _login_with_password_proof(username, "wrong-password", next_path="/")
        self.assertEqual(bad.status_code, 303)
        self.assertEqual("/login", bad.headers.get("location", ""))
        bad_messages = _flash_messages_from_response(bad)
        self.assertTrue(any("invalid username or password" in item for item in bad_messages))

        ok = _login_with_password_proof(username, password, next_path="/")
        self.assertEqual(ok.status_code, 303)
        self.assertIn("/problems", ok.headers.get("location", ""))
        set_cookie = _response_set_cookie_blob(ok)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", set_cookie)
        self.assertIn("Secure", set_cookie)
        token = _cookie_value_from_response(ok, AUTH_COOKIE_NAME)
        self.assertTrue(token)
        req = _request_with_cookie("/problems/alice/sample/general", f"{AUTH_COOKIE_NAME}={token}")
        self.assertEqual(session_user(req), username)

        changed = _settings_password_update_with_proof(username, password, updated)
        self.assertEqual(changed.status_code, 303)
        self.assertIn(f"/settings", changed.headers.get("location", ""))
        changed_messages = _flash_messages_from_response(changed)
        self.assertTrue(any("password updated" in item for item in changed_messages))
        changed_set_cookie = _response_set_cookie_blob(changed)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", changed_set_cookie)
        self.assertIn("Secure", changed_set_cookie)

        old_login = _login_with_password_proof(username, password, next_path="/")
        self.assertEqual(old_login.status_code, 303)
        self.assertEqual("/login", old_login.headers.get("location", ""))
        old_messages = _flash_messages_from_response(old_login)
        self.assertTrue(any("invalid username or password" in item for item in old_messages))

        new_login = _login_with_password_proof(username, updated, next_path="/")
        self.assertEqual(new_login.status_code, 303)
        new_login_set_cookie = _response_set_cookie_blob(new_login)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", new_login_set_cookie)
        self.assertIn("Secure", new_login_set_cookie)

    def test_auth_password_proof_flow_works_without_plaintext_submission(self) -> None:
        username = f"proof-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        updated = "UpdatedPass456"

        register_resp = register_page(_request("/register"))
        self.assertEqual(register_resp.status_code, 200)
        register_html = register_resp.body.decode("utf-8", errors="replace")
        register_csrf = _extract_hidden_input_value(register_html, "csrf_token")
        register_salt = _extract_hidden_input_value(register_html, "password_salt")
        register_iters = int(_extract_hidden_input_value(register_html, "password_iters") or "0")
        self.assertTrue(register_csrf)
        self.assertRegex(register_salt, r"^[0-9a-f]{32}$")
        self.assertGreater(register_iters, 0)
        self.assertIn('name="terms_accepted"', register_html)
        self.assertIn("I have read and agree to the Terms of Use.", register_html)
        self.assertIn("Review Terms of Use", register_html)
        self.assertIn("Effective date: April 19, 2026.", register_html)
        self.assertIn("No warranty.", register_html)
        self.assertIn('data-popup-open="terms-of-use-popup"', register_html)
        self.assertIn('id="terms-of-use-popup"', register_html)
        self.assertGreater(
            register_html.rfind('data-popup-open="terms-of-use-popup"'),
            register_html.find('id="profile-judgehost-health-summary"'),
        )

        register_verifier = _password_verifier_hex(password, register_salt, register_iters)
        register_proof = _sha256_hex(register_csrf + register_verifier)
        register_password_hash = _sha256_hex(register_csrf + password)

        reg = register_submit(
            request=_post_request("/register"),
            username=username,
            password=register_password_hash,
            password_confirm=register_password_hash,
            password_verifier=register_verifier,
            password_proof=register_proof,
            csrf_token=register_csrf,
            password_salt=register_salt,
            password_iters=str(register_iters),
            next="/",
            terms_accepted="yes",
        )
        self.assertEqual(reg.status_code, 303)
        self.assertIn("/problems", reg.headers.get("location", ""))

        login_resp = login_page(_request("/login"))
        self.assertEqual(login_resp.status_code, 200)
        login_html = login_resp.body.decode("utf-8", errors="replace")
        login_csrf = _extract_hidden_input_value(login_html, "csrf_token")
        self.assertTrue(login_csrf)
        login_meta = auth_password_meta(username=username, csrf_token=login_csrf)
        login_salt = str(login_meta.get("salt") or "")
        login_iters = int(login_meta.get("iters") or 0)
        self.assertRegex(login_salt, r"^[0-9a-f]{32}$")
        self.assertGreater(login_iters, 0)

        login_verifier = _password_verifier_hex(password, login_salt, login_iters)
        login_proof = _sha256_hex(login_csrf + login_verifier)
        login_password_hash = _sha256_hex(login_csrf + password)
        login_ok = login_submit(
            request=_post_request("/login"),
            username=username,
            password=login_password_hash,
            password_proof=login_proof,
            csrf_token=login_csrf,
            next="/",
        )
        self.assertEqual(login_ok.status_code, 303)
        self.assertIn("/problems", login_ok.headers.get("location", ""))

        settings_csrf = issue_password_form_csrf_token("settings-password")
        self.assertTrue(settings_csrf)
        auth_row = db_fetch_one("SELECT password_salt,password_iters FROM users WHERE username=?", [username])
        self.assertIsNotNone(auth_row)
        current_salt = str(auth_row["password_salt"] or "").strip().lower()
        current_iters = int(auth_row["password_iters"] or 0)
        new_salt = uuid.uuid4().hex
        new_iters = current_iters
        self.assertRegex(current_salt, r"^[0-9a-f]{32}$")
        self.assertRegex(new_salt, r"^[0-9a-f]{32}$")
        self.assertGreater(current_iters, 0)

        current_verifier = _password_verifier_hex(password, current_salt, current_iters)
        current_proof = _sha256_hex(settings_csrf + current_verifier)
        new_verifier = _password_verifier_hex(updated, new_salt, new_iters)
        new_proof = _sha256_hex(settings_csrf + new_verifier)
        current_password_hash = _sha256_hex(settings_csrf + password)
        updated_password_hash = _sha256_hex(settings_csrf + updated)

        changed = settings_password_update(
            user=username,
            current_password=current_password_hash,
            new_password=updated_password_hash,
            new_password_confirm=updated_password_hash,
            current_password_proof=current_proof,
            new_password_verifier=new_verifier,
            new_password_proof=new_proof,
            csrf_token=settings_csrf,
            new_password_salt=new_salt,
            new_password_iters=str(new_iters),
        )
        self.assertEqual(changed.status_code, 303)
        self.assertIn(f"/settings", changed.headers.get("location", ""))
        changed_messages = _flash_messages_from_response(changed)
        self.assertTrue(any("password updated" in item for item in changed_messages))

        old_login_resp = login_page(_request("/login"))
        old_login_html = old_login_resp.body.decode("utf-8", errors="replace")
        old_login_csrf = _extract_hidden_input_value(old_login_html, "csrf_token")
        old_meta = auth_password_meta(username=username, csrf_token=old_login_csrf)
        old_salt = str(old_meta.get("salt") or "")
        old_iters = int(old_meta.get("iters") or 0)
        old_verifier = _password_verifier_hex(password, old_salt, old_iters)
        old_proof = _sha256_hex(old_login_csrf + old_verifier)
        old_password_hash = _sha256_hex(old_login_csrf + password)
        old_login = login_submit(
            request=_post_request("/login"),
            username=username,
            password=old_password_hash,
            password_proof=old_proof,
            csrf_token=old_login_csrf,
            next="/",
        )
        self.assertEqual(old_login.status_code, 303)
        self.assertEqual("/login", old_login.headers.get("location", ""))
        old_messages = _flash_messages_from_response(old_login)
        self.assertTrue(any("invalid username or password" in item for item in old_messages))

        new_login_resp = login_page(_request("/login"))
        new_login_html = new_login_resp.body.decode("utf-8", errors="replace")
        new_login_csrf = _extract_hidden_input_value(new_login_html, "csrf_token")
        new_meta = auth_password_meta(username=username, csrf_token=new_login_csrf)
        new_salt_login = str(new_meta.get("salt") or "")
        new_iters_login = int(new_meta.get("iters") or 0)
        new_verifier_login = _password_verifier_hex(updated, new_salt_login, new_iters_login)
        new_proof_login = _sha256_hex(new_login_csrf + new_verifier_login)
        new_password_hash_login = _sha256_hex(new_login_csrf + updated)
        new_login = login_submit(
            request=_post_request("/login"),
            username=username,
            password=new_password_hash_login,
            password_proof=new_proof_login,
            csrf_token=new_login_csrf,
            next="/",
        )
        self.assertEqual(new_login.status_code, 303)
        self.assertIn("/problems", new_login.headers.get("location", ""))

    def test_register_rejects_invalid_username_format(self) -> None:
        invalid = register_submit(
            request=_post_request("/register"),
            username="Alice_1",
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(invalid.status_code, 303)
        loc = invalid.headers.get("location", "")
        self.assertEqual("/register", loc)
        invalid_messages = _flash_messages_from_response(invalid)
        self.assertTrue(any("invalid username" in item.lower() for item in invalid_messages))

    def test_register_rejects_invalid_username_length(self) -> None:
        too_short = register_submit(
            request=_post_request("/register"),
            username="ab",
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(too_short.status_code, 303)
        self.assertEqual("/register", too_short.headers.get("location", ""))
        short_messages = _flash_messages_from_response(too_short)
        self.assertTrue(any("invalid username" in item.lower() for item in short_messages))

        too_long = register_submit(
            request=_post_request("/register"),
            username="abcdefghijklmnopq",
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(too_long.status_code, 303)
        self.assertEqual("/register", too_long.headers.get("location", ""))
        long_messages = _flash_messages_from_response(too_long)
        self.assertTrue(any("invalid username" in item.lower() for item in long_messages))

    def test_register_requires_terms_of_use_acceptance(self) -> None:
        username = self.random_id("terms")
        password = "StrongPass123"
        page = register_page(_request("/register"))
        html = page.body.decode("utf-8", errors="replace")
        csrf = _extract_hidden_input_value(html, "csrf_token")
        salt = _extract_hidden_input_value(html, "password_salt")
        iters = int(_extract_hidden_input_value(html, "password_iters") or "0")
        verifier = _password_verifier_hex(password, salt, iters)
        proof = _sha256_hex(csrf + verifier)
        password_hash = _sha256_hex(csrf + password)

        resp = register_submit(
            request=_post_request("/register"),
            username=username,
            password=password_hash,
            password_confirm=password_hash,
            password_verifier=verifier,
            password_proof=proof,
            csrf_token=csrf,
            password_salt=salt,
            password_iters=str(iters),
            next="/",
            terms_accepted="",
        )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location", ""), "/register")
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("terms of use" in item.lower() for item in messages))

    def test_setup_page_shows_config_when_no_registered_users(self) -> None:
        count = db_fetch_one(
            "SELECT COUNT(*) AS c FROM users WHERE COALESCE(TRIM(password_hash), '') <> ''",
            [],
        )
        self.assertIsNotNone(count)
        self.assertEqual(int(count["c"]), 0)

        resp = setup_page(_request("/setup"))
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("System Setup", html)
        self.assertIn("Create Super Admin", html)
        self.assertIn("POLYGON_REPLICA_DB", html)
        self.assertIn("I confirm the configuration paths below.", html)

    def test_setup_submit_creates_super_admin(self) -> None:
        username = f"boot-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"

        page = setup_page(_request("/setup"))
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        csrf = _extract_hidden_input_value(html, "csrf_token")
        salt = _extract_hidden_input_value(html, "password_salt")
        iters = int(_extract_hidden_input_value(html, "password_iters") or "0")
        self.assertTrue(csrf)
        self.assertRegex(salt, r"^[0-9a-f]{32}$")
        self.assertGreater(iters, 0)

        verifier = _password_verifier_hex(password, salt, iters)
        proof = _sha256_hex(csrf + verifier)
        password_hash = _sha256_hex(csrf + password)

        resp = setup_submit(
            request=_post_request("/setup"),
            username=username,
            password=password_hash,
            password_confirm=password_hash,
            password_verifier=verifier,
            password_proof=proof,
            csrf_token=csrf,
            password_salt=salt,
            password_iters=str(iters),
            confirm_config="1",
            next="/",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems", resp.headers.get("location", ""))
        set_cookie = _response_set_cookie_blob(resp)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", set_cookie)

        row = db_fetch_one(
            "SELECT is_system_admin,password_hash,password_salt,password_iters FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_system_admin"] or 0), 1)
        self.assertTrue(str(row["password_hash"] or ""))
        self.assertTrue(str(row["password_salt"] or ""))
        self.assertGreater(int(row["password_iters"] or 0), 0)

    def test_setup_submit_requires_config_confirmation(self) -> None:
        username = f"boot-{uuid.uuid4().hex[:8]}"
        resp = _setup_with_password_proof(username, "StrongPass123", confirm_config="0", next_path="/")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual("/setup", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("confirm current system configuration paths" in item for item in messages))

    def test_register_does_not_claim_existing_passwordless_user(self) -> None:
        username = f"claim-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(username)
        row_before = db_fetch_one("SELECT password_hash FROM users WHERE username=?", [username])
        self.assertIsNotNone(row_before)
        self.assertFalse(str(row_before["password_hash"] or ""))

        resp = _register_with_password_proof(username, "StrongPass123", next_path="/")
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertEqual("/register", location)
        unavailable_messages = _flash_messages_from_response(resp)
        self.assertTrue(any("username is unavailable" in item for item in unavailable_messages))

        row_after = db_fetch_one("SELECT password_hash FROM users WHERE username=?", [username])
        self.assertIsNotNone(row_after)
        self.assertFalse(str(row_after["password_hash"] or ""))

    def test_first_registered_user_becomes_system_admin(self) -> None:
        first = f"first-{uuid.uuid4().hex[:8]}"
        second = f"second-{uuid.uuid4().hex[:8]}"

        before = db_fetch_one(
            "SELECT COUNT(*) AS c FROM users WHERE COALESCE(TRIM(password_hash), '') <> ''",
            [],
        )
        self.assertIsNotNone(before)
        self.assertEqual(int(before["c"]), 0)

        first_reg = _register_with_password_proof(first, "StrongPass123", next_path="/")
        self.assertEqual(first_reg.status_code, 303)
        first_row = db_fetch_one("SELECT is_system_admin FROM users WHERE username=?", [first])
        self.assertIsNotNone(first_row)
        self.assertEqual(int(first_row["is_system_admin"] or 0), 1)

        second_reg = _register_with_password_proof(second, "StrongPass123", next_path="/")
        self.assertEqual(second_reg.status_code, 303)
        second_row = db_fetch_one("SELECT is_system_admin FROM users WHERE username=?", [second])
        self.assertIsNotNone(second_row)
        self.assertEqual(int(second_row["is_system_admin"] or 0), 0)

    def test_passwordless_user_does_not_take_system_admin_slot(self) -> None:
        workspace_service.ensure_user("placeholder-user")
        row = db_fetch_one("SELECT is_system_admin,password_hash FROM users WHERE username=?", ["placeholder-user"])
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_system_admin"] or 0), 0)
        self.assertFalse(str(row["password_hash"] or ""))

    def test_login_rate_limit_blocks_repeated_failures(self) -> None:
        username = self.random_id("ratelim")
        password = "StrongPass123"
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)

        blocked_location = ""
        blocked_message = ""
        for _ in range(12):
            bad = _login_with_password_proof(username, "wrong-password", next_path="/")
            self.assertEqual(bad.status_code, 303)
            blocked_location = bad.headers.get("location", "")
            blocked_messages = _flash_messages_from_response(bad)
            blocked_message = blocked_messages[0] if blocked_messages else ""
            if "too many failed attempts" in blocked_message:
                break
        self.assertEqual("/login", blocked_location)
        self.assertIn("too many failed attempts", blocked_message)

        blocked_ok = _login_with_password_proof(username, password, next_path="/")
        self.assertEqual(blocked_ok.status_code, 303)
        blocked_ok_messages = _flash_messages_from_response(blocked_ok)
        self.assertTrue(any("too many failed attempts" in item for item in blocked_ok_messages))

    def test_auth_middleware_blocks_cross_origin_post(self) -> None:
        username = f"csrf-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            f"/problems/alice/sample/{username}/git/pull",
            f"{AUTH_COOKIE_NAME}={token}",
            method="POST",
            extra_headers=[(b"origin", b"http://evil.example")],
        )

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        with self.assertRaises(HTTPException) as blocked:
            asyncio.run(auth_middleware(req, _next))
        self.assertEqual(blocked.exception.status_code, 403)

    def test_auth_middleware_allows_same_origin_post(self) -> None:
        username = f"csrfok-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            f"/problems/alice/sample/{username}/git/pull",
            f"{AUTH_COOKIE_NAME}={token}",
            method="POST",
            extra_headers=[(b"origin", b"http://testserver")],
        )

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        resp = asyncio.run(auth_middleware(req, _next))
        self.assertEqual(resp.status_code, 200)

    def test_auth_middleware_allows_userless_contest_path(self) -> None:
        username = f"ctauth-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            "/contests/demo/overview",
            f"{AUTH_COOKIE_NAME}={token}",
            method="GET",
        )

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        resp = asyncio.run(auth_middleware(req, _next))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body, b"ok")

    def test_auth_middleware_keeps_userless_contest_query_intact(self) -> None:
        username = self.random_id("ctmsg")
        password = "StrongPass123"
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            "/contests/demo/overview",
            f"{AUTH_COOKIE_NAME}={token}",
            query="keep=1&message=legacy+notice",
            method="GET",
        )

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        resp = asyncio.run(auth_middleware(req, _next))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body, b"ok")
        self.assertEqual(resp.headers.get("location", ""), "")
        self.assertEqual(_flash_messages_from_response(resp), [])

    def test_auth_middleware_does_not_retry_schema_operational_error(self) -> None:
        req = _request("/login")

        async def _next(_: Request) -> PlainTextResponse:
            raise sqlite3.OperationalError("no such table: users")

        with patch.object(config.db, "init") as init_mock:
            with self.assertRaises(sqlite3.OperationalError):
                asyncio.run(auth_middleware(req, _next))
        init_mock.assert_not_called()

    def test_auth_middleware_redirects_to_setup_when_no_registered_users(self) -> None:
        req = _request("/problems/alice/sample/general")

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        resp = asyncio.run(auth_middleware(req, _next))
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/setup?next=", resp.headers.get("location", ""))

    def test_settings_page_shows_system_admin_panel_for_system_admin(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        resp = settings_page(_request("/settings"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("System Admin Panel", html)
        self.assertIn("Configuration Center", html)
        self.assertIn("settings-admin-panel", html)
        self.assertIn("settings-account-panel", html)
        self.assertNotIn('data-main="problems" class="active"', html)
        self.assertIn("/settings/config/", html)
        self.assertIn("Judging", html)

    def test_settings_page_runtime_runner_hides_auth_fields_when_judgehost_disabled(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()
        self.addCleanup(settings_system_config_reset, user="alice")
        admin = db_fetch_one("SELECT id FROM users WHERE username=?", ["alice"])
        self.assertIsNotNone(admin)
        config.system_config_service.apply_patch(
            {
                "JUDGEHOST_ENABLE": False,
                "JUDGEHOST_API_USERNAME": "judgehost",
                "JUDGEHOST_API_TOKEN": "token-disabled-demo",
            },
            actor_user_id=int(admin["id"]),
        )
        config.reload_runtime_values()

        resp = settings_page(_request("/settings"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('data-popup-open="judgehost-gen-script-popup"', html)
        self.assertIn('id="judgehost-gen-script-popup"', html)
        self.assertIn('data-gen-script-baseurl="1"', html)
        self.assertIn('data-gen-script-sudo="1"', html)
        self.assertIn('data-gen-script-output="1"', html)
        self.assertIn('data-judgehost-enable-toggle="1"', html)
        self.assertIn('data-judgehost-auth-block="1" hidden', html)

    def test_settings_page_runtime_runner_shows_auth_fields_when_judgehost_enabled(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()
        self.addCleanup(settings_system_config_reset, user="alice")
        admin = db_fetch_one("SELECT id FROM users WHERE username=?", ["alice"])
        self.assertIsNotNone(admin)
        config.system_config_service.apply_patch(
            {
                "JUDGEHOST_ENABLE": True,
                "JUDGEHOST_API_USERNAME": "judgehost",
                "JUDGEHOST_API_TOKEN": "token-enabled-demo",
            },
            actor_user_id=int(admin["id"]),
        )
        config.reload_runtime_values()

        resp = settings_page(_request("/settings"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('data-judgehost-enable-toggle="1"', html)
        self.assertIn('data-judgehost-auth-block="1"', html)
        self.assertNotIn('data-judgehost-auth-block="1" hidden', html)
        self.assertIn('data-judgehost-api-username="1"', html)
        self.assertIn('data-judgehost-api-token="1"', html)

    def test_settings_page_formats_judgehost_last_seen_in_user_timezone(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        raw_last_seen = "2026-02-28T21:43:08.505465+00:00"
        fake_status = {
            "enabled": True,
            "auth_configured": True,
            "hosts_online": 1,
            "hosts_total": 1,
            "queue": {"queued": 0, "leased": 0, "completed": 0, "failed": 0},
            "hosts": [
                {
                    "hostname": "judgehost-lastseen-time",
                    "online": True,
                    "age_sec": 3,
                    "last_seen_at": raw_last_seen,
                    "last_action": "heartbeat",
                    "active_leases": 0,
                    "last_task_id": "",
                    "last_run_id": "",
                }
            ],
        }
        with patch.object(config.judgehost_task_service, "status", return_value=fake_status):
            resp = settings_page(_request("/settings"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("judgehost-lastseen-time", html)
        self.assertIn("Disabled", html)
        self.assertNotIn(raw_last_seen, html)

    def test_settings_config_category_update_requires_system_admin(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            asyncio.run(
                settings_config_category_update(
                    _post_form_request(
                        "/settings/config/judging",
                        {"config_RUN_TEST_SELECTOR_LIMIT": "777"},
                    ),
                    user="alice",
                    category="judging",
                )
            )
        self.assertEqual(blocked.exception.status_code, 403)

    def test_settings_config_category_update_and_reset(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        override_value = 777
        update_resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/settings/config/judging",
                    {"config_RUN_TEST_SELECTOR_LIMIT": str(override_value)},
                ),
                user="alice",
                category="judging",
            )
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn("/settings/config/judging", update_resp.headers.get("location", ""))
        self.assertEqual(
            int(config.system_config_service.get("RUN_TEST_SELECTOR_LIMIT")),
            override_value,
        )
        self.assertEqual(
            int(config.constants.RUN_TEST_SELECTOR_LIMIT),
            override_value,
        )

        row = db_fetch_one("SELECT value_json FROM system_config WHERE key=?", ["RUN_TEST_SELECTOR_LIMIT"])
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(str(row["value_json"] or "null")), override_value)

        reset_resp = settings_system_config_reset(user="alice")
        self.assertEqual(reset_resp.status_code, 303)
        self.assertEqual(
            int(config.system_config_service.get("RUN_TEST_SELECTOR_LIMIT")),
            int(ADMIN_CONFIG_DEFAULTS["RUN_TEST_SELECTOR_LIMIT"]),
        )
        row_after = db_fetch_one("SELECT value_json FROM system_config WHERE key=?", ["RUN_TEST_SELECTOR_LIMIT"])
        self.assertIsNone(row_after)

    def test_settings_config_category_update_can_revert_single_override_to_default(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()
        self.addCleanup(settings_system_config_reset, user="alice")

        override_value = int(ADMIN_CONFIG_DEFAULTS["RUN_TEST_SELECTOR_LIMIT"]) + 123
        set_override_resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/settings/config/judging",
                    {"config_RUN_TEST_SELECTOR_LIMIT": str(override_value)},
                ),
                user="alice",
                category="judging",
            )
        )
        self.assertEqual(set_override_resp.status_code, 303)
        self.assertEqual(int(config.system_config_service.get("RUN_TEST_SELECTOR_LIMIT")), override_value)
        row = db_fetch_one("SELECT value_json FROM system_config WHERE key=?", ["RUN_TEST_SELECTOR_LIMIT"])
        self.assertIsNotNone(row)

        revert_resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/settings/config/judging",
                    {
                        "config_RUN_TEST_SELECTOR_LIMIT": str(override_value + 999),
                        "config_reset_RUN_TEST_SELECTOR_LIMIT": "1",
                    },
                ),
                user="alice",
                category="judging",
            )
        )
        self.assertEqual(revert_resp.status_code, 303)
        self.assertEqual(
            int(config.system_config_service.get("RUN_TEST_SELECTOR_LIMIT")),
            int(ADMIN_CONFIG_DEFAULTS["RUN_TEST_SELECTOR_LIMIT"]),
        )
        row_after = db_fetch_one("SELECT value_json FROM system_config WHERE key=?", ["RUN_TEST_SELECTOR_LIMIT"])
        self.assertIsNone(row_after)

    def test_settings_config_category_update_rejects_non_ascii_judgehost_token(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        self.addCleanup(settings_system_config_reset, user="alice")
        bad_token = "abc_non_ascii_" + chr(0x00E9)
        resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/settings/config/judgehost",
                    {"config_JUDGEHOST_API_TOKEN": bad_token},
                ),
                user="alice",
                category="judgehost",
            )
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/settings/config/judgehost", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("JUDGEHOST_API_TOKEN must contain only visible ASCII characters", messages[0])
        self.assertNotEqual(str(config.system_config_service.get("JUDGEHOST_API_TOKEN") or ""), bad_token)

    def test_settings_config_category_update_allows_printable_ascii_compile_flags(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        self.addCleanup(settings_system_config_reset, user="alice")
        flags = "-x c++ -Wall -O2 -static -pipe"
        resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/settings/config/toolchain",
                    {"config_TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS": flags},
                ),
                user="alice",
                category="toolchain",
            )
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/settings/config/toolchain", resp.headers.get("location", ""))
        self.assertEqual(str(config.system_config_service.get("TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS") or ""), flags)

    def test_settings_config_category_page_and_hot_reload(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()
        self.addCleanup(settings_system_config_reset, user="alice")

        page_resp = settings_config_category_page(
            _request("/settings/config/judging"),
            user="alice",
            category="judging",
        )
        self.assertEqual(page_resp.status_code, 200)
        page_html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("System Configuration", page_html)
        self.assertIn("RUN_EXEC_MEMORY_MB", page_html)

        update_value = 1536
        update_resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/settings/config/judging",
                    {"config_RUN_EXEC_MEMORY_MB": str(update_value)},
                ),
                user="alice",
                category="judging",
            )
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn("/settings/config/judging", update_resp.headers.get("location", ""))
        self.assertEqual(int(config.system_config_service.get("RUN_EXEC_MEMORY_MB")), update_value)
        self.assertEqual(int(config.constants.RUN_EXEC_MEMORY_MB), update_value)

    def test_settings_config_category_page_renders_token_generate_button(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        page_resp = settings_config_category_page(
            _request("/settings/config/judgehost"),
            user="alice",
            category="judgehost",
        )
        self.assertEqual(page_resp.status_code, 200)
        page_html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("JUDGEHOST_API_TOKEN", page_html)
        self.assertIn("data-token-generate=\"1\"", page_html)
        self.assertIn("data-token-target=\"config_JUDGEHOST_API_TOKEN\"", page_html)
        self.assertIn(">Generate</button>", page_html)

    def test_settings_worker_queue_snapshot_requires_system_admin(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            settings_worker_queue_snapshot(user="alice")
        self.assertEqual(blocked.exception.status_code, 403)

    def test_settings_worker_queue_snapshot_returns_metrics_for_admin(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()
        future, queued, reason = config.worker_queue_service.submit(
            name="snapshot-probe",
            fn=lambda: None,
            queue_name="ops",
            job_type="snapshot-probe",
        )
        self.assertTrue(queued, msg=reason)
        config.worker_queue_service.wait_for_futures([future], timeout_sec=5.0)
        resp = settings_worker_queue_snapshot(user="alice", limit=50)
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertEqual(int(payload.get("limit") or 0), 50)
        self.assertIn("queue_capacity", payload)
        self.assertIn("job_type_stats", payload)
        self.assertIn("snapshot-probe", payload.get("job_type_stats") or {})

    def test_settings_judgehost_snapshot_requires_system_admin(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            settings_judgehost_snapshot(user="alice")
        self.assertEqual(blocked.exception.status_code, 403)

    def test_settings_judgehost_snapshot_returns_hosts_for_admin(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()
        service = config.judgehost_task_service
        old_enabled = bool(service._state.enabled)
        old_token = str(service._state.api_token or "")
        self.addCleanup(setattr, service._state, "enabled", old_enabled)
        self.addCleanup(setattr, service._state, "api_token", old_token)
        service._state.enabled = True
        service._state.api_token = "admin-snapshot-token"
        service.fetch_work("judgehost-admin-snapshot")
        resp = settings_judgehost_snapshot(user="alice")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertIn("hosts", payload)
        self.assertIn("hosts_online", payload)
        hosts = payload.get("hosts") or []
        self.assertTrue(any(str(item.get("hostname") or "") == "judgehost-admin-snapshot" for item in hosts))

    def test_problem_id_validation_requires_lowercase_dash_format(self) -> None:
        username = f"swauth-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)
        cookie_header = f"{AUTH_COOKIE_NAME}={token}"

        with self.assertRaises(ValueError) as bad_format:
            workspace_service.ensure_problem("Sample_Problem")
        self.assertIn("Use <owner>/<slug>", str(bad_format.exception))

        with self.assertRaises(ValueError) as bad_dash:
            workspace_service.ensure_problem("sample-")
        self.assertIn("Use <owner>/<slug>", str(bad_dash.exception))

        invalid_open = switch_workspace(
            _request_with_cookie("/switch-workspace", cookie_header),
            problem="Sample_Problem",
            page="statement",
        )
        self.assertEqual(invalid_open.status_code, 303)
        invalid_loc = invalid_open.headers.get("location", "")
        self.assertIn("/problems", invalid_loc)
        self.assertNotIn("message=", invalid_loc)
        invalid_messages = _flash_messages_from_response(invalid_open)
        self.assertTrue(invalid_messages)
        self.assertIn("Use <owner>/<slug>", invalid_messages[0])

        valid = switch_workspace(
            _request_with_cookie("/switch-workspace", cookie_header),
            problem="minimal-spanning-tree",
            page="statement",
        )
        self.assertEqual(valid.status_code, 303)
        self.assertIn(f"/problems/{username}/minimal-spanning-tree/statement", valid.headers.get("location", ""))
