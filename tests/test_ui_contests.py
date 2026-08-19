import tempfile
import threading

from tests.db_helpers import (
    db_execute,
    db_fetch_all,
    db_fetch_one,
)

from app.impl.contest.statement_source import contest_statement_source_context
from app.service.platform.git_process import run_git
from starlette.requests import Request

from tests.common import E2ETestBase
from tests.ui_support import (
    Path,
    UIHelpersMixin,
    _register_with_password_envelope,
    _request,
    contest_access_grant,
    contest_access_revoke,
    contest_problems_add,
    contest_problems_remove_selected,
    contest_problems_save,
    contest_properties_save,
    contest_property_delete,
    contest_property_insert_preset,
    contests_root_create,
    json,
    uuid,
    runtime,
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
        previous = dict(runtime.config_values.snapshot())
        limited = dict(previous)
        limited["CONTEST_MAX_PROBLEMS"] = 1
        runtime.config_values.replace(limited)
        self.addCleanup(runtime.config_values.replace, previous)

        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        actor_id = int(actor["id"])
        contest_id = runtime.contest_service.create_contest_with_owner(
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
                runtime.contest_service.add_problem(
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

    def test_contest_problem_indices_are_the_only_roster_order(self) -> None:
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        actor_id = int(actor["id"])
        contest_id = runtime.contest_service.create_contest_with_owner(
            slug=f"idx-order-{uuid.uuid4().hex[:8]}",
            title="Index order",
            owner_user_id=actor_id,
        )
        expected_slugs: dict[str, str] = {}
        contest_problem_ids: dict[str, int] = {}
        for idx in ("B", "A", "C"):
            problem_id = self._insert_problem_row(
                f"idx-{idx.lower()}-{uuid.uuid4().hex[:8]}"
            )
            runtime.contest_service.add_problem(
                contest_id,
                idx,
                problem_id,
                actor_id,
            )
            row = db_fetch_one(
                "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
                [contest_id, problem_id],
            )
            self.assertIsNotNone(row)
            expected_slugs[idx] = str(
                db_fetch_one("SELECT slug FROM problems WHERE id=?", [problem_id])["slug"]
            )
            contest_problem_ids[idx] = int(row["id"])

        self.assertEqual(
            [item["idx"] for item in runtime.contest_service.contest_problems(contest_id)],
            ["A", "B", "C"],
        )
        roster = runtime.contest_service.agent_roster(
            str(
                db_fetch_one("SELECT slug FROM contests WHERE id=?", [contest_id])["slug"]
            )
        )
        self.assertIsNotNone(roster)
        self.assertEqual([item["idx"] for item in roster["problems"]], ["A", "B", "C"])
        self.assertEqual(
            [item["problem_slug"] for item in roster["problems"]],
            [expected_slugs["A"], expected_slugs["B"], expected_slugs["C"]],
        )

        generation = int(
            db_fetch_one("SELECT source_generation FROM contests WHERE id=?", [contest_id])[
                "source_generation"
            ]
        )
        unchanged = runtime.contest_service.set_problem_indices(
            contest_id,
            [
                (contest_problem_ids["B"], "B"),
                (contest_problem_ids["A"], "A"),
                (contest_problem_ids["C"], "C"),
            ],
        )
        self.assertFalse(unchanged)
        self.assertEqual(
            int(
                db_fetch_one(
                    "SELECT source_generation FROM contests WHERE id=?",
                    [contest_id],
                )["source_generation"]
            ),
            generation,
        )

        changed = runtime.contest_service.set_problem_indices(
            contest_id,
            [
                (contest_problem_ids["B"], "A"),
                (contest_problem_ids["A"], "B"),
                (contest_problem_ids["C"], "C"),
            ],
        )
        self.assertTrue(changed)
        self.assertEqual(
            int(
                db_fetch_one(
                    "SELECT source_generation FROM contests WHERE id=?",
                    [contest_id],
                )["source_generation"]
            ),
            generation + 1,
        )
        self.assertEqual(
            [item["idx"] for item in runtime.contest_service.contest_problems(contest_id)],
            ["A", "B", "C"],
        )

    def test_existing_over_limit_contest_remains_mutable_except_for_addition(self) -> None:
        previous = dict(runtime.config_values.snapshot())
        self.addCleanup(runtime.config_values.replace, previous)
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        actor_id = int(actor["id"])
        contest_slug = f"over-limit-{uuid.uuid4().hex[:8]}"
        contest_id = runtime.contest_service.create_contest_with_owner(
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
        runtime.config_values.replace(initial)
        runtime.contest_service.add_problem(contest_id, "A", problem_ids[0], actor_id)
        runtime.contest_service.add_problem(contest_id, "B", problem_ids[1], actor_id)

        limited = dict(previous)
        limited["CONTEST_MAX_PROBLEMS"] = 1
        runtime.config_values.replace(limited)
        runtime.contest_service.set_properties(
            contest_id,
            actor_id,
            {"location": "Still editable"},
        )
        self.assertEqual(len(runtime.contest_service.contest_problems(contest_id)), 2)
        self.assertEqual(
            runtime.contest_service.overview_properties_map(
                contest_id,
                contest_slug,
            )["location"],
            "Still editable",
        )
        with self.assertRaisesRegex(ValueError, "configured maximum"):
            runtime.contest_service.add_problem(
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

    def test_contest_statement_defaults_are_projected_as_files(self) -> None:
        contest_slug = f"ui-contest-sources-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Statement Sources")

        context = contest_statement_source_context(
            contest_id=contest_id,
            contest_slug=contest_slug,
            language="english",
            source_path="",
            additional_languages=("english",),
        )

        rows = {
            str(row["display_path"]): row
            for row in context["contest_statement_source_rows"]
        }
        self.assertEqual(set(rows), {"statements.ftl", "olymp.sty"})
        for row in rows.values():
            self.assertEqual(row["source_display"], "Default")
            self.assertFalse(row["stored"])
            self.assertGreater(int(row["size_bytes"]), 0)
        self.assertEqual(context["contest_statement_selected_path"], "")
        self.assertFalse(context["contest_statement_selected_is_text"])

        actor_id = workspace_service.known_user_id("alice")
        shared_key = runtime.contest_service.normalize_statement_source_key(
            language="_shared",
            path="figures/logo.svg",
        )
        self.assertEqual(shared_key, "statements/_shared/figures/logo.svg")
        runtime.contest_service.write_statement_source_file(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_id,
            key=shared_key,
            package_bytes=b"<svg></svg>\n",
        )
        shared_context = contest_statement_source_context(
            contest_id=contest_id,
            contest_slug=contest_slug,
            language="",
            source_path="",
            scope="all",
            additional_languages=("english",),
        )
        self.assertTrue(shared_context["contest_statement_source_is_shared"])
        self.assertEqual(shared_context["contest_statement_language"], "_shared")
        self.assertEqual(
            [
                str(row["display_path"])
                for row in shared_context["contest_statement_source_rows"]
            ],
            ["figures/logo.svg"],
        )
        english_key = runtime.contest_service.normalize_statement_source_key(
            language="english",
            path="figures/logo.svg",
        )
        runtime.contest_service.write_statement_source_file(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_id,
            key=english_key,
            package_bytes=b"language override\n",
        )
        generation_before_remove = int(
            db_fetch_one(
                "SELECT source_generation FROM contests WHERE id=?",
                [contest_id],
            )["source_generation"]
        )
        self.assertEqual(
            runtime.contest_service.delete_statement_language_sources(
                contest_id=contest_id,
                contest_slug=contest_slug,
                language="english",
            ),
            1,
        )
        self.assertEqual(
            int(
                db_fetch_one(
                    "SELECT source_generation FROM contests WHERE id=?",
                    [contest_id],
                )["source_generation"]
            ),
            generation_before_remove + 1,
        )
        remaining_keys = {
            str(row["rel_path"])
            for row in runtime.contest_service.statement_attachment_rows(contest_id)
        }
        self.assertEqual(remaining_keys, {shared_key})
        with self.assertRaisesRegex(ValueError, "cannot replace statements.ftl"):
            runtime.contest_service.normalize_statement_source_key(
                language="_shared",
                path="statements.ftl",
            )
        with self.assertRaisesRegex(ValueError, "rendered Contest statement file"):
            runtime.contest_service.normalize_statement_source_key(
                language="english",
                path="statements.tex",
            )

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

        admin_access = runtime.access_query.contest_context(contest_id, workspace_service.known_user_id("alice"))
        self.assertEqual(admin_access["role"], "admin")
        self.assertTrue(admin_access["can_manage"])
        overview_rows = runtime.contest_service.user_contests_overview(
            workspace_service.known_user_id("alice"),
            limit=20,
        )
        admin_row = next(row for row in overview_rows if row["slug"] == contest_slug)
        self.assertEqual(admin_row["role"], "admin")

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
        self.assertTrue(
            add_resp.headers["location"].startswith(
                f"/contests/{contest_slug}/overview"
            )
        )
        rows = db_fetch_all(
            "SELECT id,problem_id,idx FROM contest_problems WHERE contest_id=? ORDER BY idx ASC, id ASC",
            [contest_id],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0]["idx"]), "A")
        self.assertEqual(str(rows[1]["idx"]), "B")

        remove_resp = contest_problems_remove_selected(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(rows[1]["problem_id"])],
        )
        self.assertEqual(remove_resp.status_code, 303)
        self.assertTrue(
            remove_resp.headers["location"].startswith(
                f"/contests/{contest_slug}/overview"
            )
        )
        after_remove = db_fetch_all("SELECT problem_id FROM contest_problems WHERE contest_id=?", [contest_id])
        self.assertEqual(len(after_remove), 1)

        empty_add = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[],
            q="missing problem",
        )
        self.assertTrue(
            empty_add.headers["location"].startswith(
                f"/contests/{contest_slug}/problems?q=missing+problem"
            )
        )
        empty_remove = contest_problems_remove_selected(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[],
        )
        self.assertTrue(
            empty_remove.headers["location"].startswith(
                f"/contests/{contest_slug}/problems"
            )
        )

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
        contest_problem_row = db_fetch_one(
            "SELECT id,idx FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, pid],
        )
        self.assertIsNotNone(contest_problem_row)

        update_resp = contest_problems_save(
            contest=contest_slug,
            user="alice",
            contest_problem_ids=[str(contest_problem_row["id"])],
            contest_problem_indices=[str(contest_problem_row["idx"])],
            problem_ids=[str(pid)],
            time_limit_ms_values=["3500"],
            memory_limit_mb_values=["512"],
            original_time_limit_ms_values=["2000"],
            original_memory_limit_mb_values=["1024"],
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertTrue(
            update_resp.headers["location"].startswith(
                f"/contests/{contest_slug}/overview"
            )
        )

        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(int(cfg.get("time_limit_ms") or 0), 3500)
        self.assertEqual(int(cfg.get("memory_limit_mb") or 0), 512)

        last_subject = run_git(["git", "-C", str(ws), "log", "-1", "--pretty=%s"]).stdout.strip()
        self.assertEqual(last_subject, f"contest {contest_slug}: bulk update TL/ML")

    def test_system_admin_can_add_problem_without_explicit_repo_acl(self) -> None:
        contest_slug = f"admin-contest-problems-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug)
        foreign_problem = f"bob/admin-only-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user("bob")
        workspace_service.ensure_problem(foreign_problem)
        db_execute("UPDATE users SET is_system_admin=0")
        db_execute("UPDATE users SET is_system_admin=1 WHERE username=?", ["alice"])
        workspace_service.clear_identity_caches()

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
        runtime.contest_service.add_problem(contest_id, "A", problem_id, int(alice_row["id"]))
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
        bob = db_fetch_one("SELECT id FROM users WHERE username='bob'")
        self.assertIsNotNone(bob)
        self.assertTrue(runtime.access_query.problem_context(problem_id, int(bob["id"]))["can_write"])
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
        before = db_fetch_one(
            "SELECT source_generation FROM contests WHERE id=?",
            [contest_id],
        )
        self.assertIsNotNone(before)

        save_props = contest_properties_save(
            contest=contest_slug,
            user="alice",
            property_keys=[
                "title",
                "title.chinese",
                "location",
                "location.chinese",
                "date",
                "date.chinese",
                "insertBlankPage",
                "banner",
                "banner.chinese",
                "sponsor",
                "sponsor.chinese",
            ],
            property_values=[
                "Props Contest Updated",
                "\u5c5e\u6027\u6bd4\u8d5b",
                "San Francisco",
                "\u65e7\u91d1\u5c71",
                "2026-03-01",
                "2026 \u5e74 3 \u6708 1 \u65e5",
                "true",
                r"\textbf{Preview only}",
                "\\textbf{\u4ec5\u4f9b\u9884\u89c8}",
                "Example Foundation",
                "\u793a\u4f8b\u57fa\u91d1\u4f1a",
            ],
            existing_property_keys=["title"],
        )
        self.assertEqual(save_props.status_code, 303)
        after = db_fetch_one(
            "SELECT source_generation FROM contests WHERE id=?",
            [contest_id],
        )
        self.assertIsNotNone(after)
        self.assertEqual(
            int(after["source_generation"]),
            int(before["source_generation"]) + 1,
        )
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        contest_rows = db_fetch_all(
            "SELECT key,value FROM contest_properties WHERE contest_id=? ORDER BY key",
            [contest_id],
        )
        self.assertEqual(
            {str(row["key"]): str(row["value"]) for row in contest_rows},
            {
                "date": "2026-03-01",
                "date.chinese": "2026 \u5e74 3 \u6708 1 \u65e5",
                "location": "San Francisco",
                "location.chinese": "\u65e7\u91d1\u5c71",
                "sponsor": "Example Foundation",
                "sponsor.chinese": "\u793a\u4f8b\u57fa\u91d1\u4f1a",
                "banner": r"\textbf{Preview only}",
                "banner.chinese": "\\textbf{\u4ec5\u4f9b\u9884\u89c8}",
                "insertBlankPage": "true",
                "title": "Props Contest Updated",
                "title.chinese": "\u5c5e\u6027\u6bd4\u8d5b",
            },
        )
        localized = runtime.contest_service.localized_properties_map(
            contest_id,
            "chinese",
        )
        self.assertEqual(localized["title"], "\u5c5e\u6027\u6bd4\u8d5b")
        self.assertEqual(localized["location"], "\u65e7\u91d1\u5c71")

        unchanged = runtime.contest_service.set_properties(
            contest_id,
            int(alice_row["id"]),
            {
                "date": "2026-03-01",
                "date.chinese": "2026 \u5e74 3 \u6708 1 \u65e5",
                "location": "San Francisco",
                "location.chinese": "\u65e7\u91d1\u5c71",
                "sponsor": "Example Foundation",
                "sponsor.chinese": "\u793a\u4f8b\u57fa\u91d1\u4f1a",
                "banner": r"\textbf{Preview only}",
                "banner.chinese": "\\textbf{\u4ec5\u4f9b\u9884\u89c8}",
                "insertBlankPage": True,
                "title": "Props Contest Updated",
                "title.chinese": "\u5c5e\u6027\u6bd4\u8d5b",
            },
        )
        self.assertFalse(unchanged)
        current = db_fetch_one(
            "SELECT source_generation FROM contests WHERE id=?",
            [contest_id],
        )
        self.assertIsNotNone(current)
        self.assertEqual(
            int(current["source_generation"]),
            int(after["source_generation"]),
        )

        language_delete = contest_property_delete(
            contest=contest_slug,
            user="alice",
            property_key="location.chinese",
        )
        self.assertEqual(language_delete.status_code, 303)
        cleared = runtime.contest_service.set_properties(
            contest_id,
            int(alice_row["id"]),
            {"title.chinese": ""},
        )
        self.assertTrue(cleared)
        fallback = runtime.contest_service.localized_properties_map(
            contest_id,
            "chinese",
        )
        self.assertEqual(fallback["title"], "Props Contest Updated")
        self.assertEqual(fallback["location"], "San Francisco")
        self.assertEqual(fallback["sponsor"], "\u793a\u4f8b\u57fa\u91d1\u4f1a")
        cleared_rows = db_fetch_all(
            """
            SELECT key FROM contest_properties
            WHERE contest_id=? AND key IN ('title.chinese','location.chinese')
            """,
            [contest_id],
        )
        self.assertEqual(cleared_rows, [])

        removed = runtime.contest_service.set_properties(
            contest_id,
            int(alice_row["id"]),
            {"sponsor": None, "sponsor.chinese": None},
        )
        self.assertTrue(removed)
        self.assertEqual(
            db_fetch_all(
                "SELECT key FROM contest_properties WHERE contest_id=? AND key LIKE 'sponsor%'",
                [contest_id],
            ),
            [],
        )
        removed_blank_page = runtime.contest_service.set_properties(
            contest_id,
            int(alice_row["id"]),
            {"insertBlankPage": None},
        )
        self.assertTrue(removed_blank_page)

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="write")
        self.assertEqual(grant.status_code, 303)
        membership = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNotNone(membership)
        self.assertEqual(str(membership["role"]), "write")

    def test_contest_property_presets_create_template_values(self) -> None:
        contest_slug = f"ui-contest-property-presets-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Preset Contest")

        banner_response = contest_property_insert_preset(
            contest=contest_slug,
            user="alice",
            property_key="banner",
        )
        blank_page_response = contest_property_insert_preset(
            contest=contest_slug,
            user="alice",
            property_key="insertBlankPage",
        )

        self.assertEqual(banner_response.status_code, 303)
        self.assertEqual(blank_page_response.status_code, 303)
        properties = runtime.contest_service.properties_map(contest_id)
        self.assertIn(r"\ifdefined\thecontestname", properties["banner"])
        self.assertIn(r"\contestname", properties["banner"])
        self.assertEqual(properties["insertBlankPage"], "true")

    def test_contest_overview_properties_map_infers_location_and_date_from_statements(self) -> None:
        contest_slug = f"ui-contest-overview-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Overview Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        with tempfile.TemporaryDirectory(prefix="contest-statement-source-") as temp:
            source_path = Path(temp) / "statements.ftl"
            source_path.write_bytes(
                b"\\documentclass{article}\n"
                b"\\begin{document}\n"
                b"\\contest\n"
                b"{Overview Contest}%\n"
                b"{Hangzhou, China}%\n"
                b"{1 February, 2026}%\n"
                b"\\end{document}\n"
            )
            runtime.contest_service.replace_statement_sources(
                contest_id=contest_id,
                contest_slug=contest_slug,
                actor_user_id=actor_user_id,
                files=[
                    {
                        "key": "statements/english/statements.ftl",
                        "language": "english",
                        "source_path": source_path,
                    }
                ],
            )
        properties = runtime.contest_service.overview_properties_map(
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
        membership = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNone(membership)

        revoke = contest_access_revoke(contest=contest_slug, user="alice", target_user="alice")
        self.assertEqual(revoke.status_code, 303)
