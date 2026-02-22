from __future__ import annotations

import fcntl
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

    def ensure_workspace(self, problem: str, username: str) -> Path:
        self.ensure_user(username)
        p = self._problem_row(problem)
        u = self._user_row(username)

        workspace = self.settings.workspace_root / str(u["id"]) / problem
        bare = self.settings.bare_root / p["repo_name"]
        if not workspace.exists():
            ensure_dir(workspace.parent)
            run_cmd(["git", "clone", str(bare), str(workspace)])
            if not (workspace / ".git").exists():
                raise RuntimeError("workspace clone failed")
            self._seed_problem_repo(workspace)

        ws_row = self.db.fetch_one(
            "SELECT id FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]]
        )
        if ws_row is None:
            self.db.execute(
                "INSERT INTO workspaces(problem_id,user_id,path,updated_at) VALUES(?,?,?,?)",
                [p["id"], u["id"], str(workspace), now_iso()],
            )

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
        dirty = 1 if run_cmd(["git", "-C", str(workspace), "status", "--porcelain"]).stdout.strip() else 0
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
        latest_build = self.db.fetch_one(
            "SELECT id,status,created_at FROM builds WHERE problem_id=? ORDER BY created_at DESC LIMIT 1", [p["id"]]
        )
        return {
            "problem": dict(p),
            "user": dict(u),
            "workspace": dict(ws),
            "latest_build": dict(latest_build) if latest_build else None,
        }

    def create_snapshot(self, workspace: Path, commit: str | None) -> Path:
        run_id = f"snapshot-{uuid.uuid4().hex[:12]}"
        snap = self.settings.run_root / run_id / "src"
        ensure_dir(snap.parent)
        if commit:
            clone_proc = run_cmd(["git", "clone", str(workspace), str(snap)])
            if clone_proc.returncode != 0:
                raise RuntimeError(clone_proc.stderr or clone_proc.stdout)
            proc = run_cmd(["git", "-C", str(snap), "checkout", commit])
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr or proc.stdout)
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
