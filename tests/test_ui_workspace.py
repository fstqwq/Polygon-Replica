from __future__ import annotations
import io
import re
import zipfile

from .ui_support import (
    AUTH_COOKIE_NAME,
    HTTPException,
    Path,
    UIBaseSuite,
    asyncio,
    _cookie_value_from_response,
    _flash_messages_from_response,
    _post_form_request,
    _post_request,
    _register_with_password_proof,
    _request,
    _request_with_cookie,
    _sudo_with_password_proof,
    access_page,
    config,
    contests_root_create,
    contests_root_import,
    contests_root_import_confirm,
    contests_root_import_review,
    contests_root_page,
    db,
    files_page,
    general_page,
    general_save,
    git_commit,
    git_rebase_abort,
    git_restore_revision,
    git_service,
    history_page,
    json,
    os,
    patch,
    problem_delete,
    problems_root_import,
    problems_root_import_slug_hint,
    problems_root_page,
    run_cmd,
    switch_workspace,
    urlparse,
    parse_qs,
    uuid,
    workspace_access_grant,
    workspace_access_revoke,
    workspace_delete,
    workspace_page,
    workspace_service,
)

SUDO_COOKIE_NAME = config.constants.SUDO_COOKIE_NAME


class TestUIWorkspace(UIBaseSuite):
    def _ensure_committed_head(self, problem: str, user: str) -> tuple[Path, str]:
        ws = Path(workspace_service.ensure_workspace(problem, user))
        head_res = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"])
        head = head_res.stdout.strip() if head_res.returncode == 0 else ""
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return ws, head
        marker_rel = f"notes/ui-seed-{uuid.uuid4().hex[:8]}.txt"
        marker = ws / marker_rel
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("seed\n", encoding="utf-8")
        self.assertEqual(run_cmd(["git", "-C", str(ws), "config", "user.name", user]).returncode, 0)
        self.assertEqual(run_cmd(["git", "-C", str(ws), "config", "user.email", f"{user}@polygonlike.local"]).returncode, 0)
        # Seed commit should include the default workspace skeleton to avoid pull conflicts in sibling workspaces.
        self.assertEqual(run_cmd(["git", "-C", str(ws), "add", "-A"]).returncode, 0)
        commit = run_cmd(["git", "-C", str(ws), "commit", "-m", f"ui-seed-{uuid.uuid4().hex[:6]}"])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        push = run_cmd(["git", "-C", str(ws), "push", "origin", "HEAD:main"])
        self.assertEqual(push.returncode, 0, push.stderr or push.stdout)
        workspace_service.ensure_workspace(problem, user, refresh_status=True)
        refreshed = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"])
        refreshed_head = refreshed.stdout.strip() if refreshed.returncode == 0 else ""
        self.assertRegex(refreshed_head, r"^[0-9a-f]{40}$")
        return ws, refreshed_head

    def _issue_auth_cookie_header(self, username: str, password: str) -> str:
        reg = _register_with_password_proof(username, password, next_path="/")
        self.assertEqual(reg.status_code, 303)
        auth_token = _cookie_value_from_response(reg, AUTH_COOKIE_NAME)
        self.assertTrue(auth_token)
        return f"{AUTH_COOKIE_NAME}={auth_token}"

    def test_workspace_delete_requires_sudo_then_deletes_copy(self) -> None:
        username = f"wsdel-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        workspace_service.grant_repo_access("alice/sample", username, "owner")
        ws = Path(workspace_service.ensure_workspace("alice/sample", username))
        marker = ws / f"notes/delete-{uuid.uuid4().hex[:8]}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("temporary\n", encoding="utf-8")

        denied = workspace_delete(
            request=_request_with_cookie(
                f"/problems/alice/sample/{username}/workspace/delete",
                auth_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem="alice/sample",
            user=username,
        )
        self.assertEqual(denied.status_code, 303)
        self.assertIn("/sudo?next=", denied.headers.get("location", ""))

        sudo_resp = _sudo_with_password_proof(auth_cookie, password, next_path=f"/problems/alice/sample/{username}/workspace")
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        deleted = workspace_delete(
            request=_request_with_cookie(
                f"/problems/alice/sample/{username}/workspace/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem="alice/sample",
            user=username,
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual("/problems", deleted.headers.get("location", ""))
        self.assertFalse(ws.exists())
        ws_row = db.fetch_one(
            """
            SELECT id,path,branch,head_commit,dirty FROM workspaces
            WHERE problem_id=(SELECT id FROM problems WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE username=?)
            """,
            ["alice/sample", username],
        )
        self.assertIsNotNone(ws_row)
        self.assertEqual(str(ws_row["path"]), str(ws))
        self.assertEqual(int(ws_row["dirty"] or 0), 0)

    def test_problem_delete_requires_sudo_and_confirmation(self) -> None:
        username = f"pdel-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        problem = f"alice/pdel-problem-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, "Delete Problem Target")
        workspace_service.grant_repo_access(problem, username, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, username))
        self.assertTrue(ws.exists())
        row_before = db.fetch_one("SELECT id,repo_name FROM problems WHERE slug=?", [problem])
        self.assertIsNotNone(row_before)
        bare_repo = Path(config.settings.bare_root) / str(row_before["repo_name"])
        self.assertTrue(bare_repo.exists())

        denied = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/{username}/problem/delete",
                auth_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem=problem,
        )
        self.assertEqual(denied.status_code, 303)
        self.assertIn("/sudo?next=", denied.headers.get("location", ""))

        sudo_resp = _sudo_with_password_proof(auth_cookie, password, next_path=f"/problems/{problem}/{username}/workspace")
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        mismatch = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/{username}/problem/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem="wrong-slug",
        )
        self.assertEqual(mismatch.status_code, 303)
        self.assertIn(f"/problems/{problem}/{username}/workspace", mismatch.headers.get("location", ""))
        mismatch_messages = _flash_messages_from_response(mismatch)
        self.assertTrue(any("confirmation mismatch" in item for item in mismatch_messages))
        self.assertIsNotNone(db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem]))

        deleted = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/{username}/problem/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem=problem,
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual("/problems", deleted.headers.get("location", ""))
        self.assertIsNone(db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem]))
        self.assertFalse(ws.exists())
        self.assertFalse(bare_repo.exists())

    def test_problem_delete_unexpected_error_redirects_instead_of_500(self) -> None:
        username = f"pdelx-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        problem = f"alice/pdelx-problem-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, "Delete Problem Target")
        workspace_service.grant_repo_access(problem, username, "owner")
        workspace_service.ensure_workspace(problem, username)

        sudo_resp = _sudo_with_password_proof(
            auth_cookie,
            password,
            next_path=f"/problems/{problem}/{username}/workspace",
        )
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        with patch.object(workspace_service, "delete_problem", side_effect=Exception("boom")):
            resp = problem_delete(
                request=_request_with_cookie(
                    f"/problems/{problem}/{username}/problem/delete",
                    both_cookie,
                    method="POST",
                    extra_headers=[(b"origin", b"http://testserver")],
                ),
                problem=problem,
                user=username,
                confirm_problem=problem,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{problem}/{username}/workspace", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("problem delete failed" in item for item in messages))

    def test_problem_delete_rejects_unsafe_repo_name(self) -> None:
        username = f"pdelu-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        problem = f"alice/pdelu-problem-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, "Delete Problem Unsafe Repo")
        workspace_service.grant_repo_access(problem, username, "owner")
        workspace_service.ensure_workspace(problem, username)
        db.execute("UPDATE problems SET repo_name='' WHERE slug=?", [problem])

        sudo_resp = _sudo_with_password_proof(
            auth_cookie,
            password,
            next_path=f"/problems/{problem}/{username}/workspace",
        )
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        resp = problem_delete(
            request=_request_with_cookie(
                f"/problems/{problem}/{username}/problem/delete",
                both_cookie,
                method="POST",
                extra_headers=[(b"origin", b"http://testserver")],
            ),
            problem=problem,
            user=username,
            confirm_problem=problem,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{problem}/{username}/workspace", resp.headers.get("location", ""))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(any("unsafe" in item.lower() for item in messages))
        self.assertIsNotNone(db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem]))

    def test_general_save_persists_problem_config(self) -> None:
        resp = general_save(
            problem="alice/sample",
            user="alice",
            problem_name="Workspace General Title",
            time_limit_ms="3500",
            memory_limit_mb="768",
            mode="interactive",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/alice/sample/alice/statement", resp.headers.get("location", ""))

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        cfg_path = ws / "config" / "problem.json"
        self.assertTrue(cfg_path.exists())
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("time_limit_ms"), 3500)
        self.assertEqual(payload.get("memory_limit_mb"), 768)
        self.assertNotIn("interactive", payload)
        self.assertEqual(payload.get("mode"), "interactive")
        row = db.fetch_one("SELECT name FROM problems WHERE slug=?", ["alice/sample"])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["name"]), "Workspace General Title")
        self.assertFalse((ws / "statement" / "rendered").exists())

    def test_general_limits_are_clamped_to_configured_bounds(self) -> None:
        resp = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="10",
            memory_limit_mb="99999",
            mode="pass-fail",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/alice/sample/alice/statement", resp.headers.get("location", ""))

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        payload = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("time_limit_ms"), 100)
        self.assertEqual(payload.get("memory_limit_mb"), 2048)

    def test_general_save_accepts_multi_pass_mode(self) -> None:
        resp = general_save(
            problem="alice/sample",
            user="alice",
            time_limit_ms="2000",
            memory_limit_mb="1024",
            mode="multi-pass",
        )
        self.assertEqual(resp.status_code, 303)
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        payload = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("mode"), "multi-pass")
        self.assertNotIn("interactive", payload)

    def test_workspace_page_main_only_controls(self) -> None:
        resp = workspace_page(_request("/problems/alice/sample/alice/workspace"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Working Copy", html)
        self.assertIn("Main Working Copy", html)
        self.assertIn("Based on <strong>", html)
        self.assertNotIn("/problems/alice/sample/alice/git/pull", html)
        self.assertIn("Commit and Publish", html)
        self.assertNotIn("Problem Access", html)
        self.assertNotIn("<h2>Access</h2>", html)
        self.assertNotIn("Branch Operations", html)

    def test_workspace_page_get_does_not_persist_workspace_status(self) -> None:
        username = f"wsget-{uuid.uuid4().hex[:8]}"
        workspace_service.grant_repo_access("alice/sample", username, "owner")
        ws = Path(workspace_service.ensure_workspace("alice/sample", username))
        marker = ws / f"notes/get-dirty-{uuid.uuid4().hex[:8]}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("dirty\n", encoding="utf-8")

        ctx = workspace_service.workspace_context("alice/sample", username, include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        sentinel_updated_at = "2026-03-05T00:00:00Z"
        db.execute(
            "UPDATE workspaces SET branch=?, head_commit=?, dirty=?, updated_at=? WHERE id=?",
            ["main", "sentinel-head", 0, sentinel_updated_at, workspace_id],
        )
        before = db.fetch_one("SELECT branch,head_commit,dirty,updated_at FROM workspaces WHERE id=?", [workspace_id])
        self.assertIsNotNone(before)

        resp = workspace_page(_request(f"/problems/alice/sample/{username}/workspace"), "alice/sample", username)
        self.assertEqual(resp.status_code, 200)

        after = db.fetch_one("SELECT branch,head_commit,dirty,updated_at FROM workspaces WHERE id=?", [workspace_id])
        self.assertIsNotNone(after)
        self.assertEqual(str(after["branch"] or ""), str(before["branch"] or ""))
        self.assertEqual(str(after["head_commit"] or ""), str(before["head_commit"] or ""))
        self.assertEqual(int(after["dirty"] or 0), int(before["dirty"] or 0))
        self.assertEqual(str(after["updated_at"] or ""), str(before["updated_at"] or ""))

    def test_workspace_page_danger_zone_is_collapsed_and_sudo_gated(self) -> None:
        resp = workspace_page(_request("/problems/alice/sample/alice/workspace"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("<summary>Danger Zone</summary>", html)
        self.assertNotIn("Enable sudo mode now", html)
        self.assertIn('data-sudo-gated="1"', html)
        self.assertIn('data-sudo-required="1"', html)
        self.assertIn('data-sudo-url="/sudo?next=', html)
        self.assertIn("sudo_popup_done%3D1", html)
        self.assertIn("Delete Working Copy", html)
        self.assertIn("Delete Problem", html)

    def test_workspace_page_marks_delete_forms_ready_when_sudo_cookie_exists(self) -> None:
        username = f"ws-sudo-ready-{uuid.uuid4().hex[:8]}"
        password = "StrongPass123"
        auth_cookie = self._issue_auth_cookie_header(username, password)
        workspace_service.grant_repo_access("alice/sample", username, "owner")
        workspace_service.ensure_workspace("alice/sample", username)
        sudo_resp = _sudo_with_password_proof(auth_cookie, password, next_path=f"/problems/alice/sample/{username}/workspace")
        self.assertEqual(sudo_resp.status_code, 303)
        sudo_token = _cookie_value_from_response(sudo_resp, SUDO_COOKIE_NAME)
        self.assertTrue(sudo_token)
        both_cookie = f"{auth_cookie}; {SUDO_COOKIE_NAME}={sudo_token}"

        resp = workspace_page(
            _request_with_cookie(f"/problems/alice/sample/{username}/workspace", both_cookie),
            "alice/sample",
            username,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('data-sudo-gated="1"', html)
        self.assertNotIn('data-sudo-required="1"', html)
        self.assertIn('data-sudo-required="0"', html)

    def test_workspace_readiness_separates_statement_failure_from_pipeline_runnable(self) -> None:
        ws = self._prepare_verification_workspace("alice/sample", "alice")
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "tests": [
                        {"id": "001", "kind": "manual", "sample": True},
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        preview_id = f"p-workspace-failed-{uuid.uuid4().hex[:8]}"
        preview_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / preview_id
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text("statement/main.tex:7 Undefined control sequence\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "failed",
                "{}",
                str(preview_root),
                "2026-02-23T00:59:00Z",
                "2026-02-23T01:00:00Z",
            ],
        )

        resp = workspace_page(_request("/problems/alice/sample/alice/workspace"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'<span class="readiness-label">Statement</span>\s*<span class="readiness-state[^"]*submenu-status-warn[^"]*">\s*failed\s*</span>',
        )
        self.assertNotRegex(
            html,
            r'<span class="readiness-label">Statement</span>\s*<span class="readiness-state[^"]*submenu-status-danger[^"]*">',
        )
        self.assertRegex(
            html,
            r'<span class="readiness-label">Judge Pipeline</span>\s*<span class="readiness-state[^"]*">\s*runnable\s*</span>',
        )

    def test_workspace_page_shows_colored_diff_for_selected_file(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/workspace-diff-{uuid.uuid4().hex[:8]}.txt"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("base\n", encoding="utf-8")
        git_service.commit(ws, f"workspace-diff-base-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        target.write_text("base\nchanged\n", encoding="utf-8")

        resp = workspace_page(_request("/problems/alice/sample/alice/workspace", f"path={rel}"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"Selected: <code>{rel}</code>", html)
        self.assertIn("base", html)
        self.assertIn("changed", html)

    def test_commit_and_publish_rolls_back_commit_when_push_fails(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/ui-atomic-commit-{uuid.uuid4().hex[:8]}.txt"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("atomic-check\n", encoding="utf-8")
        head_before = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(head_before)

        with patch.object(git_service, "push", side_effect=RuntimeError("non-fast-forward")):
            resp = git_commit(problem="alice/sample", user="alice", message=f"ui-atomic-{uuid.uuid4().hex[:6]}")
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/workspace", loc)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("commit rolled back", messages[0])

        head_after = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head_after, head_before)
        status_text = run_cmd(["git", "-C", str(ws), "status", "--short", "--untracked-files=all"]).stdout
        self.assertIn(rel, status_text)

    def test_update_working_copy_shows_only_when_upstream_is_newer(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        initial = general_page(_request("/problems/alice/sample/alice/general"), "alice/sample", "alice")
        self.assertEqual(initial.status_code, 200)
        initial_html = initial.body.decode("utf-8", errors="replace")
        self.assertNotIn("/problems/alice/sample/alice/git/pull", initial_html)

        workspace_service.grant_repo_access("alice/sample", "bob", "owner")
        bob_ws = Path(workspace_service.ensure_workspace("alice/sample", "bob"))
        self.assertEqual(run_cmd(["git", "config", "user.name", "Bob"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_cmd(["git", "config", "user.email", "bob@example.com"], cwd=bob_ws).returncode, 0)
        # Keep this test deterministic when bob workspace already exists from earlier cases.
        self.assertEqual(run_cmd(["git", "pull", "--rebase", "origin", "main"], cwd=bob_ws).returncode, 0)
        marker = f"upstream-{uuid.uuid4().hex[:8]}.txt"
        (bob_ws / marker).write_text("upstream update\n", encoding="utf-8")
        self.assertEqual(run_cmd(["git", "add", marker], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_cmd(["git", "commit", "-m", "upstream update"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_cmd(["git", "push", "origin", "main"], cwd=bob_ws).returncode, 0)

        refreshed = general_page(_request("/problems/alice/sample/alice/general"), "alice/sample", "alice")
        self.assertEqual(refreshed.status_code, 200)
        refreshed_html = refreshed.body.decode("utf-8", errors="replace")
        self.assertIn("/problems/alice/sample/alice/git/pull", refreshed_html)

    def test_problem_page_denies_user_without_acl(self) -> None:
        private_problem = f"alice/ui-private-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(private_problem, "Private Problem")
        workspace_service.grant_repo_access(private_problem, "bob", "owner")
        workspace_service.ensure_workspace(private_problem, "bob")
        with self.assertRaises(HTTPException) as denied:
            general_page(_request(f"/problems/{private_problem}/alice/general"), private_problem, "alice")
        self.assertEqual(denied.exception.status_code, 403)

    def test_workspace_owner_can_manage_problem_access(self) -> None:
        register_bob = _register_with_password_proof("bob", "StrongPass123", next_path="/")
        self.assertEqual(register_bob.status_code, 303)
        grant_resp = workspace_access_grant(
            problem="alice/sample",
            user="alice",
            target_user="bob",
            role="write",
        )
        self.assertEqual(grant_resp.status_code, 303)
        member = db.fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["alice/sample", "bob"],
        )
        self.assertIsNotNone(member)
        self.assertEqual(str(member["role"]), "write")

        page = access_page(_request("/problems/alice/sample/alice/access"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Problem Access", html)
        self.assertIn("Grant / Update", html)
        self.assertIn("bob", html)

        revoke_resp = workspace_access_revoke(problem="alice/sample", user="alice", target_user="bob")
        self.assertEqual(revoke_resp.status_code, 303)
        removed = db.fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["alice/sample", "bob"],
        )
        self.assertIsNone(removed)

    def test_workspace_access_grant_requires_registered_user(self) -> None:
        target = f"user-{uuid.uuid4().hex[:8]}"
        row = db.fetch_one("SELECT id FROM users WHERE username=?", [target])
        self.assertIsNone(row)

        grant_resp = workspace_access_grant(
            problem="alice/sample",
            user="alice",
            target_user=target,
            role="read",
        )
        self.assertEqual(grant_resp.status_code, 303)
        loc = grant_resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/access", loc)
        grant_messages = _flash_messages_from_response(grant_resp)
        self.assertTrue(grant_messages)
        self.assertIn("register first", grant_messages[0])
        member = db.fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["alice/sample", target],
        )
        self.assertIsNone(member)

    def test_workspace_access_cannot_remove_last_owner(self) -> None:
        db.execute("DELETE FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?)", ["alice/sample"])
        workspace_service.grant_repo_access("alice/sample", "alice", "owner")
        resp = workspace_access_revoke(problem="alice/sample", user="alice", target_user="alice")
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/access", loc)
        revoke_messages = _flash_messages_from_response(resp)
        self.assertTrue(revoke_messages)
        self.assertIn("cannot remove the last owner", revoke_messages[0])

    def test_switch_workspace_denies_existing_problem_without_acl(self) -> None:
        private_problem = f"alice/ui-switch-private-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(private_problem, "Private Problem")
        workspace_service.grant_repo_access(private_problem, "bob", "owner")
        resp = switch_workspace(
            _request("/switch-workspace"),
            problem=private_problem,
            user="alice",
            page="general",
        )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems", loc)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("do not have access to this problem", messages[0])

    def test_switch_workspace_creates_problem_with_requested_name(self) -> None:
        slug = f"ui-switch-create-{uuid.uuid4().hex[:8]}"
        requested_name = f"Custom Created Name {uuid.uuid4().hex[:6]}"
        resp = switch_workspace(
            _request("/switch-workspace"),
            problem=slug,
            problem_name=requested_name,
            user="alice",
            page="statement",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{slug}/alice/statement", str(resp.headers.get("location", "")))
        row = db.fetch_one("SELECT name FROM problems WHERE slug=?", [f"alice/{slug}"])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["name"] or ""), requested_name)

    def test_problems_page_shows_only_participating_problems(self) -> None:
        owner_problem = f"alice/ui-owner-{uuid.uuid4().hex[:8]}"
        read_problem = f"alice/ui-read-{uuid.uuid4().hex[:8]}"
        other_problem = f"alice/ui-other-{uuid.uuid4().hex[:8]}"

        workspace_service.ensure_problem(owner_problem, "Owner Problem")
        workspace_service.ensure_workspace(owner_problem, "alice")
        workspace_service.grant_repo_access(owner_problem, "alice", "owner")

        workspace_service.ensure_problem(read_problem, "Read Problem")
        alice_row = db.fetch_one("SELECT id FROM users WHERE username=?", ["alice"])
        read_row = db.fetch_one("SELECT id FROM problems WHERE slug=?", [read_problem])
        self.assertIsNotNone(alice_row)
        self.assertIsNotNone(read_row)
        db.execute(
            "INSERT OR IGNORE INTO repo_acl(problem_id,user_id,role,created_at) VALUES(?,?,?,?)",
            [int(read_row["id"]), int(alice_row["id"]), "read", "2026-01-01T00:00:00+00:00"],
        )

        workspace_service.ensure_problem(other_problem, "Other Problem")
        workspace_service.ensure_workspace(other_problem, "bob")

        resp = problems_root_page(_request("/problems"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("My Problems", html)
        self.assertIn(owner_problem, html)
        self.assertIn(read_problem, html)
        self.assertNotIn(other_problem, html)
        self.assertNotIn(f"/problems/{other_problem}/alice/statement", html)
        self.assertIn("/problems/alice/sample/alice/statement", html)
        self.assertIn("v0 / upstream v0", html)
        self.assertIn("none / upstream missing", html)
        self.assertIn("revision-alert", html)

    def test_problems_page_orders_by_last_updated_desc(self) -> None:
        older_slug = f"alice/ui-sort-old-{uuid.uuid4().hex[:8]}"
        newer_slug = f"alice/ui-sort-new-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(older_slug, "Sort Older Problem")
        workspace_service.ensure_problem(newer_slug, "Sort Newer Problem")
        workspace_service.grant_repo_access(older_slug, "alice", "owner")
        workspace_service.grant_repo_access(newer_slug, "alice", "owner")
        workspace_service.ensure_workspace(older_slug, "alice")
        workspace_service.ensure_workspace(newer_slug, "alice")
        db.execute(
            """
            UPDATE workspaces
            SET updated_at=?
            WHERE problem_id=(SELECT id FROM problems WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE username='alice')
            """,
            ["2026-01-01T00:00:00+00:00", older_slug],
        )
        db.execute(
            """
            UPDATE workspaces
            SET updated_at=?
            WHERE problem_id=(SELECT id FROM problems WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE username='alice')
            """,
            ["2026-01-01T00:00:01+00:00", newer_slug],
        )

        resp = problems_root_page(_request("/problems"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(older_slug, html)
        self.assertIn(newer_slug, html)
        self.assertLess(html.find(newer_slug), html.find(older_slug))

    def test_problems_root_page_shows_import_entry(self) -> None:
        resp = problems_root_page(_request("/problems"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('id="polygon-import-form"', html)
        self.assertIn('id="polygon-import-slug-hint"', html)
        self.assertIn('/problems/import/slug-hint', html)
        self.assertNotIn('/problems/alice/sample/alice/export/import/slug-hint', html)

    def test_problems_root_page_exposes_title_action_links_with_popups(self) -> None:
        resp = problems_root_page(_request("/problems"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('data-popup-open="problem-open-popup"', html)
        self.assertIn('id="problem-open-popup"', html)
        self.assertIn('name="problem_name"', html)
        self.assertIn('data-popup-open="problem-import-popup"', html)
        self.assertIn('id="problem-import-popup"', html)
        self.assertIn('id="polygon-import-form"', html)

    def test_problems_root_import_slug_hint_uses_filename_and_avoids_duplicates(self) -> None:
        token = uuid.uuid4().hex[:8]
        base_slug = f"root-import-hint-{token}"
        workspace_service.ensure_problem(f"alice/{base_slug}", f"{base_slug} title")
        resp = problems_root_import_slug_hint(_request("/problems/import/slug-hint"), user="alice", filename=f"{base_slug}.zip", requested_slug="")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertTrue(bool(payload.get("valid")))
        self.assertTrue(bool(payload.get("exists")))
        self.assertEqual(str(payload.get("base") or ""), base_slug)
        suggested = str(payload.get("suggested") or "")
        self.assertTrue(suggested.startswith(base_slug + "-"))
        self.assertNotEqual(suggested, base_slug)

    def test_problems_root_import_slug_hint_strips_polygon_linux_suffix(self) -> None:
        base_slug = f"suffix-trim-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(f"alice/{base_slug}", f"{base_slug} title")
        filename = f"{base_slug}-46$linux.zip"
        resp = problems_root_import_slug_hint(_request("/problems/import/slug-hint"), user="alice", filename=filename, requested_slug="")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertEqual(str(payload.get("base") or ""), base_slug)
        suggested = str(payload.get("suggested") or "")
        self.assertTrue(suggested.startswith(base_slug + "-"))
        self.assertNotEqual(suggested, base_slug)

    def test_problems_root_import_creates_new_problem(self) -> None:
        class _Upload:
            def __init__(self, path: Path):
                self.filename = path.name
                self.file = path.open("rb")

        package_path = Path("third_party/polygon-package-examples/run-twice-guess-the-number-46$linux.zip")
        self.assertTrue(package_path.exists(), f"missing package fixture: {package_path}")
        upload = _Upload(package_path)
        target_slug = f"root-import-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/alice/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"polygon package imported as alice/{target_slug}", messages[0])
        ws = Path(workspace_service.ensure_workspace(f"alice/{target_slug}", "alice"))
        self.assertTrue((ws / "statement" / "statements.ftl").is_file())
        self.assertTrue((ws / "statement-sections" / "english" / "legend.tex").is_file())

    def test_problems_root_import_recovers_from_stale_user_cache(self) -> None:
        class _Upload:
            def __init__(self, path: Path):
                self.filename = path.name
                self.file = path.open("rb")

        package_path = Path("third_party/polygon-package-examples/run-twice-guess-the-number-46$linux.zip")
        self.assertTrue(package_path.exists(), f"missing package fixture: {package_path}")
        with workspace_service._cache_lock:
            workspace_service._user_cache["alice"] = {"id": 2_147_483_647, "username": "alice"}

        upload = _Upload(package_path)
        target_slug = f"root-import-cache-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/alice/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"polygon package imported as alice/{target_slug}", messages[0])

    def test_problems_root_import_accepts_icpc_package(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "icpc/problem.yaml",
                "\n".join(
                    [
                        "problem_format_version: 2025-09",
                        "name: Root Import ICPC",
                        "type: pass-fail",
                    ]
                )
                + "\n",
            )
            zf.writestr("icpc/data/secret/001.in", "1\n")
            zf.writestr("icpc/data/secret/001.ans", "1\n")
            zf.writestr("icpc/data/sample/1.in", "1\n")
            zf.writestr("icpc/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr("icpc/input_validators/validator.cpp", "int main(){return 0;}\n")
            zf.writestr("icpc/output_validator/checker.cpp", "int main(){return 0;}\n")

        upload = _Upload("root-import-icpc.zip", payload.getvalue())
        target_slug = f"root-icpc-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/alice/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn(f"icpc package imported as alice/{target_slug}", messages[0])
        ws = Path(workspace_service.ensure_workspace(f"alice/{target_slug}", "alice"))
        self.assertTrue((ws / "tests" / "manual" / "001.in").is_file())
        self.assertTrue((ws / "tests" / "answers" / "001.ans").is_file())

    def test_problems_root_import_warns_when_english_statement_language_missing(self) -> None:
        class _Upload:
            def __init__(self, filename: str, content: bytes):
                self.filename = filename
                self.file = io.BytesIO(content)

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "poly/problem.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="root-warn-lang">
  <names>
    <name language="english" value="Root Warn Lang"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
""",
            )
            zf.writestr("poly/tests/01", "1\n")
            zf.writestr("poly/tests/01.a", "1\n")
            zf.writestr("poly/statement-sections/russian/legend.tex", "Legend RU\n")

        upload = _Upload("root-import-non-english.zip", payload.getvalue())
        target_slug = f"root-warn-{uuid.uuid4().hex[:8]}"
        resp = problems_root_import(
            _post_request("/problems/import"),
            user="alice",
            package_upload=upload,
            problem_slug=target_slug,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/alice/statement", str(resp.headers.get("location", "")))
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("warning:", messages[0])
        self.assertIn("english not found", messages[0])
        self.assertIn("defaulting to russian", messages[0])
        ws = Path(workspace_service.ensure_workspace(f"alice/{target_slug}", "alice"))
        self.assertTrue((ws / "statement-sections" / "russian" / "legend.tex").is_file())
        self.assertFalse((ws / "statement-sections" / "english").exists())

    def test_files_page_embeds_pdf_preview_for_pdf_source(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = "statement-sections/english/problem.pdf"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4\n% test\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")

        resp = files_page(_request("/problems/alice/sample/alice/files", f"path={rel}"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Binary file is read-only in text editor.", html)
        self.assertIn("PDF preview:", html)
        self.assertIn(f"/problems/alice/sample/alice/files/download?path={rel}", html)
        self.assertIn("files-pdf-preview", html)
        self.assertNotIn('data-code-editor="1"', html)

    def test_files_page_uses_two_column_panel_layout_classes(self) -> None:
        resp = files_page(_request("/problems/alice/sample/alice/files"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('class="files-panel files-panel-browser"', html)
        self.assertIn('class="files-panel files-panel-editor"', html)
        self.assertIn('class="files-panel files-panel-ops"', html)

    def test_contests_root_page_is_top_level_without_selected_contest(self) -> None:
        slug = f"ui-root-contest-{uuid.uuid4().hex[:8]}"
        create_resp = contests_root_create(_post_request("/contests/create"), user="alice", contest_slug=slug, contest_title="Root Contest")
        self.assertEqual(create_resp.status_code, 303)
        self.assertIn("/contests", create_resp.headers.get("location", ""))

        resp = contests_root_page(_request("/contests"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("My Contests", html)
        self.assertIn(slug, html)
        self.assertIn("Import Polygon Contest Package", html)
        self.assertIn("/contests/import", html)

    def test_contests_root_page_exposes_title_action_links_with_popups(self) -> None:
        resp = contests_root_page(_request("/contests"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('data-popup-open="contest-create-popup"', html)
        self.assertIn('id="contest-create-popup"', html)
        self.assertIn('data-popup-open="contest-import-popup"', html)
        self.assertIn('id="contest-import-popup"', html)
        self.assertIn("/contests/create", html)
        self.assertIn("/contests/import", html)

    def test_contests_root_page_orders_by_last_updated_desc(self) -> None:
        older_slug = f"ui-contest-sort-old-{uuid.uuid4().hex[:8]}"
        newer_slug = f"ui-contest-sort-new-{uuid.uuid4().hex[:8]}"
        older_create = contests_root_create(_post_request("/contests/create"), user="alice", contest_slug=older_slug, contest_title="Old Contest")
        newer_create = contests_root_create(_post_request("/contests/create"), user="alice", contest_slug=newer_slug, contest_title="New Contest")
        self.assertEqual(older_create.status_code, 303)
        self.assertEqual(newer_create.status_code, 303)
        older_row = db.fetch_one("SELECT id FROM contests WHERE slug=?", [older_slug])
        newer_row = db.fetch_one("SELECT id FROM contests WHERE slug=?", [newer_slug])
        alice_row = db.fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(older_row)
        self.assertIsNotNone(newer_row)
        self.assertIsNotNone(alice_row)
        older_contest_id = int(older_row["id"])
        newer_contest_id = int(newer_row["id"])
        alice_id = int(alice_row["id"])
        db.execute("UPDATE contests SET created_at=? WHERE id=?", ["2026-01-01T00:00:00+00:00", older_contest_id])
        db.execute("UPDATE contests SET created_at=? WHERE id=?", ["2026-01-01T00:00:00+00:00", newer_contest_id])
        db.execute(
            """
            INSERT OR REPLACE INTO contest_properties(contest_id,key,value_json,updated_at,updated_by_user_id)
            VALUES(?,?,?,?,?)
            """,
            [newer_contest_id, "sort_probe", json.dumps({"v": 1}), "2026-01-01T00:00:05+00:00", alice_id],
        )
        db.execute(
            """
            INSERT OR REPLACE INTO contest_properties(contest_id,key,value_json,updated_at,updated_by_user_id)
            VALUES(?,?,?,?,?)
            """,
            [older_contest_id, "sort_probe", json.dumps({"v": 1}), "2026-01-01T00:00:01+00:00", alice_id],
        )

        resp = contests_root_page(_request("/contests"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(older_slug, html)
        self.assertIn(newer_slug, html)
        self.assertLess(html.find(newer_slug), html.find(older_slug))

    def test_contests_root_import_polygon_contest_package_creates_contest_and_normalizes_newlines(self) -> None:
        class _Upload:
            def __init__(self, path: Path):
                self.filename = path.name
                self.file = path.open("rb")

        package = Path("third_party/polygon-package-examples/contest/contest-55738.zip")
        self.assertTrue(package.exists(), f"missing contest package fixture: {package}")
        upload = _Upload(package)
        target_slug = f"contest-import-{uuid.uuid4().hex[:8]}"
        custom_problem_slugs = {
            1: f"contest-problem-a-{uuid.uuid4().hex[:8]}",
            2: f"contest-problem-b-{uuid.uuid4().hex[:8]}",
            3: f"contest-problem-c-{uuid.uuid4().hex[:8]}",
            4: f"contest-problem-d-{uuid.uuid4().hex[:8]}",
        }

        resp = contests_root_import(
            _post_request("/contests/import"),
            user="alice",
            package_upload=upload,
            contest_slug=target_slug,
            contest_title="",
        )
        self.assertEqual(resp.status_code, 303)
        location = str(resp.headers.get("location", ""))
        self.assertIn("/contests/import/review?", location)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("contest package parsed (4 problems)", messages[0])

        draft_id = ""
        parsed_location = urlparse(location)
        query = parse_qs(parsed_location.query)
        if "draft_id" in query and query["draft_id"]:
            draft_id = str(query["draft_id"][0] or "").strip()
        self.assertTrue(draft_id)

        review_resp = contests_root_import_review(
            _request("/contests/import/review", f"draft_id={draft_id}"),
            user="alice",
            draft_id=draft_id,
        )
        self.assertEqual(review_resp.status_code, 200)
        review_html = review_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Review Contest Import", review_html)
        self.assertIn('name="problem_slug_1"', review_html)
        self.assertIn('name="problem_slug_4"', review_html)

        confirm_form = {
            "draft_id": draft_id,
            "contest_slug": target_slug,
            "contest_title": "",
            "problem_slug_1": custom_problem_slugs[1],
            "problem_slug_2": custom_problem_slugs[2],
            "problem_slug_3": custom_problem_slugs[3],
            "problem_slug_4": custom_problem_slugs[4],
        }
        with patch(
            "app.impl.run_export.import_source._materialize_polygon_sample_answers",
            return_value={
                "sample_manual_total": 0,
                "sample_answers_missing": 0,
                "sample_answers_materialized": 0,
                "build_id": "",
            },
        ):
            confirm_resp = asyncio.run(
                contests_root_import_confirm(
                    _post_form_request("/contests/import/confirm", confirm_form),
                    user="alice",
                )
            )
        self.assertEqual(confirm_resp.status_code, 303)
        self.assertIn(f"/contests/{target_slug}/alice/overview", str(confirm_resp.headers.get("location", "")))
        confirm_messages = _flash_messages_from_response(confirm_resp)
        self.assertTrue(confirm_messages)
        self.assertIn(f"contest {target_slug} imported (4 problems)", confirm_messages[0])

        contest_row = db.fetch_one("SELECT id,title FROM contests WHERE slug=?", [target_slug])
        self.assertIsNotNone(contest_row)
        self.assertIn("The 2025 ICPC Asia East Continent Final Practice Contest", str(contest_row["title"] or ""))
        contest_id = int(contest_row["id"])
        imported_rows = db.fetch_all(
            """
            SELECT cp.idx,p.slug
            FROM contest_problems cp
            JOIN problems p ON p.id=cp.problem_id
            WHERE cp.contest_id=?
            ORDER BY cp.idx COLLATE NOCASE ASC
            """,
            [contest_id],
        )
        self.assertEqual(len(imported_rows), 4)
        self.assertEqual([str(row["idx"] or "") for row in imported_rows], ["A", "B", "C", "D"])
        self.assertEqual(
            [str(row["slug"] or "").strip() for row in imported_rows],
            [
                f"alice/{custom_problem_slugs[1]}",
                f"alice/{custom_problem_slugs[2]}",
                f"alice/{custom_problem_slugs[3]}",
                f"alice/{custom_problem_slugs[4]}",
            ],
        )

        taxi_problem_slug = ""
        for row in imported_rows:
            idx = str(row["idx"] or "").strip().upper()
            if idx == "C":
                taxi_problem_slug = str(row["slug"] or "").strip()
                break
        self.assertTrue(taxi_problem_slug)
        ws = Path(workspace_service.ensure_workspace(taxi_problem_slug, "alice"))
        manual_files = sorted((ws / "tests" / "manual").glob("*.in"))
        self.assertTrue(manual_files)
        self.assertNotIn(b"\r\n", manual_files[0].read_bytes())

        for row in imported_rows:
            problem_slug = str(row["slug"] or "").strip()
            if not problem_slug:
                continue
            pws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
            self.assertFalse((pws / "README.problem.md").exists())
            manual_rows = sorted((pws / "tests" / "manual").glob("*.in"))
            if manual_rows:
                self.assertNotIn(b"\r\n", manual_rows[0].read_bytes())
            answers_files = sorted((pws / "tests" / "answers").glob("*.ans"))
            if not answers_files:
                continue
            self.assertNotIn(b"\r\n", answers_files[0].read_bytes())

    def test_revision_history_page_v0_does_not_show_head_error_notification(self) -> None:
        # Fresh test workspace starts at v0 (no commits).
        resp = history_page(_request("/problems/alice/sample/alice/history"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Revision History", html)
        self.assertIn("No commits yet.", html)
        self.assertNotIn("ambiguous argument 'HEAD'", html)
        self.assertNotIn("unknown revision or path not in the working tree", html)

    def test_revision_history_page_lists_commits(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/ui-history-{uuid.uuid4().hex[:8]}.txt"
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("history-check\n", encoding="utf-8")
        marker = f"ui-history-{uuid.uuid4().hex[:6]}"
        git_service.commit(ws, marker, "alice", "alice@polygonlike.local")

        resp = history_page(_request("/problems/alice/sample/alice/history"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Revision History", html)
        self.assertIn(marker, html)
        self.assertIn("View Diff", html)
        self.assertIn("Restore To Working Copy", html)

    def test_revision_history_page_can_view_selected_revision_diff(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/ui-history-diff-{uuid.uuid4().hex[:8]}.txt"
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("before\n", encoding="utf-8")
        git_service.commit(ws, f"ui-history-diff-base-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        p.write_text("before\nafter\n", encoding="utf-8")
        marker = f"ui-history-diff-{uuid.uuid4().hex[:6]}"
        git_service.commit(ws, marker, "alice", "alice@polygonlike.local")
        selected = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(selected)

        resp = history_page(_request("/problems/alice/sample/alice/history", f"revision={selected}"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Revision Diff", html)
        self.assertIn("Selected revision:", html)
        self.assertIn(marker, html)
        self.assertIn("workspace-diff-line-add", html)
        self.assertIn("+after", html)

    def test_restore_revision_to_working_copy_from_history(self) -> None:
        self._ensure_committed_head("alice/sample", "alice")
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        rel = f"notes/ui-restore-{uuid.uuid4().hex[:8]}.txt"
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)

        p.write_text("old-version\n", encoding="utf-8")
        git_service.commit(ws, f"ui-restore-old-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        old_commit = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(old_commit)

        p.write_text("new-version\n", encoding="utf-8")
        git_service.commit(ws, f"ui-restore-new-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        p.write_text("dirty-local-change\n", encoding="utf-8")
        dirty_untracked = ws / f"notes/ui-restore-untracked-{uuid.uuid4().hex[:8]}.txt"
        dirty_untracked.write_text("temp\n", encoding="utf-8")

        resp = git_restore_revision(problem="alice/sample", user="alice", revision=old_commit, page="history")
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/history", location)
        restore_messages = _flash_messages_from_response(resp)
        self.assertTrue(restore_messages)
        self.assertIn("restored files from", restore_messages[0])

        self.assertEqual(p.read_text(encoding="utf-8"), "old-version\n")
        self.assertFalse(dirty_untracked.exists())
        change_summary = git_service.status_change_summary(ws, limit=32)
        self.assertGreater(int(change_summary.get("total") or 0), 0)

    def test_rebase_conflict_is_visible_and_abortable_from_ui(self) -> None:
        problem = "alice/sample"
        workspace_service.grant_repo_access(problem, "alice", "owner")
        workspace_service.grant_repo_access(problem, "bob", "owner")
        self._ensure_committed_head(problem, "alice")
        alice = Path(workspace_service.ensure_workspace(problem, "alice"))
        bob = Path(workspace_service.ensure_workspace(problem, "bob"))
        file_rel = f"notes/ui-rebase-conflict-{uuid.uuid4().hex[:8]}.txt"
        for ws in [alice, bob]:
            status = git_service.status(ws)
            if status.get("rebase_active"):
                try:
                    git_service.rebase_abort(ws)
                except Exception:
                    pass
            switched = run_cmd(["git", "-C", str(ws), "switch", "main"])
            if switched.returncode != 0:
                raise RuntimeError(switched.stderr or switched.stdout or "unable to switch workspace to main")
            git_service.pull(ws, "main")

        alice = Path(workspace_service.ensure_workspace(problem, "alice"))
        bob = Path(workspace_service.ensure_workspace(problem, "bob"))
        file_alice = alice / file_rel
        file_bob = bob / file_rel
        file_alice.parent.mkdir(parents=True, exist_ok=True)
        file_bob.parent.mkdir(parents=True, exist_ok=True)

        file_alice.write_text("base\n", encoding="utf-8")
        alice = Path(workspace_service.ensure_workspace(problem, "alice"))
        git_service.commit(alice, f"ui-base-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        git_service.push(alice, "main")
        bob = Path(workspace_service.ensure_workspace(problem, "bob"))
        git_service.pull(bob, "main")

        alice = Path(workspace_service.ensure_workspace(problem, "alice"))
        file_alice.write_text("alice-change\n", encoding="utf-8")
        git_service.commit(alice, f"ui-alice-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        git_service.push(alice, "main")

        bob = Path(workspace_service.ensure_workspace(problem, "bob"))
        file_bob.write_text("bob-change\n", encoding="utf-8")
        git_service.commit(bob, f"ui-bob-{uuid.uuid4().hex[:6]}", "bob", "bob@polygonlike.local")

        with self.assertRaises(RuntimeError):
            git_service.pull(bob, "main")

        ws_page = workspace_page(_request("/problems/alice/sample/bob/workspace"), "alice/sample", "bob")
        self.assertEqual(ws_page.status_code, 200)
        html = ws_page.body.decode("utf-8", errors="replace")
        self.assertIn("Rebase In Progress", html)
        self.assertIn("Continue Rebase", html)
        self.assertIn("Abort Rebase", html)
        self.assertIn(file_rel, html)
        self.assertIn("src=workspace", html)

        abort = git_rebase_abort("alice/sample", "bob")
        self.assertEqual(abort.status_code, 303)
        self.assertIn("/problems/alice/sample/bob/workspace", abort.headers.get("location", ""))

        status_after = git_service.status(bob)
        self.assertFalse(bool(status_after.get("rebase_active")))



