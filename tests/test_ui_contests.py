from __future__ import annotations

import tempfile
import threading

from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_execute,
    db_fetch_all,
    db_fetch_one,
    read_contest_job_summary,
    verification_programs_for_tasks,
)

from app.service.platform.git_process import run_git
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from starlette.requests import Request

from tests.common import E2ETestBase
from tests.identity_helpers import canonical_test_verification_id
from tests.ui_support import (
    Path,
    UIHelpersMixin,
    _flash_messages_from_response,
    _register_with_password_envelope,
    _request,
    contest_access_grant,
    contest_access_revoke,
    contest_overview_page,
    contest_problems_add,
    contest_problems_change_general,
    contest_problems_page,
    contest_problems_remove_selected,
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

    def _insert_problem_row(self, suffix: str) -> int:
        slug = f"alice/{suffix}"
        db_execute(
            "INSERT INTO problems(slug,repo_name,created_at) VALUES(?,?,?)",
            [slug, f"{slug}.git", "2026-08-11T00:00:00+00:00"],
        )
        row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [slug])
        self.assertIsNotNone(row)
        return int(row["id"])

    def test_contest_problem_limit_serializes_the_last_slot(self) -> None:
        previous = dict(config.config_values.snapshot())
        limited = dict(previous)
        limited["CONTEST_MAX_PROBLEMS"] = 1
        config.config_values.replace(limited)
        self.addCleanup(config.config_values.replace, previous)

        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        actor_id = int(actor["id"])
        contest_id = config.contest_service.create_contest_with_owner(
            slug=f"limit-{uuid.uuid4().hex[:8]}",
            title="Limit race",
            owner_user_id=actor_id,
        )
        problem_ids = [
            self._insert_problem_row(f"limit-{uuid.uuid4().hex[:8]}")
            for _ in range(2)
        ]
        barrier = threading.Barrier(3)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def add(label: str, problem_id: int) -> None:
            barrier.wait()
            try:
                config.contest_service.add_problem(
                    contest_id,
                    label,
                    problem_id,
                    actor_id,
                )
            except ValueError:
                outcome = "rejected"
            else:
                outcome = "added"
            with outcomes_lock:
                outcomes.append(outcome)

        workers = [
            threading.Thread(target=add, args=(label, problem_id))
            for label, problem_id in zip(("A", "B"), problem_ids, strict=True)
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(outcomes), ["added", "rejected"])
        self.assertEqual(
            int(
                db_fetch_one(
                    "SELECT COUNT(*) AS n FROM contest_problems WHERE contest_id=?",
                    [contest_id],
                )["n"]
            ),
            1,
        )

    def test_existing_over_limit_contest_remains_mutable_except_for_addition(self) -> None:
        previous = dict(config.config_values.snapshot())
        self.addCleanup(config.config_values.replace, previous)
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        actor_id = int(actor["id"])
        contest_slug = f"over-limit-{uuid.uuid4().hex[:8]}"
        contest_id = config.contest_service.create_contest_with_owner(
            slug=contest_slug,
            title="Existing over limit",
            owner_user_id=actor_id,
        )
        problem_ids = [
            self._insert_problem_row(f"over-limit-{uuid.uuid4().hex[:8]}")
            for _ in range(3)
        ]
        initial = dict(previous)
        initial["CONTEST_MAX_PROBLEMS"] = 2
        config.config_values.replace(initial)
        config.contest_service.add_problem(contest_id, "A", problem_ids[0], actor_id)
        config.contest_service.add_problem(contest_id, "B", problem_ids[1], actor_id)

        limited = dict(previous)
        limited["CONTEST_MAX_PROBLEMS"] = 1
        config.config_values.replace(limited)
        config.contest_service.upsert_property(
            contest_id,
            actor_id,
            "location",
            "Still editable",
        )
        self.assertEqual(len(config.contest_service.contest_problems(contest_id)), 2)
        self.assertEqual(
            config.contest_service.overview_properties_map(
                contest_id,
                contest_slug,
            )["location"],
            "Still editable",
        )
        with self.assertRaisesRegex(ValueError, "configured maximum"):
            config.contest_service.add_problem(
                contest_id,
                "C",
                problem_ids[2],
                actor_id,
            )

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
        admin_access = config.access_query.contest_context(contest_id, workspace_service.known_user_id("alice"))
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

    def test_contest_problem_add_and_remove_flow(self) -> None:
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

    def test_contest_review_hides_verification_failure_reason(self) -> None:
        contest_slug = f"review-reason-{uuid.uuid4().hex[:8]}"
        self._create_contest(contest_slug)
        workspace_service.grant_repo_access("alice/sample", "alice", "owner")
        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=["alice/sample"],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        ctx = workspace_service.workspace_context(
            "alice/sample",
            "alice",
            include_recent=False,
        )
        verification_id = canonical_test_verification_id(
            f"contest-review:{uuid.uuid4().hex}"
        )
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            source_commit=str(ctx["workspace"]["head_commit"] or ""),
            kind="all",
        )
        self.assertEqual(admission.outcome, "admitted")
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        tasks = [
            PlannedTask(
                task_id=task_id,
                predecessor_task_id=None,
                task_kind="main-correct",
                source_path="solutions/accepted.cpp",
                program_id="accepted",
                test_name="001.in",
                expected_behavior="accepted",
            )
        ]
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
        )
        self.assertEqual(activation.outcome, "activated")
        failure = config.verification_service.fail_verification(
            verification_id,
            reason="private checker detail",
        )
        self.assertEqual(failure.outcome, "transitioned")

        overview = contest_overview_page(
            _app_request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        html = overview.body.decode("utf-8", errors="replace")
        self.assertNotIn("private checker detail", html)

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
        self.assertTrue(config.access_query.problem_context(problem_id, int(bob["id"]))["can_write"])
        self.assertEqual(
            db_fetch_all(
                "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
                [problem_id, int(bob["id"])],
            ),
            [],
        )

    def test_contest_properties_and_access_flow(self) -> None:
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

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="write")
        self.assertEqual(grant.status_code, 303)
        membership = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNotNone(membership)
        self.assertEqual(str(membership["role"]), "write")

    def test_contest_overview_properties_map_infers_location_and_date_from_statements(self) -> None:
        contest_slug = f"ui-contest-overview-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Overview Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        with tempfile.TemporaryDirectory(prefix="contest-statement-source-") as temp:
            source_path = Path(temp) / "statements.tex"
            source_path.write_bytes(
                b"\\documentclass{article}\n"
                b"\\begin{document}\n"
                b"\\contest\n"
                b"{Overview Contest}%\n"
                b"{Hangzhou, China}%\n"
                b"{1 February, 2026}%\n"
                b"\\end{document}\n"
            )
            config.contest_service.replace_statement_sources(
                contest_id=contest_id,
                contest_slug=contest_slug,
                actor_user_id=actor_user_id,
                files=[
                    {
                        "key": "statements/english/statements.tex",
                        "language": "english",
                        "source_path": source_path,
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
