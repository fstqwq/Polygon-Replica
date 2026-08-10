from __future__ import annotations

from tests.db_helpers import db_execute, db_fetch_one

import asyncio
import sqlite3
from unittest.mock import patch

from fastapi import HTTPException
from starlette.responses import PlainTextResponse
import app.impl.admin.panel as admin_panel_module
from app.service.auth.password_hash import password_verifier_storage_hash
from app import main_constant
from app.impl.auth.password_envelope import PasswordEnvelopeStore
from app.impl.root.auth_pages import logout
from app.service.platform.maintenance import CleanupStart
from tests.common import E2ETestBase

from tests.ui_support import (
    ADMIN_CONFIG_DEFAULTS,
    AUTH_COOKIE_NAME,
    admin_judgehosts_page,
    admin_overview_page,
    admin_users_page,
    Request,
    UIHelpersMixin,
    _cookie_value_from_response,
    _extract_hidden_input_value,
    _flash_messages_from_response,
    issue_password_form_csrf_token,
    _login_with_password_envelope,
    _password_envelope_fields_direct,
    _password_verifier_hex,
    _post_form_request,
    _post_request,
    _register_with_password_envelope,
    _request,
    _request_with_cookie,
    _response_set_cookie_blob,
    _settings_admin_password_update_with_envelope,
    session_user,
    _settings_password_update_with_envelope,
    _setup_with_password_envelope,
    _sudo_with_password_envelope,
    auth_middleware,
    auth_password_meta,
    config,
    json,
    login_page,
    login_submit,
    register_page,
    register_submit,
    register_verify,
    register_verify_page,
    settings_page,
    settings_judgehost_snapshot,
    settings_config_category_page,
    settings_config_category_update,
    settings_password_update,
    settings_system_config_reset,
    settings_user_ban_update,
    settings_user_system_admin_update,
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


class TestUIAuth(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = False
    seed_default_workspace = True

    def test_password_crypto_production_parameters_remain_strong(self) -> None:
        self.assertEqual(main_constant.PASSWORD_HASH_ITERS, 240_000)
        private_key = PasswordEnvelopeStore()._key_factory()
        self.assertEqual(private_key.key_size, 2048)

    def _replace_auth_constants(self, **overrides: object) -> None:
        previous = config.constants.to_dict()
        updated = dict(previous)
        updated.update(overrides)
        config.constants.replace(updated)
        self.addCleanup(config.constants.replace, previous)

    def _valid_registration_kwargs(self, username: str, *, email: str | None = None) -> dict[str, object]:
        password = "StrongPass123"
        page = register_page(_request("/register"))
        html = page.body.decode("utf-8", errors="replace")
        csrf = _extract_hidden_input_value(html, "csrf_token")
        salt = _extract_hidden_input_value(html, "password_salt")
        iters = int(_extract_hidden_input_value(html, "password_iters") or "0")
        verifier = _password_verifier_hex(password, salt, iters)
        envelope = _password_envelope_fields_direct(
            scope="register-password",
            purpose="register",
            username=username,
            csrf_token=csrf,
            verifier=verifier,
        )
        return {
            "username": username,
            "email": email or f"{username}@gmail.com",
            "password": "",
            "password_confirm": "",
            "key_id": envelope["key_id"],
            "envelope_token": envelope["envelope_token"],
            "encrypted_verifier": envelope["encrypted_verifier"],
            "csrf_token": csrf,
            "password_salt": salt,
            "password_iters": str(iters),
            "next": "/",
            "terms_accepted": "yes",
        }

    def _submit_pending_registration_with_smtp(
        self,
        username: str,
        *,
        request: Request | None = None,
        email: str | None = None,
    ) -> tuple[object, str]:
        kwargs = self._valid_registration_kwargs(username, email=email)
        sent_codes: list[str] = []

        with patch.object(config.smtp_config_service, "delivery_configured", return_value=True):
            with patch.object(config.smtp_config_service, "send_registration_email") as send_mail:
                def _capture_registration_email(*, recipient, verification_code, expires_in_sec):
                    del recipient, expires_in_sec
                    sent_codes.append(str(verification_code))

                send_mail.side_effect = _capture_registration_email
                resp = register_submit(
                    request=request if request is not None else _post_request("/register"),
                    **kwargs,
                )

        self.assertEqual(len(sent_codes), 1)
        return resp, sent_codes[0]

    def test_sudo_password_envelope_flow_sets_short_lived_token(self) -> None:
        username = self.random_id("sudo")
        password = "StrongPass123"
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        auth_token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(auth_token)
        cookie_header = f"{AUTH_COOKIE_NAME}={auth_token}"
        next_path = "/settings"

        page = sudo_page(_request_with_cookie("/sudo", cookie_header, query=f"next={next_path}"))
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Sudo Mode", html)
        self.assertIn("Enable Sudo Mode", html)

        enabled = _sudo_with_password_envelope(cookie_header, password, next_path=next_path)
        self.assertEqual(enabled.status_code, 303)
        self.assertEqual(next_path, enabled.headers.get("location", ""))
        sudo_set_cookie = _response_set_cookie_blob(enabled)
        self.assertIn(f"{SUDO_COOKIE_NAME}=", sudo_set_cookie)
        self.assertIn(f"Max-Age={SUDO_COOKIE_MAX_AGE}", sudo_set_cookie)

        denied = _sudo_with_password_envelope(cookie_header, "WrongPass123", next_path=next_path)
        self.assertEqual(denied.status_code, 303)
        self.assertIn("/sudo?next=", denied.headers.get("location", ""))
        denied_messages = _flash_messages_from_response(denied)
        self.assertTrue(any("invalid password envelope" in item for item in denied_messages))

    def test_register_login_and_password_update(self) -> None:
        username = self.random_id("user")
        password = "StrongPass123"
        updated = "UpdatedPass456"

        reg = _register_with_password_envelope(username, password, next_path="/")
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
        stored_hash = str(user_row["password_hash"] or "")
        registered_verifier = _password_verifier_hex(
            password,
            str(user_row["password_salt"] or ""),
            int(user_row["password_iters"] or 0),
        )
        self.assertNotEqual(stored_hash, registered_verifier)
        self.assertEqual(stored_hash, password_verifier_storage_hash(registered_verifier))

        bad = _login_with_password_envelope(username, "wrong-password", next_path="/")
        self.assertEqual(bad.status_code, 303)
        self.assertEqual("/login", bad.headers.get("location", ""))
        bad_messages = _flash_messages_from_response(bad)
        self.assertTrue(any("invalid username or password" in item for item in bad_messages))

        ok = _login_with_password_envelope(username, password, next_path="/")
        self.assertEqual(ok.status_code, 303)
        self.assertIn("/problems", ok.headers.get("location", ""))
        set_cookie = _response_set_cookie_blob(ok)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", set_cookie)
        self.assertIn("Secure", set_cookie)
        token = _cookie_value_from_response(ok, AUTH_COOKIE_NAME)
        self.assertTrue(token)
        req = _request_with_cookie("/problems/alice/sample/general", f"{AUTH_COOKIE_NAME}={token}")
        self.assertEqual(session_user(req), username)

        changed = _settings_password_update_with_envelope(username, password, updated)
        self.assertEqual(changed.status_code, 303)
        self.assertIn("/settings", changed.headers.get("location", ""))
        changed_messages = _flash_messages_from_response(changed)
        self.assertTrue(any("password updated" in item for item in changed_messages))
        changed_set_cookie = _response_set_cookie_blob(changed)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", changed_set_cookie)
        self.assertIn("Secure", changed_set_cookie)

        old_login = _login_with_password_envelope(username, password, next_path="/")
        self.assertEqual(old_login.status_code, 303)
        self.assertEqual("/login", old_login.headers.get("location", ""))
        old_messages = _flash_messages_from_response(old_login)
        self.assertTrue(any("invalid username or password" in item for item in old_messages))

        new_login = _login_with_password_envelope(username, updated, next_path="/")
        self.assertEqual(new_login.status_code, 303)
        new_login_set_cookie = _response_set_cookie_blob(new_login)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", new_login_set_cookie)
        self.assertIn("Secure", new_login_set_cookie)

    def test_login_envelope_rejects_replay_and_stored_hash_as_verifier(self) -> None:
        username = self.random_id("envelope")
        password = "StrongPass123"
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        auth_row = db_fetch_one(
            "SELECT password_hash,password_salt,password_iters FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(auth_row)
        stored_hash = str(auth_row["password_hash"] or "")
        salt = str(auth_row["password_salt"] or "")
        iters = int(auth_row["password_iters"] or 0)

        csrf = issue_password_form_csrf_token("login-password")
        verifier = _password_verifier_hex(password, salt, iters)
        envelope = _password_envelope_fields_direct(
            scope="login-password",
            purpose="login",
            username=username,
            csrf_token=csrf,
            verifier=verifier,
        )
        ok = login_submit(
            request=_post_request("/login"),
            username=username,
            password="",
            key_id=envelope["key_id"],
            envelope_token=envelope["envelope_token"],
            encrypted_verifier=envelope["encrypted_verifier"],
            csrf_token=csrf,
            next="/",
        )
        self.assertEqual(ok.status_code, 303)

        replay = login_submit(
            request=_post_request("/login"),
            username=username,
            password="",
            key_id=envelope["key_id"],
            envelope_token=envelope["envelope_token"],
            encrypted_verifier=envelope["encrypted_verifier"],
            csrf_token=csrf,
            next="/",
        )
        self.assertEqual("/login", replay.headers.get("location", ""))

        leaked_csrf = issue_password_form_csrf_token("login-password")
        leaked_envelope = _password_envelope_fields_direct(
            scope="login-password",
            purpose="login",
            username=username,
            csrf_token=leaked_csrf,
            verifier=stored_hash,
        )
        leaked = login_submit(
            request=_post_request("/login"),
            username=username,
            password="",
            key_id=leaked_envelope["key_id"],
            envelope_token=leaked_envelope["envelope_token"],
            encrypted_verifier=leaked_envelope["encrypted_verifier"],
            csrf_token=leaked_csrf,
            next="/",
        )
        self.assertEqual("/login", leaked.headers.get("location", ""))

    def test_settings_password_envelope_purpose_prevents_current_new_swap(self) -> None:
        username = self.random_id("swap")
        password = "StrongPass123"
        updated = "UpdatedPass456"
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        auth_row = db_fetch_one(
            "SELECT id,password_hash,password_salt,password_iters FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(auth_row)
        original_hash = str(auth_row["password_hash"] or "")
        csrf = issue_password_form_csrf_token("settings-password")
        current_salt = str(auth_row["password_salt"] or "")
        current_iters = int(auth_row["password_iters"] or 0)
        new_salt = uuid.uuid4().hex
        current_verifier = _password_verifier_hex(password, current_salt, current_iters)
        new_verifier = _password_verifier_hex(updated, new_salt, current_iters)
        current_as_new = _password_envelope_fields_direct(
            scope="settings-password",
            purpose="settings-new",
            username=username,
            csrf_token=csrf,
            verifier=current_verifier,
        )
        new_as_current = _password_envelope_fields_direct(
            scope="settings-password",
            purpose="settings-current",
            username=username,
            csrf_token=csrf,
            verifier=new_verifier,
        )

        changed = settings_password_update(
            user=username,
            current_password="",
            new_password="",
            new_password_confirm="",
            current_password_key_id=current_as_new["key_id"],
            current_password_envelope_token=current_as_new["envelope_token"],
            current_password_encrypted_verifier=current_as_new["encrypted_verifier"],
            new_password_key_id=new_as_current["key_id"],
            new_password_envelope_token=new_as_current["envelope_token"],
            new_password_encrypted_verifier=new_as_current["encrypted_verifier"],
            csrf_token=csrf,
            new_password_salt=new_salt,
            new_password_iters=str(current_iters),
        )

        self.assertEqual(changed.status_code, 303)
        messages = _flash_messages_from_response(changed)
        self.assertTrue(any("current password is incorrect" in item for item in messages))
        after_row = db_fetch_one("SELECT password_hash FROM users WHERE username=?", [username])
        self.assertIsNotNone(after_row)
        self.assertEqual(str(after_row["password_hash"] or ""), original_hash)

    def test_auth_password_envelope_flow_works_without_plaintext_submission(self) -> None:
        username = self.random_id("env")
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
        self.assertIn('name="email"', register_html)
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
        self.assertNotIn('name="password_verifier"', register_html)
        self.assertNotIn('name="password_proof"', register_html)
        self.assertIn('name="encrypted_verifier"', register_html)

        register_verifier = _password_verifier_hex(password, register_salt, register_iters)
        register_envelope = _password_envelope_fields_direct(
            scope="register-password",
            purpose="register",
            username=username,
            csrf_token=register_csrf,
            verifier=register_verifier,
        )

        reg = register_submit(
            request=_post_request("/register"),
            username=username,
            email=f"{username}@gmail.com",
            password="",
            password_confirm="",
            key_id=register_envelope["key_id"],
            envelope_token=register_envelope["envelope_token"],
            encrypted_verifier=register_envelope["encrypted_verifier"],
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
        self.assertNotIn('name="password_proof"', login_html)
        self.assertIn('name="encrypted_verifier"', login_html)
        login_meta = auth_password_meta(username=username, csrf_token=login_csrf)
        login_salt = str(login_meta.get("salt") or "")
        login_iters = int(login_meta.get("iters") or 0)
        self.assertRegex(login_salt, r"^[0-9a-f]{32}$")
        self.assertGreater(login_iters, 0)

        login_verifier = _password_verifier_hex(password, login_salt, login_iters)
        login_envelope = _password_envelope_fields_direct(
            scope="login-password",
            purpose="login",
            username=username,
            csrf_token=login_csrf,
            verifier=login_verifier,
        )
        login_ok = login_submit(
            request=_post_request("/login"),
            username=username,
            password="",
            key_id=login_envelope["key_id"],
            envelope_token=login_envelope["envelope_token"],
            encrypted_verifier=login_envelope["encrypted_verifier"],
            csrf_token=login_csrf,
            next="/",
        )
        self.assertEqual(login_ok.status_code, 303)
        self.assertIn("/problems", login_ok.headers.get("location", ""))

        settings_csrf = issue_password_form_csrf_token("settings-password")
        self.assertTrue(settings_csrf)
        auth_row = db_fetch_one(
            "SELECT email_normalized,email_verified_at,password_salt,password_iters FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(auth_row)
        self.assertEqual(str(auth_row["email_normalized"]), f"{username}@gmail.com")
        self.assertFalse(str(auth_row["email_verified_at"] or ""))
        current_salt = str(auth_row["password_salt"] or "").strip().lower()
        current_iters = int(auth_row["password_iters"] or 0)
        new_salt = uuid.uuid4().hex
        new_iters = current_iters
        self.assertRegex(current_salt, r"^[0-9a-f]{32}$")
        self.assertRegex(new_salt, r"^[0-9a-f]{32}$")
        self.assertGreater(current_iters, 0)

        current_verifier = _password_verifier_hex(password, current_salt, current_iters)
        current_envelope = _password_envelope_fields_direct(
            scope="settings-password",
            purpose="settings-current",
            username=username,
            csrf_token=settings_csrf,
            verifier=current_verifier,
        )
        new_verifier = _password_verifier_hex(updated, new_salt, new_iters)
        new_envelope = _password_envelope_fields_direct(
            scope="settings-password",
            purpose="settings-new",
            username=username,
            csrf_token=settings_csrf,
            verifier=new_verifier,
        )

        changed = settings_password_update(
            user=username,
            current_password="",
            new_password="",
            new_password_confirm="",
            current_password_key_id=current_envelope["key_id"],
            current_password_envelope_token=current_envelope["envelope_token"],
            current_password_encrypted_verifier=current_envelope["encrypted_verifier"],
            new_password_key_id=new_envelope["key_id"],
            new_password_envelope_token=new_envelope["envelope_token"],
            new_password_encrypted_verifier=new_envelope["encrypted_verifier"],
            csrf_token=settings_csrf,
            new_password_salt=new_salt,
            new_password_iters=str(new_iters),
        )
        self.assertEqual(changed.status_code, 303)
        self.assertIn("/settings", changed.headers.get("location", ""))
        changed_messages = _flash_messages_from_response(changed)
        self.assertTrue(any("password updated" in item for item in changed_messages))

        old_login_resp = login_page(_request("/login"))
        old_login_html = old_login_resp.body.decode("utf-8", errors="replace")
        old_login_csrf = _extract_hidden_input_value(old_login_html, "csrf_token")
        old_meta = auth_password_meta(username=username, csrf_token=old_login_csrf)
        old_salt = str(old_meta.get("salt") or "")
        old_iters = int(old_meta.get("iters") or 0)
        old_verifier = _password_verifier_hex(password, old_salt, old_iters)
        old_envelope = _password_envelope_fields_direct(
            scope="login-password",
            purpose="login",
            username=username,
            csrf_token=old_login_csrf,
            verifier=old_verifier,
        )
        old_login = login_submit(
            request=_post_request("/login"),
            username=username,
            password="",
            key_id=old_envelope["key_id"],
            envelope_token=old_envelope["envelope_token"],
            encrypted_verifier=old_envelope["encrypted_verifier"],
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
        new_envelope = _password_envelope_fields_direct(
            scope="login-password",
            purpose="login",
            username=username,
            csrf_token=new_login_csrf,
            verifier=new_verifier_login,
        )
        new_login = login_submit(
            request=_post_request("/login"),
            username=username,
            password="",
            key_id=new_envelope["key_id"],
            envelope_token=new_envelope["envelope_token"],
            encrypted_verifier=new_envelope["encrypted_verifier"],
            csrf_token=new_login_csrf,
            next="/",
        )
        self.assertEqual(new_login.status_code, 303)
        self.assertIn("/problems", new_login.headers.get("location", ""))

    def test_register_rejects_invalid_username_format(self) -> None:
        invalid = register_submit(
            request=_post_request("/register"),
            username="Alice_1",
            email="alice@gmail.com",
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
            email="ab@gmail.com",
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
            email="abcdefghijklmnopq@gmail.com",
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(too_long.status_code, 303)
        self.assertEqual("/register", too_long.headers.get("location", ""))
        long_messages = _flash_messages_from_response(too_long)
        self.assertTrue(any("invalid username" in item.lower() for item in long_messages))

    def test_register_accepts_uppercase_username_and_login_lookup_is_case_insensitive(self) -> None:
        username = "Qingyu"
        password = "StrongPass123"

        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        self.assertIn("/problems", reg.headers.get("location", ""))

        stored = db_fetch_one("SELECT username FROM users WHERE username=?", [username])
        self.assertIsNotNone(stored)
        self.assertEqual(str(stored["username"] or ""), username)

        login_lower = _login_with_password_envelope(username.lower(), password, next_path="/")
        self.assertEqual(login_lower.status_code, 303)
        self.assertIn("/problems", login_lower.headers.get("location", ""))

    def test_register_requires_terms_of_use_acceptance(self) -> None:
        username = self.random_id("terms")
        password = "StrongPass123"
        page = register_page(_request("/register"))
        html = page.body.decode("utf-8", errors="replace")
        csrf = _extract_hidden_input_value(html, "csrf_token")
        salt = _extract_hidden_input_value(html, "password_salt")
        iters = int(_extract_hidden_input_value(html, "password_iters") or "0")
        verifier = _password_verifier_hex(password, salt, iters)
        envelope = _password_envelope_fields_direct(
            scope="register-password",
            purpose="register",
            username=username,
            csrf_token=csrf,
            verifier=verifier,
        )

        resp = register_submit(
            request=_post_request("/register"),
            username=username,
            email=f"{username}@gmail.com",
            password="",
            password_confirm="",
            key_id=envelope["key_id"],
            envelope_token=envelope["envelope_token"],
            encrypted_verifier=envelope["encrypted_verifier"],
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

    def test_register_uses_pending_email_verification_when_smtp_configured(self) -> None:
        username = self.random_id("verify")
        resp, code = self._submit_pending_registration_with_smtp(username)

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location", ""), "/register/verify")
        self.assertFalse(_cookie_value_from_response(resp, AUTH_COOKIE_NAME))
        self.assertIsNone(db_fetch_one("SELECT id FROM users WHERE username=?", [username]))

        verified = register_verify(_post_request("/register/verify"), code=code)
        self.assertEqual(verified.status_code, 303)
        self.assertEqual(verified.headers.get("location", ""), "/problems")
        self.assertTrue(_cookie_value_from_response(verified, AUTH_COOKIE_NAME))
        user_row = db_fetch_one(
            "SELECT email_normalized,email_verified_at FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(user_row)
        self.assertEqual(str(user_row["email_normalized"]), f"{username}@gmail.com")
        self.assertTrue(str(user_row["email_verified_at"] or ""))

    def test_register_verify_page_prompts_for_email_code(self) -> None:
        resp = register_verify_page(_request("/register/verify"))
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Enter the verification code from your email", html)
        self.assertIn('name="code"', html)
        self.assertIn("The code expires in", html)

    def test_register_email_verification_ignores_poisoned_host(self) -> None:
        username = self.random_id("host")
        request = _request(
            "/register",
            method="POST",
            headers=[
                (b"host", b"evil.example"),
                (b"origin", b"http://evil.example"),
            ],
        )
        resp, code = self._submit_pending_registration_with_smtp(username, request=request)
        self.assertEqual(resp.status_code, 303)
        self.assertNotIn("evil.example", code)
        self.assertNotIn("/register/verify", code)
        self.assertNotIn("token=", code)

    def test_register_verification_code_accepts_spacing_and_case(self) -> None:
        username = self.random_id("code")
        resp, code = self._submit_pending_registration_with_smtp(username)
        self.assertEqual(resp.status_code, 303)
        submitted = code.replace("-", " ").lower()
        verified = register_verify(_post_request("/register/verify"), code=submitted)
        self.assertEqual(verified.status_code, 303)
        self.assertEqual(verified.headers.get("location", ""), "/problems")
        self.assertIsNotNone(db_fetch_one("SELECT id FROM users WHERE username=?", [username]))

    def test_register_verification_code_rejects_wrong_code(self) -> None:
        username = self.random_id("wrong")
        resp, code = self._submit_pending_registration_with_smtp(username)
        self.assertEqual(resp.status_code, 303)
        wrong = ("A" if code[0] != "A" else "B") + code[1:]
        rejected = register_verify(_post_request("/register/verify"), code=wrong)
        self.assertEqual(rejected.status_code, 303)
        self.assertEqual(rejected.headers.get("location", ""), "/register/verify")
        self.assertIsNone(db_fetch_one("SELECT id FROM users WHERE username=?", [username]))

    def test_register_verification_code_expires(self) -> None:
        username = self.random_id("expiry")
        resp, code = self._submit_pending_registration_with_smtp(username)

        self.assertEqual(resp.status_code, 303)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("within" in item.lower() for item in messages))
        db_execute(
            "UPDATE pending_registrations SET expires_at=? WHERE username=?",
            ["2000-01-01T00:00:00+00:00", username],
        )

        expired = register_verify(_post_request("/register/verify"), code=code)
        self.assertEqual(expired.status_code, 303)
        self.assertEqual(expired.headers.get("location", ""), "/register/verify")
        expired_messages = _flash_messages_from_response(expired)
        self.assertTrue(any("expired" in item.lower() for item in expired_messages))
        self.assertIsNone(db_fetch_one("SELECT id FROM users WHERE username=?", [username]))

    def test_register_submit_uses_global_rate_limit(self) -> None:
        self._replace_auth_constants(
            AUTH_REGISTER_SUBMIT_WINDOW_SEC=3600,
            AUTH_REGISTER_SUBMIT_MAX=1,
        )
        first_kwargs = self._valid_registration_kwargs(self.random_id("global-one"))
        second_kwargs = self._valid_registration_kwargs(self.random_id("global-two"))
        first = register_submit(request=_post_request("/register"), **first_kwargs)
        self.assertEqual(first.status_code, 303)

        second_request = _request(
            "/register",
            method="POST",
            headers=[
                (b"origin", b"http://testserver"),
                (b"x-forwarded-for", b"203.0.113.7"),
            ],
        )
        second = register_submit(request=second_request, **second_kwargs)
        self.assertEqual(second.status_code, 303)
        self.assertEqual(second.headers.get("location", ""), "/register")
        messages = _flash_messages_from_response(second)
        self.assertTrue(any("too many registration attempts" in item for item in messages))

    def test_register_email_send_uses_global_daily_limit(self) -> None:
        self._replace_auth_constants(
            AUTH_REGISTER_SUBMIT_WINDOW_SEC=3600,
            AUTH_REGISTER_SUBMIT_MAX=100,
            AUTH_REGISTER_EMAIL_GLOBAL_WINDOW_SEC=86400,
            AUTH_REGISTER_EMAIL_GLOBAL_MAX=1,
        )
        first_kwargs = self._valid_registration_kwargs(self.random_id("mail-one"))
        second_kwargs = self._valid_registration_kwargs(self.random_id("mail-two"))

        with patch.object(config.smtp_config_service, "delivery_configured", return_value=True):
            with patch.object(config.smtp_config_service, "send_registration_email") as send_mail:
                first = register_submit(request=_post_request("/register"), **first_kwargs)
                second = register_submit(request=_post_request("/register"), **second_kwargs)

        self.assertEqual(first.status_code, 303)
        self.assertEqual(first.headers.get("location", ""), "/register/verify")
        self.assertEqual(second.status_code, 303)
        self.assertEqual(second.headers.get("location", ""), "/register")
        self.assertEqual(send_mail.call_count, 1)
        blocked_row = db_fetch_one(
            "SELECT id FROM pending_registrations WHERE username=?",
            [str(second_kwargs["username"])],
        )
        self.assertIsNone(blocked_row)
        messages = _flash_messages_from_response(second)
        self.assertTrue(any("too many registration emails" in item for item in messages))

    def test_register_email_send_uses_per_email_cooldown(self) -> None:
        self._replace_auth_constants(
            AUTH_REGISTER_SUBMIT_WINDOW_SEC=3600,
            AUTH_REGISTER_SUBMIT_MAX=100,
            AUTH_REGISTER_EMAIL_GLOBAL_WINDOW_SEC=86400,
            AUTH_REGISTER_EMAIL_GLOBAL_MAX=100,
            AUTH_REGISTER_EMAIL_SEND_WINDOW_SEC=5,
            AUTH_REGISTER_EMAIL_SEND_MAX=1,
        )
        target_email = f"{self.random_id('target')}@gmail.com"
        first_kwargs = self._valid_registration_kwargs(self.random_id("cool-one"), email=target_email)
        second_kwargs = self._valid_registration_kwargs(self.random_id("cool-two"), email=target_email)
        third_kwargs = self._valid_registration_kwargs(self.random_id("cool-three"))

        with patch.object(config.smtp_config_service, "delivery_configured", return_value=True):
            with patch.object(config.smtp_config_service, "send_registration_email") as send_mail:
                first = register_submit(request=_post_request("/register"), **first_kwargs)
                second = register_submit(request=_post_request("/register"), **second_kwargs)
                third = register_submit(request=_post_request("/register"), **third_kwargs)

        self.assertEqual(first.status_code, 303)
        self.assertEqual(first.headers.get("location", ""), "/register/verify")
        self.assertEqual(second.status_code, 303)
        self.assertEqual(second.headers.get("location", ""), "/register")
        self.assertEqual(third.status_code, 303)
        self.assertEqual(third.headers.get("location", ""), "/register/verify")
        self.assertEqual(send_mail.call_count, 2)
        blocked_row = db_fetch_one(
            "SELECT id FROM pending_registrations WHERE username=?",
            [str(second_kwargs["username"])],
        )
        self.assertIsNone(blocked_row)
        messages = _flash_messages_from_response(second)
        self.assertTrue(any("too many registration emails" in item for item in messages))

    def test_register_verify_fail_uses_global_rate_limit(self) -> None:
        self._replace_auth_constants(
            AUTH_REGISTER_VERIFY_FAIL_WINDOW_SEC=3600,
            AUTH_REGISTER_VERIFY_FAIL_MAX=1,
        )
        first = register_verify(_post_request("/register/verify"), code="AAAA-BBBB-CCCC")
        second_request = _request(
            "/register/verify",
            method="POST",
            headers=[
                (b"origin", b"http://testserver"),
                (b"x-forwarded-for", b"203.0.113.9"),
            ],
        )
        second = register_verify(second_request, code="BBBB-CCCC-DDDD")

        self.assertEqual(first.status_code, 303)
        self.assertEqual(first.headers.get("location", ""), "/register/verify")
        first_messages = _flash_messages_from_response(first)
        self.assertTrue(any("registration verification failed" in item for item in first_messages))
        self.assertEqual(second.status_code, 303)
        self.assertEqual(second.headers.get("location", ""), "/register/verify")
        second_messages = _flash_messages_from_response(second)
        self.assertTrue(any("too many registration verification attempts" in item for item in second_messages))

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
        username = self.random_id("boot")
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
        envelope = _password_envelope_fields_direct(
            scope="setup-password",
            purpose="setup",
            username=username,
            csrf_token=csrf,
            verifier=verifier,
        )

        resp = setup_submit(
            request=_post_request("/setup"),
            username=username,
            password="",
            password_confirm="",
            key_id=envelope["key_id"],
            envelope_token=envelope["envelope_token"],
            encrypted_verifier=envelope["encrypted_verifier"],
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
        username = self.random_id("boot")
        resp = _setup_with_password_envelope(username, "StrongPass123", confirm_config="0", next_path="/")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual("/setup", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("confirm current system configuration paths" in item for item in messages))

    def test_register_does_not_claim_existing_passwordless_user(self) -> None:
        username = self.random_id("claim")
        workspace_service.ensure_user(username)
        row_before = db_fetch_one("SELECT password_hash FROM users WHERE username=?", [username])
        self.assertIsNotNone(row_before)
        self.assertFalse(str(row_before["password_hash"] or ""))

        resp = _register_with_password_envelope(username, "StrongPass123", next_path="/")
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

        first_reg = _register_with_password_envelope(first, "StrongPass123", next_path="/")
        self.assertEqual(first_reg.status_code, 303)
        first_row = db_fetch_one("SELECT is_system_admin FROM users WHERE username=?", [first])
        self.assertIsNotNone(first_row)
        self.assertEqual(int(first_row["is_system_admin"] or 0), 1)

        second_reg = _register_with_password_envelope(second, "StrongPass123", next_path="/")
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
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)

        blocked_location = ""
        blocked_message = ""
        for _ in range(12):
            bad = _login_with_password_envelope(username, "wrong-password", next_path="/")
            self.assertEqual(bad.status_code, 303)
            blocked_location = bad.headers.get("location", "")
            blocked_messages = _flash_messages_from_response(bad)
            blocked_message = blocked_messages[0] if blocked_messages else ""
            if "too many failed attempts" in blocked_message:
                break
        self.assertEqual("/login", blocked_location)
        self.assertIn("too many failed attempts", blocked_message)

        blocked_ok = _login_with_password_envelope(username, password, next_path="/")
        self.assertEqual(blocked_ok.status_code, 303)
        blocked_ok_messages = _flash_messages_from_response(blocked_ok)
        self.assertTrue(any("too many failed attempts" in item for item in blocked_ok_messages))

    def test_auth_middleware_blocks_cross_origin_post(self) -> None:
        username = self.random_id("csrf")
        password = "StrongPass123"
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            f"/problems/alice/sample/{username}/merge/start",
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
        username = self.random_id("csrfok")
        password = "StrongPass123"
        reg = _register_with_password_envelope(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            f"/problems/alice/sample/{username}/merge/start",
            f"{AUTH_COOKIE_NAME}={token}",
            method="POST",
            extra_headers=[(b"origin", b"http://testserver")],
        )

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        resp = asyncio.run(auth_middleware(req, _next))
        self.assertEqual(resp.status_code, 200)

    def test_auth_middleware_allows_userless_contest_path(self) -> None:
        username = self.random_id("ctauth")
        password = "StrongPass123"
        reg = _register_with_password_envelope(username, password, next_path="/")
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
        reg = _register_with_password_envelope(username, password, next_path="/")
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

    def test_admin_panel_is_separate_from_account_settings(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        resp = settings_page(_request("/settings"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("settings-account-panel", html)
        self.assertIn('class="root-section-navigation"', html)
        self.assertIn('aria-label="Settings sections"', html)
        self.assertIn('href="/settings"', html)
        self.assertIn('href="/agent/sessions"', html)
        self.assertIn('aria-current="page"', html)
        self.assertIn(">Problem</a>", html)
        self.assertIn(">Contest</a>", html)
        self.assertNotIn("Open admin panel", html)
        self.assertIn('href="/admin">Admin</a>', html)
        self.assertNotIn("System Admin Panel", html)
        self.assertNotIn("Judgehost Runtime Controls", html)

        admin_resp = admin_overview_page(_request("/admin"), user="alice")
        self.assertEqual(admin_resp.status_code, 200)
        admin_html = admin_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Administration", admin_html)
        self.assertIn('class="root-section-navigation"', admin_html)
        self.assertNotIn("admin-workbench", admin_html)
        self.assertNotIn("System operations and runtime health", admin_html)
        self.assertNotIn("Current runtime state", admin_html)
        self.assertNotIn("Judgehost task lifecycle totals", admin_html)
        self.assertNotIn("Cleanup preserves", admin_html)
        self.assertIn("Generated artifacts", admin_html)
        self.assertIn('/admin/maintenance/artifacts/cleanup', admin_html)
        self.assertIn('/admin/judgehosts', admin_html)
        self.assertIn('/admin/judgehosts/snapshot', admin_html)
        self.assertNotIn('/admin/worker-queue/snapshot', admin_html)
        self.assertIn('/admin/users', admin_html)

    def test_admin_overview_shows_generated_artifact_usage(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()
        usage = {
            "artifacts_bytes": 1024,
            "artifacts_files": 2,
            "cache_bytes": 512,
            "cache_files": 5,
            "total_bytes": 1536,
            "total_files": 7,
            "artifact_rows": 37,
            "audit_rows": 5,
            "removable_rows": 42,
            "table_rows": {"verifications": 3},
        }

        with patch.object(
            config.artifact_cleanup_service,
            "usage_snapshot",
            return_value=usage,
        ):
            response = admin_overview_page(_request("/admin"), user="alice")

        self.assertEqual(response.status_code, 200)
        html = response.body.decode("utf-8", errors="replace")
        self.assertIn("1.5 KiB", html)
        self.assertIn("across 7 files", html)
        self.assertIn("42 removable database rows", html)
        self.assertIn("including 5 audit entries", html)
        self.assertIn("Artifact files", html)
        self.assertIn("Runtime cache", html)
        self.assertIn("Verifications", html)

    def test_admin_routes_do_not_keep_settings_compatibility_paths(self) -> None:
        from app.main import app

        route_paths = {route.path for route in app.routes}
        self.assertTrue(
            {
                "/admin",
                "/admin/judgehosts",
                "/admin/users",
                "/admin/mail",
                "/admin/config/{category}",
            }.issubset(route_paths)
        )
        self.assertTrue(
            {
                "/settings/config/{category}",
                "/settings/judgehosts",
                "/settings/users",
                "/settings/smtp",
                "/settings/maintenance/artifacts/cleanup",
            }.isdisjoint(route_paths)
        )

    def test_artifact_cleanup_admin_action_redirects_or_returns_busy_counts(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        with patch.object(
            config.maintenance_service,
            "start_cleanup",
            return_value=CleanupStart(True, "started", {}),
        ):
            accepted = admin_panel_module.admin_artifacts_cleanup(user="alice")
        self.assertEqual(accepted.status_code, 303)
        self.assertEqual(accepted.headers.get("location"), "/maintenance")

        busy_counts = {
            "worker_queued": 1,
            "worker_running": 0,
            "judgehost_queued": 0,
            "judgehost_leased": 0,
            "judgehost_reporting": 0,
            "inflight_requests": 0,
        }
        with patch.object(
            config.maintenance_service,
            "start_cleanup",
            return_value=CleanupStart(False, "busy", busy_counts),
        ):
            busy = admin_panel_module.admin_artifacts_cleanup(user="alice")
        self.assertEqual(busy.status_code, 409)
        self.assertIn(b'"worker_queued":1', busy.body)

    def test_admin_users_page_can_search_user_list(self) -> None:
        match_user = self.random_id("lookupa")
        other_user = self.random_id("lookupb")
        match_email = f"{match_user}@gmail.com"
        other_email = f"{other_user}@gmail.com"
        self.assertEqual(_register_with_password_envelope(match_user, "StrongPass123", next_path="/").status_code, 303)
        self.assertEqual(_register_with_password_envelope(other_user, "StrongPass123", next_path="/").status_code, 303)
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        resp = admin_users_page(_request("/admin/users", query=f"query={match_user}"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Directory", html)
        self.assertIn(match_user, html)
        self.assertIn(match_email, html)
        self.assertNotIn(other_user, html)
        self.assertNotIn(other_email, html)
        self.assertIn("Showing 1 matching users", html)

    def test_system_admin_can_grant_and_revoke_system_admin(self) -> None:
        target = self.random_id("adminuser")
        password = "StrongPass123"
        reg = _register_with_password_envelope(target, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        grant = settings_user_system_admin_update(
            user="alice",
            target_username=target,
            action="grant",
        )
        self.assertEqual(grant.status_code, 303)
        row = db_fetch_one("SELECT is_system_admin FROM users WHERE LOWER(username)=LOWER(?)", [target])
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_system_admin"] or 0), 1)

        revoke = settings_user_system_admin_update(
            user="alice",
            target_username=target,
            action="revoke",
        )
        self.assertEqual(revoke.status_code, 303)
        row_after = db_fetch_one("SELECT is_system_admin FROM users WHERE LOWER(username)=LOWER(?)", [target])
        self.assertIsNotNone(row_after)
        self.assertEqual(int(row_after["is_system_admin"] or 0), 0)

    def test_system_admin_can_ban_and_unban_user(self) -> None:
        target = self.random_id("banuser")
        password = "StrongPass123"
        reg = _register_with_password_envelope(target, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        auth_token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(auth_token)
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        banned = settings_user_ban_update(
            user="alice",
            target_username=target,
            action="ban",
        )
        self.assertEqual(banned.status_code, 303)
        row = db_fetch_one("SELECT is_banned,banned_at FROM users WHERE LOWER(username)=LOWER(?)", [target])
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_banned"] or 0), 1)
        self.assertTrue(str(row["banned_at"] or ""))
        self.assertEqual(session_user(_request_with_cookie("/problems", f"{AUTH_COOKIE_NAME}={auth_token}")), "")

        denied_login = _login_with_password_envelope(target, password, next_path="/")
        self.assertEqual(denied_login.status_code, 303)
        self.assertTrue(any("account is banned" in item for item in _flash_messages_from_response(denied_login)))

        unbanned = settings_user_ban_update(
            user="alice",
            target_username=target,
            action="unban",
        )
        self.assertEqual(unbanned.status_code, 303)
        row_after = db_fetch_one("SELECT is_banned,banned_at FROM users WHERE LOWER(username)=LOWER(?)", [target])
        self.assertIsNotNone(row_after)
        self.assertEqual(int(row_after["is_banned"] or 0), 0)
        self.assertEqual(str(row_after["banned_at"] or ""), "")

        allowed_login = _login_with_password_envelope(target, password, next_path="/")
        self.assertEqual(allowed_login.status_code, 303)
        self.assertTrue(_cookie_value_from_response(allowed_login, AUTH_COOKIE_NAME))

    def test_system_admin_can_reset_another_users_password(self) -> None:
        target = self.random_id("pwuser")
        old_password = "StrongPass123"
        new_password = "UpdatedPass456"
        reg = _register_with_password_envelope(target, old_password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        old_token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(old_token)
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        changed = _settings_admin_password_update_with_envelope("alice", target, new_password)
        self.assertEqual(changed.status_code, 303)
        self.assertEqual(session_user(_request_with_cookie("/problems", f"{AUTH_COOKIE_NAME}={old_token}")), "")

        old_login = _login_with_password_envelope(target, old_password, next_path="/")
        self.assertEqual(old_login.status_code, 303)
        self.assertFalse(_cookie_value_from_response(old_login, AUTH_COOKIE_NAME))

        new_login = _login_with_password_envelope(target, new_password, next_path="/")
        self.assertEqual(new_login.status_code, 303)
        self.assertTrue(_cookie_value_from_response(new_login, AUTH_COOKIE_NAME))

    def test_admin_judgehosts_hides_auth_fields_when_disabled(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()
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

        resp = admin_judgehosts_page(_request("/admin/judgehosts"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('data-popup-open="judgehost-gen-script-popup"', html)
        self.assertIn('id="judgehost-gen-script-popup"', html)
        self.assertIn('data-gen-script-baseurl="1"', html)
        self.assertIn('data-gen-script-sudo="1"', html)
        self.assertIn('data-gen-script-output="1"', html)
        self.assertIn('data-judgehost-enable-toggle="1"', html)
        self.assertIn('data-judgehost-auth-block="1" hidden', html)
        self.assertIn('action="/admin/judgehosts/runtime"', html)

    def test_admin_judgehosts_shows_auth_fields_when_enabled(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()
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

        resp = admin_judgehosts_page(_request("/admin/judgehosts"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('data-judgehost-enable-toggle="1"', html)
        self.assertIn('data-judgehost-auth-block="1"', html)
        self.assertNotIn('data-judgehost-auth-block="1" hidden', html)
        self.assertIn('data-judgehost-api-username="1"', html)
        self.assertIn('data-judgehost-api-token="1"', html)

    def test_admin_judgehosts_formats_reported_telemetry(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        raw_last_judging = "2026-02-28T21:43:08.505465+00:00"
        fake_status = {
            "enabled": True,
            "auth_configured": True,
            "hosts_online": 1,
            "hosts_total": 1,
            "queue": {"queued": 0, "leased": 0, "completed": 0, "failed": 0},
            "hosts": [
                {
                    "hostname": "judgehost-lastseen-time",
                    "peer_addr": "203.0.113.10",
                    "enabled": True,
                    "online": True,
                    "age_sec": 3,
                    "last_seen_at": raw_last_judging,
                    "last_action": "heartbeat",
                    "first_seen_at": raw_last_judging,
                    "update_count": 9,
                    "active_leases": 0,
                    "last_task_id": "",
                    "last_run_id": "",
                    "judged_case_count": 42,
                    "last_judging_at": raw_last_judging,
                    "last_judging": {
                        "verification_id": "ver-123456789abcdef",
                        "problem_slug": "alice/sample",
                        "task_kind": "solution-run",
                        "source_label": "ac.cpp",
                        "test_name": "001.in",
                    },
                    "recent_avg_per_case_sec": 0.125,
                    "toolchains": [
                        {
                            "language_id": "cpp",
                            "compiler": "command=/usr/bin/g++\ng++ 14.2.0",
                            "runner": "",
                            "observed_at": raw_last_judging,
                            "judgetask_id": 123,
                        }
                    ],
                }
            ],
        }
        with patch.object(config.judgehost_task_service, "status", return_value=fake_status):
            resp = admin_judgehosts_page(_request("/admin/judgehosts"), user="alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("judgehost-lastseen-time", html)
        self.assertIn("203.0.113.10", html)
        self.assertIn("Judged cases", html)
        self.assertIn("Last judging", html)
        self.assertIn("Recent average", html)
        self.assertIn(">42</dd>", html)
        self.assertIn("0.125 s", html)
        self.assertIn(config.templates.env.filters["local_time"](raw_last_judging), html)
        self.assertNotIn(raw_last_judging, html)
        self.assertIn(
            'href="/problems/alice/sample/run/details?verification_id=ver-123456789abcdef"',
            html,
        )
        self.assertIn("Solution Run · <code>alice/sample</code> · ac.cpp / 001.in", html)
        self.assertIn("Reported toolchains", html)
        self.assertIn("g++ 14.2.0", html)
        self.assertIn("Reported for judging task 123", html)

    def test_settings_config_category_update_requires_system_admin(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            asyncio.run(
                settings_config_category_update(
                    _post_form_request(
                        "/admin/config/judging",
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
        workspace_service.clear_identity_caches()

        override_value = 777
        update_resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/admin/config/judging",
                    {"config_RUN_TEST_SELECTOR_LIMIT": str(override_value)},
                ),
                user="alice",
                category="judging",
            )
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn("/admin/config/judging", update_resp.headers.get("location", ""))
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

    def test_system_config_refresh_prunes_removed_keys(self) -> None:
        removed_key = "JUDGEHOST_INCLUDE_BUILD_PAYLOAD"
        db_execute(
            """
            INSERT OR REPLACE INTO system_config(key, value_json, updated_at, updated_by_user_id)
            VALUES(?,?,?,NULL)
            """,
            [removed_key, "false", "2026-08-08T00:00:00+00:00"],
        )

        config.system_config_service.refresh()

        self.assertIsNone(db_fetch_one("SELECT key FROM system_config WHERE key=?", [removed_key]))

    def test_settings_config_category_update_can_revert_single_override_to_default(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()
        self.addCleanup(settings_system_config_reset, user="alice")

        override_value = int(ADMIN_CONFIG_DEFAULTS["RUN_TEST_SELECTOR_LIMIT"]) + 123
        set_override_resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/admin/config/judging",
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
                    "/admin/config/judging",
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
        workspace_service.clear_identity_caches()

        self.addCleanup(settings_system_config_reset, user="alice")
        bad_token = "abc_non_ascii_" + chr(0x00E9)
        resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/admin/config/judgehost",
                    {"config_JUDGEHOST_API_TOKEN": bad_token},
                ),
                user="alice",
                category="judgehost",
            )
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/admin/config/judgehost", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("JUDGEHOST_API_TOKEN must contain only visible ASCII characters", messages[0])
        self.assertNotEqual(str(config.system_config_service.get("JUDGEHOST_API_TOKEN") or ""), bad_token)

    def test_branding_config_renders_unicode_escaped_values_and_optional_tagline(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()
        self.addCleanup(config.reload_runtime_values, include_restart_required=True)
        self.addCleanup(settings_system_config_reset, user="alice")

        config.system_config_service.apply_patch(
            {
                "UI_BRAND_NAME": "团队 <brand>",
                "UI_BRAND_TAGLINE": "Tagline & details",
                "UI_BROWSER_TITLE": "控制台 & title",
            },
            actor_user_id=int(db_fetch_one("SELECT id FROM users WHERE username=?", ["alice"])["id"]),
        )
        config.reload_runtime_values()

        rendered = settings_page(_request("/settings"), user="alice")
        html = rendered.body.decode("utf-8", errors="replace")
        self.assertIn("<title>控制台 &amp; title</title>", html)
        self.assertIn("团队 &lt;brand&gt;", html)
        self.assertIn("Tagline &amp; details", html)

        config.system_config_service.apply_patch(
            {"UI_BRAND_TAGLINE": ""},
            actor_user_id=int(db_fetch_one("SELECT id FROM users WHERE username=?", ["alice"])["id"]),
        )
        config.reload_runtime_values()
        without_tagline = settings_page(_request("/settings"), user="alice")
        without_tagline_html = without_tagline.body.decode("utf-8", errors="replace")
        self.assertNotIn('class="tagline"', without_tagline_html)

    def test_cookie_names_validate_distinct_http_tokens(self) -> None:
        service = config.system_config_service
        with self.assertRaisesRegex(ValueError, "valid HTTP cookie token"):
            service.validate_patch({"AUTH_COOKIE_NAME": "invalid cookie"})
        with self.assertRaisesRegex(ValueError, "must be distinct"):
            service.validate_patch(
                {
                    "AUTH_COOKIE_NAME": "same-cookie",
                    "SUDO_COOKIE_NAME": "same-cookie",
                }
            )

    def test_restart_cookie_names_wait_for_restart_and_apply_to_auth_flow(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()
        self.addCleanup(config.reload_runtime_values, include_restart_required=True)
        self.addCleanup(settings_system_config_reset, user="alice")
        config.system_config_service.reset()
        config.reload_runtime_values(include_restart_required=True)

        admin = db_fetch_one("SELECT id FROM users WHERE username=?", ["alice"])
        self.assertIsNotNone(admin)
        old_auth_name = str(config.constants.AUTH_COOKIE_NAME)
        old_sudo_name = str(config.constants.SUDO_COOKIE_NAME)
        old_flash_name = str(config.constants.FLASH_COOKIE_NAME)
        custom_names = {
            "AUTH_COOKIE_NAME": "test_auth_cookie",
            "SUDO_COOKIE_NAME": "test_sudo_cookie",
            "FLASH_COOKIE_NAME": "test_flash_cookie",
        }
        config.system_config_service.apply_patch(custom_names, actor_user_id=int(admin["id"]))

        self.assertEqual(str(config.constants.AUTH_COOKIE_NAME), old_auth_name)
        self.assertEqual(str(config.system_config_service.get("AUTH_COOKIE_NAME")), old_auth_name)
        auth_row = next(
            row
            for section in config.system_config_service.ui_sections()
            for row in section["rows"]
            if row["key"] == "AUTH_COOKIE_NAME"
        )
        self.assertEqual(auth_row["current_value"], custom_names["AUTH_COOKIE_NAME"])
        self.assertEqual(auth_row["effective_value"], old_auth_name)
        self.assertTrue(auth_row["pending_restart"])

        config.reload_runtime_values()
        self.assertEqual(str(config.constants.AUTH_COOKIE_NAME), old_auth_name)
        config.reload_runtime_values(include_restart_required=True)
        self.assertEqual(str(config.constants.AUTH_COOKIE_NAME), custom_names["AUTH_COOKIE_NAME"])
        self.assertEqual(str(config.constants.SUDO_COOKIE_NAME), custom_names["SUDO_COOKIE_NAME"])
        self.assertEqual(str(config.constants.FLASH_COOKIE_NAME), custom_names["FLASH_COOKIE_NAME"])

        username = self.random_id("cookie")
        registration = _register_with_password_envelope(username, "StrongPass123")
        set_cookie = _response_set_cookie_blob(registration)
        self.assertIn(f"{custom_names['AUTH_COOKIE_NAME']}=", set_cookie)
        token = _cookie_value_from_response(registration, custom_names["AUTH_COOKIE_NAME"])
        logout_response = logout(
            _request_with_cookie(
                "/logout",
                f"{custom_names['AUTH_COOKIE_NAME']}={token}",
            )
        )
        logout_set_cookie = _response_set_cookie_blob(logout_response)
        self.assertIn(f"{custom_names['AUTH_COOKIE_NAME']}=", logout_set_cookie)
        self.assertIn(f"{custom_names['FLASH_COOKIE_NAME']}=", logout_set_cookie)
        self.assertNotEqual(str(config.constants.AUTH_COOKIE_NAME), old_auth_name)
        self.assertNotEqual(str(config.constants.SUDO_COOKIE_NAME), old_sudo_name)
        self.assertNotEqual(str(config.constants.FLASH_COOKIE_NAME), old_flash_name)

    def test_settings_config_category_update_allows_printable_ascii_compile_flags(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        self.addCleanup(settings_system_config_reset, user="alice")
        flags = "-x c++ -Wall -O2 -static -pipe"
        resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/admin/config/toolchain",
                    {"config_TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS": flags},
                ),
                user="alice",
                category="toolchain",
            )
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/admin/config/toolchain", resp.headers.get("location", ""))
        self.assertEqual(str(config.system_config_service.get("TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS") or ""), flags)

    def test_settings_config_category_page_and_hot_reload(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()
        self.addCleanup(settings_system_config_reset, user="alice")

        page_resp = settings_config_category_page(
            _request("/admin/config/judging"),
            user="alice",
            category="judging",
        )
        self.assertEqual(page_resp.status_code, 200)
        page_html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Configuration", page_html)
        self.assertIn('class="admin-config-sidebar"', page_html)
        self.assertIn('class="admin-config-reset"', page_html)
        self.assertNotIn("10 keys", page_html)
        self.assertIn("RUN_EXEC_PROCESS_LIMIT", page_html)
        self.assertNotIn("RUN_EXEC_MEMORY_MB", page_html)
        self.assertNotIn("VERIFICATION_EXEC_MEMORY_MB", page_html)
        self.assertNotIn(">runtime</span>", page_html)

        restart_page = settings_config_category_page(
            _request("/admin/config/auth"),
            user="alice",
            category="auth",
        )
        restart_html = restart_page.body.decode("utf-8", errors="replace")
        self.assertIn('<strong class="warn">Restart required</strong>', restart_html)
        self.assertNotIn(">runtime</span>", restart_html)
        self.assertNotIn("pending restart", restart_html)

        update_value = 1536
        update_resp = asyncio.run(
            settings_config_category_update(
                _post_form_request(
                    "/admin/config/judging",
                    {"config_RUN_EXEC_PROCESS_LIMIT": str(update_value)},
                ),
                user="alice",
                category="judging",
            )
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn("/admin/config/judging", update_resp.headers.get("location", ""))
        self.assertEqual(int(config.system_config_service.get("RUN_EXEC_PROCESS_LIMIT")), update_value)
        self.assertEqual(int(config.constants.RUN_EXEC_PROCESS_LIMIT), update_value)

    def test_settings_config_category_page_renders_token_generate_button(self) -> None:
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        page_resp = settings_config_category_page(
            _request("/admin/config/judgehost"),
            user="alice",
            category="judgehost",
        )
        self.assertEqual(page_resp.status_code, 200)
        page_html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("JUDGEHOST_API_TOKEN", page_html)
        self.assertNotIn("JUDGEHOST_INCLUDE_BUILD_PAYLOAD", page_html)
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
        workspace_service.clear_identity_caches()
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
        workspace_service.clear_identity_caches()
        service = config.judgehost_task_service
        old_enabled = bool(service.state.enabled)
        old_token = str(service.state.api_token or "")
        self.addCleanup(setattr, service.state, "enabled", old_enabled)
        self.addCleanup(setattr, service.state, "api_token", old_token)
        service.state.enabled = True
        service.state.api_token = "admin-snapshot-token"
        service.domjudge_register_host("judgehost-admin-snapshot")
        resp = settings_judgehost_snapshot(user="alice")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertIn("hosts", payload)
        self.assertIn("hosts_online", payload)
        hosts = payload.get("hosts") or []
        self.assertTrue(any(str(item.get("hostname") or "") == "judgehost-admin-snapshot" for item in hosts))

    def test_problem_id_validation_requires_lowercase_dash_format(self) -> None:
        username = self.random_id("swauth")
        password = "StrongPass123"
        reg = _register_with_password_envelope(username, password, next_path="/")
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
