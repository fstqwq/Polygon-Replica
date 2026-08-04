from __future__ import annotations

from tests.contest_support import ContestActionBase
from tests.db_helpers import db_execute, db_fetch_one
from tests.ui_support import (
    _flash_messages_from_response,
    config,
    contest_access_revoke,
    contest_access_revoke_with_problems,
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
        self.assertIn("problem access was unchanged", _flash_messages_from_response(response)[0])
        acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='carol')",
            [problem_id],
        )
        self.assertIsNotNone(acl)
        self.assertEqual(str(acl["role"]), "read")

    def test_combined_revoke_is_atomic_and_respects_manage_boundaries(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("revoke-combined")
        _row_a, problem_a_id, problem_a = self.add_owned_problem(contest_id, actor_user_id, "A", "managed-a")
        _row_b, problem_b_id, problem_b = self.add_owned_problem(contest_id, actor_user_id, "B", "managed-b")
        workspace_service.ensure_user("bob")
        config.contest_service.grant_member_role(contest_id, "bob", "write")
        workspace_service.grant_repo_access(problem_a, "bob", "write")
        workspace_service.grant_repo_access(problem_b, "bob", "owner")

        problem_c = f"bob/unmanaged-{self.random_id('problem')}"
        workspace_service.ensure_problem(problem_c)
        workspace_service.grant_repo_access(problem_c, "bob", "owner")
        workspace_service.grant_repo_access(problem_c, "alice", "read")
        problem_c_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_c])
        self.assertIsNotNone(problem_c_row)
        problem_c_id = int(problem_c_row["id"])
        config.contest_service.add_problem(contest_id, "C", problem_c_id, actor_user_id)
        workspace_service.ensure_workspace(problem_a, "bob")
        workspace_before = db_fetch_one(
            "SELECT COUNT(*) AS c FROM workspaces WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [problem_a_id],
        )
        db_execute("UPDATE users SET is_system_admin=1 WHERE username='bob'")
        workspace_service.clear_identity_caches()

        response = contest_access_revoke_with_problems(
            contest=contest_slug,
            user="alice",
            target_user="bob",
        )

        self.assertEqual(response.status_code, 303)
        message = _flash_messages_from_response(response)[0]
        self.assertIn("removed 1 non-owner", message)
        self.assertIn("preserved 1 owner", message)
        self.assertIn("skipped 1 problem", message)
        self.assertIn("system administrator access remains effective", message)
        membership = db_fetch_one(
            "SELECT 1 FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNone(membership)
        self.assertIsNone(
            db_fetch_one(
                "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
                [problem_a_id],
            )
        )
        owner_acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [problem_b_id],
        )
        unmanaged_acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [problem_c_id],
        )
        self.assertEqual(str(owner_acl["role"]), "owner")
        self.assertEqual(str(unmanaged_acl["role"]), "owner")
        workspace_after = db_fetch_one(
            "SELECT COUNT(*) AS c FROM workspaces WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [problem_a_id],
        )
        self.assertEqual(int(workspace_before["c"]), int(workspace_after["c"]))
