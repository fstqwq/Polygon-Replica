from __future__ import annotations

import uuid
from pathlib import Path

from app.service.platform.git_process import run_git
from app.service.repository.git import GitService

from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import isolated_db_fetch_one


class TestWorkspaceStoreService(DBTestBase):
    def test_audit_event_downgrades_missing_foreign_keys_to_null(self) -> None:
        self.workspace_service.record_audit_event(
            actor_user_id=987654321,
            problem_id=987654322,
            action="test.missing_fk",
            details={"ok": True},
        )

        row = isolated_db_fetch_one(
            self.db,
            """
            SELECT actor_user_id,problem_id,action,details_json
            FROM audit_log
            WHERE action='test.missing_fk'
            ORDER BY id DESC
            LIMIT 1
            """,
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertIsNone(row["actor_user_id"])
        self.assertIsNone(row["problem_id"])
        self.assertEqual(str(row["action"]), "test.missing_fk")

    def test_ensure_workspace_repairs_unborn_clone_after_origin_main_appears(
        self,
    ) -> None:
        owner = self.user
        collaborator = f"bob-{uuid.uuid4().hex[:8]}"
        problem = self.problem
        self.workspace_service.ensure_problem(problem)
        self.workspace_service.ensure_user(owner)
        self.workspace_service.ensure_user(collaborator)
        self.workspace_service.grant_repo_access(problem, owner, "owner")
        self.workspace_service.grant_repo_access(
            problem,
            collaborator,
            "write",
        )

        owner_workspace = Path(
            self.workspace_service.ensure_workspace(problem, owner)
        )
        self.assertNotEqual(
            run_git(
                [
                    "git",
                    "-C",
                    str(owner_workspace),
                    "rev-parse",
                    "--verify",
                    "HEAD",
                ]
            ).returncode,
            0,
        )

        collaborator_workspace = Path(
            self.workspace_service.ensure_workspace(problem, collaborator)
        )
        config_path = collaborator_workspace / "config" / "problem.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            '{"time_limit_ms":1000,"memory_limit_mb":256,'
            '"mode":"pass-fail","pass_limit":1}\n',
            encoding="utf-8",
        )
        git_service = GitService()
        commit_id = git_service.commit(
            collaborator_workspace,
            "init",
            collaborator,
            f"{collaborator}@polygonlike.local",
        )
        self.assertRegex(commit_id, r"^[0-9a-f]{40}$")
        git_service.push(collaborator_workspace, "main")

        repaired_workspace = Path(
            self.workspace_service.ensure_workspace(problem, owner)
        )
        repaired_head = run_git(
            [
                "git",
                "-C",
                str(repaired_workspace),
                "rev-parse",
                "--verify",
                "HEAD",
            ]
        )
        self.assertEqual(repaired_head.returncode, 0)
        self.assertRegex(repaired_head.stdout.strip(), r"^[0-9a-f]{40}$")
        current_branch = run_git(
            [
                "git",
                "-C",
                str(repaired_workspace),
                "branch",
                "--show-current",
            ]
        ).stdout.strip()
        self.assertEqual(current_branch, "main")
        origin_main = run_git(
            [
                "git",
                "-C",
                str(repaired_workspace),
                "show-ref",
                "--verify",
                "refs/remotes/origin/main",
            ]
        )
        self.assertEqual(origin_main.returncode, 0)
