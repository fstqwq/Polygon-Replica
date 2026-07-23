from __future__ import annotations

import asyncio
import io

from .db_helpers import (
    db_execute,
    db_fetch_all,
    db_fetch_one,
    read_contest_job_summary,
    write_contest_job_summary,
)

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.service.platform.git_process import run_git
from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec
from app.service.sandbox.base import ExecResult
from app.service.statement.render import ensure_statement_language_sources
from app.service.verification.signature import verification_signature

from .ui_support import (
    Path,
    UIBaseSuite,
    _flash_messages_from_response,
    _register_with_password_envelope,
    _request,
    _wait_for_row,
    contest_access_grant,
    contest_access_page,
    contest_access_revoke,
    contest_access_sync_all,
    contest_access_sync_user,
    contest_overview_page,
    contest_packages_artifact_download,
    contest_packages_build_start,
    contest_packages_job_status,
    contest_packages_page,
    contest_packages_preview_start,
    contest_statement_source_file,
    contest_statement_source_save,
    contest_statement_source_upload,
    contest_problems_add,
    contest_problems_change_general,
    contest_problems_page,
    contest_problems_remove_selected,
    contest_problems_renumber,
    contest_problems_reorder,
    contest_properties_page,
    contest_properties_save,
    contests_root_create,
    contests_root_page,
    git_service,
    json,
    uuid,
    config,
    workspace_service,
)


class TestUIContests(UIBaseSuite):
    class _FakeUpload:
        def __init__(self, filename: str, data: bytes):
            self.filename = filename
            self._buf = io.BytesIO(data)

        async def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        async def close(self) -> None:
            self._buf.close()

    def _create_contest(self, slug: str, title: str = "UI Contest") -> int:
        resp = contests_root_create(
            _request("/contests/create"),
            user="alice",
            contest_slug=slug,
            contest_title=title,
        )
        self.assertEqual(resp.status_code, 303)
        row = db_fetch_one("SELECT id FROM contests WHERE slug=?", [slug])
        self.assertIsNotNone(row)
        return int(row["id"])

    def test_system_admin_can_view_and_manage_all_contests(self) -> None:
        contest_slug = f"admin-contest-{uuid.uuid4().hex[:8]}"
        member_user = self.random_id("cmember")
        workspace_service.ensure_user(member_user)
        resp = contests_root_create(
            _request("/contests/create"),
            user="bob",
            contest_slug=contest_slug,
            contest_title="Admin Contest",
        )
        self.assertEqual(resp.status_code, 303)
        contest_id = int(db_fetch_one("SELECT id FROM contests WHERE slug=?", [contest_slug])["id"])
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        root_page = contests_root_page(_request("/contests"), user="alice")
        self.assertEqual(root_page.status_code, 200)
        root_html = root_page.body.decode("utf-8", errors="replace")
        self.assertIn(contest_slug, root_html)
        admin_access = config.contest_service.access_context(contest_id, workspace_service.known_user_id("alice"))
        self.assertEqual(admin_access["role"], "admin")
        self.assertTrue(admin_access["can_manage"])
        overview_rows = config.contest_service.user_contests_overview(
            workspace_service.known_user_id("alice"),
            limit=20,
        )
        admin_row = next(row for row in overview_rows if row["slug"] == contest_slug)
        self.assertEqual(admin_row["role"], "admin")

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)

        grant = contest_access_grant(
            contest=contest_slug,
            user="alice",
            target_user=member_user,
            role="read",
        )
        self.assertEqual(grant.status_code, 303)
        member_row = db_fetch_one(
            """
            SELECT role
            FROM contest_members
            WHERE contest_id=(SELECT id FROM contests WHERE slug=?)
              AND user_id=(SELECT id FROM users WHERE LOWER(username)=LOWER(?))
            """,
            [contest_slug, member_user],
        )
        self.assertIsNotNone(member_row)
        self.assertEqual(str(member_row["role"] or ""), "read")

    def test_contest_create_assigns_owner_membership(self) -> None:
        contest_slug = f"ui-contest-owner-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug)
        owner_row = db_fetch_one(
            """
            SELECT role
            FROM contest_members
            WHERE contest_id=?
              AND user_id=(SELECT id FROM users WHERE username='alice')
            """,
            [contest_id],
        )
        self.assertIsNotNone(owner_row)
        self.assertEqual(str(owner_row["role"] or ""), "owner")

    def test_contest_pages_and_problem_management_flow(self) -> None:
        contest_slug = f"ui-contest-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug)

        extra_problem = f"alice/ui-contest-prob-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(extra_problem)
        workspace_service.grant_repo_access(extra_problem, "alice", "owner")
        workspace_service.grant_repo_access("alice/sample", "alice", "owner")

        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=["alice/sample", extra_problem],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        add_msgs = _flash_messages_from_response(add_resp)
        self.assertTrue(add_msgs)
        self.assertIn("added 2 problem", add_msgs[0].lower())

        rows = db_fetch_all(
            "SELECT id,problem_id,idx FROM contest_problems WHERE contest_id=? ORDER BY idx COLLATE NOCASE ASC, id ASC",
            [contest_id],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0]["idx"]), "A")
        self.assertEqual(str(rows[1]["idx"]), "B")

        reorder_resp = contest_problems_reorder(
            contest=contest_slug,
            user="alice",
            contest_problem_ids=[str(rows[0]["id"]), str(rows[1]["id"])],
            contest_problem_indices=["B", "A"],
        )
        self.assertEqual(reorder_resp.status_code, 303)
        reorder_msgs = _flash_messages_from_response(reorder_resp)
        self.assertTrue(reorder_msgs)
        self.assertIn("problem order saved", reorder_msgs[0].lower())
        reordered = db_fetch_all(
            "SELECT id,idx FROM contest_problems WHERE contest_id=? ORDER BY id ASC",
            [contest_id],
        )
        idx_by_id = {int(row["id"]): str(row["idx"]) for row in reordered}
        self.assertEqual(idx_by_id[int(rows[0]["id"])], "B")
        self.assertEqual(idx_by_id[int(rows[1]["id"])], "A")

        renumber_resp = contest_problems_renumber(contest=contest_slug, user="alice")
        self.assertEqual(renumber_resp.status_code, 303)
        renumbered = db_fetch_all(
            "SELECT idx FROM contest_problems WHERE contest_id=? ORDER BY idx COLLATE NOCASE ASC, id ASC",
            [contest_id],
        )
        self.assertEqual([str(row["idx"]) for row in renumbered], ["A", "B"])

        remove_resp = contest_problems_remove_selected(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(rows[1]["problem_id"])],
        )
        self.assertEqual(remove_resp.status_code, 303)
        after_remove = db_fetch_all("SELECT problem_id FROM contest_problems WHERE contest_id=?", [contest_id])
        self.assertEqual(len(after_remove), 1)

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Overview", overview_html)
        self.assertIn("Contest Problems", overview_html)

        problems_page = contest_problems_page(
            _request(f"/contests/{contest_slug}/problems"),
            contest_slug,
            "alice",
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn("Change TL/ML", problems_html)
        self.assertIn("/problems/change-general", problems_html)

    def test_change_names_tl_ml_creates_per_problem_commit(self) -> None:
        problem_slug = f"alice/ui-bulk-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        add = run_git(["git", "-C", str(ws), "add", "."])
        self.assertEqual(add.returncode, 0, add.stderr)
        commit = run_git(["git", "-C", str(ws), "commit", "-m", "init problem"])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        push = run_git(["git", "-C", str(ws), "push", "origin", "HEAD:main"])
        self.assertEqual(push.returncode, 0, push.stderr or push.stdout)
        contest_slug = f"ui-contest-bulk-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Bulk Contest")

        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[problem_slug],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        pid = int(problem_row["id"])

        update_resp = contest_problems_change_general(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(pid)],
            problem_ids=[str(pid)],
            time_limit_ms_values=["3500"],
            memory_limit_mb_values=["512"],
            retry_job_id="",
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn(f"/contests/{contest_slug}/problems", update_resp.headers.get("location", ""))

        job_row = db_fetch_one(
            "SELECT id,status FROM contest_jobs WHERE contest_id=? ORDER BY created_at DESC LIMIT 1",
            [contest_id],
        )
        self.assertIsNotNone(job_row)
        summary = read_contest_job_summary(contest_id, str(job_row["id"]))
        self.assertEqual(str(summary.get("job_type") or ""), "change-general")
        results = summary.get("results") or []
        self.assertEqual(len(results), 1)
        first = dict(results[0])
        self.assertEqual(str(first.get("status") or ""), "success")
        commit_id = str(first.get("commit_id") or "")
        self.assertRegex(commit_id, r"^[0-9a-f]{40}$")

        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(int(cfg.get("time_limit_ms") or 0), 3500)
        self.assertEqual(int(cfg.get("memory_limit_mb") or 0), 512)

        last_subject = run_git(["git", "-C", str(ws), "log", "-1", "--pretty=%s"]).stdout.strip()
        self.assertEqual(last_subject, f"contest {contest_slug}: bulk update TL/ML")

    def test_change_names_tl_ml_rejects_uninitialized_repository(self) -> None:
        problem_slug = f"alice/ui-bulk-unborn-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        contest_slug = f"ui-contest-bulk-unborn-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Bulk Contest Unborn")

        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[problem_slug],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        pid = int(problem_row["id"])

        update_resp = contest_problems_change_general(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(pid)],
            problem_ids=[str(pid)],
            time_limit_ms_values=["3500"],
            memory_limit_mb_values=["512"],
            retry_job_id="",
        )
        self.assertEqual(update_resp.status_code, 303)

        job_row = db_fetch_one(
            "SELECT id,status FROM contest_jobs WHERE contest_id=? ORDER BY created_at DESC LIMIT 1",
            [contest_id],
        )
        self.assertIsNotNone(job_row)
        summary = read_contest_job_summary(contest_id, str(job_row["id"]))
        results = summary.get("results") or []
        self.assertEqual(len(results), 1)
        first = dict(results[0])
        self.assertEqual(str(first.get("status") or ""), "failed")
        self.assertIn("requires an initialized repository", str(first.get("error") or ""))

    def test_system_admin_can_add_problem_without_explicit_repo_acl(self) -> None:
        contest_slug = f"admin-contest-problems-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug)
        foreign_problem = f"bob/admin-only-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user("bob")
        workspace_service.ensure_problem(foreign_problem)
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

        page = contest_problems_page(
            _request(f"/contests/{contest_slug}/problems"),
            contest_slug,
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        page_html = page.body.decode("utf-8", errors="replace")
        self.assertIn(foreign_problem, page_html)

        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[foreign_problem],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        rows = db_fetch_all(
            "SELECT problem_id FROM contest_problems WHERE contest_id=?",
            [contest_id],
        )
        self.assertEqual(len(rows), 1)
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [foreign_problem])
        self.assertIsNotNone(problem_row)
        self.assertEqual(int(rows[0]["problem_id"]), int(problem_row["id"]))

    def test_contest_access_grant_reminds_to_sync_problem_access(self) -> None:
        contest_slug = f"ui-contest-access-remind-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Access Reminder Contest")
        problem_slug = f"alice/ui-access-remind-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(alice_row)
        self.assertIsNotNone(problem_row)
        config.contest_service.add_problem(contest_id, "A", int(problem_row["id"]), int(alice_row["id"]))
        self.assertEqual(_register_with_password_envelope("bob", "StrongPass123", next_path="/").status_code, 303)
        db_execute("UPDATE users SET is_system_admin=0 WHERE username=?", ["bob"])
        workspace_service.clear_identity_caches()

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="write")
        self.assertEqual(grant.status_code, 303)
        grant_messages = _flash_messages_from_response(grant)
        self.assertTrue(grant_messages)
        self.assertIn("reminder:", grant_messages[0].lower())
        self.assertIn("sync 1 writable contest problem", grant_messages[0].lower())

    def test_contest_access_sync_user_applies_contest_role_to_writable_problems(self) -> None:
        contest_slug = f"ui-contest-access-sync-user-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Access Sync User Contest")
        problem_a = f"alice/ui-access-sync-a-{uuid.uuid4().hex[:8]}"
        problem_b = f"alice/ui-access-sync-b-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_a)
        workspace_service.ensure_problem(problem_b)
        workspace_service.grant_repo_access(problem_a, "alice", "owner")
        workspace_service.grant_repo_access(problem_b, "alice", "owner")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        row_a = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_a])
        row_b = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_b])
        self.assertIsNotNone(alice_row)
        self.assertIsNotNone(row_a)
        self.assertIsNotNone(row_b)
        config.contest_service.add_problem(contest_id, "A", int(row_a["id"]), int(alice_row["id"]))
        config.contest_service.add_problem(contest_id, "B", int(row_b["id"]), int(alice_row["id"]))
        self.assertEqual(_register_with_password_envelope("bob", "StrongPass123", next_path="/").status_code, 303)
        db_execute("UPDATE users SET is_system_admin=0 WHERE username=?", ["bob"])
        workspace_service.clear_identity_caches()
        self.assertEqual(contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="read").status_code, 303)

        sync_resp = contest_access_sync_user(contest=contest_slug, user="alice", target_user="bob")
        self.assertEqual(sync_resp.status_code, 303)
        sync_messages = _flash_messages_from_response(sync_resp)
        self.assertTrue(sync_messages)
        self.assertIn("synced 2 problem access entry", sync_messages[0].lower())

        acl_rows = db_fetch_all(
            """
            SELECT p.slug,a.role
            FROM repo_acl a
            JOIN problems p ON p.id=a.problem_id
            WHERE a.user_id=(SELECT id FROM users WHERE username='bob')
              AND p.slug IN (?, ?)
            ORDER BY p.slug ASC
            """,
            [problem_a, problem_b],
        )
        self.assertEqual([(str(row["slug"]), str(row["role"])) for row in acl_rows], [(problem_a, "read"), (problem_b, "read")])

    def test_contest_access_sync_all_applies_roles_for_all_members(self) -> None:
        contest_slug = f"ui-contest-access-sync-all-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Access Sync All Contest")
        problem_a = f"alice/ui-access-sync-all-a-{uuid.uuid4().hex[:8]}"
        problem_b = f"alice/ui-access-sync-all-b-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_a)
        workspace_service.ensure_problem(problem_b)
        workspace_service.grant_repo_access(problem_a, "alice", "owner")
        workspace_service.grant_repo_access(problem_b, "alice", "owner")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        row_a = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_a])
        row_b = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_b])
        self.assertIsNotNone(alice_row)
        self.assertIsNotNone(row_a)
        self.assertIsNotNone(row_b)
        config.contest_service.add_problem(contest_id, "A", int(row_a["id"]), int(alice_row["id"]))
        config.contest_service.add_problem(contest_id, "B", int(row_b["id"]), int(alice_row["id"]))
        self.assertEqual(_register_with_password_envelope("bob", "StrongPass123", next_path="/").status_code, 303)
        self.assertEqual(_register_with_password_envelope("carol", "StrongPass123", next_path="/").status_code, 303)
        db_execute("UPDATE users SET is_system_admin=0 WHERE username IN (?, ?)", ["bob", "carol"])
        workspace_service.clear_identity_caches()
        self.assertEqual(contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="read").status_code, 303)
        self.assertEqual(contest_access_grant(contest=contest_slug, user="alice", target_user="carol", role="write").status_code, 303)

        sync_resp = contest_access_sync_all(contest=contest_slug, user="alice")
        self.assertEqual(sync_resp.status_code, 303)
        sync_messages = _flash_messages_from_response(sync_resp)
        self.assertTrue(sync_messages)
        self.assertIn("synced contest problem access for 2 member", sync_messages[0].lower())
        self.assertIn("4 entry change", sync_messages[0].lower())

        acl_rows = db_fetch_all(
            """
            SELECT u.username,p.slug,a.role
            FROM repo_acl a
            JOIN users u ON u.id=a.user_id
            JOIN problems p ON p.id=a.problem_id
            WHERE u.username IN ('bob', 'carol')
              AND p.slug IN (?, ?)
            ORDER BY u.username ASC, p.slug ASC
            """,
            [problem_a, problem_b],
        )
        self.assertEqual(
            [(str(row["username"]), str(row["slug"]), str(row["role"])) for row in acl_rows],
            [
                ("bob", problem_a, "read"),
                ("bob", problem_b, "read"),
                ("carol", problem_a, "write"),
                ("carol", problem_b, "write"),
            ],
        )

    def test_contest_properties_access_and_packages_pages(self) -> None:
        contest_slug = f"ui-contest-props-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Props Contest")
        workspace_service.ensure_user("bob")

        save_props = contest_properties_save(
            contest=contest_slug,
            user="alice",
            title="Props Contest Updated",
            location="San Francisco",
            date_text="2026-03-01",
        )
        self.assertEqual(save_props.status_code, 303)
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        config.contest_service.set_statement_default_language(contest_id, int(alice_row["id"]), "english")

        contest_row = db_fetch_one("SELECT title FROM contests WHERE id=?", [contest_id])
        self.assertIsNotNone(contest_row)
        self.assertEqual(str(contest_row["title"]), "Props Contest Updated")

        props_page = contest_properties_page(
            _request(f"/contests/{contest_slug}/properties"),
            contest_slug,
            "alice",
        )
        self.assertEqual(props_page.status_code, 200)
        props_html = props_page.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Properties", props_html)
        self.assertIn("Statement Language", props_html)
        self.assertIn("english", props_html)

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="write")
        self.assertEqual(grant.status_code, 303)
        membership = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNotNone(membership)
        self.assertEqual(str(membership["role"]), "write")

        access_page_resp = contest_access_page(
            _request(f"/contests/{contest_slug}/access"),
            contest_slug,
            "alice",
        )
        self.assertEqual(access_page_resp.status_code, 200)
        access_html = access_page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Access", access_html)
        self.assertIn("bob", access_html)
        self.assertIn('option value="write"', access_html)
        self.assertIn('option value="read"', access_html)
        self.assertNotIn('option value="owner"', access_html)
        self.assertIn("fixed owner", access_html)

        revoke = contest_access_revoke(contest=contest_slug, user="alice", target_user="bob")
        self.assertEqual(revoke.status_code, 303)
        removed = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNone(removed)

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Packages", packages_html)
        self.assertIn('<span class="submenu-title">Build PDF</span>', packages_html)

    def test_contest_overview_best_effort_infers_location_and_date_from_statements(self) -> None:
        contest_slug = f"ui-contest-overview-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Overview Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        config.contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_user_id,
            files=[
                {
                    "key": "statements/english/statements.tex",
                    "language": "english",
                    "package_bytes": (
                        b"\\documentclass{article}\n"
                        b"\\begin{document}\n"
                        b"\\contest\n"
                        b"{Overview Contest}%\n"
                        b"{Hangzhou, China}%\n"
                        b"{1 February, 2026}%\n"
                        b"\\end{document}\n"
                    ),
                }
            ],
        )
        config.contest_service.set_statement_default_language(contest_id, actor_user_id, "english")

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn("Hangzhou, China", overview_html)
        self.assertIn("1 February, 2026", overview_html)

    def test_contest_access_cannot_transfer_owner_role(self) -> None:
        contest_slug = f"ui-contest-owner-transfer-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Owner Transfer Contest")
        _register_with_password_envelope("bob", "StrongPass123", next_path="/")

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="owner")
        self.assertEqual(grant.status_code, 303)
        grant_messages = _flash_messages_from_response(grant)
        self.assertTrue(grant_messages)
        self.assertIn("owner access is fixed and cannot be transferred", grant_messages[0])
        membership = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNone(membership)

        revoke = contest_access_revoke(contest=contest_slug, user="alice", target_user="alice")
        self.assertEqual(revoke.status_code, 303)
        revoke_messages = _flash_messages_from_response(revoke)
        self.assertTrue(revoke_messages)
        self.assertIn("owner access is fixed and cannot be transferred", revoke_messages[0])

    def test_contest_packages_exposes_statement_sources_and_uploads_resources(self) -> None:
        contest_slug = f"contest-stmt-src-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Statement Source Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)

        page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Statement Sources", html)
        self.assertIn("Edit statements.tex", html)
        self.assertIn("Edit olymp.sty", html)
        self.assertIn("default, not saved yet", html)

        save_resp = contest_statement_source_save(
            contest=contest_slug,
            user="alice",
            language="english",
            path="olymp.sty",
            content="% custom contest style\n",
        )
        self.assertEqual(save_resp.status_code, 303)
        self.assertIn("source_path=olymp.sty", str(save_resp.headers.get("location", "")))
        source_root = config.contest_service.contest_source_root(contest_slug)
        self.assertEqual(
            (source_root / "statements" / "english" / "olymp.sty").read_text(encoding="utf-8"),
            "% custom contest style\n",
        )
        row = db_fetch_one(
            "SELECT key FROM contest_attachments WHERE contest_id=? AND key=?",
            [contest_id, "statements/english/olymp.sty"],
        )
        self.assertIsNotNone(row)

        ftl_resp = contest_statement_source_save(
            contest=contest_slug,
            user="alice",
            language="english",
            path="statements.ftl",
            content="FTL template\r\n",
        )
        self.assertEqual(ftl_resp.status_code, 303)
        self.assertEqual(
            (source_root / "statements" / "english" / "statements.ftl").read_text(encoding="utf-8"),
            "FTL template\n",
        )

        upload_resp = asyncio.run(
            contest_statement_source_upload(
                contest=contest_slug,
                user="alice",
                language="english",
                path="logos/",
                upload=self._FakeUpload("logo.png", b"PNG"),
            )
        )
        self.assertEqual(upload_resp.status_code, 303)
        self.assertEqual((source_root / "statements" / "english" / "logos" / "logo.png").read_bytes(), b"PNG")
        row = db_fetch_one(
            "SELECT key FROM contest_attachments WHERE contest_id=? AND key=?",
            [contest_id, "statements/english/logos/logo.png"],
        )
        self.assertIsNotNone(row)

        updated_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages", "language=english&source_path=olymp.sty"),
            contest_slug,
            "alice",
            language="english",
            source_path="olymp.sty",
        )
        self.assertEqual(updated_page.status_code, 200)
        updated_html = updated_page.body.decode("utf-8", errors="replace")
        self.assertIn("% custom contest style", updated_html)
        self.assertIn("logos/logo.png", updated_html)
        file_resp = contest_statement_source_file(
            contest=contest_slug,
            user="alice",
            language="english",
            path="logos/logo.png",
        )
        self.assertEqual(file_resp.status_code, 200)

    def test_contest_packages_queues_selected_problem_language(self) -> None:
        problem_slug = f"alice/contest-language-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        workspace = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        ensure_statement_language_sources(workspace, "chinese")
        git_service.commit(workspace, "add chinese statement", "alice", "alice@polygonlike.local")
        git_service.push(workspace, "main")

        contest_slug = f"contest-language-{uuid.uuid4().hex[:8]}"
        self._create_contest(contest_slug, "Language Contest")
        contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[problem_slug],
            q="",
        )

        page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('<option value="chinese"', html)

        with patch(
            "app.impl.contest.package._queue_contest_job",
            return_value=("pdf-language-job", True, "queued"),
        ) as queue_job:
            response = contest_packages_preview_start(
                contest=contest_slug,
                user="alice",
                language="chinese",
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(queue_job.call_args.kwargs["language"], "chinese")
        redirect_query = parse_qs(urlparse(str(response.headers["location"])).query)
        self.assertEqual(redirect_query["language"], ["chinese"])
        self.assertEqual(redirect_query["job_id"], ["pdf-language-job"])

    def test_contest_pdf_and_package_jobs_create_artifacts(self) -> None:
        problem_slug = f"alice/ui-contest-pack-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        ensure_statement_language_sources(ws, "english")
        (ws / "README.problem.md").write_text("contest package test\n", encoding="utf-8")
        (ws / "statement" / "olymp.sty").write_text(
            "% problem style\n\\definecolor{gapfill}{RGB}{255,225,225}\n\\colorlet{gapline}{red!60!black}\n",
            encoding="utf-8",
        )
        (ws / "statement-sections" / "english" / "legend.tex").write_text("Problem legend\n", encoding="utf-8")
        (ws / "statement-sections" / "english" / "notes.tex").write_text(
            "\\usetikzlibrary{arrows.meta,calc}\n\\begin{tikzpicture}\\end{tikzpicture}\n",
            encoding="utf-8",
        )
        (ws / "statement-assets").mkdir(parents=True, exist_ok=True)
        (ws / "statement-assets" / "example.mp").write_text("verbatimtex\netex\nbeginfig(1);endfig;end.\n", encoding="utf-8")
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("1 2 3\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps({"version": 2, "tests": [{"id": "001", "kind": "manual", "sample": True}]}, indent=2) + "\n",
            encoding="utf-8",
        )
        commit_id = git_service.commit(ws, "seed commit", "alice", "alice@polygonlike.local")
        git_service.push(ws, "main")
        self.assertRegex(str(commit_id), r"^[0-9a-f]{40}$")

        contest_slug = f"ui-contest-pkg-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Contest Package Build")
        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[problem_slug],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        problem_id = int(problem_row["id"])
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        config.contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_user_id,
            files=[
                {
                    "key": "statements/english/statements.tex",
                    "language": "english",
                    "package_bytes": b"\\\\documentclass{article}\n\\\\usepackage{olymp}\n\\\\begin{document}\n\\\\import{../../problems/src-problem/statements/}{./problem.tex}\n\\\\end{document}\n",
                },
                {
                    "key": "statements/english/olymp.sty",
                    "language": "english",
                    "package_bytes": b"% contest style\n",
                },
                {
                    "key": "statements/english/banner.tex",
                    "language": "english",
                    "package_bytes": b"% contest banner\n",
                },
                {
                    "key": "statements/english/banner.png",
                    "language": "english",
                    "package_bytes": b"\x89PNG\r\n\x1a\nmock",
                },
            ],
        )
        config.contest_service.set_statement_default_language(contest_id, actor_user_id, "english")
        config.contest_service.set_statement_problem_source_folders(contest_id, actor_user_id, {problem_id: "src-problem"})

        tex_commands: list[tuple[list[str], str, tuple[str, ...], dict[str, str] | None]] = []

        def _fake_sandbox_run(spec):
            command = [str(token) for token in spec.command]
            cwd = Path(spec.cwd)
            tex_commands.append(
                (
                    command,
                    str(cwd),
                    tuple(str(Path(path)) for path in spec.extra_mounts),
                    None if spec.env is None else dict(spec.env),
                )
            )
            if command[0] == "extractbb":
                source = cwd / command[1]
                (source.with_suffix(source.suffix + ".xbb")).write_text("%%BoundingBox: 0 0 10 10\n", encoding="utf-8")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            if command[0] == "mpost":
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            if command[0] == "xelatex":
                (cwd / "statements.pdf").write_bytes(b"%PDF-1.4\n%mock contest pdf\n")
                (cwd / "statements.log").write_text("xelatex ok\n", encoding="utf-8")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            return ExecResult(backend="test", status="error", returncode=1, elapsed_ms=1, stdout="", stderr="unexpected command")

        def _fake_run_build(problem: str, username: str, *, commit: str = "", ref: str = "", force_recompile: bool = False) -> str:
            _ = bool(force_recompile)
            problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem])
            self.assertIsNotNone(problem_row)
            ws_ctx = workspace_service.workspace_context(problem, username, include_recent=False)
            workspace_id = int(ws_ctx["workspace"]["id"])
            workspace_path = Path(str(ws_ctx["workspace"]["path"] or "")).resolve()
            verification_id = f"ver-{uuid.uuid4().hex[:12]}"
            artifact_root = config.fs_manager.prepare_verification_layout(verification_id).root
            artifact_root.mkdir(parents=True, exist_ok=True)
            db_execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    int(problem_row["id"]),
                    workspace_id,
                    verification_signature(workspace_path),
                    "all",
                    "ok",
                    "2026-02-28T00:00:00+00:00",
                    "2026-02-28T00:00:00+00:00",
                ],
            )
            return verification_id

        def _fake_create_export(problem: str, verification_id: str, export_type: str):
            self.assertEqual(str(export_type), "icpc")
            export_dir = Path(config.settings.artifacts_root) / problem / verification_id / "export"
            export_dir.mkdir(parents=True, exist_ok=True)
            out = export_dir / f"{problem.replace('/', '-')}-v1.zip"
            out.write_bytes(b"PK\x03\x04mock export")
            return out

        sample_sync_calls: list[tuple[str, str, str]] = []

        def _fake_sync_sample_payloads(problem: str, username: str, snapshot: Path) -> dict[str, object]:
            sample_sync_calls.append((problem, username, str(snapshot)))
            tests = load_tests_spec(snapshot / "tests" / "spec.json")
            tests[0]["sample_output"] = "6\n"
            (snapshot / "tests" / "spec.json").write_text(dumps_tests_spec(tests), encoding="utf-8")
            return {"sample_count": 1, "copied": 1, "verification_id": "ver-sample-sync"}

        with (
            patch.object(config.tex_compile_service.sandbox, "run", side_effect=_fake_sandbox_run),
            patch.object(config.preview_service, "sync_sample_payloads_for_snapshot", side_effect=_fake_sync_sample_payloads),
            patch.object(config.verification_service, "run_verification", side_effect=_fake_run_build),
            patch.object(config.export_service, "create_export", side_effect=_fake_create_export),
        ):
            preview_start = contest_packages_preview_start(
                contest=contest_slug,
                user="alice",
                language="english",
            )
            self.assertEqual(preview_start.status_code, 303)
            preview_q = parse_qs(urlparse(str(preview_start.headers.get("location", ""))).query)
            preview_job_id = str((preview_q.get("job_id") or [""])[0])
            self.assertTrue(preview_job_id)
            preview_done = _wait_for_row(
                "SELECT id,status FROM contest_jobs WHERE id=? AND contest_id=? AND finished_at IS NOT NULL",
                [preview_job_id, contest_id],
            )
            self.assertIsNotNone(preview_done)
            self.assertEqual(str(preview_done["status"]), "ok")

            package_start = contest_packages_build_start(contest=contest_slug, user="alice")
            self.assertEqual(package_start.status_code, 303)
            package_q = parse_qs(urlparse(str(package_start.headers.get("location", ""))).query)
            package_job_id = str((package_q.get("job_id") or [""])[0])
            self.assertTrue(package_job_id)
            package_done = _wait_for_row(
                "SELECT id,status FROM contest_jobs WHERE id=? AND contest_id=? AND finished_at IS NOT NULL",
                [package_job_id, contest_id],
            )
            self.assertIsNotNone(package_done)
            self.assertEqual(str(package_done["status"]), "ok")

        status_resp = contest_packages_job_status(contest=contest_slug, user="alice", job_id=package_job_id)
        self.assertEqual(status_resp.status_code, 200)
        status_payload = json.loads(status_resp.body.decode("utf-8"))
        self.assertEqual(str(status_payload.get("status") or ""), "ok")
        self.assertFalse(bool(status_payload.get("running")))

        preview_artifact = db_fetch_one(
            "SELECT id FROM contest_artifacts WHERE contest_id=? AND job_id=? AND artifact_type='contest-pdf' ORDER BY created_at DESC LIMIT 1",
            [contest_id, preview_job_id],
        )
        self.assertIsNotNone(preview_artifact)
        package_artifact = db_fetch_one(
            "SELECT id FROM contest_artifacts WHERE contest_id=? AND job_id=? AND artifact_type='package-bundle' ORDER BY created_at DESC LIMIT 1",
            [contest_id, package_job_id],
        )
        self.assertIsNotNone(package_artifact)

        download_resp = contest_packages_artifact_download(
            contest=contest_slug,
            user="alice",
            artifact_id=str(package_artifact["id"]),
        )
        self.assertEqual(download_resp.status_code, 200)
        disposition = str(download_resp.headers.get("content-disposition") or "").lower()
        self.assertIn("attachment", disposition)
        preview_job = db_fetch_one(
            "SELECT id FROM contest_jobs WHERE id=? AND contest_id=?",
            [preview_job_id, contest_id],
        )
        self.assertIsNotNone(preview_job)
        preview_summary = read_contest_job_summary(contest_id, preview_job_id)
        self.assertEqual(str(preview_summary.get("job_type") or ""), "pdf")
        self.assertEqual(str(preview_summary.get("language") or ""), "english")
        self.assertTrue(str(preview_summary.get("pdf_file") or "").endswith("statements.pdf"))
        contest_job_root = config.contest_service.job_root(contest_slug, preview_job_id)
        compile_root = contest_job_root / "contest-pdf-src"
        contest_statements_text = (compile_root / "statements" / "english" / "statements.tex").read_text(encoding="utf-8")
        self.assertIn("\\usepackage{xeCJK}", contest_statements_text)
        self.assertIn("\\setCJKmainfont{Noto Serif CJK SC}", contest_statements_text)
        self.assertIn("\\definecolor{gapfill}{RGB}{255,225,225}", contest_statements_text)
        self.assertIn("\\colorlet{gapline}{red!60!black}", contest_statements_text)
        self.assertIn("\\usetikzlibrary{arrows.meta,calc}", contest_statements_text)
        self.assertTrue((compile_root / "statements" / "english" / "olymp.sty").is_file())
        self.assertEqual((compile_root / "statements" / "english" / "olymp.sty").read_text(encoding="utf-8"), "% contest style\n")
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "problem.tex").is_file())
        rendered_problem_tex = (compile_root / "problems" / "src-problem" / "statements" / "english" / "problem.tex").read_text(encoding="utf-8")
        self.assertIn("\\Example", rendered_problem_tex)
        self.assertIn("sample.001.in", rendered_problem_tex)
        self.assertIn("sample.001.ans", rendered_problem_tex)
        self.assertNotIn("\\usetikzlibrary", rendered_problem_tex)
        self.assertIn("\\begin{tikzpicture}", rendered_problem_tex)
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "sample.001.in").is_file())
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "sample.001.ans").is_file())
        self.assertEqual(len(sample_sync_calls), 1)
        self.assertEqual(sample_sync_calls[0][0], problem_slug)
        self.assertEqual(sample_sync_calls[0][1], "alice")
        command_names = [command[0] for command, _cwd, _mounts, _env in tex_commands]
        self.assertIn("extractbb", command_names)
        self.assertIn("mpost", command_names)
        self.assertIn("xelatex", command_names)
        self.assertNotIn("latex", command_names)
        self.assertNotIn("dvips", command_names)
        self.assertNotIn("dvipdfmx", command_names)
        self.assertNotIn("pdflatex", command_names)
        self.assertFalse((compile_root / "statements" / "english" / "tutorials.pdf").exists())
        for _command, _cwd, mounts, env in tex_commands:
            self.assertIn(str(compile_root), mounts)
            self.assertIsNotNone(env)
            assert env is not None
            self.assertEqual(env.get("HOME"), str(compile_root))
            self.assertEqual(env.get("TEXMFVAR"), str(compile_root / ".texmf-var"))
            self.assertEqual(env.get("TEXMFCACHE"), str(compile_root / ".texmf-cache"))
            self.assertEqual(env.get("TEXMFCONFIG"), str(compile_root / ".texmf-config"))
            self.assertEqual(env.get("VARTEXFONTS"), str(compile_root / ".texfonts"))
            self.assertEqual(env.get("TEXMFOUTPUT"), str(compile_root / ".texmf-output"))
        xelatex_commands = [command for command, _cwd, _mounts, _env in tex_commands if command[0] == "xelatex"]
        self.assertEqual(len(xelatex_commands), 2)
        for command in xelatex_commands:
            self.assertIn("-interaction=nonstopmode", command)
            self.assertIn("-halt-on-error", command)
            self.assertIn("-jobname=statements", command)
            self.assertEqual(command[-1], "__contest_wrapper__.tex")
        wrapper_path = compile_root / "statements" / "english" / "__contest_wrapper__.tex"
        self.assertTrue(wrapper_path.is_file())
        wrapper_text = wrapper_path.read_text(encoding="utf-8")
        self.assertIn("\\AtBeginDocument", wrapper_text)
        self.assertIn("\\providecommand{\\url}[1]", wrapper_text)
        self.assertIn("\\providecommand{\\href}[2]", wrapper_text)
        self.assertIn("\\intentionallyblankpagestrue", wrapper_text)

    def test_contest_packages_surfaces_top_level_job_error(self) -> None:
        contest_slug = f"ui-contest-job-error-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Contest Job Error")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        job_id = f"cj-{uuid.uuid4().hex[:12]}"
        db_execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [
                job_id,
                contest_id,
                int(alice_row["id"]),
                "pdf",
                "failed",
                "2026-03-01T10:00:00+00:00",
                "2026-03-01T10:00:02+00:00",
            ],
        )
        write_contest_job_summary(
            contest_id,
            job_id,
            {"job_type": "pdf", "error": "contest statement default language is missing"},
        )

        status_resp = contest_packages_job_status(contest=contest_slug, user="alice", job_id=job_id)
        self.assertEqual(status_resp.status_code, 200)
        status_payload = json.loads(status_resp.body.decode("utf-8"))
        self.assertEqual(status_payload.get("error"), "contest statement default language is missing")
        self.assertEqual(status_payload.get("summary", {}).get("error"), "contest statement default language is missing")

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages?job_id={job_id}"),
            contest_slug,
            "alice",
            job_id=job_id,
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("Selected Job Report", packages_html)
        self.assertIn("contest statement default language is missing", packages_html)
        self.assertIn("No per-problem report was produced", packages_html)

    def test_contest_pdf_job_uses_fallback_statement_template(self) -> None:
        problem_slug = f"alice/ui-contest-fallback-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        ensure_statement_language_sources(ws, "english")
        (ws / "statement-sections" / "english" / "legend.tex").write_text("Fallback statement body\n", encoding="utf-8")
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps({"version": 2, "tests": [{"id": "001", "kind": "manual", "sample": False}]}, indent=2) + "\n",
            encoding="utf-8",
        )
        commit_id = git_service.commit(ws, "seed fallback statement", "alice", "alice@polygonlike.local")
        git_service.push(ws, "main")
        self.assertRegex(str(commit_id), r"^[0-9a-f]{40}$")

        contest_slug = f"ui-contest-fallback-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Fallback Contest")
        add_resp = contest_problems_add(contest=contest_slug, user="alice", problem_slugs=[problem_slug], q="")
        self.assertEqual(add_resp.status_code, 303)
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        config.contest_service.write_statement_source_file(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=int(alice_row["id"]),
            key="statements/english/logo.png",
            package_bytes=b"PNG",
        )

        def _fake_sandbox_run(spec):
            command = [str(token) for token in spec.command]
            cwd = Path(spec.cwd)
            if command[0] == "xelatex":
                (cwd / "statements.pdf").write_bytes(b"%PDF-1.4\n%mock contest pdf\n")
                (cwd / "statements.log").write_text("xelatex ok\n", encoding="utf-8")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")

        with (
            patch.object(config.tex_compile_service.sandbox, "run", side_effect=_fake_sandbox_run),
            patch.object(config.preview_service, "sync_sample_payloads_for_snapshot", side_effect=RuntimeError("judgehost is offline")),
        ):
            preview_start = contest_packages_preview_start(contest=contest_slug, user="alice")
            self.assertEqual(preview_start.status_code, 303)
            preview_q = parse_qs(urlparse(str(preview_start.headers.get("location", ""))).query)
            preview_job_id = str((preview_q.get("job_id") or [""])[0])
            self.assertTrue(preview_job_id)
            preview_done = _wait_for_row(
                "SELECT id,status FROM contest_jobs WHERE id=? AND contest_id=? AND finished_at IS NOT NULL",
                [preview_job_id, contest_id],
            )
            self.assertIsNotNone(preview_done)
            self.assertEqual(str(preview_done["status"]), "ok")

        summary = read_contest_job_summary(contest_id, preview_job_id)
        self.assertEqual(str(summary.get("language") or ""), "english")
        self.assertEqual(summary.get("totals", {}).get("success"), 1)
        self.assertIn("sample sync skipped", str(summary.get("results", [{}])[0].get("warning", "")))
        contest_job_root = config.contest_service.job_root(contest_slug, preview_job_id)
        statements_tex = contest_job_root / "contest-pdf-src" / "statements" / "english" / "statements.tex"
        self.assertTrue(statements_tex.is_file())
        statements_text = statements_tex.read_text(encoding="utf-8")
        self.assertIn("\\usepackage{olymp}", statements_text)
        self.assertIn("\\usepackage{xeCJK}", statements_text)
        self.assertIn("\\setCJKmainfont{Noto Serif CJK SC}", statements_text)
        self.assertIn("\\usepackage{tikz}", statements_text)
        self.assertIn("\\usepackage{pgfplots}", statements_text)
        self.assertIn("\\usepackage{algorithm}", statements_text)
        self.assertIn("\\usepackage{algpseudocode}", statements_text)
        self.assertIn("\\intentionallyblankpagestrue", statements_text)
        self.assertIn("/statements/english/", statements_text)
        self.assertTrue((statements_tex.parent / "olymp.sty").is_file())
        self.assertEqual((statements_tex.parent / "logo.png").read_bytes(), b"PNG")

    def test_contest_statement_sources_normalize_text_newlines(self) -> None:
        contest_slug = f"ui-contest-src-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Contest Source Normalize")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        config.contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_user_id,
            files=[
                {
                    "key": "statements/english/statements.tex",
                    "language": "english",
                    "package_bytes": b"\\documentclass{article}\r\n\\begin{document}\r\ncontest\r\n\\end{document}\r\n",
                },
                {
                    "key": "statements/english/banner.png",
                    "language": "english",
                    "package_bytes": b"\x89PNG\r\n\x1a\nmock",
                },
            ],
        )
        text_path = config.contest_service.statement_file_path(contest_slug, "statements/english/statements.tex")
        self.assertEqual(text_path.read_text(encoding="utf-8"), "\\documentclass{article}\n\\begin{document}\ncontest\n\\end{document}\n")
        image_path = config.contest_service.statement_file_path(contest_slug, "statements/english/banner.png")
        self.assertEqual(image_path.read_bytes(), b"\x89PNG\r\n\x1a\nmock")

    def test_contest_status_labels_render_consistently_in_ui(self) -> None:
        contest_slug = f"ui-contest-status-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Status Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])

        running_job_id = f"cj-{uuid.uuid4().hex[:10]}"
        db_execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [
                running_job_id,
                contest_id,
                actor_user_id,
                "pdf",
                "running",
                "2026-03-01T10:00:00+00:00",
                "2026-03-01T10:00:10+00:00",
            ],
        )
        write_contest_job_summary(contest_id, running_job_id, {"job_type": "pdf", "results": [], "language": "english"})

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn(running_job_id, overview_html)
        self.assertIn("pdf", overview_html)
        self.assertIn("RUNNING", overview_html)

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("<td>RUNNING</td>", packages_html)

        change_job_id = f"cj-{uuid.uuid4().hex[:10]}"
        db_execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [
                change_job_id,
                contest_id,
                actor_user_id,
                "change-general",
                "success",
                "2026-03-01T10:01:00+00:00",
                "2026-03-01T10:01:05+00:00",
            ],
        )
        write_contest_job_summary(
            contest_id,
            change_job_id,
            {
                "job_type": "change-general",
                "results": [
                    {
                        "problem_slug": "alice/sample",
                        "status": "failed",
                        "commit_id": "",
                        "error": "mock error",
                    }
                ],
            },
        )

        problems_page = contest_problems_page(
            _request(f"/contests/{contest_slug}/problems?job_id={change_job_id}"),
            contest_slug,
            "alice",
            job_id=change_job_id,
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn(f"<code>{change_job_id}</code> / SUCCESS", problems_html)
        self.assertIn("<td>FAILED</td>", problems_html)
