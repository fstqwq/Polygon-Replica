from unittest.mock import patch

from tests.contest_support import ContestActionBase
from tests.db_helpers import db_fetch_all
from tests.ui_support import (
    contest_build_all_packages,
    runtime,
    contest_problems_change_general_retry,
    contest_problems_save,
)


class TestContestProblemActions(ContestActionBase):
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
                "verified_revision_number": None,
                "verified_revision_id": "",
                "status": "none",
                "missing_reason": "No verified revision",
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

    def test_retry_uses_only_failed_job_rows_and_original_limits(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("retry")
        _first_row_id, first_problem_id, first_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "retry-first",
        )
        _second_row_id, second_problem_id, second_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "B",
            "retry-second",
        )
        job_id = runtime.contest_service.create_job(
            contest_id,
            actor_user_id,
            "change-general",
            "failed",
            {
                "job_type": "change-general",
                "results": [
                    {
                        "problem_id": first_problem_id,
                        "problem_slug": first_slug,
                        "status": "failed",
                        "requested": {"time_limit_ms": "1234", "memory_limit_mb": "256"},
                    },
                    {
                        "problem_id": second_problem_id,
                        "problem_slug": second_slug,
                        "status": "success",
                        "requested": {"time_limit_ms": "9999", "memory_limit_mb": "999"},
                    },
                ],
                "totals": {"total": 2, "success": 1, "failed": 1, "skipped": 0},
            },
        )

        with patch(
            "app.impl.contest.problem._run_problem_general_update",
            return_value={"problem_id": first_problem_id, "status": "success"},
        ) as update:
            response = contest_problems_change_general_retry(
                contest=contest_slug,
                user="alice",
                retry_job_id=job_id,
            )

        self.assertEqual(response.status_code, 303)
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["problem_id"], first_problem_id)
        self.assertEqual(update.call_args.kwargs["requested_time_limit_ms"], "1234")
        self.assertEqual(update.call_args.kwargs["requested_memory_limit_mb"], "256")
