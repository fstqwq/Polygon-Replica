from app.db import now_iso
from app.service.access.policy import (
    agent_general_scope,
    agent_scope,
    contest_role,
    repo_role,
)

from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import isolated_db_execute, isolated_db_fetch_one


class TestAccessService(DBTestBase):
    def _problem(self) -> tuple[int, int]:
        self.workspace_service.ensure_problem(self.problem)
        owner = self.workspace_service.ensure_user(self.user)
        self.workspace_service.grant_repo_access(self.problem, self.user, "owner")
        problem_id = self.workspace_service.known_problem_id(self.problem)
        assert problem_id is not None
        return int(problem_id), int(owner["id"])

    def _user_id(self, username: str) -> int:
        return int(self.workspace_service.ensure_user(username)["id"])

    def _workspace_id(self, problem_id: int, username: str) -> int:
        self.workspace_service.ensure_workspace(
            self.problem,
            username,
            refresh_status=False,
        )
        row = isolated_db_fetch_one(
            self.db,
            """
            SELECT id FROM workspaces
            WHERE problem_id=?
              AND user_id=(SELECT id FROM users WHERE username=?)
            """,
            [problem_id, username],
        )
        assert row is not None
        return int(row["id"])

    def _contest(self, owner_user_id: int, problem_id: int) -> int:
        isolated_db_execute(
            self.db,
            """
            INSERT INTO contests(
                slug,title,owner_user_id,status,source_generation,location,
                date_text,statement_default_language,created_at
            ) VALUES(?,?,?,'draft',1,'','','english',?)
            """,
            [f"contest-{self.user}", "Contest", owner_user_id, now_iso()],
        )
        row = isolated_db_fetch_one(
            self.db,
            "SELECT id FROM contests WHERE owner_user_id=? ORDER BY id DESC LIMIT 1",
            [owner_user_id],
        )
        assert row is not None
        contest_id = int(row["id"])
        isolated_db_execute(
            self.db,
            "INSERT INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)",
            [contest_id, owner_user_id, "owner", now_iso()],
        )
        isolated_db_execute(
            self.db,
            """
            INSERT INTO contest_problems(
                contest_id,position,label,problem_id,statement_folder,
                added_by_user_id,created_at
            ) VALUES(?,1,'A',?,'',?,?)
            """,
            [contest_id, problem_id, owner_user_id, now_iso()],
        )
        return contest_id

    def test_problem_role_combines_without_manage_escalation(self) -> None:
        problem_id, owner_user_id = self._problem()
        contest_id = self._contest(owner_user_id, problem_id)
        member_user_id = self._user_id(f"member-{self.user}")
        isolated_db_execute(
            self.db,
            "INSERT INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)",
            [contest_id, member_user_id, "write", now_iso()],
        )

        access = self.access_query.problem_context(problem_id, member_user_id)

        self.assertEqual(access["role"], "write")
        self.assertTrue(access["can_write"])
        self.assertFalse(access["can_manage"])
        self.assertTrue(access["can_rejudge"])
        self.assertTrue(access["can_create_packages"])

    def test_direct_acl_outweighs_contest_derived_role(self) -> None:
        problem_id, owner_user_id = self._problem()
        contest_id = self._contest(owner_user_id, problem_id)
        direct_owner = f"direct-{self.user}"
        direct_owner_id = self._user_id(direct_owner)
        self.workspace_service.grant_repo_access(
            self.problem,
            direct_owner,
            "owner",
        )
        isolated_db_execute(
            self.db,
            "INSERT INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)",
            [contest_id, direct_owner_id, "read", now_iso()],
        )

        effective = self.access_query.problem_context(problem_id, direct_owner_id)
        direct = self.access_query.direct_problem_context(problem_id, direct_owner_id)

        self.assertEqual(effective["role"], "owner")
        self.assertTrue(effective["can_manage"])
        self.assertEqual(direct["role"], "owner")

    def test_read_only_user_can_rejudge_but_only_owning_workspace_can_cancel(self) -> None:
        problem_id, _owner_user_id = self._problem()
        reader = f"reader-{self.user}"
        reader_user_id = self._user_id(reader)
        self.workspace_service.grant_repo_access(self.problem, reader, "read")
        reader_workspace_id = self._workspace_id(problem_id, reader)
        other_workspace_id = self._workspace_id(problem_id, self.user)
        verification = {
            "id": "ver-owned",
            "problem_id": problem_id,
            "workspace_id": reader_workspace_id,
        }

        own = self.access_query.verification_context(
            actor_user_id=reader_user_id,
            actor_workspace_id=reader_workspace_id,
            expected_problem_id=problem_id,
            verification=verification,
        )
        foreign = self.access_query.verification_context(
            actor_user_id=reader_user_id,
            actor_workspace_id=other_workspace_id,
            expected_problem_id=problem_id,
            verification=verification,
        )
        published = self.access_query.verification_context(
            actor_user_id=reader_user_id,
            actor_workspace_id=reader_workspace_id,
            expected_problem_id=problem_id,
            verification={**verification, "id": "ver-published", "workspace_id": None},
        )
        forged = self.access_query.verification_context(
            actor_user_id=reader_user_id,
            actor_workspace_id=other_workspace_id,
            expected_problem_id=problem_id,
            verification={**verification, "workspace_id": other_workspace_id},
        )

        self.assertTrue(own["can_view"])
        self.assertTrue(own["can_rejudge"])
        self.assertTrue(own["can_cancel"])
        self.assertTrue(foreign["can_view"])
        self.assertTrue(foreign["can_rejudge"])
        self.assertFalse(foreign["can_cancel"])
        self.assertTrue(published["can_view"])
        self.assertFalse(published["can_cancel"])
        self.assertFalse(forged["can_cancel"])

    def test_workspace_access_requires_the_persisted_owner(self) -> None:
        problem_id, owner_user_id = self._problem()
        reader = f"reader-{self.user}"
        reader_user_id = self._user_id(reader)
        self.workspace_service.grant_repo_access(self.problem, reader, "read")
        owner_workspace_id = self._workspace_id(problem_id, self.user)
        reader_workspace_id = self._workspace_id(problem_id, reader)

        own = self.access_query.workspace_context(
            problem_id=problem_id,
            actor_user_id=reader_user_id,
            workspace_id=reader_workspace_id,
        )
        foreign = self.access_query.workspace_context(
            problem_id=problem_id,
            actor_user_id=reader_user_id,
            workspace_id=owner_workspace_id,
        )
        owner = self.access_query.workspace_context(
            problem_id=problem_id,
            actor_user_id=owner_user_id,
            workspace_id=owner_workspace_id,
        )

        self.assertTrue(own["can_read"])
        self.assertFalse(own["can_write"])
        self.assertTrue(own["can_manage"])
        self.assertFalse(foreign["can_read"])
        self.assertFalse(foreign["can_manage"])
        self.assertTrue(owner["can_write"])
        self.assertTrue(owner["can_manage"])

    def test_problem_listing_uses_the_same_effective_role(self) -> None:
        problem_id, owner_user_id = self._problem()
        contest_id = self._contest(owner_user_id, problem_id)
        member_user_id = self._user_id(f"member-{self.user}")
        isolated_db_execute(
            self.db,
            """
            INSERT INTO contest_members(contest_id,user_id,role,created_at)
            VALUES(?,?,?,?)
            """,
            [contest_id, member_user_id, "write", now_iso()],
        )

        rows = self.access_query.participating_problem_rows(
            member_user_id,
            limit=20,
        )

        row = next(row for row in rows if row["slug"] == self.problem)
        self.assertEqual(row["role"], "write")
        self.assertEqual(
            row["role"],
            self.access_query.problem_context(problem_id, member_user_id)["role"],
        )

    def test_package_jobs_share_only_succeeded_or_manager_visible_history(self) -> None:
        problem_id, owner_user_id = self._problem()
        reader_user_id = self._user_id(f"reader-{self.user}")
        self.workspace_service.grant_repo_access(
            self.problem,
            f"reader-{self.user}",
            "read",
        )

        queued = self.access_query.package_job_context(
            actor_user_id=reader_user_id,
            problem_id=problem_id,
            job_actor_user_id=owner_user_id,
            status="queued",
        )
        succeeded = self.access_query.package_job_context(
            actor_user_id=reader_user_id,
            problem_id=problem_id,
            job_actor_user_id=owner_user_id,
            status="succeeded",
        )
        manager = self.access_query.package_job_context(
            actor_user_id=owner_user_id,
            problem_id=problem_id,
            job_actor_user_id=reader_user_id,
            status="failed",
        )

        self.assertFalse(queued["can_view"])
        self.assertTrue(succeeded["can_view"])
        self.assertTrue(succeeded["can_download"])
        self.assertTrue(manager["can_view"])

    def test_agent_scope_is_intersection_with_current_problem_role(self) -> None:
        problem_id, _owner_user_id = self._problem()
        reader = f"reader-{self.user}"
        reader_user_id = self._user_id(reader)
        self.workspace_service.grant_repo_access(self.problem, reader, "read")

        self.assertEqual(
            self.access_query.effective_agent_scope(
                declared_scope="commit",
                problem_id=problem_id,
                user_id=reader_user_id,
            ),
            "readonly",
        )
        self.assertTrue(self.access_query.agent_scope_allows("commit", "workspace"))
        self.assertFalse(self.access_query.agent_scope_allows("readonly", "workspace"))

    def test_role_boundaries_reject_noncanonical_tokens(self) -> None:
        self.assertEqual(agent_general_scope("none"), "none")
        self.assertEqual(agent_general_scope("readonly"), "readonly")
        for parser in (repo_role, contest_role, agent_scope):
            with self.subTest(parser=parser.__name__):
                with self.assertRaises(ValueError):
                    parser(" READ ")
