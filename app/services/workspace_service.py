from __future__ import annotations

import fcntl
import shlex
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.db import DB, now_iso
from app.settings import Settings
from app.services.util import ensure_dir, run_cmd


class WorkspaceService:
    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings

    def ensure_layout(self) -> None:
        for p in [
            self.settings.bare_root,
            self.settings.workspace_root,
            self.settings.run_root,
            self.settings.artifacts_root,
            self.settings.cache_root,
        ]:
            ensure_dir(p)

    def ensure_problem(self, slug: str, name: str) -> None:
        self.ensure_layout()
        repo_name = f"{slug}.git"
        bare = self.settings.bare_root / repo_name
        if not bare.exists():
            run_cmd(["git", "init", "--bare", str(bare)])
        row = self.db.fetch_one("SELECT id FROM problems WHERE slug=?", [slug])
        if row is None:
            self.db.execute(
                "INSERT INTO problems(slug, name, repo_name, created_at) VALUES(?,?,?,?)",
                [slug, name, repo_name, now_iso()],
            )

    def ensure_user(self, username: str) -> None:
        row = self.db.fetch_one("SELECT id FROM users WHERE username=?", [username])
        if row is None:
            self.db.execute(
                "INSERT INTO users(username, created_at) VALUES(?,?)",
                [username, now_iso()],
            )

    def _problem_row(self, slug: str):
        row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [slug])
        if row is None:
            raise ValueError(f"Unknown problem: {slug}")
        return row

    def _user_row(self, username: str):
        row = self.db.fetch_one("SELECT * FROM users WHERE username=?", [username])
        if row is None:
            raise ValueError(f"Unknown user: {username}")
        return row

    def ensure_workspace(self, problem: str, username: str, refresh_status: bool = True) -> Path:
        self.ensure_user(username)
        p = self._problem_row(problem)
        u = self._user_row(username)

        workspace = self.settings.workspace_root / str(u["id"]) / problem
        bare = self.settings.bare_root / p["repo_name"]
        workspace_created = False
        if not workspace.exists():
            ensure_dir(workspace.parent)
            run_cmd(["git", "clone", str(bare), str(workspace)])
            if not (workspace / ".git").exists():
                raise RuntimeError("workspace clone failed")
            self._seed_problem_repo(workspace)
            workspace_created = True

        ws_row = self.db.fetch_one(
            "SELECT id FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]]
        )
        ws_row_created = False
        if ws_row is None:
            self.db.execute(
                "INSERT INTO workspaces(problem_id,user_id,path,updated_at) VALUES(?,?,?,?)",
                [p["id"], u["id"], str(workspace), now_iso()],
            )
            ws_row_created = True

        if refresh_status or workspace_created or ws_row_created:
            self.refresh_workspace_status(problem, username)
        return workspace

    def _seed_problem_repo(self, workspace: Path) -> None:
        required_dirs = [
            "statement",
            "config",
            "validators",
            "checkers",
            "interactors",
            "generators",
            "solutions",
            "tests/manual",
            "third_party/testlib",
        ]
        for d in required_dirs:
            (workspace / d).mkdir(parents=True, exist_ok=True)
        readme = workspace / "README.problem.md"
        if not readme.exists():
            readme.write_text("# Problem repository\n", encoding="utf-8")
        testlib = workspace / "third_party/testlib/testlib.h"
        if not testlib.exists():
            source = (Path(__file__).resolve().parents[2] / "third_party/upstream/testlib/testlib.h")
            if source.exists():
                testlib.write_bytes(source.read_bytes())
            else:
                testlib.write_text("// place fixed testlib.h copy here\n", encoding="utf-8")
        if not run_cmd(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"]).returncode == 0:
            run_cmd(["git", "-C", str(workspace), "config", "user.email", "system@polygonlike.local"])
            run_cmd(["git", "-C", str(workspace), "config", "user.name", "Polygonlike System"])
            run_cmd(["git", "-C", str(workspace), "add", "."])
            run_cmd(["git", "-C", str(workspace), "commit", "-m", "Initialize problem repository"])
            run_cmd(["git", "-C", str(workspace), "branch", "-M", "main"])
            run_cmd(["git", "-C", str(workspace), "push", "origin", "main"])

    def refresh_workspace_status(self, problem: str, username: str) -> dict[str, str | int | None]:
        p = self._problem_row(problem)
        u = self._user_row(username)
        workspace = Path(self.settings.workspace_root / str(u["id"]) / problem)
        branch = run_cmd(["git", "-C", str(workspace), "branch", "--show-current"]).stdout.strip() or "main"
        head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
        dirty_status = run_cmd(["git", "-C", str(workspace), "status", "--porcelain"]).stdout
        dirty = 1 if self._is_status_dirty(dirty_status) else 0
        self.db.execute(
            "UPDATE workspaces SET branch=?, head_commit=?, dirty=?, updated_at=? WHERE problem_id=? AND user_id=?",
            [branch, head, dirty, now_iso(), p["id"], u["id"]],
        )
        return {"branch": branch, "head_commit": head, "dirty": dirty}

    def workspace_context(self, problem: str, username: str) -> dict:
        p = self._problem_row(problem)
        u = self._user_row(username)
        ws = self.db.fetch_one("SELECT * FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]])
        if ws is None:
            self.ensure_workspace(problem, username)
            ws = self.db.fetch_one("SELECT * FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]])
        if ws is None:
            raise RuntimeError(f"workspace not available for {problem}/{username}")
        ws_path = Path(str(ws["path"] or "")).resolve()
        expected_root = (self.settings.workspace_root / str(u["id"]) / problem).resolve()
        if ws_path != expected_root:
            raise RuntimeError(f"workspace path mismatch for {problem}/{username}")
        if not ws_path.exists() or not ws_path.is_dir():
            raise RuntimeError(f"workspace path missing for {problem}/{username}")
        git_dir = ws_path / ".git"
        if not git_dir.exists() or not git_dir.is_dir():
            raise RuntimeError(f"workspace git metadata missing for {problem}/{username}")
        latest_build = self.db.fetch_one(
            "SELECT id,status,created_at FROM builds WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",
            [ws["id"]],
        )
        latest_preview = self.db.fetch_one(
            "SELECT id,status,created_at FROM previews WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",
            [ws["id"]],
        )
        return {
            "problem": dict(p),
            "user": dict(u),
            "workspace": dict(ws),
            "latest_build": dict(latest_build) if latest_build else None,
            "latest_preview": dict(latest_preview) if latest_preview else None,
        }

    def _extract_commit_snapshot(self, workspace: Path, commit: str, snap: Path) -> None:
        ensure_dir(snap)
        cmd = (
            "set -euo pipefail; "
            f"git -C {shlex.quote(str(workspace))} archive {shlex.quote(commit)} "
            f"| tar -x -C {shlex.quote(str(snap))}"
        )
        proc = run_cmd(["bash", "-lc", cmd], timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)

    def resolve_commit(self, workspace: Path, commit_ref: str) -> str:
        proc = run_cmd(["git", "-C", str(workspace), "rev-parse", "--verify", f"{commit_ref}^{{commit}}"])
        commit = proc.stdout.strip()
        if proc.returncode != 0 or not commit:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(detail or f"unable to resolve commit reference: {commit_ref}")
        return commit

    def _workspace_dirty(self, workspace: Path) -> bool:
        proc = run_cmd(["git", "-C", str(workspace), "status", "--porcelain"])
        return self._is_status_dirty(proc.stdout)

    def workspace_is_dirty(self, workspace: Path) -> bool:
        return self._workspace_dirty(workspace)

    def _is_status_dirty(self, status_output: str) -> bool:
        for raw in status_output.splitlines():
            line = raw.strip()
            if not line:
                continue
            path = line[3:].strip() if len(line) >= 4 else line
            if path == ".polygonlike.lock":
                continue
            if path.endswith("/.polygonlike.lock"):
                continue
            return True
        return False

    def create_snapshot(self, workspace: Path, commit: str | None) -> Path:
        run_id = f"snapshot-{uuid.uuid4().hex[:12]}"
        snap = self.settings.run_root / run_id / "src"
        ensure_dir(snap.parent)
        if commit:
            self._extract_commit_snapshot(workspace, commit, snap)
        else:
            # Fast path for clean workspaces: archive current HEAD instead of copying entire tree.
            if not self._workspace_dirty(workspace):
                head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
                self._extract_commit_snapshot(workspace, head, snap)
            else:
                from app.services.util import copytree

                copytree(workspace, snap)
        git_dir = snap / ".git"
        if git_dir.exists():
            import shutil

            shutil.rmtree(git_dir)
        return snap

    @contextmanager
    def workspace_lock(self, workspace: Path):
        lock_path = workspace / ".polygonlike.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
