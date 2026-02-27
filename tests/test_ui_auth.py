from __future__ import annotations

from tests.ui_support import (
    ADMIN_CONFIG_DEFAULTS,
    AUTH_COOKIE_NAME,
    HTTPException,
    PlainTextResponse,
    Request,
    UIBaseSuite,
    _cookie_value_from_response,
    _extract_hidden_input_value,
    _flash_messages_from_response,
    _issue_password_form_csrf_token,
    _password_verifier_hex,
    _post_request,
    _request,
    _request_with_cookie,
    _response_set_cookie_blob,
    _session_user,
    _sha256_hex,
    asyncio,
    auth_middleware,
    auth_password_meta,
    config,
    db,
    json,
    login_page,
    login_submit,
    register_page,
    register_submit,
    settings_page,
    settings_password_update,
    settings_system_config_reset,
    settings_system_config_update,
    setup_page,
    setup_submit,
    switch_workspace,
    uuid,
    workspace_service,
)


class TestUIAuth(UIBaseSuite):
    def test_register_login_and_password_update(self) -> None:
        username = f"user-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        updated = "UpdatedPass456"

        reg = register_submit(
            request=_post_request("/register"),
            username=username,
            password=password,
            password_confirm=password,
            next="/",
        )
        self.assertEqual(reg.status_code, 303)
        self.assertIn(f"/problems/{username}/problems", reg.headers.get("location", ""))
        reg_set_cookie = _response_set_cookie_blob(reg)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", reg_set_cookie)
        self.assertIn("Secure", reg_set_cookie)

        user_row = db.fetch_one(
            "SELECT id,password_hash,password_salt,password_iters FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(user_row)
        self.assertTrue(str(user_row["password_hash"] or ""))
        self.assertTrue(str(user_row["password_salt"] or ""))
        self.assertGreater(int(user_row["password_iters"] or 0), 0)

        bad = login_submit(request=_post_request("/login"), username=username, password="wrong-password", next="/")
        self.assertEqual(bad.status_code, 303)
        self.assertEqual("/login", bad.headers.get("location", ""))
        bad_messages = _flash_messages_from_response(bad)
        self.assertTrue(any("invalid username or password" in item for item in bad_messages))

        ok = login_submit(request=_post_request("/login"), username=username, password=password, next="/")
        self.assertEqual(ok.status_code, 303)
        self.assertIn(f"/problems/{username}/problems", ok.headers.get("location", ""))
        set_cookie = _response_set_cookie_blob(ok)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", set_cookie)
        self.assertIn("Secure", set_cookie)
        token = _cookie_value_from_response(ok, AUTH_COOKIE_NAME)
        self.assertTrue(token)
        req = _request_with_cookie("/problems/sample/alice/general", f"{AUTH_COOKIE_NAME}={token}")
        self.assertEqual(_session_user(req), username)

        changed = settings_password_update(
            problem="sample",
            user=username,
            current_password=password,
            new_password=updated,
            new_password_confirm=updated,
        )
        self.assertEqual(changed.status_code, 303)
        self.assertIn("/problems/sample/", changed.headers.get("location", ""))
        changed_messages = _flash_messages_from_response(changed)
        self.assertTrue(any("password updated" in item for item in changed_messages))
        changed_set_cookie = _response_set_cookie_blob(changed)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", changed_set_cookie)
        self.assertIn("Secure", changed_set_cookie)

        old_login = login_submit(request=_post_request("/login"), username=username, password=password, next="/")
        self.assertEqual(old_login.status_code, 303)
        self.assertEqual("/login", old_login.headers.get("location", ""))
        old_messages = _flash_messages_from_response(old_login)
        self.assertTrue(any("invalid username or password" in item for item in old_messages))

        new_login = login_submit(request=_post_request("/login"), username=username, password=updated, next="/")
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
        )
        self.assertEqual(reg.status_code, 303)
        self.assertIn(f"/problems/{username}/problems", reg.headers.get("location", ""))

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
        self.assertIn(f"/problems/{username}/problems", login_ok.headers.get("location", ""))

        settings_csrf = _issue_password_form_csrf_token("settings-password")
        self.assertTrue(settings_csrf)
        auth_row = db.fetch_one("SELECT password_salt,password_iters FROM users WHERE username=?", [username])
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
            problem="sample",
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
        self.assertIn(f"/problems/sample/{username}/settings", changed.headers.get("location", ""))
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
        self.assertIn(f"/problems/{username}/problems", new_login.headers.get("location", ""))

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
        self.assertTrue(any("Use lowercased words, separated by dash" in item for item in invalid_messages))

    def test_setup_page_shows_config_when_no_registered_users(self) -> None:
        count = db.fetch_one(
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
        self.assertIn("POLYGONLIKE_DB", html)
        self.assertIn("I confirm the configuration paths below.", html)

    def test_setup_submit_creates_super_admin(self) -> None:
        username = f"bootstrap-{uuid.uuid4().hex[:8]}"
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
        self.assertIn(f"/problems/{username}/problems", resp.headers.get("location", ""))
        set_cookie = _response_set_cookie_blob(resp)
        self.assertIn(f"{AUTH_COOKIE_NAME}=", set_cookie)

        row = db.fetch_one(
            "SELECT is_system_admin,password_hash,password_salt,password_iters FROM users WHERE username=?",
            [username],
        )
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_system_admin"] or 0), 1)
        self.assertTrue(str(row["password_hash"] or ""))
        self.assertTrue(str(row["password_salt"] or ""))
        self.assertGreater(int(row["password_iters"] or 0), 0)

    def test_setup_submit_requires_config_confirmation(self) -> None:
        username = f"bootstrap-{uuid.uuid4().hex[:8]}"
        resp = setup_submit(
            request=_post_request("/setup"),
            username=username,
            password="StrongPass123",
            password_confirm="StrongPass123",
            confirm_config="0",
            next="/",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual("/setup", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("confirm current system configuration paths" in item for item in messages))

    def test_register_does_not_claim_existing_passwordless_user(self) -> None:
        username = f"claim-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(username)
        row_before = db.fetch_one("SELECT password_hash FROM users WHERE username=?", [username])
        self.assertIsNotNone(row_before)
        self.assertFalse(str(row_before["password_hash"] or ""))

        resp = register_submit(
            request=_post_request("/register"),
            username=username,
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertEqual("/register", location)
        unavailable_messages = _flash_messages_from_response(resp)
        self.assertTrue(any("username is unavailable" in item for item in unavailable_messages))

        row_after = db.fetch_one("SELECT password_hash FROM users WHERE username=?", [username])
        self.assertIsNotNone(row_after)
        self.assertFalse(str(row_after["password_hash"] or ""))

    def test_first_registered_user_becomes_system_admin(self) -> None:
        first = f"first-{uuid.uuid4().hex[:8]}"
        second = f"second-{uuid.uuid4().hex[:8]}"

        before = db.fetch_one(
            "SELECT COUNT(*) AS c FROM users WHERE COALESCE(TRIM(password_hash), '') <> ''",
            [],
        )
        self.assertIsNotNone(before)
        self.assertEqual(int(before["c"]), 0)

        first_reg = register_submit(
            request=_post_request("/register"),
            username=first,
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(first_reg.status_code, 303)
        first_row = db.fetch_one("SELECT is_system_admin FROM users WHERE username=?", [first])
        self.assertIsNotNone(first_row)
        self.assertEqual(int(first_row["is_system_admin"] or 0), 1)

        second_reg = register_submit(
            request=_post_request("/register"),
            username=second,
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(second_reg.status_code, 303)
        second_row = db.fetch_one("SELECT is_system_admin FROM users WHERE username=?", [second])
        self.assertIsNotNone(second_row)
        self.assertEqual(int(second_row["is_system_admin"] or 0), 0)

    def test_passwordless_user_does_not_take_system_admin_slot(self) -> None:
        workspace_service.ensure_user("placeholder-user")
        row = db.fetch_one("SELECT is_system_admin,password_hash FROM users WHERE username=?", ["placeholder-user"])
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_system_admin"] or 0), 0)
        self.assertFalse(str(row["password_hash"] or ""))

    def test_login_rate_limit_blocks_repeated_failures(self) -> None:
        username = f"ratelimit-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        reg = register_submit(
            request=_post_request("/register"),
            username=username,
            password=password,
            password_confirm=password,
            next="/",
        )
        self.assertEqual(reg.status_code, 303)

        blocked_location = ""
        blocked_message = ""
        for _ in range(12):
            bad = login_submit(request=_post_request("/login"), username=username, password="wrong-password", next="/")
            self.assertEqual(bad.status_code, 303)
            blocked_location = bad.headers.get("location", "")
            blocked_messages = _flash_messages_from_response(bad)
            blocked_message = blocked_messages[0] if blocked_messages else ""
            if "too many failed attempts" in blocked_message:
                break
        self.assertEqual("/login", blocked_location)
        self.assertIn("too many failed attempts", blocked_message)

        blocked_ok = login_submit(request=_post_request("/login"), username=username, password=password, next="/")
        self.assertEqual(blocked_ok.status_code, 303)
        blocked_ok_messages = _flash_messages_from_response(blocked_ok)
        self.assertTrue(any("too many failed attempts" in item for item in blocked_ok_messages))

    def test_auth_middleware_blocks_cross_origin_post(self) -> None:
        username = f"csrf-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        reg = register_submit(
            request=_post_request("/register"),
            username=username,
            password=password,
            password_confirm=password,
            next="/",
        )
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            f"/problems/sample/{username}/git/pull",
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
        reg = register_submit(
            request=_post_request("/register"),
            username=username,
            password=password,
            password_confirm=password,
            next="/",
        )
        self.assertEqual(reg.status_code, 303)
        token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(token)

        req = _request_with_cookie(
            f"/problems/sample/{username}/git/pull",
            f"{AUTH_COOKIE_NAME}={token}",
            method="POST",
            extra_headers=[(b"origin", b"http://testserver")],
        )

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        resp = asyncio.run(auth_middleware(req, _next))
        self.assertEqual(resp.status_code, 200)

    def test_auth_middleware_redirects_to_setup_when_no_registered_users(self) -> None:
        req = _request("/problems/sample/alice/general")

        async def _next(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", status_code=200)

        resp = asyncio.run(auth_middleware(req, _next))
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/setup?next=", resp.headers.get("location", ""))

    def test_settings_page_shows_system_admin_panel_for_system_admin(self) -> None:
        db.execute("UPDATE users SET is_system_admin=0")
        db.execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        resp = settings_page(_request("/problems/sample/alice/settings"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("System Admin Panel", html)
        self.assertIn("System Config JSON", html)
        self.assertIn("RUN_TEST_SELECTOR_LIMIT", html)

    def test_settings_system_config_update_requires_system_admin(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            settings_system_config_update(
                problem="sample",
                user="alice",
                config_json=json.dumps({"RUN_TEST_SELECTOR_LIMIT": 777}),
            )
        self.assertEqual(blocked.exception.status_code, 403)

    def test_settings_system_config_update_and_reset(self) -> None:
        db.execute("UPDATE users SET is_system_admin=0")
        db.execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        with workspace_service._cache_lock:
            workspace_service._user_cache.clear()

        override_value = 777
        update_resp = settings_system_config_update(
            problem="sample",
            user="alice",
            config_json=json.dumps({"RUN_TEST_SELECTOR_LIMIT": override_value}),
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn("/problems/sample/alice/settings", update_resp.headers.get("location", ""))
        self.assertEqual(
            int(config.system_config_service.get("RUN_TEST_SELECTOR_LIMIT")),
            override_value,
        )
        self.assertEqual(
            int(config.constants.RUN_TEST_SELECTOR_LIMIT),
            int(ADMIN_CONFIG_DEFAULTS["RUN_TEST_SELECTOR_LIMIT"]),
        )

        row = db.fetch_one("SELECT value_json FROM system_config WHERE key=?", ["RUN_TEST_SELECTOR_LIMIT"])
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(str(row["value_json"] or "null")), override_value)

        reset_resp = settings_system_config_reset(problem="sample", user="alice")
        self.assertEqual(reset_resp.status_code, 303)
        self.assertEqual(
            int(config.system_config_service.get("RUN_TEST_SELECTOR_LIMIT")),
            int(ADMIN_CONFIG_DEFAULTS["RUN_TEST_SELECTOR_LIMIT"]),
        )
        row_after = db.fetch_one("SELECT value_json FROM system_config WHERE key=?", ["RUN_TEST_SELECTOR_LIMIT"])
        self.assertIsNone(row_after)

    def test_problem_id_validation_requires_lowercase_dash_format(self) -> None:
        with self.assertRaises(ValueError) as bad_format:
            workspace_service.ensure_problem("Sample_Problem", "Bad Format")
        self.assertIn("Use lowercased words, separated by dash", str(bad_format.exception))

        with self.assertRaises(ValueError) as bad_dash:
            workspace_service.ensure_problem("sample-", "Bad Dash")
        self.assertIn("Use lowercased words, separated by dash", str(bad_dash.exception))

        invalid_open = switch_workspace(
            _request("/switch-workspace"),
            problem="Sample_Problem",
            user="alice",
            page="general",
        )
        self.assertEqual(invalid_open.status_code, 303)
        invalid_loc = invalid_open.headers.get("location", "")
        self.assertIn("/problems/alice/problems", invalid_loc)
        self.assertNotIn("message=", invalid_loc)
        invalid_messages = _flash_messages_from_response(invalid_open)
        self.assertTrue(invalid_messages)
        self.assertIn("Use lowercased words, separated by dash", invalid_messages[0])

        valid = switch_workspace(
            _request("/switch-workspace"),
            problem="minimal-spanning-tree",
            user="alice",
            page="general",
        )
        self.assertEqual(valid.status_code, 303)
        self.assertIn("/problems/minimal-spanning-tree/alice/general", valid.headers.get("location", ""))
