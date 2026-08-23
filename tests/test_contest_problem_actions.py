from unittest.mock import patch

from fastapi import HTTPException

from tests.contest_support import ContestActionBase
from tests.db_helpers import db_fetch_all
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

    def test_build_all_packages_queues_each_non_current_problem(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("build-all")
        _first_id, first_problem_id, _ = self.add_owned_problem(
            contest_id, actor_user_id, "A", "build-all-first"
        )
        _second_id, second_problem_id, _ = self.add_owned_problem(
            contest_id, actor_user_id, "B", "build-all-second"
        )
        readiness = {
            problem_id: {
                "problem_id": problem_id,
                "published_commit": "a" * 40,
                "published_revision_number": 1,
                "native_package_revision_number": None,
                "native_package_id": "",
                "status": "none",
                "verified": False,
                "missing_reason": "No Native Package",
            }
            for problem_id in (first_problem_id, second_problem_id)
        }

        with (
            patch.object(
                runtime.problem_package_service,
                "published_readiness_many",
                return_value=readiness,
            ),
            patch(
                "app.impl.contest.overview.start_export_job",
                return_value=True,
            ) as start,
        ):
            response = contest_build_all_packages(
                contest=contest_slug,
                user="alice",
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(
            {call.kwargs["problem_id"] for call in start.call_args_list},
            {first_problem_id, second_problem_id},
        )

    def test_save_updates_indices_and_only_changed_limits(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("save")
        first_id, first_problem_id, _ = self.add_owned_problem(
            contest_id, actor_user_id, "A", "save-first"
        )
        second_id, second_problem_id, _ = self.add_owned_problem(
            contest_id, actor_user_id, "B", "save-second"
        )

        with patch(
            "app.impl.contest.problem._run_problem_general_update",
            return_value={"problem_id": second_problem_id, "status": "success"},
        ) as update:
            response = contest_problems_save(
                contest=contest_slug,
                user="alice",
                contest_problem_ids=[str(first_id), str(second_id)],
                contest_problem_indices=["B", "A"],
                problem_ids=[str(first_problem_id), str(second_problem_id)],
                time_limit_ms_values=["1000", "3000"],
                memory_limit_mb_values=["256", "512"],
                original_time_limit_ms_values=["1000", "2000"],
                original_memory_limit_mb_values=["256", "512"],
            )

        self.assertEqual(response.status_code, 303)
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["problem_id"], second_problem_id)
        rows = db_fetch_all(
            "SELECT id,idx FROM contest_problems WHERE contest_id=?",
            [contest_id],
        )
        self.assertEqual(
            {int(row["id"]): str(row["idx"]) for row in rows},
            {first_id: "B", second_id: "A"},
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
        inherited_id, inherited_slug = self._create_problem("writer-inherited")
        workspace_service.grant_repo_access(direct_slug, writer, "write")
        runtime.contest_service.add_problem(
            source_id,
            "A",
            inherited_id,
            source_actor_id,
        )
        self.assertTrue(
            runtime.access_query.problem_context(
                inherited_id,
                int(writer_user_id),
            )["can_write"]
        )
        self.assertFalse(
            runtime.access_query.direct_problem_context(
                inherited_id,
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
        self.assertNotIn(inherited_slug, candidate_roles)

        response = contest_problems_add(
            contest=target_slug,
            user=writer,
            problem_slugs=[direct_slug, inherited_slug],
            q="",
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            runtime.contest_service.contest_has_problem(target_id, direct_id)
        )
        self.assertFalse(
            runtime.contest_service.contest_has_problem(target_id, inherited_id)
        )

        workspace_service.revoke_repo_access_for_problem_id(direct_id, writer)
        self.assertTrue(
            runtime.contest_service.contest_has_problem(target_id, direct_id)
        )
        self.assertTrue(
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
        remove = contest_problems_remove_selected(
            contest=target_slug,
            user=writer,
            selected_problem_ids=[str(direct_id)],
        )
        self.assertEqual(remove.status_code, 303)
        self.assertTrue(
            runtime.contest_service.contest_has_problem(target_id, direct_id)
        )

    def test_contest_writer_removes_direct_problem_and_owner_removes_any_problem(
        self,
    ) -> None:
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
        writer_user_id = workspace_service.known_user_id(writer)
        self.assertIsNotNone(writer_user_id)
        workspace_service.grant_repo_access(direct_slug, writer, "write")
        runtime.contest_service.grant_member_role(contest_id, writer, "write")

        display_rows = runtime.contest_problem_query_service.problem_rows(
            contest_id,
            writer,
            int(writer_user_id),
            include_review=False,
        )
        direct_by_problem = {
            int(row["problem_id"]): bool(row["can_direct_problem_write"])
            for row in display_rows
        }
        self.assertFalse(direct_by_problem[locked_problem_id])
        self.assertTrue(direct_by_problem[direct_problem_id])

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
        self.assertEqual(remaining, {locked_problem_id})

        owner_remove = contest_problems_remove_selected(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(locked_problem_id)],
        )
        self.assertEqual(owner_remove.status_code, 303)
        self.assertEqual(
            db_fetch_all(
                "SELECT problem_id FROM contest_problems WHERE contest_id=?",
                [contest_id],
            ),
            [],
        )

    def test_contest_read_cannot_mutate_roster_and_write_cannot_reorder(self) -> None:
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
            {first_row_id: "A", second_row_id: "B"},
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
