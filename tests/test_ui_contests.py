from __future__ import annotations

from unittest.mock import patch

from tests.db_helpers import (
    db_execute,
    db_fetch_all,
    db_fetch_one,
    read_contest_job_summary,
    write_contest_job_summary,
)

from app.service.platform.git_process import run_git
from app.impl.contest.problem_rows import (
    contest_problem_rows,
    package_revision_display,
)
from app.service.problem.resource_limits import resource_limit_display
from starlette.requests import Request

from tests.common import E2ETestBase
from tests.ui_support import (
    Path,
    UIHelpersMixin,
    _flash_messages_from_response,
    _register_with_password_envelope,
    _request,
    contest_access_grant,
    contest_access_page,
    contest_access_revoke,
    contest_overview_page,
    contest_packages_page,
    contest_problems_add,
    contest_problems_change_general,
    contest_problems_page,
    contest_problems_remove_selected,
    contest_properties_page,
    contest_properties_save,
    contests_root_create,
    contests_root_page,
    json,
    uuid,
    config,
    workspace_service,
)


def _app_request(path: str) -> Request:
    from app.main import app

    request = _request(path)
    request.scope["app"] = app
    return request


class TestUIContests(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = False
    seed_default_workspace = True

    def test_package_revision_display_separates_ready_buildable_and_blocked(self) -> None:
        base = {
            "problem_id": 7,
            "published_commit": "a" * 40,
            "published_revision_number": 3,
            "materialized_commit": "a" * 40,
            "materialized_revision_number": 3,
            "materialization_id": "mat-current",
            "archive_sha256": "b" * 64,
            "current_is_materialized": True,
            "status": "ready",
            "statement_languages": ["english"],
            "missing_reason": "",
        }
        self.assertEqual(
            package_revision_display(base),
            ("Native ready on v3", "ready"),
        )
        buildable = {
            **base,
            "materialized_commit": "",
            "materialized_revision_number": None,
            "materialization_id": "",
            "archive_sha256": "",
            "current_is_materialized": False,
            "status": "buildable",
        }
        self.assertEqual(
            package_revision_display(buildable),
            ("Native pending for v3", "buildable"),
        )
        blocked = {
            **base,
            "materialized_commit": "",
            "materialized_revision_number": None,
            "materialization_id": "",
            "archive_sha256": "",
            "current_is_materialized": False,
            "status": "blocked",
            "missing_reason": "no complete Native materialization",
        }
        self.assertEqual(
            package_revision_display(blocked),
            ("Native blocked on v3", "blocked"),
        )

    def test_contest_problem_rows_batch_acl_skips_inaccessible_workspaces(self) -> None:
        problem = {
            "contest_problem_id": 11,
            "position": 0,
            "idx": "A",
            "problem_id": 7,
            "statement_folder": "",
            "problem_slug": "alice/private",
            "slug_leaf": "private",
            "created_at": "2026-08-08T00:00:00+00:00",
        }
        access = {
            "role": "none",
            "can_read": False,
            "can_write": False,
            "can_manage": False,
            "read_block_reason": "problem access required",
            "write_block_reason": "problem write access required",
            "manage_block_reason": "problem manage access required",
        }
        readiness = {
            "problem_id": 7,
            "published_commit": "",
            "published_revision_number": None,
            "materialized_commit": "",
            "materialized_revision_number": None,
            "materialization_id": "",
            "archive_sha256": "",
            "current_is_materialized": False,
            "status": "blocked",
            "statement_languages": [],
            "missing_reason": "no published Git revision",
        }
        with (
            patch.object(
                config.contest_service,
                "contest_problems",
                return_value=[problem],
            ),
            patch.object(
                config.workspace_service,
                "access_contexts",
                return_value={7: access},
            ) as access_contexts,
            patch.object(config.workspace_service, "workspace_rows") as workspace_rows,
            patch.object(config.workspace_service, "ensure_workspace") as ensure_workspace,
            patch.object(
                config.problem_package_service,
                "readiness",
                return_value=readiness,
            ) as package_readiness,
        ):
            rows = contest_problem_rows(5, "alice", 3)

        access_contexts.assert_called_once_with([7], 3)
        workspace_rows.assert_not_called()
        ensure_workspace.assert_not_called()
        package_readiness.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workspace_revision_display"], "no problem access")
        self.assertEqual(rows[0]["package_revision_status"], "blocked")
        self.assertEqual(rows[0]["package_revision_display"], "Package unavailable")
        self.assertNotIn("revision_display", rows[0])

    def test_contest_problem_rows_reuses_stored_workspace_status(self) -> None:
        problem_id, user_id = workspace_service.page_identity(
            self.default_problem,
            self.default_user,
        )
        workspace_state = workspace_service.workspace_rows(
            [problem_id],
            user_id,
        )[problem_id]
        workspace_state = {
            **workspace_state,
            "revision_local": 2,
            "revision_upstream": 2,
            "revision_missing": 0,
            "revision_highlight": 0,
        }
        problem = {
            "contest_problem_id": 11,
            "position": 0,
            "idx": "A",
            "problem_id": problem_id,
            "statement_folder": "",
            "problem_slug": self.default_problem,
            "slug_leaf": "sample",
            "created_at": "2026-08-08T00:00:00+00:00",
        }
        access = {
            "role": "owner",
            "can_read": True,
            "can_write": True,
            "can_manage": True,
            "read_block_reason": "",
            "write_block_reason": "",
            "manage_block_reason": "",
        }
        readiness = {
            "problem_id": problem_id,
            "published_commit": "a" * 40,
            "published_revision_number": 3,
            "materialized_commit": "",
            "materialized_revision_number": None,
            "materialization_id": "",
            "archive_sha256": "",
            "current_is_materialized": False,
            "statement_languages": [],
            "missing_reason": "no complete Native materialization",
        }
        with (
            patch.object(
                config.contest_service,
                "contest_problems",
                return_value=[problem],
            ),
            patch.object(
                config.workspace_service,
                "access_contexts",
                return_value={problem_id: access},
            ),
            patch.object(
                config.workspace_service,
                "workspace_rows",
                return_value={problem_id: workspace_state},
            ) as workspace_rows,
            patch.object(config.workspace_service, "ensure_workspace") as ensure_workspace,
            patch.object(
                config.workspace_service,
                "refresh_workspace_status_with_ids",
            ) as refresh_workspace_status,
            patch.object(
                config.problem_package_service,
                "readiness",
                return_value=readiness,
            ),
        ):
            rows = contest_problem_rows(5, self.default_user, user_id)

        workspace_rows.assert_called_once_with([problem_id], user_id)
        ensure_workspace.assert_not_called()
        refresh_workspace_status.assert_not_called()
        self.assertEqual(
            rows[0]["workspace_revision_display"],
            "Workspace on v2 / Upstream v3",
        )
        self.assertTrue(rows[0]["workspace_revision_warn"])

    def test_resource_limit_display_uses_shared_units_and_warning_boundaries(self) -> None:
        self.assertEqual(
            resource_limit_display(2_000, 1_024),
            {
                "time_limit_display": "2s",
                "time_limit_warn": False,
                "memory_limit_display": "1G",
                "memory_limit_warn": False,
            },
        )
        self.assertTrue(resource_limit_display(499, 256)["time_limit_warn"])
        self.assertFalse(resource_limit_display(500, 256)["time_limit_warn"])
        self.assertFalse(resource_limit_display(10_000, 256)["time_limit_warn"])
        self.assertTrue(resource_limit_display(10_001, 256)["time_limit_warn"])
        self.assertTrue(resource_limit_display(2_000, 255)["memory_limit_warn"])
        self.assertFalse(resource_limit_display(2_000, 256)["memory_limit_warn"])

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
            _app_request(f"/contests/{contest_slug}/overview"),
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
            "SELECT id,problem_id,label FROM contest_problems WHERE contest_id=? ORDER BY position ASC, id ASC",
            [contest_id],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0]["label"]), "A")
        self.assertEqual(str(rows[1]["label"]), "B")

        remove_resp = contest_problems_remove_selected(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(rows[1]["problem_id"])],
        )
        self.assertEqual(remove_resp.status_code, 303)
        after_remove = db_fetch_all("SELECT problem_id FROM contest_problems WHERE contest_id=?", [contest_id])
        self.assertEqual(len(after_remove), 1)

        overview = contest_overview_page(
            _app_request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn('<h1 class="page-title">Problems</h1>', overview_html)
        self.assertNotIn("Contest Overview", overview_html)
        self.assertNotIn("Contest Problems", overview_html)
        self.assertNotIn("<h2>Summary</h2>", overview_html)
        self.assertNotIn("<h2>Properties</h2>", overview_html)
        self.assertIn('<h3>Contest</h3>', overview_html)
        self.assertIn("Manage problems", overview_html)
        self.assertIn('class="table-base problem-list-table contest-problem-list-table"', overview_html)
        self.assertIn('class="problem-slug-link problem-list-slug-link"', overview_html)
        self.assertIn('<span class="problem-list-slug-owner">alice/</span>', overview_html)
        self.assertIn('<span class="problem-list-slug-leaf">sample</span>', overview_html)
        self.assertNotIn("alice/sample</code> - sample", overview_html)
        self.assertIn("<th>Details</th>", overview_html)
        self.assertIn('<th class="problem-list-head">Title</th>', overview_html)
        self.assertNotIn("<th>TL/ML</th>", overview_html)
        self.assertNotIn("<th>Mode</th>", overview_html)
        self.assertIn("pass-fail", overview_html)
        self.assertIn(">2s</span>", overview_html)
        self.assertIn(">1G</span>", overview_html)
        self.assertNotIn("1024MB", overview_html)
        self.assertIn("solution", overview_html)
        self.assertIn("language", overview_html)
        self.assertIn("Checker:", overview_html)
        self.assertIn("Validator:", overview_html)
        self.assertIn('class="danger">0 tests</span>', overview_html)
        self.assertIn('class="danger">0 solutions</span>', overview_html)
        self.assertIn('class="danger">missing</span>', overview_html)
        self.assertIn("Packages:", overview_html)
        self.assertIn("0 ready", overview_html)
        self.assertIn("1 blocked", overview_html)
        self.assertNotIn(" buildable</span>", overview_html)
        self.assertNotIn(" available</span>", overview_html)
        self.assertIn("Native blocked", overview_html)
        self.assertIn("Workspace on v", overview_html)

        problems_page = contest_problems_page(
            _app_request(f"/contests/{contest_slug}/problems"),
            contest_slug,
            "alice",
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn('<h1 class="page-title">Manage Problems</h1>', problems_html)
        self.assertIn("contest-manage-link active", problems_html)
        self.assertIn("Update limits", problems_html)
        self.assertIn("Contest problems", problems_html)
        self.assertIn("Add problems", problems_html)
        self.assertIn("<th>#</th>", problems_html)
        self.assertEqual(problems_html.count('<th class="problem-list-head">Title</th>'), 2)
        self.assertNotIn("<th>Idx</th>", problems_html)
        self.assertNotIn("<th class=\"problem-list-head\">Problem ID</th>", problems_html)
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
            _app_request(f"/contests/{contest_slug}/problems"),
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

    def test_contest_membership_grants_dynamic_problem_access_without_sync(self) -> None:
        contest_slug = f"ui-contest-dynamic-access-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Dynamic Access Contest")
        problem_slug = f"alice/ui-dynamic-access-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(alice_row)
        self.assertIsNotNone(problem_row)
        problem_id = int(problem_row["id"])
        config.contest_service.add_problem(contest_id, "A", problem_id, int(alice_row["id"]))
        self.assertEqual(
            _register_with_password_envelope("bob", "StrongPass123", next_path="/").status_code,
            303,
        )
        db_execute("UPDATE users SET is_system_admin=0 WHERE username=?", ["bob"])
        workspace_service.clear_identity_caches()

        grant = contest_access_grant(
            contest=contest_slug,
            user="alice",
            target_user="bob",
            role="write",
        )
        self.assertEqual(grant.status_code, 303)
        self.assertIn("effective immediately", _flash_messages_from_response(grant)[0].lower())
        bob = db_fetch_one("SELECT id FROM users WHERE username='bob'")
        self.assertIsNotNone(bob)
        self.assertTrue(workspace_service.access_context(problem_id, int(bob["id"]))["can_write"])
        self.assertEqual(
            db_fetch_all(
                "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
                [problem_id, int(bob["id"])],
            ),
            [],
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
        self.assertIn('<h1 class="page-title">Properties</h1>', props_html)
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
        self.assertIn('<h1 class="page-title">Access</h1>', access_html)
        self.assertIn("bob", access_html)
        self.assertIn('option value="write"', access_html)
        self.assertIn('option value="read"', access_html)
        self.assertNotIn('option value="owner"', access_html)
        self.assertIn("fixed owner", access_html)

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("Statements &amp; Builds", packages_html)
        self.assertIn('class="section-tab active"', packages_html)
        self.assertIn('aria-current="page"', packages_html)
        self.assertNotIn('class="problem-submenu"', packages_html)

    def test_contest_overview_properties_map_infers_location_and_date_from_statements(self) -> None:
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

        properties = config.contest_service.overview_properties_map(
            contest_id,
            contest_slug,
        )
        self.assertEqual(properties["location"], "Hangzhou, China")
        self.assertEqual(properties["date"], "1 February, 2026")

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

    def test_contest_status_labels_render_consistently_in_ui(self) -> None:
        contest_slug = f"ui-contest-status-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Status Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])

        running_job_id = f"cj-{uuid.uuid4().hex[:10]}"
        db_execute(
            """
            INSERT INTO contest_jobs(
                id,contest_id,actor_user_id,job_type,status,source_generation,created_at,finished_at
            ) VALUES(?,?,?,?,?,1,?,?)
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
            _app_request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertNotIn(running_job_id, overview_html)

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
            INSERT INTO contest_jobs(
                id,contest_id,actor_user_id,job_type,status,source_generation,created_at,finished_at
            ) VALUES(?,?,?,?,?,1,?,?)
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
            _app_request(f"/contests/{contest_slug}/problems?job_id={change_job_id}"),
            contest_slug,
            "alice",
            job_id=change_job_id,
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn(f"<code>{change_job_id}</code> / SUCCESS", problems_html)
        self.assertIn("<td>FAILED</td>", problems_html)
