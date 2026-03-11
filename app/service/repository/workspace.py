from __future__ import annotations

import errno
import fcntl
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
import re

from app.db import DB, now_iso
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.fs.op import copytree, ensure_dir, extract_git_archive, remove_symlinks
from app.setting import Settings
from app.service.statement.render import seed_statement_sources
from app.service.platform.process import run_cmd

PROBLEM_ID_RULE_MESSAGE: str = "invalid problem id"
USERNAME_RULE_MESSAGE: str = "invalid username"
APP_PROBLEM_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")
APP_USER_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def apply_runtime_values(values: RuntimeValues) -> None:
    global PROBLEM_ID_RULE_MESSAGE
    global USERNAME_RULE_MESSAGE
    global APP_PROBLEM_IDENT_RE
    global APP_USER_IDENT_RE
    PROBLEM_ID_RULE_MESSAGE = str(values.PROBLEM_ID_RULE_MESSAGE)
    USERNAME_RULE_MESSAGE = str(values.USERNAME_RULE_MESSAGE)
    APP_PROBLEM_IDENT_RE = values.PROBLEM_IDENT_RE
    APP_USER_IDENT_RE = values.USER_IDENT_RE

apply_runtime_values(build_runtime_values())


class WorkspaceService:
    IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    PROBLEM_IDENT_RE = APP_PROBLEM_IDENT_RE
    USER_IDENT_RE = APP_USER_IDENT_RE
    PROBLEM_CACHE_MAX_ENTRIES = 512
    USER_CACHE_MAX_ENTRIES = 2048
    REPO_ROLES = {"owner", "write", "read"}

    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings
        self._problem_cache: dict[str, dict] = {}
        self._user_cache: dict[str, dict] = {}
        self._cache_lock = threading.Lock()

    def _cache_get(self, cache: dict[str, dict], key: str) -> dict | None:
        with self._cache_lock:
            value = cache.get(key)
            if value is None:
                return None
            # Promote on access so active identities stay resident.
            cache.pop(key, None)
            cache[key] = value
            return value

    def _cache_put(self, cache: dict[str, dict], key: str, value: dict, max_entries: int) -> None:
        with self._cache_lock:
            cache.pop(key, None)
            cache[key] = value
            while len(cache) > max_entries:
                oldest_key = next(iter(cache))
                cache.pop(oldest_key, None)

    def _cache_evict(self, cache: dict[str, dict], key: str) -> None:
        with self._cache_lock:
            cache.pop(key, None)

    def _validate_identifier(self, value: str, label: str) -> str:
        ident = str(value or "").strip()
        if label == "problem":
            if len(ident) > 129 or not self.PROBLEM_IDENT_RE.fullmatch(ident):
                raise ValueError(PROBLEM_ID_RULE_MESSAGE)
            return ident
        if label == "user":
            if len(ident) > 64 or not self.USER_IDENT_RE.fullmatch(ident):
                raise ValueError(USERNAME_RULE_MESSAGE)
            return ident
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
            ensure_dir(bare.parent)
            run_cmd(["git", "init", "--bare", str(bare)])
        row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [slug])
        if row is None:
            self.db.execute(
                "INSERT OR IGNORE INTO problems(slug, name, repo_name, created_at) VALUES(?,?,?,?)",
                [slug, name, repo_name, now_iso()],
            )
            row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [slug])
        if row is not None:
            self._cache_put(self._problem_cache, slug, dict(row), self.PROBLEM_CACHE_MAX_ENTRIES)

    def set_problem_name(self, slug: str, name: str) -> dict:
        safe_slug = self._validate_identifier(slug, "problem")
        safe_name = str(name or "").strip()
        if not safe_name:
            raise ValueError("problem name is required")
        row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [safe_slug])
        if row is None:
            raise ValueError(f"Unknown problem: {safe_slug}")
        if str(row["name"] or "") != safe_name:
            self.db.execute("UPDATE problems SET name=? WHERE id=?", [safe_name, int(row["id"])])
            refreshed = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [safe_slug])
            if refreshed is not None:
                row = refreshed
        row_dict = dict(row)
        self._cache_put(self._problem_cache, safe_slug, row_dict, self.PROBLEM_CACHE_MAX_ENTRIES)
        return row_dict

    def ensure_user(self, username: str):
        username = self._validate_identifier(username, "user")
        cached = self._cache_get(self._user_cache, username)
        if cached is not None:
            try:
                cached_id = int(cached.get("id") or 0)
            except Exception:
                cached_id = 0
            if cached_id > 0:
                row = self.db.fetch_one("SELECT * FROM users WHERE id=? AND username=?", [cached_id, username])
                if row is not None:
                    row_dict = dict(row)
                    self._cache_put(self._user_cache, username, row_dict, self.USER_CACHE_MAX_ENTRIES)
                    return row_dict
            self._cache_evict(self._user_cache, username)
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
        self._cache_put(self._user_cache, username, row_dict, self.USER_CACHE_MAX_ENTRIES)
        return row_dict

    def _normalize_repo_role(self, role: str) -> str:
        safe_role = str(role or "").strip().lower()
        if safe_role not in self.REPO_ROLES:
            raise ValueError("invalid repo role")
        return safe_role

    def grant_repo_access(self, problem: str, username: str, role: str) -> None:
        p = self._problem_row(problem)
        u = self.ensure_user(username)
        safe_role = self._normalize_repo_role(role)
        self.db.execute(
            """
            INSERT INTO repo_acl(problem_id,user_id,role,created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(problem_id,user_id) DO UPDATE SET role=excluded.role
            """,
            [p["id"], u["id"], safe_role, now_iso()],
        )

    def _problem_row(self, slug: str):
        slug = self._validate_identifier(slug, "problem")
        cached = self._cache_get(self._problem_cache, slug)
        if cached is not None:
            try:
                cached_id = int(cached.get("id") or 0)
            except Exception:
                cached_id = 0
            if cached_id > 0:
                row = self.db.fetch_one("SELECT * FROM problems WHERE id=? AND slug=?", [cached_id, slug])
                if row is not None:
                    row_dict = dict(row)
                    self._cache_put(self._problem_cache, slug, row_dict, self.PROBLEM_CACHE_MAX_ENTRIES)
                    return row_dict
            self._cache_evict(self._problem_cache, slug)
        row = self.db.fetch_one("SELECT * FROM problems WHERE slug=?", [slug])
        if row is None:
            raise ValueError(f"Unknown problem: {slug}")
        row_dict = dict(row)
        self._cache_put(self._problem_cache, slug, row_dict, self.PROBLEM_CACHE_MAX_ENTRIES)
        return row_dict

    def _user_row(self, username: str):
        username = self._validate_identifier(username, "user")
        row = self.ensure_user(username)
        if row is None:
            raise ValueError(f"Unknown user: {username}")
        return row

    @contextmanager
    def _workspace_provision_lock(self, workspace_parent: Path, problem: str):
        ensure_dir(workspace_parent)
        lock_key = uuid.uuid5(uuid.NAMESPACE_URL, f"workspace:{problem}").hex
        lock_path = workspace_parent / f".{lock_key}.provision.lock"
        with self._exclusive_lock_file(lock_path, "workspace provision"):
            yield

    @contextmanager
    def _exclusive_lock_file(self, lock_path: Path, label: str):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not hasattr(os, "O_NOFOLLOW") and lock_path.is_symlink():
            raise ValueError(f"{label} lock path is invalid")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(lock_path), flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EPERM, errno.EACCES}:
                raise ValueError(f"{label} lock path is invalid") from exc
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def ensure_workspace(self, problem: str, username: str, refresh_status: bool = True) -> Path:
        u = self.ensure_user(username)
        p = self._problem_row(problem)

        username_key = str(u["username"])
        workspace = self.settings.workspace_root / username_key / problem
        bare = self.settings.bare_root / p["repo_name"]
        ws_row = self.db.fetch_one("SELECT id FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]])

        # Steady-state fast path: avoid provisioning lock when workspace and DB row already exist.
        if ws_row is not None and workspace.exists() and (workspace / ".git").is_dir():
            if refresh_status:
                with self.workspace_lock(workspace):
                    self._refresh_workspace_status_with_ids(workspace, int(p["id"]), int(u["id"]))
            return workspace

        with self._workspace_provision_lock(workspace.parent, problem):
            if workspace.exists():
                git_dir = workspace / ".git"
                if not git_dir.exists() or not git_dir.is_dir():
                    if workspace.is_symlink():
                        raise RuntimeError("workspace path is invalid")
                    shutil.rmtree(workspace, ignore_errors=False)
            if not workspace.exists():
                ensure_dir(workspace.parent)
                run_cmd(["git", "clone", str(bare), str(workspace)])
                if not (workspace / ".git").exists():
                    raise RuntimeError("workspace clone failed")
                self._ensure_main_checkout(workspace)
                self._seed_problem_repo(workspace)
            self._ensure_main_checkout(workspace)

            ws_row = self.db.fetch_one("SELECT id FROM workspaces WHERE problem_id=? AND user_id=?", [p["id"], u["id"]])
            if ws_row is None:
                try:
                    self.db.execute(
                        "INSERT INTO workspaces(problem_id,user_id,path,updated_at) VALUES(?,?,?,?)",
                        [p["id"], u["id"], str(workspace), now_iso()],
                    )
                except sqlite3.IntegrityError:
                    pass
            else:
                self.db.execute(
                    "UPDATE workspaces SET path=?, updated_at=? WHERE problem_id=? AND user_id=? AND path IS NOT ?",
                    [str(workspace), now_iso(), p["id"], u["id"], str(workspace)],
                )

            if refresh_status:
                self._refresh_workspace_status_with_ids(workspace, int(p["id"]), int(u["id"]))
        return workspace

    def _ensure_main_checkout(self, workspace: Path) -> None:
        has_local_main = run_cmd(
            ["git", "-C", str(workspace), "show-ref", "--verify", "--quiet", "refs/heads/main"]
        ).returncode == 0
        if has_local_main:
            run_cmd(["git", "-C", str(workspace), "switch", "--quiet", "main"])
            return

        has_origin_main = run_cmd(
            ["git", "-C", str(workspace), "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"]
        ).returncode == 0
        if has_origin_main:
            run_cmd(["git", "-C", str(workspace), "switch", "--quiet", "-c", "main", "--track", "origin/main"])
            return

        # Keep empty repositories on unborn main branch.
        run_cmd(["git", "-C", str(workspace), "symbolic-ref", "HEAD", "refs/heads/main"])

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
            "tests/generator",
            "third_party/testlib",
        ]
        for d in required_dirs:
            (workspace / d).mkdir(parents=True, exist_ok=True)
        seed_statement_sources(workspace)
        testlib = workspace / "third_party/testlib/testlib.h"
        if not testlib.exists():
            source = (Path(__file__).resolve().parents[2] / "third_party/upstream/testlib/testlib.h")
            if source.exists():
                testlib.write_bytes(source.read_bytes())
            else:
                testlib.write_text("// place fixed testlib.h copy here\n", encoding="utf-8")
        has_head = run_cmd(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"]).returncode == 0
        if not has_head:
            # Keep newly-created repositories at v0 without an automatic initial commit.
            run_cmd(["git", "-C", str(workspace), "symbolic-ref", "HEAD", "refs/heads/main"])

    def _refresh_workspace_status_with_ids(self, workspace: Path, problem_id: int, user_id: int) -> dict[str, str | int | None]:
        status = self.read_workspace_status(workspace)
        branch = str(status.get("branch") or "main")
        head = str(status.get("head_commit") or "")
        dirty = 1 if bool(status.get("dirty")) else 0
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

    def read_workspace_status(self, workspace: Path) -> dict[str, str | int | None]:
        status_v2 = run_cmd(["git", "-C", str(workspace), "status", "--porcelain=2", "--branch"])
        if status_v2.returncode == 0:
            branch, head, dirty = self._parse_status_v2(status_v2.stdout)
            return {"branch": branch, "head_commit": head, "dirty": dirty}

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
        expected_root = (self.settings.workspace_root / str(u["username"]) / problem).resolve()
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

    def _workspace_expected_path(self, username: str, problem: str) -> Path:
        return (self.settings.workspace_root / str(username) / str(problem)).resolve()

    @staticmethod
    def _assert_safe_delete_target(path: Path, root: Path, *, label: str) -> Path:
        target = path.resolve()
        root_resolved = root.resolve()
        if str(root_resolved) in {"", "/"}:
            raise RuntimeError(f"{label} root is unsafe: {root_resolved}")
        if target == root_resolved:
            raise RuntimeError(f"{label} target resolves to root: {target}")
        if root_resolved not in target.parents:
            raise RuntimeError(f"{label} target escapes root: {target}")
        return target

    @staticmethod
    def _is_active_status(raw: object) -> bool:
        status = str(raw or "").strip().lower()
        if not status:
            return False
        return status not in {"ok", "failed", "error", "done", "completed", "cancelled"}

    def delete_workspace(self, problem: str, username: str) -> dict[str, object]:
        safe_problem = self._validate_identifier(problem, "problem")
        safe_user = self._validate_identifier(username, "user")
        p = self._problem_row(safe_problem)
        u = self._user_row(safe_user)
        ws_row = self.db.fetch_one(
            "SELECT id,path FROM workspaces WHERE problem_id=? AND user_id=?",
            [int(p["id"]), int(u["id"])],
        )
        expected = self._workspace_expected_path(str(u["username"]), safe_problem)
        workspace_id = int(ws_row["id"]) if ws_row is not None else 0
        workspace_path = expected
        if ws_row is not None:
            row_path = Path(str(ws_row["path"] or "")).resolve()
            if row_path != expected:
                raise RuntimeError(f"workspace path mismatch for {safe_problem}/{safe_user}")
            workspace_path = row_path
        if workspace_id > 0:
            active_build = self.db.fetch_one(
                "SELECT id,status FROM builds WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",
                [workspace_id],
            )
            if active_build is not None and self._is_active_status(active_build["status"]):
                raise ValueError("workspace has active build jobs")
            active_preview = self.db.fetch_one(
                "SELECT id,status FROM previews WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",
                [workspace_id],
            )
            if active_preview is not None and self._is_active_status(active_preview["status"]):
                raise ValueError("workspace has active preview jobs")
            active_run = self.db.fetch_one(
                "SELECT id,status FROM runs WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",
                [workspace_id],
            )
            if active_run is not None and self._is_active_status(active_run["status"]):
                raise ValueError("workspace has active invocation jobs")

        workspace_path = self._assert_safe_delete_target(
            workspace_path,
            self.settings.workspace_root,
            label="workspace path",
        )

        removed = False
        with self._workspace_provision_lock(workspace_path.parent, safe_problem):
            if workspace_path.exists():
                if workspace_path.is_symlink():
                    raise RuntimeError("workspace path is invalid")
                shutil.rmtree(workspace_path, ignore_errors=False)
                removed = True
            if workspace_id > 0:
                self.db.execute(
                    "UPDATE workspaces SET path=?, branch=NULL, head_commit=NULL, dirty=0, updated_at=? WHERE id=?",
                    [str(workspace_path), now_iso(), workspace_id],
                )
        return {
            "problem": safe_problem,
            "username": safe_user,
            "workspace_path": str(workspace_path),
            "removed": bool(removed),
            "workspace_row_reset": bool(workspace_id > 0),
        }

    def delete_problem(self, problem: str) -> dict[str, object]:
        safe_problem = self._validate_identifier(problem, "problem")
        p = self._problem_row(safe_problem)
        problem_id = int(p["id"])
        repo_name = str(p["repo_name"] or "").strip()
        repo_name_path = Path(repo_name)
        expected_repo_name = f"{safe_problem}.git"
        if (
            (not repo_name)
            or repo_name in {".", ".."}
            or repo_name_path.is_absolute()
            or ("\\" in repo_name)
            or (repo_name != expected_repo_name)
        ):
            raise RuntimeError("problem repository name is unsafe")

        active_rows = self.db.fetch_all(
            """
            SELECT kind,id,status FROM (
                SELECT 'build' AS kind,id,status,created_at FROM builds WHERE problem_id=?
                UNION ALL
                SELECT 'preview' AS kind,id,status,created_at FROM previews WHERE problem_id=?
                UNION ALL
                SELECT 'run' AS kind,id,status,created_at FROM runs WHERE problem_id=?
            )
            ORDER BY created_at DESC
            LIMIT 64
            """,
            [problem_id, problem_id, problem_id],
        )
        for row in active_rows:
            if self._is_active_status(row["status"]):
                kind = str(row["kind"] or "").strip() or "job"
                raise ValueError(f"cannot delete problem while {kind} jobs are active")

        workspace_rows = self.db.fetch_all(
            """
            SELECT w.id,w.path,u.username
            FROM workspaces w
            JOIN users u ON u.id=w.user_id
            WHERE w.problem_id=?
            ORDER BY u.username ASC
            """,
            [problem_id],
        )
        workspace_paths: list[Path] = []
        for row in workspace_rows:
            username = str(row["username"] or "").strip()
            expected = self._workspace_expected_path(username, safe_problem)
            row_path = Path(str(row["path"] or "")).resolve()
            if row_path != expected:
                raise RuntimeError(f"workspace path mismatch for {safe_problem}/{username}")
            self._assert_safe_delete_target(
                row_path,
                self.settings.workspace_root,
                label=f"workspace path for {safe_problem}/{username}",
            )
            workspace_paths.append(row_path)

        artifact_root = self._assert_safe_delete_target(
            (self.settings.artifacts_root / safe_problem).resolve(),
            self.settings.artifacts_root,
            label=f"artifact root for {safe_problem}",
        )
        bare_repo_path = self._assert_safe_delete_target(
            (self.settings.bare_root / repo_name).resolve(),
            self.settings.bare_root,
            label=f"bare repo path for {safe_problem}",
        )

        def _tx(conn: sqlite3.Connection) -> list[str]:
            run_rows = conn.execute("SELECT id FROM runs WHERE problem_id=?", [problem_id]).fetchall()
            collected_run_ids = [str(row["id"] or "").strip() for row in run_rows if row is not None]
            conn.execute("DELETE FROM contest_problems WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM exports WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM runs WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM previews WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM builds WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM workspaces WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM repo_acl WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM audit_log WHERE problem_id=?", [problem_id])
            conn.execute("DELETE FROM problems WHERE id=?", [problem_id])
            return collected_run_ids

        run_ids: list[str] = list(self.db.write_transaction(_tx))
        try:
            from app.impl.runtime.config import config

            config.judgehost_task_service.forget_problem_tasks(safe_problem)
            config.judgehost_task_service.forget_domjudge_runs(run_ids)
        except Exception:
            pass

        fs_warnings: list[str] = []
        for ws_path in workspace_paths:
            try:
                with self._workspace_provision_lock(ws_path.parent, safe_problem):
                    if ws_path.exists():
                        if ws_path.is_symlink():
                            fs_warnings.append(f"workspace path is symlink: {ws_path}")
                            continue
                        shutil.rmtree(ws_path, ignore_errors=False)
            except Exception as exc:
                fs_warnings.append(f"workspace cleanup failed ({ws_path}): {exc}")
        try:
            if bare_repo_path.exists():
                if bare_repo_path.is_symlink():
                    fs_warnings.append(f"bare repo path is symlink: {bare_repo_path}")
                else:
                    shutil.rmtree(bare_repo_path, ignore_errors=False)
        except Exception as exc:
            fs_warnings.append(f"bare repo cleanup failed ({bare_repo_path}): {exc}")
        try:
            if artifact_root.exists():
                if artifact_root.is_symlink():
                    fs_warnings.append(f"artifact root is symlink: {artifact_root}")
                else:
                    shutil.rmtree(artifact_root, ignore_errors=False)
        except Exception as exc:
            fs_warnings.append(f"artifact cleanup failed ({artifact_root}): {exc}")

        with self._cache_lock:
            self._problem_cache.pop(safe_problem, None)
        return {
            "problem": safe_problem,
            "problem_id": problem_id,
            "workspace_count": len(workspace_paths),
            "fs_warnings": fs_warnings,
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
        with self._exclusive_lock_file(lock_path, "workspace"):
            yield
