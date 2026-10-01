import threading
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.db import SQLValue
from app.service.platform.git_process import GitCommandResult, run_git
from app.service.problem.preflight import (
    PublishedProblemSource,
    inspect_published_problem_sources,
)
from app.service.problem.runtime_config import problem_config_limits
from app.service.repository.git import GitService
from app.service.repository.revision import git_commit_sha, workspace_revision_info
from app.service.statement.constant import STATEMENT_DEFAULT_FILES
from app.service.statement.render import statement_templates_are_default

from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import isolated_db_execute, isolated_db_fetch_one


class TestWorkspaceStoreService(DBTestBase):
    def _publication_workspaces(self) -> tuple[Path, Path, int]:
        self.workspace_service.ensure_problem(self.problem)
        self.workspace_service.grant_repo_access(self.problem, self.user, "owner")
        owner = Path(self.workspace_service.ensure_workspace(self.problem, self.user))
        git = GitService()
        with self.workspace_service.workspace_lock(owner):
            git.commit(owner, "initial", self.user, "owner@example.test")
            self.workspace_service.publish(owner, git_service=git)
        reader = self.workspace_service.ensure_user(f"reader-{uuid.uuid4().hex[:8]}")
        self.workspace_service.grant_repo_access(self.problem, reader["username"], "write")
        other = Path(self.workspace_service.ensure_workspace(self.problem, reader["username"]))
        return owner, other, reader["id"]

    def test_publication_updates_existing_list_rows_preserving_checkout_and_acl(self) -> None:
        owner, other, reader_id = self._publication_workspaces()
        other_problem = f"{self.user}/recent"
        self.workspace_service.ensure_problem(other_problem)
        # A second participating problem makes list ordering observable.
        reader_row = isolated_db_fetch_one(self.db, "SELECT username FROM users WHERE id=?", [reader_id])
        assert reader_row is not None
        self.workspace_service.grant_repo_access(other_problem, reader_row["username"], "read")
        with patch("app.service.disk.workspace_store.now_iso", return_value="2040-01-01T00:00:00+00:00"):
            self.workspace_service.ensure_workspace(other_problem, reader_row["username"])
        invited = self.workspace_service.ensure_user(f"invited-{uuid.uuid4().hex[:8]}")
        self.workspace_service.grant_repo_access(self.problem, invited["username"], "read")
        outsider = self.workspace_service.ensure_user(f"outsider-{uuid.uuid4().hex[:8]}")
        local_file = other / "notes.txt"
        with self.workspace_service.workspace_lock(other):
            local_file.write_text("uncommitted\n", encoding="utf-8")
        before = self.access_query.participating_problem_rows(reader_id, limit=10)
        old = next(row for row in before if row["slug"] == self.problem)
        self.assertEqual(before[0]["slug"], other_problem)
        git = GitService()
        with patch("app.service.disk.workspace_store.now_iso", return_value="2041-01-01T00:00:00+00:00"):
            with self.workspace_service.workspace_lock(owner):
                (owner / "published.txt").write_text("new\n", encoding="utf-8")
                head = git.commit(owner, "new publication", self.user, "owner@example.test")
                self.workspace_service.publish(owner, git_service=git)
        rows = self.access_query.participating_problem_rows(reader_id, limit=10)
        current = rows[0]
        self.assertEqual(current["slug"], self.problem)
        self.assertEqual(current["updated_at"], "2041-01-01T00:00:00+00:00")
        self.assertEqual(current["revision_upstream"], 2)
        self.assertEqual(current["revision_upstream_higher"], 1)
        self.assertEqual(current["revision_highlight"], 1)
        revision = workspace_revision_info(other)
        self.assertEqual((revision["local"], revision["upstream"]), (1, 2))
        self.assertTrue(revision["upstream_higher"])
        self.assertEqual(current["dirty"], old["dirty"])
        self.assertEqual(current["dirty"], 1)
        self.assertEqual(current["head_commit"], old["head_commit"])
        self.assertEqual(git_commit_sha(other, "HEAD"), old["head_commit"])
        self.assertEqual(local_file.read_text(encoding="utf-8"), "uncommitted\n")
        self.assertIsNone(current["revision_ahead_count"])
        self.assertIsNone(current["revision_behind_count"])
        owner_rows = self.access_query.participating_problem_rows(self.workspace_service.known_user_id(self.user), limit=10)
        published_row = next(row for row in owner_rows if row["slug"] == self.problem)
        self.assertEqual(published_row["head_commit"], head)
        self.assertEqual(published_row["updated_at"], current["updated_at"])
        self.assertEqual(published_row["revision_upstream_higher"], 0)
        self.assertIsNone(self.access_query.participating_problem_rows(invited["id"], limit=10)[0]["workspace_id"])
        self.assertEqual(self.access_query.participating_problem_rows(outsider["id"], limit=10), [])
        with self.workspace_service.workspace_lock(owner):
            self.workspace_service.publish(owner, git_service=git)
        self.assertEqual(self.access_query.participating_problem_rows(reader_id, limit=10), rows)
        self.assertEqual(self.access_query.participating_problem_rows(self.workspace_service.known_user_id(self.user), limit=10), owner_rows)

    def test_rejected_push_keeps_other_workspace_list_state(self) -> None:
        owner, other, reader_id = self._publication_workspaces()
        git = GitService()
        with self.workspace_service.workspace_lock(owner):
            (owner / "published.txt").write_text("remote\n", encoding="utf-8")
            local = git.commit(owner, "unpublished", self.user, "owner@example.test")
        hook = self.storage_layout.bare_repository(f"{self.problem}.git") / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o700)
        before = self.access_query.participating_problem_rows(reader_id, limit=10)
        with self.assertRaises(RuntimeError):
            with self.workspace_service.workspace_lock(owner):
                self.workspace_service.publish(owner, git_service=git)
        self.assertEqual(self.access_query.participating_problem_rows(reader_id, limit=10), before)
        self.assertEqual(git_commit_sha(owner, "HEAD"), local)
        self.assertEqual(git_commit_sha(other, "HEAD"), before[0]["head_commit"])

    def test_publication_survives_atomic_broadcast_failure_and_same_head_retry_repairs_it(self) -> None:
        owner, other, reader_id = self._publication_workspaces()
        before = self.access_query.participating_problem_rows(reader_id, limit=10)
        isolated_db_execute(self.db, f"""
            CREATE TRIGGER fail_publication BEFORE UPDATE OF revision_upstream ON workspaces
            WHEN NEW.user_id={reader_id} AND NEW.revision_upstream=2
            BEGIN SELECT RAISE(ABORT, 'injected broadcast failure'); END
        """)
        git = GitService()
        with self.assertLogs("app.service.repository.workspace", level="ERROR"):
            with self.workspace_service.workspace_lock(owner):
                (owner / "published.txt").write_text("remote\n", encoding="utf-8")
                head = git.commit(owner, "published", self.user, "owner@example.test")
                self.workspace_service.publish(owner, git_service=git)
        self.assertEqual(self.access_query.participating_problem_rows(reader_id, limit=10), before)
        self.assertEqual(git_commit_sha(self.storage_layout.bare_repository(f"{self.problem}.git"), "main"), head)
        self.assertEqual(git_commit_sha(other, "HEAD"), before[0]["head_commit"])
        isolated_db_execute(self.db, "DROP TRIGGER fail_publication")
        with self.workspace_service.workspace_lock(owner):
            self.workspace_service.publish(owner, git_service=git)
        repaired = self.access_query.participating_problem_rows(reader_id, limit=10)[0]
        self.assertEqual(repaired["revision_upstream"], 2)
        self.assertEqual(repaired["revision_upstream_higher"], 1)
        self.assertNotEqual(repaired["updated_at"], before[0]["updated_at"])

    def test_status_refresh_cannot_overwrite_a_new_publication(self) -> None:
        owner, other, reader_id = self._publication_workspaces()
        git = GitService()
        (owner / "published.txt").write_text("remote\n", encoding="utf-8")
        git.commit(owner, "published", self.user, "owner@example.test")
        snapshot_ready = threading.Event()
        release_refresh = threading.Event()
        publishing = threading.Event()
        execute = self.db.execute

        def paused_execute(sql: str, params: Iterable[SQLValue] = ()) -> None:
            if threading.current_thread().name.startswith("status-refresh") and "UPDATE workspaces" in sql:
                snapshot_ready.set()
                if not release_refresh.wait(10):
                    raise RuntimeError("refresh barrier timed out")
            return execute(sql, params)

        def publish() -> None:
            with self.workspace_service.workspace_lock(owner):
                publishing.set()
                self.workspace_service.publish(owner, git_service=git)

        with patch.object(self.db, "execute", side_effect=paused_execute):
            with ThreadPoolExecutor(1, thread_name_prefix="status-refresh") as reader_pool, ThreadPoolExecutor(1) as writer_pool:
                refresh = reader_pool.submit(self.workspace_service.refresh_workspace_status_by_path, other)
                try:
                    self.assertTrue(snapshot_ready.wait(10))
                    publication = writer_pool.submit(publish)
                    self.assertTrue(publishing.wait(10))
                    with self.assertRaises(TimeoutError):
                        publication.result(timeout=0.5)
                finally:
                    release_refresh.set()
                refresh.result(timeout=10)
                publication.result(timeout=10)
        row = self.access_query.participating_problem_rows(reader_id, limit=10)[0]
        self.assertEqual(row["revision_upstream"], 2)
        self.assertEqual(row["revision_upstream_higher"], 1)

    def test_origin_head_repair_failure_preserves_successful_publication(self) -> None:
        owner, _other, reader_id = self._publication_workspaces()
        git = GitService()

        def fail_symbolic_head(args: list[str]) -> GitCommandResult:
            if "symbolic-ref" in args:
                raise OSError("injected origin HEAD failure")
            return run_git(args)

        with self.workspace_service.workspace_lock(owner):
            (owner / "published.txt").write_text("published\n", encoding="utf-8")
            head = git.commit(owner, "published", self.user, "owner@example.test")
            with patch("app.service.repository.git.run_git", side_effect=fail_symbolic_head):
                with self.assertLogs("app.service.repository.git", level="ERROR"):
                    self.workspace_service.publish(owner, git_service=git)
        self.assertEqual(git_commit_sha(self.storage_layout.bare_repository(f"{self.problem}.git"), "main"), head)
        self.assertEqual(self.access_query.participating_problem_rows(reader_id, limit=10)[0]["revision_upstream"], 2)

    def test_identity_lookup_observes_changed_and_replaced_users(self) -> None:
        for lookup in (self.workspace_service.ensure_user, self.workspace_service.known_user):
            with self.subTest(lookup=lookup.__name__):
                username = f"cached-{uuid.uuid4().hex[:8]}"
                original = self.workspace_service.ensure_user(username)
                isolated_db_execute(
                    self.db, "UPDATE users SET is_banned=1 WHERE id=?", [original["id"]]
                )
                self.assertEqual(lookup(username)["is_banned"], 1)
                isolated_db_execute(
                    self.db,
                    "UPDATE users SET username=? WHERE id=?",
                    [f"renamed-{username}", original["id"]],
                )
                isolated_db_execute(
                    self.db,
                    "INSERT INTO users(username,created_at) VALUES(?,?)",
                    [username, original["created_at"]],
                )
                replacement = lookup(username)
                self.assertNotEqual(replacement["id"], original["id"])
                self.assertEqual(replacement["username"], username)
                self.assertEqual(replacement["is_banned"], 0)

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
