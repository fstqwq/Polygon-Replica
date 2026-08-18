import uuid
from pathlib import Path

from app.service.platform.git_process import run_git
from app.service.problem.preflight import (
    PublishedProblemSource,
    inspect_published_problem_sources,
)
from app.service.problem.runtime_config import problem_config_limits
from app.service.repository.git import GitService
from app.service.statement.constant import STATEMENT_DEFAULT_FILES
from app.service.statement.render import statement_templates_are_default

from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import isolated_db_fetch_one


class TestWorkspaceStoreService(DBTestBase):
    def test_new_workspace_seeds_default_statement_templates(self) -> None:
        self.workspace_service.ensure_problem(self.problem)
        self.workspace_service.ensure_user(self.user)
        self.workspace_service.grant_repo_access(
            self.problem,
            self.user,
            "owner",
        )

        workspace = Path(
            self.workspace_service.ensure_workspace(self.problem, self.user)
        )

        for rel, expected in STATEMENT_DEFAULT_FILES.items():
            with self.subTest(path=rel):
                self.assertEqual(
                    (workspace / rel).read_text(encoding="utf-8"),
                    expected,
                )
        self.assertTrue(statement_templates_are_default(workspace))

    def test_published_source_preflight_reports_without_mutating_git(self) -> None:
        self.workspace_service.ensure_problem(self.problem)
        self.workspace_service.ensure_user(self.user)
        self.workspace_service.grant_repo_access(
            self.problem, self.user, "owner"
        )
        workspace = Path(
            self.workspace_service.ensure_workspace(self.problem, self.user)
        )
        git_service = GitService()
        git_service.commit(
            workspace,
            "canonical source",
            self.user,
            f"{self.user}@example.test",
        )
        git_service.push(workspace, "main")
        problem_row = isolated_db_fetch_one(
            self.db,
            "SELECT repo_name FROM problems WHERE slug=?", [self.problem]
        )
        self.assertIsNotNone(problem_row)
        assert problem_row is not None
        published = [
            PublishedProblemSource(
                slug=self.problem,
                repo_name=str(problem_row["repo_name"]),
            )
        ]
        config_snapshot = self.config_values.snapshot()

        rows = inspect_published_problem_sources(
            published,
            bare_root=self.settings.bare_root,
            problem_limits=problem_config_limits(self.config_values),
            tests_spec_max_bytes=int(config_snapshot["TEXTAREA_MAX_BYTES"]),
            statement_sample_max_bytes=int(
                config_snapshot["STATEMENT_SAMPLE_MAX_BYTES"]
            ),
        )
        self.assertEqual(rows[0]["error"], "")

        config_path = workspace / "config/problem.json"
        original = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            original.rstrip("\n}") + ',\n  "legacy": true\n}\n',
            encoding="utf-8",
        )
        bad_commit = git_service.commit(
            workspace,
            "noncanonical source",
            self.user,
            f"{self.user}@example.test",
        )
        git_service.push(workspace, "main")

        rows = inspect_published_problem_sources(
            published,
            bare_root=self.settings.bare_root,
            problem_limits=problem_config_limits(self.config_values),
            tests_spec_max_bytes=int(config_snapshot["TEXTAREA_MAX_BYTES"]),
            statement_sample_max_bytes=int(
                config_snapshot["STATEMENT_SAMPLE_MAX_BYTES"]
            ),
        )
        self.assertEqual(rows[0]["source_commit"], bad_commit)
        self.assertIn("unsupported key 'legacy'", rows[0]["error"])
        self.assertEqual(config_path.read_text(encoding="utf-8").count("legacy"), 1)

        config_path.write_text(original, encoding="utf-8")
        link = workspace / "attachments/source-link"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("../config/problem.json")
        link_commit = git_service.commit(
            workspace,
            "noncanonical source link",
            self.user,
            f"{self.user}@example.test",
        )
        git_service.push(workspace, "main")

        rows = inspect_published_problem_sources(
            published,
            bare_root=self.settings.bare_root,
            problem_limits=problem_config_limits(self.config_values),
            tests_spec_max_bytes=int(config_snapshot["TEXTAREA_MAX_BYTES"]),
            statement_sample_max_bytes=int(
                config_snapshot["STATEMENT_SAMPLE_MAX_BYTES"]
            ),
        )
        self.assertEqual(rows[0]["source_commit"], link_commit)
        self.assertIn("symbolic link", rows[0]["error"])

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
