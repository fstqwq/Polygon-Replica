from unittest.mock import patch

from tests.contest_support import ContestActionBase
from tests.db_helpers import db_fetch_all
from tests.ui_support import (
    _flash_messages_from_response,
    runtime,
    contest_problems_change_general_retry,
    contest_problems_renumber,
)


class TestContestProblemActions(ContestActionBase):
    def test_renumber_uses_complete_submitted_order_atomically(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("renumber")
        first_id, _first_problem_id, _ = self.add_owned_problem(contest_id, actor_user_id, "A", "first")
        second_id, _second_problem_id, _ = self.add_owned_problem(contest_id, actor_user_id, "B", "second")

        response = contest_problems_renumber(
            contest=contest_slug,
            user="alice",
            contest_problem_ids=[str(first_id), str(second_id)],
            contest_problem_indices=["B", "A"],
        )
        self.assertEqual(response.status_code, 303)
        rows = db_fetch_all("SELECT id,label FROM contest_problems WHERE contest_id=?", [contest_id])
        self.assertEqual({int(row["id"]): str(row["label"]) for row in rows}, {first_id: "B", second_id: "A"})

        incomplete = contest_problems_renumber(
            contest=contest_slug,
            user="alice",
            contest_problem_ids=[str(first_id)],
            contest_problem_indices=["A"],
        )
        self.assertIn("every contest problem", _flash_messages_from_response(incomplete)[0])
        unchanged = db_fetch_all("SELECT id,label FROM contest_problems WHERE contest_id=?", [contest_id])
        self.assertEqual(
            {int(row["id"]): str(row["label"]) for row in unchanged},
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
