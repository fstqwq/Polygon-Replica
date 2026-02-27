from __future__ import annotations

from tests.ui_support import (
    HTTPException,
    Path,
    UIBaseSuite,
    _flash_messages_from_response,
    _post_request,
    _request,
    access_page,
    contests_root_create,
    contests_root_page,
    db,
    general_page,
    general_save,
    git_commit,
    git_rebase_abort,
    git_restore_revision,
    git_service,
    history_page,
    json,
    patch,
    problems_root_page,
    register_submit,
    run_cmd,
    switch_workspace,
    uuid,
    workspace_access_grant,
    workspace_access_revoke,
    workspace_page,
    workspace_service,
)


class TestUIWorkspace(UIBaseSuite):
    def test_general_save_persists_problem_config(self) -> None:
        resp = general_save(
            problem="sample",
            user="alice",
            problem_name="Workspace General Title",
            time_limit_ms="3500",
            memory_limit_mb="768",
            mode="interactive",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/sample/alice/general", resp.headers.get("location", ""))

        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        cfg_path = ws / "config" / "problem.json"
        self.assertTrue(cfg_path.exists())
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("time_limit_ms"), 3500)
        self.assertEqual(payload.get("memory_limit_mb"), 768)
        self.assertNotIn("interactive", payload)
        self.assertEqual(payload.get("mode"), "interactive")
        row = db.fetch_one("SELECT name FROM problems WHERE slug=?", ["sample"])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["name"]), "Workspace General Title")
        self.assertIn("\\ProblemTitle{Workspace General Title}", (ws / "statement" / "main.tex").read_text(encoding="utf-8"))

    def test_general_limits_are_clamped_to_configured_bounds(self) -> None:
        resp = general_save(
            problem="sample",
            user="alice",
            time_limit_ms="10",
            memory_limit_mb="99999",
            mode="pass-fail",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/sample/alice/general", resp.headers.get("location", ""))

        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        payload = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("time_limit_ms"), 100)
        self.assertEqual(payload.get("memory_limit_mb"), 2048)

    def test_general_save_accepts_multi_pass_mode(self) -> None:
        resp = general_save(
            problem="sample",
            user="alice",
            time_limit_ms="2000",
            memory_limit_mb="1024",
            mode="multi-pass",
        )
        self.assertEqual(resp.status_code, 303)
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        payload = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("mode"), "multi-pass")
        self.assertNotIn("interactive", payload)

    def test_workspace_page_main_only_controls(self) -> None:
        resp = workspace_page(_request("/problems/sample/alice/workspace"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Working Copy", html)
        self.assertIn("Main Working Copy", html)
        self.assertIn("Based on <strong>", html)
        self.assertNotIn("/problems/sample/alice/git/pull", html)
        self.assertIn("Commit and Publish", html)
        self.assertNotIn("Problem Access", html)
        self.assertNotIn("<h2>Access</h2>", html)
        self.assertNotIn("Branch Operations", html)

    def test_workspace_page_shows_colored_diff_for_selected_file(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = f"notes/workspace-diff-{uuid.uuid4().hex[:8]}.txt"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("base\n", encoding="utf-8")
        git_service.commit(ws, f"workspace-diff-base-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        target.write_text("base\nchanged\n", encoding="utf-8")

        resp = workspace_page(_request("/problems/sample/alice/workspace", f"path={rel}"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("workspace-diff-files", html)
        self.assertIn("workspace-diff-file-modified active", html)
        self.assertIn(f"Selected: <code>{rel}</code>", html)
        self.assertIn("workspace-diff-view", html)
        self.assertIn("workspace-diff-line-hunk", html)
        self.assertIn("workspace-diff-line-add", html)
        self.assertNotIn("workspace-diff-line-head", html)

    def test_commit_and_publish_rolls_back_commit_when_push_fails(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = f"notes/ui-atomic-commit-{uuid.uuid4().hex[:8]}.txt"
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("atomic-check\n", encoding="utf-8")
        head_before = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(head_before)

        with patch.object(git_service, "push", side_effect=RuntimeError("non-fast-forward")):
            resp = git_commit(problem="sample", user="alice", message=f"ui-atomic-{uuid.uuid4().hex[:6]}")
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/workspace", loc)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("commit rolled back", messages[0])

        head_after = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head_after, head_before)
        status_text = run_cmd(["git", "-C", str(ws), "status", "--short", "--untracked-files=all"]).stdout
        self.assertIn(rel, status_text)

    def test_update_working_copy_shows_only_when_upstream_is_newer(self) -> None:
        initial = general_page(_request("/problems/sample/alice/general"), "sample", "alice")
        self.assertEqual(initial.status_code, 200)
        initial_html = initial.body.decode("utf-8", errors="replace")
        self.assertNotIn("/problems/sample/alice/git/pull", initial_html)

        workspace_service.grant_repo_access("sample", "bob", "owner")
        bob_ws = Path(workspace_service.ensure_workspace("sample", "bob"))
        self.assertEqual(run_cmd(["git", "config", "user.name", "Bob"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_cmd(["git", "config", "user.email", "bob@example.com"], cwd=bob_ws).returncode, 0)
        # Keep this test deterministic when bob workspace already exists from earlier cases.
        self.assertEqual(run_cmd(["git", "pull", "--rebase", "origin", "main"], cwd=bob_ws).returncode, 0)
        marker = f"upstream-{uuid.uuid4().hex[:8]}.txt"
        (bob_ws / marker).write_text("upstream update\n", encoding="utf-8")
        self.assertEqual(run_cmd(["git", "add", marker], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_cmd(["git", "commit", "-m", "upstream update"], cwd=bob_ws).returncode, 0)
        self.assertEqual(run_cmd(["git", "push", "origin", "main"], cwd=bob_ws).returncode, 0)

        refreshed = general_page(_request("/problems/sample/alice/general"), "sample", "alice")
        self.assertEqual(refreshed.status_code, 200)
        refreshed_html = refreshed.body.decode("utf-8", errors="replace")
        self.assertIn("/problems/sample/alice/git/pull", refreshed_html)

    def test_problem_page_denies_user_without_acl(self) -> None:
        private_problem = f"ui-private-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(private_problem, "Private Problem")
        workspace_service.grant_repo_access(private_problem, "bob", "owner")
        workspace_service.ensure_workspace(private_problem, "bob")
        with self.assertRaises(HTTPException) as denied:
            general_page(_request(f"/problems/{private_problem}/alice/general"), private_problem, "alice")
        self.assertEqual(denied.exception.status_code, 403)

    def test_workspace_owner_can_manage_problem_access(self) -> None:
        register_bob = register_submit(
            request=_post_request("/register"),
            username="bob",
            password="StrongPass123",
            password_confirm="StrongPass123",
            next="/",
        )
        self.assertEqual(register_bob.status_code, 303)
        grant_resp = workspace_access_grant(
            problem="sample",
            user="alice",
            target_user="bob",
            role="write",
        )
        self.assertEqual(grant_resp.status_code, 303)
        member = db.fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["sample", "bob"],
        )
        self.assertIsNotNone(member)
        self.assertEqual(str(member["role"]), "write")

        page = access_page(_request("/problems/sample/alice/access"), "sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Problem Access", html)
        self.assertIn("Grant / Update", html)
        self.assertIn("bob", html)

        revoke_resp = workspace_access_revoke(problem="sample", user="alice", target_user="bob")
        self.assertEqual(revoke_resp.status_code, 303)
        removed = db.fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["sample", "bob"],
        )
        self.assertIsNone(removed)

    def test_workspace_access_grant_requires_registered_user(self) -> None:
        target = f"user-{uuid.uuid4().hex[:8]}"
        row = db.fetch_one("SELECT id FROM users WHERE username=?", [target])
        self.assertIsNone(row)

        grant_resp = workspace_access_grant(
            problem="sample",
            user="alice",
            target_user=target,
            role="read",
        )
        self.assertEqual(grant_resp.status_code, 303)
        loc = grant_resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/access", loc)
        grant_messages = _flash_messages_from_response(grant_resp)
        self.assertTrue(grant_messages)
        self.assertIn("register first", grant_messages[0])
        member = db.fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?) AND user_id=(SELECT id FROM users WHERE username=?)",
            ["sample", target],
        )
        self.assertIsNone(member)

    def test_workspace_access_cannot_remove_last_owner(self) -> None:
        db.execute("DELETE FROM repo_acl WHERE problem_id=(SELECT id FROM problems WHERE slug=?)", ["sample"])
        workspace_service.grant_repo_access("sample", "alice", "owner")
        resp = workspace_access_revoke(problem="sample", user="alice", target_user="alice")
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/access", loc)
        revoke_messages = _flash_messages_from_response(resp)
        self.assertTrue(revoke_messages)
        self.assertIn("cannot remove the last owner", revoke_messages[0])

    def test_switch_workspace_denies_existing_problem_without_acl(self) -> None:
        private_problem = f"ui-switch-private-{uuid.uuid4().hex[:8]}"
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
        self.assertIn("/problems/alice/problems", loc)
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        self.assertIn("do not have access to this problem", messages[0])

    def test_problems_page_shows_only_participating_problems(self) -> None:
        owner_problem = f"ui-owner-{uuid.uuid4().hex[:8]}"
        read_problem = f"ui-read-{uuid.uuid4().hex[:8]}"
        other_problem = f"ui-other-{uuid.uuid4().hex[:8]}"

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

        resp = problems_root_page(_request("/problems/alice/problems"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("My Problems", html)
        self.assertIn(owner_problem, html)
        self.assertIn(read_problem, html)
        self.assertNotIn(other_problem, html)
        self.assertNotIn(f"/problems/{other_problem}/alice/general", html)
        self.assertIn("/problems/sample/alice/general", html)
        self.assertIn("none / upstream missing", html)
        self.assertIn("revision-alert", html)

    def test_contests_root_page_is_top_level_without_selected_contest(self) -> None:
        slug = f"ui-root-contest-{uuid.uuid4().hex[:8]}"
        create_resp = contests_root_create(user="alice", contest_slug=slug, contest_title="Root Contest")
        self.assertEqual(create_resp.status_code, 303)
        self.assertIn("/problems/alice/contests", create_resp.headers.get("location", ""))

        resp = contests_root_page(_request("/problems/alice/contests"), "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("My Contests", html)
        self.assertIn(slug, html)

    def test_revision_history_page_lists_commits(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = f"notes/ui-history-{uuid.uuid4().hex[:8]}.txt"
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("history-check\n", encoding="utf-8")
        marker = f"ui-history-{uuid.uuid4().hex[:6]}"
        git_service.commit(ws, marker, "alice", "alice@polygonlike.local")

        resp = history_page(_request("/problems/sample/alice/history"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Revision History", html)
        self.assertIn(marker, html)
        self.assertIn("Restore To Working Copy", html)

    def test_restore_revision_to_working_copy_from_history(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        rel = f"notes/ui-restore-{uuid.uuid4().hex[:8]}.txt"
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)

        p.write_text("old-version\n", encoding="utf-8")
        git_service.commit(ws, f"ui-restore-old-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")
        old_commit = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(old_commit)

        p.write_text("new-version\n", encoding="utf-8")
        git_service.commit(ws, f"ui-restore-new-{uuid.uuid4().hex[:6]}", "alice", "alice@polygonlike.local")

        resp = git_restore_revision(problem="sample", user="alice", revision=old_commit, page="history")
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/history", location)
        restore_messages = _flash_messages_from_response(resp)
        self.assertTrue(restore_messages)
        self.assertIn("restored files from", restore_messages[0])

        self.assertEqual(p.read_text(encoding="utf-8"), "old-version\n")
        change_summary = git_service.status_change_summary(ws, limit=32)
        self.assertGreater(int(change_summary.get("total") or 0), 0)

    def test_rebase_conflict_is_visible_and_abortable_from_ui(self) -> None:
        problem = "sample"
        workspace_service.grant_repo_access(problem, "alice", "owner")
        workspace_service.grant_repo_access(problem, "bob", "owner")
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

        ws_page = workspace_page(_request("/problems/sample/bob/workspace"), "sample", "bob")
        self.assertEqual(ws_page.status_code, 200)
        html = ws_page.body.decode("utf-8", errors="replace")
        self.assertIn("Rebase In Progress", html)
        self.assertIn("Continue Rebase", html)
        self.assertIn("Abort Rebase", html)
        self.assertIn(file_rel, html)
        self.assertIn("src=workspace", html)

        abort = git_rebase_abort("sample", "bob")
        self.assertEqual(abort.status_code, 303)
        self.assertIn("/problems/sample/bob/workspace", abort.headers.get("location", ""))

        status_after = git_service.status(bob)
        self.assertFalse(bool(status_after.get("rebase_active")))
