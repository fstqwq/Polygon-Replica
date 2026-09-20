from pathlib import Path

from fastapi import HTTPException

from app.service.problem.build_config import dumps_build_config, load_build_config

from tests.contest_support import ContestActionBase
from tests.db_helpers import db_fetch_all
from tests.package_support import blocked_export_queue
from tests.ui_support import (
    contest_build_all_packages,
    contest_problems_add,
    contest_problems_remove_selected,
    contest_problems_save,
    runtime,
    uuid,
    workspace_service,
)


class TestContestProblemActions(ContestActionBase):
    def _create_problem(self, suffix: str) -> tuple[int, str]:
        problem_slug = f"alice/{suffix}-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        problem_id = workspace_service.known_problem_id(problem_slug)
        self.assertIsNotNone(problem_id)
        return int(problem_id), problem_slug

    def test_contest_review_reports_deleted_main_solution_without_changing_source(self) -> None:
        _contest_slug, contest_id, actor_user_id = self.create_contest("missing-main")
        _row_id, _problem_id, slug = self.add_owned_problem(
            contest_id, actor_user_id, "A", "missing-main",
        )
        workspace = Path(workspace_service.ensure_workspace(slug, "alice"))
        main_source = "solutions/review-main.cpp"
        main_path = workspace / main_source
        main_path.parent.mkdir(parents=True, exist_ok=True)
        main_path.write_text("int main() {}\n", encoding="utf-8")
        (workspace / "solutions/other.cpp").write_text("int main() {}\n", encoding="utf-8")
        build = load_build_config(workspace)
        build["accepted_solution_source"] = main_source
        (workspace / "config/build.json").write_text(
            dumps_build_config(build), encoding="utf-8",
        )
        main_path.unlink()
        source_before = {
            path.relative_to(workspace): path.read_bytes()
            for path in workspace.rglob("*")
            if ".git" not in path.relative_to(workspace).parts and path.is_file()
        }

        rows = runtime.contest_problem_query_service.problem_rows(
            contest_id, "alice", actor_user_id, include_review=True,
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["details_available"])
        review = rows[0]["content_review"]
        self.assertIsNotNone(review)
        assert review is not None
        self.assertEqual(review["solutions"]["tone"], "danger")
        self.assertIn(review["solutions"], review["warnings"])
        self.assertEqual(
            {
                path.relative_to(workspace): path.read_bytes()
                for path in workspace.rglob("*")
                if ".git" not in path.relative_to(workspace).parts and path.is_file()
            },
            source_before,
        )

    def test_build_all_packages_persists_jobs_for_each_published_problem(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("build-all")
        heads: dict[int, str] = {}
        for index, label in enumerate(("A", "B")):
            _row_id, problem_id, slug = self.add_owned_problem(
                contest_id, actor_user_id, label, f"build-all-{index}",
            )
            workspace = workspace_service.ensure_workspace(slug, "alice")
            heads[problem_id] = runtime.git_service.commit(
                workspace, "Published source", "alice", "alice@example.com",
            )
            runtime.git_service.push(workspace, "main")

        with blocked_export_queue():
            response = contest_build_all_packages(contest=contest_slug, user="alice")
            self.assertEqual(response.status_code, 303)
            jobs = db_fetch_all("SELECT problem_id,source_commit,status FROM export_jobs")
            self.assertEqual(
                {(row["problem_id"], row["source_commit"], row["status"]) for row in jobs},
                {(problem_id, head, "queued") for problem_id, head in heads.items()},
            )

    def test_contest_writer_adds_only_directly_writable_problems(self) -> None:
        target_slug, target_id, _target_actor_user_id = self.create_contest(
            "writer-add"
        )
        _source_slug, source_id, source_actor_id = self.create_contest(
            "writer-add-source"
        )
        writer = f"writer-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(writer)
        writer_user_id = workspace_service.known_user_id(writer)
        self.assertIsNotNone(writer_user_id)
        runtime.contest_service.grant_member_role(target_id, writer, "write")
        runtime.contest_service.grant_member_role(source_id, writer, "write")

        direct_id, direct_slug = self._create_problem("writer-direct")
        inaccessible_id, inaccessible_slug = self._create_problem("writer-inaccessible")
        workspace_service.grant_repo_access(direct_slug, writer, "write")
        runtime.contest_service.add_problem(
            source_id,
            "A",
            inaccessible_id,
            source_actor_id,
        )
        self.assertFalse(
            runtime.access_query.problem_context(
                inaccessible_id,
                int(writer_user_id),
            )["can_write"]
        )
        self.assertFalse(
            runtime.access_query.direct_problem_context(
                inaccessible_id,
                int(writer_user_id),
            )["can_write"]
        )

        candidates = runtime.contest_service.available_problems(
            target_id,
            int(writer_user_id),
            limit=100,
            query="",
        )
        candidate_roles = {
            str(row["problem_slug"]): str(row["role"])
            for row in candidates
        }
        self.assertEqual(candidate_roles.get(direct_slug), "write")
        self.assertNotIn(inaccessible_slug, candidate_roles)

        response = contest_problems_add(
            contest=target_slug,
            user=writer,
            problem_slugs=[direct_slug, inaccessible_slug],
            q="",
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            str(response.headers["location"]).startswith(
                f"/contests/{target_slug}/access?focus_problem_id="
            )
        )
        self.assertTrue(
            str(response.headers["location"]).endswith(
                "#problem-access-matrix"
            )
        )
        self.assertTrue(
            runtime.contest_service.contest_has_problem(target_id, direct_id)
        )
        self.assertFalse(
            runtime.contest_service.contest_has_problem(target_id, inaccessible_id)
        )

        runtime.access_command.revoke_problem_access(
            actor_user_id=source_actor_id,
            problem_id=direct_id,
            target_username=writer,
        )
        self.assertTrue(
            runtime.contest_service.contest_has_problem(target_id, direct_id)
        )
        self.assertFalse(
            runtime.access_query.problem_context(
                direct_id,
                int(writer_user_id),
            )["can_write"]
        )
        self.assertFalse(
            runtime.access_query.direct_problem_context(
                direct_id,
                int(writer_user_id),
            )["can_write"]
        )

    def test_contest_writer_removes_any_problem(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("writer-remove")
        _locked_row_id, locked_problem_id, _locked_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "writer-remove-locked",
        )
        _direct_row_id, direct_problem_id, direct_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "B",
            "writer-remove-direct",
        )
        writer = f"writer-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(writer)
        workspace_service.grant_repo_access(direct_slug, writer, "write")
        runtime.contest_service.grant_member_role(contest_id, writer, "write")

        remove = contest_problems_remove_selected(
            contest=contest_slug,
            user=writer,
            selected_problem_ids=[str(locked_problem_id), str(direct_problem_id)],
        )

        self.assertEqual(remove.status_code, 303)
        remaining = {
            int(row["problem_id"])
            for row in db_fetch_all(
                "SELECT problem_id FROM contest_problems WHERE contest_id=?",
                [contest_id],
            )
        }
        self.assertEqual(remaining, set())

    def test_contest_read_cannot_mutate_roster_and_write_can_reorder(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "roster-boundary"
        )
        first_row_id, first_problem_id, first_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "roster-boundary-first",
        )
        second_row_id, second_problem_id, _second_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "B",
            "roster-boundary-second",
        )
        reader = f"reader-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(reader)
        workspace_service.grant_repo_access(first_slug, reader, "write")
        runtime.contest_service.grant_member_role(contest_id, reader, "read")

        with self.assertRaises(HTTPException) as add_error:
            contest_problems_add(
                contest=contest_slug,
                user=reader,
                problem_slugs=[first_slug],
                q="",
            )
        self.assertEqual(add_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as remove_error:
            contest_problems_remove_selected(
                contest=contest_slug,
                user=reader,
                selected_problem_ids=[str(first_problem_id)],
            )
        self.assertEqual(remove_error.exception.status_code, 403)

        runtime.contest_service.grant_member_role(contest_id, reader, "write")
        save = contest_problems_save(
            contest=contest_slug,
            user=reader,
            contest_problem_ids=[str(first_row_id), str(second_row_id)],
            contest_problem_indices=["B", "A"],
            problem_ids=[str(first_problem_id), str(second_problem_id)],
            time_limit_ms_values=["2000", "2000"],
            memory_limit_mb_values=["1024", "1024"],
            original_time_limit_ms_values=["2000", "2000"],
            original_memory_limit_mb_values=["1024", "1024"],
        )
        self.assertEqual(save.status_code, 303)
        rows = db_fetch_all(
            "SELECT id,idx FROM contest_problems WHERE contest_id=?",
            [contest_id],
        )
        self.assertEqual(
            {int(row["id"]): str(row["idx"]) for row in rows},
            {first_row_id: "B", second_row_id: "A"},
        )

    def test_contest_owner_adds_direct_write_but_not_direct_read_problem(self) -> None:
        contest_slug, contest_id, _actor_user_id = self.create_contest("owner-add")
        problem_owner = f"problem-owner-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(problem_owner)
        writable_slug = f"{problem_owner}/owner-add-write"
        readonly_slug = f"{problem_owner}/owner-add-read"
        workspace_service.ensure_problem(writable_slug)
        workspace_service.ensure_problem(readonly_slug)
        workspace_service.grant_repo_access(writable_slug, problem_owner, "owner")
        workspace_service.grant_repo_access(readonly_slug, problem_owner, "owner")
        workspace_service.grant_repo_access(writable_slug, "alice", "write")
        workspace_service.grant_repo_access(readonly_slug, "alice", "read")
        writable_id = workspace_service.known_problem_id(writable_slug)
        readonly_id = workspace_service.known_problem_id(readonly_slug)
        self.assertIsNotNone(writable_id)
        self.assertIsNotNone(readonly_id)

        response = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[writable_slug, readonly_slug],
            q="",
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            runtime.contest_service.contest_has_problem(contest_id, int(writable_id))
        )
        self.assertFalse(
            runtime.contest_service.contest_has_problem(contest_id, int(readonly_id))
        )
