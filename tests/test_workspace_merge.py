from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, cast
from unittest.mock import patch

from app.service.platform.git_process import run_git
from app.service.repository.git import GitService
from app.service.repository.merge import WorkspaceMergeService
from app.service.repository.workspace import WorkspaceService, recover_workspace_swap
from app.setting import Settings


class _WorkspaceLockStub:
    @contextmanager
    def workspace_lock(self, _workspace: Path) -> Iterator[None]:
        yield


class TestWorkspaceMerge(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="polygon-merge-")
        self.root = Path(self._temp.name)
        self.remote = self.root / "problem.git"
        self.workspace = self.root / "workspace"
        self._git(self.root, "init", "--bare", str(self.remote))
        seed = self.root / "seed"
        self._git(self.root, "clone", str(self.remote), str(seed))
        self._configure(seed)
        (seed / "local.txt").write_text("base\n", encoding="utf-8")
        (seed / "same.txt").write_text("base\n", encoding="utf-8")
        self._git(seed, "add", ".")
        self._git(seed, "commit", "-m", "initial")
        self._git(seed, "branch", "-M", "main")
        self._git(seed, "push", "origin", "main")
        self._git(self.root, "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        self._git(self.root, "clone", str(self.remote), str(self.workspace))
        self._configure(self.workspace)
        settings = Settings(
            db_path=self.root / "metadata.db",
            bare_root=self.root,
            workspace_root=self.root,
            artifacts_root=self.root / "artifacts",
            cache_root=self.root / "cache",
        )
        workspace_service = cast(WorkspaceService, _WorkspaceLockStub())
        self.service = WorkspaceMergeService(settings, workspace_service)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _git(self, cwd: Path, *args: str) -> str:
        result = run_git(["git", *args], cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout.strip()

    def _configure(self, repo: Path) -> None:
        self._git(repo, "config", "user.name", "Test User")
        self._git(repo, "config", "user.email", "test@example.invalid")

    def _shared_clone(self, name: str) -> Path:
        path = self.root / name
        self._git(self.root, "clone", str(self.remote), str(path))
        self._configure(path)
        return path

    def _push_shared_file(self, clone_name: str, rel_path: str, content: str) -> str:
        shared = self._shared_clone(clone_name)
        target = shared / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._git(shared, "add", rel_path)
        self._git(shared, "commit", "-m", clone_name)
        self._git(shared, "push", "origin", "main")
        return self._git(shared, "rev-parse", "HEAD")

    def test_clean_workspace_advances_without_preview_or_undo(self) -> None:
        latest_head = self._push_shared_file(
            "auto-update",
            "shared.txt",
            "latest\n",
        )

        self.assertTrue(self.service.advance_clean_workspace(self.workspace))
        self.assertEqual(self._git(self.workspace, "rev-parse", "HEAD"), latest_head)
        self.assertEqual(
            (self.workspace / "shared.txt").read_text(encoding="utf-8"),
            "latest\n",
        )
        self.assertFalse(self.service.has_undo(self.workspace))
        self.assertFalse(self.service.advance_clean_workspace(self.workspace))

    def test_git_service_commit_is_quiet_and_keeps_head_and_empty_commit_behavior(self) -> None:
        target = self.workspace / "quiet-commit.txt"
        target.write_text("quiet\n", encoding="utf-8")

        with patch("app.service.repository.git.run_git", wraps=run_git) as mocked_run_git:
            head = GitService().commit(self.workspace, "quiet commit", "Test User", "test@example.invalid")

        commit_command = next(
            call.args[0]
            for call in mocked_run_git.call_args_list
            if len(call.args[0]) >= 5 and call.args[0][3] == "commit"
        )
        self.assertEqual(commit_command[-3:], ["--quiet", "-m", "quiet commit"])
        self.assertEqual(head, self._git(self.workspace, "rev-parse", "HEAD"))

        with self.assertRaisesRegex(RuntimeError, "nothing to commit"):
            GitService().commit(self.workspace, "empty commit", "Test User", "test@example.invalid")

    def test_dirty_or_diverged_workspace_does_not_advance(self) -> None:
        initial_head = self._git(self.workspace, "rev-parse", "HEAD")
        self._push_shared_file("dirty-shared", "shared.txt", "latest\n")
        (self.workspace / "local.txt").write_text("mine\n", encoding="utf-8")

        self.assertFalse(self.service.advance_clean_workspace(self.workspace))
        self.assertEqual(self._git(self.workspace, "rev-parse", "HEAD"), initial_head)

        self._git(self.workspace, "add", "local.txt")
        self._git(self.workspace, "commit", "-m", "local-only")
        diverged_head = self._git(self.workspace, "rev-parse", "HEAD")
        self.assertFalse(self.service.advance_clean_workspace(self.workspace))
        self.assertEqual(self._git(self.workspace, "rev-parse", "HEAD"), diverged_head)

    def test_suggested_manifest_lists_only_actual_result_changes(self) -> None:
        (self.workspace / "local.txt").write_text("my edit\n", encoding="utf-8")
        self._push_shared_file("suggested-manifest", "shared.txt", "latest\n")

        preview = self.service.start_preview("alice", "alice/sample", self.workspace)

        self.assertTrue(preview.suggested_available)
        self.assertTrue(preview.fast_forward_possible)
        self.assertEqual(
            [entry.path for entry in preview.suggested_entries],
            ["shared.txt"],
        )

    def test_suggested_merge_is_atomic_and_undo_restores_current_files(self) -> None:
        old_head = self._git(self.workspace, "rev-parse", "HEAD")
        (self.workspace / "local.txt").write_text("my edit\n", encoding="utf-8")
        shared = self._shared_clone("shared-update")
        (shared / "shared.txt").write_text("latest\n", encoding="utf-8")
        self._git(shared, "add", ".")
        self._git(shared, "commit", "-m", "shared update")
        self._git(shared, "push", "origin", "main")

        preview = self.service.start_preview("alice", "alice/sample", self.workspace)
        self.assertTrue(preview.suggested_available)
        self.assertEqual((self.workspace / "local.txt").read_text(encoding="utf-8"), "my edit\n")
        self.assertFalse((self.workspace / "shared.txt").exists())

        self.service.apply_preview("alice", "alice/sample", preview.preview_id, "suggested", {})
        self.assertEqual((self.workspace / "local.txt").read_text(encoding="utf-8"), "my edit\n")
        self.assertEqual((self.workspace / "shared.txt").read_text(encoding="utf-8"), "latest\n")
        self.assertTrue(self.service.has_undo(self.workspace))
        self.assertEqual(
            self.service.undo_context(self.workspace),
            {"mode": "suggested", "affected_count": 1},
        )

        self.service.undo(self.workspace)
        self.assertEqual(self._git(self.workspace, "rev-parse", "HEAD"), old_head)
        self.assertEqual((self.workspace / "local.txt").read_text(encoding="utf-8"), "my edit\n")
        self.assertFalse((self.workspace / "shared.txt").exists())
        self.assertFalse(self.service.has_undo(self.workspace))

    def test_conflict_requires_complete_manual_choice(self) -> None:
        (self.workspace / "same.txt").write_text("mine\n", encoding="utf-8")
        shared = self._shared_clone("shared-conflict")
        (shared / "same.txt").write_text("theirs\n", encoding="utf-8")
        self._git(shared, "add", ".")
        self._git(shared, "commit", "-m", "conflict")
        self._git(shared, "push", "origin", "main")

        preview = self.service.start_preview("alice", "alice/sample", self.workspace)
        self.assertFalse(preview.suggested_available)
        choices = {group_id: "published" for group_id, _paths in preview.groups}
        self.service.apply_preview("alice", "alice/sample", preview.preview_id, "manual", choices)
        self.assertEqual((self.workspace / "same.txt").read_text(encoding="utf-8"), "theirs\n")

    def test_changed_workspace_invalidates_preview(self) -> None:
        shared = self._shared_clone("shared-stale")
        (shared / "shared.txt").write_text("latest\n", encoding="utf-8")
        self._git(shared, "add", ".")
        self._git(shared, "commit", "-m", "shared")
        self._git(shared, "push", "origin", "main")
        preview = self.service.start_preview("alice", "alice/sample", self.workspace)
        (self.workspace / "after-preview.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "files changed"):
            self.service.apply_preview("alice", "alice/sample", preview.preview_id, "suggested", {})
        self.assertFalse((self.workspace / "shared.txt").exists())

    def test_visible_symlink_is_rejected(self) -> None:
        os.symlink(self.workspace / "local.txt", self.workspace / "linked.txt")
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.service.start_preview("alice", "alice/sample", self.workspace)

    def test_binary_preview_is_download_only(self) -> None:
        (self.workspace / "local.txt").write_text("mine\n", encoding="utf-8")
        shared = self._shared_clone("shared-binary")
        (shared / "payload.bin").write_bytes(b"binary\0payload")
        self._git(shared, "add", "payload.bin")
        self._git(shared, "commit", "-m", "binary")
        self._git(shared, "push", "origin", "main")

        preview = self.service.start_preview("alice", "alice/sample", self.workspace)
        entry = next(row for row in preview.suggested_entries if row.path == "payload.bin")
        comparison = self.service.comparison(
            "alice",
            "alice/sample",
            preview.preview_id,
            entry.entry_id,
            "suggested",
        )

        self.assertEqual(entry.suggested.content_kind, "binary")
        self.assertTrue(comparison.binary)
        self.assertEqual(comparison.rows, ())
        path, descriptor = self.service.entry_file(
            "alice",
            "alice/sample",
            preview.preview_id,
            entry.entry_id,
            "suggested",
        )
        self.assertEqual(path.read_bytes(), b"binary\0payload")
        self.assertEqual(descriptor.content_kind, "binary")

    def test_unborn_workspace_can_merge_and_undo_first_shared_revision(self) -> None:
        unborn = self.root / "unborn"
        self._git(self.root, "init", str(unborn))
        self._git(unborn, "symbolic-ref", "HEAD", "refs/heads/main")
        self._git(unborn, "remote", "add", "origin", str(self.remote))
        self._configure(unborn)
        (unborn / "local-only.txt").write_text("mine\n", encoding="utf-8")

        preview = self.service.start_preview("alice", "alice/unborn", unborn)
        choices = {group_id: "workspace" for group_id, _paths in preview.groups}
        self.service.apply_preview("alice", "alice/unborn", preview.preview_id, "manual", choices)
        self.assertNotEqual(self._git(unborn, "rev-parse", "HEAD"), "")
        self.assertEqual((unborn / "local-only.txt").read_text(encoding="utf-8"), "mine\n")

        self.service.undo(unborn)
        head = run_git(["git", "-C", str(unborn), "rev-parse", "--verify", "HEAD"])
        self.assertNotEqual(head.returncode, 0)
        self.assertEqual((unborn / "local-only.txt").read_text(encoding="utf-8"), "mine\n")

    def test_interrupted_old_move_restores_previous_workspace(self) -> None:
        workspace = self.root / "recovery"
        workspace.mkdir()
        (workspace / "state.txt").write_text("old\n", encoding="utf-8")
        candidate = self.root / ".recovery.merge-candidate-test"
        candidate.mkdir()
        (candidate / "state.txt").write_text("new\n", encoding="utf-8")
        backup = self.root / ".recovery.merge-backup-test"
        os.replace(workspace, backup)
        journal = self.root / ".recovery.merge-transaction.json"
        journal.write_text(
            json.dumps(
                {
                    "phase": "old-moved",
                    "candidate": candidate.name,
                    "backup": backup.name,
                }
            ),
            encoding="utf-8",
        )
        recover_workspace_swap(workspace)
        self.assertEqual((workspace / "state.txt").read_text(encoding="utf-8"), "old\n")
        self.assertFalse(candidate.exists())
        self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
