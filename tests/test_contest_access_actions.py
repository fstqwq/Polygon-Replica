from __future__ import annotations

from tests.contest_support import ContestActionBase
from tests.db_helpers import db_fetch_one
from tests.ui_support import (
    _flash_messages_from_response,
    config,
    contest_access_revoke,
    workspace_service,
)


class TestContestAccessActions(ContestActionBase):
    def test_membership_only_revoke_preserves_problem_acl(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("revoke-membership")
        _row_id, problem_id, problem_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "membership-problem",
        )
        workspace_service.ensure_user("carol")
        config.contest_service.grant_member_role(contest_id, "carol", "read")
        workspace_service.grant_repo_access(problem_slug, "carol", "read")

        response = contest_access_revoke(contest=contest_slug, user="alice", target_user="carol")

        self.assertEqual(response.status_code, 303)
        self.assertIn("derived problem access ended immediately", _flash_messages_from_response(response)[0])
        acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='carol')",
            [problem_id],
        )
        self.assertIsNotNone(acl)
        self.assertEqual(str(acl["role"]), "read")

    def test_membership_grants_and_revokes_problem_access_without_repo_acl_rows(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("dynamic-access")
        _row_a, problem_id, _problem_slug = self.add_owned_problem(
            contest_id, actor_user_id, "A", "dynamic-problem"
        )
        workspace_service.ensure_user("bob")
        config.contest_service.grant_member_role(contest_id, "bob", "write")
        bob = db_fetch_one("SELECT id FROM users WHERE username='bob'")
        self.assertIsNotNone(bob)
        self.assertTrue(
            config.workspace_service.access_context(problem_id, int(bob["id"]))["can_write"]
        )
        self.assertIn(
            _problem_slug,
            config.workspace_service.accessible_problem_slugs(int(bob["id"]), limit=20),
        )
        participating = config.workspace_service.participating_problem_rows(
            int(bob["id"]), limit=20
        )
        self.assertIn(_problem_slug, [str(row["slug"]) for row in participating])
        self.assertIsNone(
            db_fetch_one(
                "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
                [problem_id, int(bob["id"])],
            )
        )

        response = contest_access_revoke(contest=contest_slug, user="alice", target_user="bob")

        self.assertEqual(response.status_code, 303)
        self.assertFalse(
            config.workspace_service.access_context(problem_id, int(bob["id"]))["can_read"]
        )
        self.assertNotIn(
            _problem_slug,
            config.workspace_service.accessible_problem_slugs(int(bob["id"]), limit=20),
        )
