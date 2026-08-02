from __future__ import annotations

from .common import E2ETestBase
from .db_helpers import db_fetch_one
from .ui_support import (
    UIHelpersMixin,
    _request,
    config,
    contests_root_create,
    uuid,
    workspace_service,
)


class ContestActionBase(UIHelpersMixin, E2ETestBase):
    seed_primary_workspace = False
    seed_default_workspace = True

    def _create_contest(self, slug: str, title: str = "Contest Actions") -> int:
        response = contests_root_create(
            _request("/contests/create"),
            user="alice",
            contest_slug=slug,
            contest_title=title,
        )
        self.assertEqual(response.status_code, 303)
        contest_row = db_fetch_one("SELECT id FROM contests WHERE slug=?", [slug])
        self.assertIsNotNone(contest_row)
        return int(contest_row["id"])

    def create_contest(self, prefix: str) -> tuple[str, int, int]:
        slug = f"{prefix}-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(slug)
        actor_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor_row)
        return slug, contest_id, int(actor_row["id"])

    def add_owned_problem(self, contest_id: int, actor_user_id: int, index: str, suffix: str) -> tuple[int, int, str]:
        problem_slug = f"alice/{suffix}-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        workspace_service.ensure_workspace(problem_slug, "alice")
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        problem_id = int(problem_row["id"])
        config.contest_service.add_problem(contest_id, index, problem_id, actor_user_id)
        contest_problem_row = db_fetch_one(
            "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, problem_id],
        )
        self.assertIsNotNone(contest_problem_row)
        return int(contest_problem_row["id"]), problem_id, problem_slug
