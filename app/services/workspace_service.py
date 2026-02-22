from __future__ import annotations

import fcntl
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
import re

from app.db import DB, now_iso
from app.settings import Settings
from app.services.util import copytree, ensure_dir, extract_git_archive, remove_symlinks, run_cmd


class WorkspaceService:
    IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings
        self._problem_cache: dict[str, dict] = {}
        self._user_cache: dict[str, dict] = {}

    def _validate_identifier(self, value: str, label: str) -> str:
        ident = str(value or "").strip()
        if not self.IDENT_RE.fullmatch(ident):
            raise ValueError(f"invalid {label}")
        return ident

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
        slug = self._validate_identifier(slug, "problem")
        self.ensure_layout()
        repo_name = f"{slug}.git"
        bare = self.settings.bare_root / repo_name
        if not bare.exists():
            run_cmd(["git", "init", "--bare", str(bare)])
        row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [slug])
        if row is None:
            self.db.execute(
                "INSERT OR IGNORE INTO problems(slug, name, repo_name, created_at) VALUES(?,?,?,?)",
                [slug, name, repo_name, now_iso()],
            )
            row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [slug])
        if row is not None:
            self._problem_cache[slug] = dict(row)

    def ensure_user(self, username: str):
        username = self._validate_identifier(username, "user")
        cached = self._user_cache.get(username)
        if cached is not None:
            return cached
        row = self.db.fetch_one("SELECT * FROM users WHERE username=?", [username])
        if row is None:
            self.db.execute(
                "INSERT OR IGNORE INTO users(username, created_at) VALUES(?,?)",
                [username, now_iso()],
            )
            row = self.db.fetch_one("SELECT * FROM users WHERE username=?", [username])
        if row is None:
            raise RuntimeError(f"unable to ensure user row for {username}")
        row_dict = dict(row)
        self._user_cache[username] = row_dict
        return row_dict

    def _problem_row(self, slug: str):
        slug = self._validate_identifier(slug, "problem")
        cached = self._problem_cache.get(slug)
        if cached is not None:
            return cached
        row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [slug])
        if row is None:
            raise ValueError(f"Unknown problem: {slug}")
        row_dict = dict(row)
        self._problem_cache[slug] = row_dict
        return row_dict

    def _user_row(self, username: str):
        username = self._validate_identifier(username, "user")
        cached = self._user_cache.get(username)
        if cached is not None:
            return cached
        row = self.db.fetch_one("SELECT * FROM users WHERE username=?", [username])
        if row is None:
            raise ValueError(f"Unknown user: {username}")
        row_dict = dict(row)
        self._user_cache[username] = row_dict
        return row_dict

    @contextmanager
    def _workspace_provision_lock(self, workspace_parent: Path, problem: str):
        ensure_dir(workspace_parent)
        lock_path = workspace_parent / f".{problem}.provision.lock"
        with lock_path.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def ensure_workspace(self, problem: str, username: str, refresh_status: bool = True) -> Path:
        u = self.ensure_user(username)
        p = self._problem_row(problem)

        workspace = self.settings.workspace_root / str(u["id"]) / problem
        bare = self.settings.bare_root / p["repo_name"]
        ws_row = self.db.fetch_one("SELECT id FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]])

        # Steady-state fast path: avoid provisioning lock when workspace and DB row already exist.
        if ws_row is not None and workspace.exists() and (workspace / ".git").is_dir():
            if refresh_status:
                self._refresh_workspace_status_with_ids(workspace, int(p["id"]), int(u["id"]))
            return workspace

        with self._workspace_provision_lock(workspace.parent, problem):
            workspace_created = False
            if not workspace.exists():
                ensure_dir(workspace.parent)
                run_cmd(["git", "clone", str(bare), str(workspace)])
                if not (workspace / ".git").exists():
                    raise RuntimeError("workspace clone failed")
                self._seed_problem_repo(workspace)
                workspace_created = True

            ws_row = self.db.fetch_one("SELECT id FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]])
            ws_row_created = False
            if ws_row is None:
                try:
                    self.db.execute(
                        "INSERT INTO workspaces(problem_id,user_id,path,updated_at) VALUES(?,?,?,?)",
                        [p["id"], u["id"], str(workspace), now_iso()],
                    )
                    ws_row_created = True
                except sqlite3.IntegrityError:
                    ws_row_created = False

            if refresh_status or workspace_created or ws_row_created:
                self._refresh_workspace_status_with_ids(workspace, int(p["id"]), int(u["id"]))
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

    def _refresh_workspace_status_with_ids(self, workspace: Path, problem_id: int, user_id: int) -> dict[str, str | int | None]:
        status_v2 = run_cmd(["git", "-C", str(workspace), "status", "--porcelain=2", "--branch"])
        if status_v2.returncode == 0:
            branch, head, dirty = self._parse_status_v2(status_v2.stdout)
        else:
            status_out = run_cmd(["git", "-C", str(workspace), "status", "--short", "--branch"]).stdout
            branch = "main"
            for raw in status_out.splitlines():
                line = raw.strip()
                if not line.startswith("## "):
                    continue
                branch_line = line[3:]
                if branch_line.startswith("HEAD"):
                    branch = "main"
                    break
                branch = branch_line.split("...", 1)[0].strip() or "main"
                break
            head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
            dirty = 1 if self._is_status_dirty(status_out) else 0
        self.db.execute(
            """
            UPDATE workspaces
            SET branch=?, head_commit=?, dirty=?, updated_at=?
            WHERE problem_id=? AND user_id=?
              AND (branch IS NOT ? OR head_commit IS NOT ? OR dirty IS NOT ?)
            """,
            [branch, head, dirty, now_iso(), problem_id, user_id, branch, head, dirty],
        )
        return {"branch": branch, "head_commit": head, "dirty": dirty}

    def _parse_status_v2(self, status_output: str) -> tuple[str, str, int]:
        branch = "main"
        head = ""
        dirty = 0
        for raw in status_output.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("# branch.head "):
                head_name = line[len("# branch.head ") :].strip()
                if head_name and head_name not in {"(detached)", "(unknown)"}:
                    branch = head_name
                continue
            if line.startswith("# branch.oid "):
                oid = line[len("# branch.oid ") :].strip()
                if oid != "(initial)":
                    head = oid
                continue
            if line.startswith("# "):
                continue

            path = ""
            if line.startswith("? ") or line.startswith("! "):
                path = line[2:].strip()
            elif line.startswith("1 ") or line.startswith("2 ") or line.startswith("u "):
                path = line.rsplit(" ", 1)[-1].split("\t", 1)[0].strip()
            else:
                path = line

            if path == ".polygonlike.lock" or path.endswith("/.polygonlike.lock"):
                continue
            dirty = 1
            break
        return branch, head, dirty

    def refresh_workspace_status(self, problem: str, username: str) -> dict[str, str | int | None]:
        p = self._problem_row(problem)
        u = self._user_row(username)
        workspace = Path(self.settings.workspace_root / str(u["id"]) / problem)
        return self._refresh_workspace_status_with_ids(workspace, int(p["id"]), int(u["id"]))

    def workspace_context(self, problem: str, username: str, include_recent: bool = True) -> dict:
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
        latest_build = None
        latest_preview = None
        if include_recent:
            recent_rows = self.db.fetch_all(
                """
                SELECT kind,id,status,created_at
                FROM (
                    SELECT 'build' AS kind,id,status,created_at
                    FROM builds
                    WHERE workspace_id=?
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                UNION ALL
                SELECT kind,id,status,created_at
                FROM (
                    SELECT 'preview' AS kind,id,status,created_at
                    FROM previews
                    WHERE workspace_id=?
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                """,
                [ws["id"], ws["id"]],
            )
            for row in recent_rows:
                entry = {"id": row["id"], "status": row["status"], "created_at": row["created_at"]}
                if row["kind"] == "build":
                    latest_build = entry
                elif row["kind"] == "preview":
                    latest_preview = entry
        return {
            "problem": dict(p),
            "user": dict(u),
            "workspace": dict(ws),
            "latest_build": latest_build,
            "latest_preview": latest_preview,
        }

    def _extract_commit_snapshot(self, workspace: Path, commit: str, snap: Path) -> None:
        ensure_dir(snap)
        extract_git_archive(workspace, commit, snap, timeout=120)

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
            if line.startswith("## "):
                continue
            path = line[3:].strip() if len(line) >= 4 else line
            if path == ".polygonlike.lock":
                continue
            if path.endswith("/.polygonlike.lock"):
                continue
            return True
        return False

    def create_snapshot(
        self,
        workspace: Path,
        commit: str | None,
        workspace_head: str | None = None,
        workspace_dirty: bool | None = None,
    ) -> Path:
        run_id = f"snapshot-{uuid.uuid4().hex[:12]}"
        snap = self.settings.run_root / run_id / "src"
        ensure_dir(snap.parent)
        if commit:
            self._extract_commit_snapshot(workspace, commit, snap)
        else:
            # Fast path for clean workspaces: archive current HEAD instead of copying entire tree.
            dirty = workspace_dirty if workspace_dirty is not None else self._workspace_dirty(workspace)
            if not dirty:
                head = (workspace_head or "").strip()
                if not head:
                    head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
                self._extract_commit_snapshot(workspace, head, snap)
            else:
                copytree(workspace, snap)
        remove_symlinks(snap)
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
