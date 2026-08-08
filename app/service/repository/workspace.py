from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import re

from app.db import DB
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.disk.workspace_store import WorkspaceDiskStore
from app.service.platform.fs.op import copytree, ensure_dir, extract_git_archive, remove_symlinks
from app.service.platform.testlib_source import maintained_testlib_header
from app.service.platform.workspace_path import is_hidden_workspace_path
from app.setting import Settings
from app.service.platform.git_process import run_git
from app.service.repository.revision import workspace_revision_info
from app.service.verification.task_store import VerificationTaskStore

PROBLEM_ID_RULE_MESSAGE: str = "invalid problem id"
USERNAME_RULE_MESSAGE: str = "invalid username"
APP_PROBLEM_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")
APP_USER_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
APP_PROBLEM_ID_MAX_LEN = 64


def _workspace_transaction_path(workspace: Path) -> Path:
    return workspace.parent / f".{workspace.name}.merge-transaction.json"


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_transaction(path: Path, payload: dict[str, str]) -> None:
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)
    _fsync_directory(path.parent)


def _transaction_member(workspace: Path, name: str, prefix: str) -> Path:
    if not name or Path(name).name != name or not name.startswith(prefix):
        raise RuntimeError("invalid workspace transaction")
    return workspace.parent / name


def _remove_transaction_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def recover_workspace_swap(workspace: Path) -> None:
    journal = _workspace_transaction_path(workspace)
    if not journal.exists():
        return
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
        phase = str(payload["phase"])
        candidate = _transaction_member(workspace, str(payload["candidate"]), f".{workspace.name}.merge-candidate-")
        backup = _transaction_member(workspace, str(payload["backup"]), f".{workspace.name}.merge-backup-")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("workspace merge recovery metadata is invalid") from exc

    if phase == "new-active":
        _remove_transaction_tree(backup)
        _remove_transaction_tree(candidate)
    else:
        if backup.exists():
            _remove_transaction_tree(workspace)
            os.replace(backup, workspace)
            _fsync_directory(workspace.parent)
        elif not workspace.exists():
            raise RuntimeError("workspace merge recovery cannot find the previous workspace")
        _remove_transaction_tree(candidate)
    journal.unlink(missing_ok=True)
    _fsync_directory(workspace.parent)


def atomic_swap_workspace(workspace: Path, candidate: Path) -> None:
    if candidate.parent != workspace.parent or candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("workspace merge candidate is invalid")
    expected_prefix = f".{workspace.name}.merge-candidate-"
    if not candidate.name.startswith(expected_prefix):
        raise ValueError("workspace merge candidate is invalid")
    tx_id = uuid.uuid4().hex
    backup = workspace.parent / f".{workspace.name}.merge-backup-{tx_id}"
    journal = _workspace_transaction_path(workspace)
    payload = {
        "phase": "prepared",
        "candidate": candidate.name,
        "backup": backup.name,
    }
    _write_transaction(journal, payload)
    os.replace(workspace, backup)
    _fsync_directory(workspace.parent)
    payload["phase"] = "old-moved"
    _write_transaction(journal, payload)
    os.replace(candidate, workspace)
    _fsync_directory(workspace.parent)
    payload["phase"] = "new-active"
    _write_transaction(journal, payload)
    _remove_transaction_tree(backup)
    journal.unlink(missing_ok=True)
    _fsync_directory(workspace.parent)


def apply_runtime_values(values: RuntimeValues) -> None:
    global PROBLEM_ID_RULE_MESSAGE
    global USERNAME_RULE_MESSAGE
    global APP_PROBLEM_IDENT_RE
    global APP_USER_IDENT_RE
    global APP_PROBLEM_ID_MAX_LEN
    PROBLEM_ID_RULE_MESSAGE = str(values.PROBLEM_ID_RULE_MESSAGE)
    USERNAME_RULE_MESSAGE = str(values.USERNAME_RULE_MESSAGE)
    APP_PROBLEM_IDENT_RE = values.PROBLEM_IDENT_RE
    APP_USER_IDENT_RE = values.USER_IDENT_RE
    APP_PROBLEM_ID_MAX_LEN = int(values.PROBLEM_ID_MAX_LEN)

apply_runtime_values(build_runtime_values())


class WorkspaceService:
    IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    PROBLEM_IDENT_RE = APP_PROBLEM_IDENT_RE
    USER_IDENT_RE = APP_USER_IDENT_RE
    PROBLEM_ID_MAX_LEN = APP_PROBLEM_ID_MAX_LEN
    PROBLEM_CACHE_MAX_ENTRIES = 512
    USER_CACHE_MAX_ENTRIES = 2048
    REPO_ROLES = {"owner", "write", "read"}
    ACCESS_ROLES = {"admin", "owner", "write", "read"}

    def __init__(
        self,
        db: DB,
        settings: Settings,
        *,
        verification_task_store: VerificationTaskStore,
    ):
        self.db = db
        self.settings = settings
        self._store = WorkspaceDiskStore(db, verification_task_store=verification_task_store)
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

    def clear_identity_caches(self) -> None:
        with self._cache_lock:
            self._problem_cache.clear()
            self._user_cache.clear()

    def set_cached_user(self, username: str, row: dict[str, object]) -> None:
        safe_username = str(username or "").strip()
        if not safe_username:
            return
        with self._cache_lock:
            self._user_cache[safe_username] = dict(row)

    def _validate_identifier(self, value: str, label: str) -> str:
        ident = str(value or "").strip()
        if label == "problem":
            if len(ident) > self.PROBLEM_ID_MAX_LEN or not self.PROBLEM_IDENT_RE.fullmatch(ident):
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
            self.settings.artifacts_root,
            self.settings.cache_root,
        ]:
            ensure_dir(p)

    def ensure_problem(self, slug: str) -> None:
        slug = self._validate_identifier(slug, "problem")
        self.ensure_layout()
        repo_name = f"{slug}.git"
        bare = self.settings.bare_root / repo_name
        if not bare.exists():
            ensure_dir(bare.parent)
            run_git(["git", "init", "--bare", str(bare)])
        row = self._store.ensure_problem_row(slug=slug, repo_name=repo_name)
        self._cache_put(self._problem_cache, slug, dict(row), self.PROBLEM_CACHE_MAX_ENTRIES)

    def ensure_user(self, username: str):
        username = self._validate_identifier(username, "user")
        cached = self._cache_get(self._user_cache, username)
        if cached is not None:
            try:
                cached_id = int(cached["id"])
            except Exception:
                cached_id = 0
            if cached_id > 0:
                row = self._store.user_row_by_id_username(cached_id, username)
                if row is not None:
                    cached_row = dict(row)
                    self._cache_put(self._user_cache, username, cached_row, self.USER_CACHE_MAX_ENTRIES)
                    return cached_row
            self._cache_evict(self._user_cache, username)
        row_dict = dict(self._store.ensure_user_row(username))
        self._cache_put(self._user_cache, username, row_dict, self.USER_CACHE_MAX_ENTRIES)
        return row_dict

    def known_user(self, username: str) -> dict[str, object]:
        safe_username = self._validate_identifier(username, "user")
        cached = self._cache_get(self._user_cache, safe_username)
        if cached is not None:
            try:
                cached_id = int(cached["id"])
            except Exception:
                cached_id = 0
            if cached_id > 0:
                row = self._store.user_row_by_id_username(cached_id, safe_username)
                if row is not None:
                    cached_row = dict(row)
                    self._cache_put(self._user_cache, safe_username, cached_row, self.USER_CACHE_MAX_ENTRIES)
                    return cached_row
            self._cache_evict(self._user_cache, safe_username)
        row = self._store.user_row_by_username(safe_username)
        if row is None:
            raise ValueError(f"user {safe_username} not found; ask them to register first")
        row_dict = dict(row)
        self._cache_put(self._user_cache, safe_username, row_dict, self.USER_CACHE_MAX_ENTRIES)
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
        self._store.upsert_repo_access(int(p["id"]), int(u["id"]), safe_role)

    def _problem_row(self, slug: str):
        slug = self._validate_identifier(slug, "problem")
        cached = self._cache_get(self._problem_cache, slug)
        if cached is not None:
            try:
                cached_id = int(cached["id"])
            except Exception:
                cached_id = 0
            if cached_id > 0:
                row = self._store.problem_row_by_id_slug(cached_id, slug)
                if row is not None:
                    cached_row = dict(row)
                    self._cache_put(self._problem_cache, slug, cached_row, self.PROBLEM_CACHE_MAX_ENTRIES)
                    return cached_row
            self._cache_evict(self._problem_cache, slug)
        row = self._store.problem_row_by_slug(slug)
        if row is None:
            raise ValueError(f"Unknown problem: {slug}")
        row_dict = dict(row)
        self._cache_put(self._problem_cache, slug, row_dict, self.PROBLEM_CACHE_MAX_ENTRIES)
        return row_dict

    def page_identity(self, problem: str, username: str) -> tuple[int, int]:
        problem_id = self._store.problem_id_by_slug(self._validate_identifier(problem, "problem"))
        if problem_id is None:
            raise ValueError(f"Unknown problem: {problem}")
        user_id = self._store.user_id_by_username(self._validate_identifier(username, "user"))
        if user_id is None:
            raise ValueError(f"Unknown user: {username}")
        return (problem_id, user_id)

    def global_user_context(self, username: str) -> dict[str, object]:
        ensured = self.ensure_user(self._validate_identifier(username, "user"))
        return {
            "id": int(ensured["id"]),
            "username": str(ensured["username"]),
            "is_system_admin": int(ensured["is_system_admin"]),
        }

    def known_user_id(self, username: str) -> int | None:
        return self._store.user_id_by_username(self._validate_identifier(username, "user"))

    def known_problem_id(self, slug: str) -> int | None:
        return self._store.problem_id_by_slug(self._validate_identifier(slug, "problem"))

    def committed_statement_languages(self, problem: str) -> list[str]:
        problem_row = self._problem_row(problem)
        bare_repo = self.settings.bare_root / str(problem_row["repo_name"])
        proc = run_git(
            [
                "git",
                "--git-dir",
                str(bare_repo),
                "ls-tree",
                "-d",
                "--name-only",
                "refs/heads/main:statement-sections",
            ]
        )
        if proc.returncode != 0:
            return []
        return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})

    def default_problem_slug_for_username(self, username: str, *, limit: int = 1) -> str:
        user_id = self.known_user_id(username)
        if user_id is None:
            return ""
        if self.user_is_system_admin(int(user_id)):
            items = self._store.all_problem_slugs(limit=max(1, int(limit)))
        else:
            items = self._store.user_problem_slugs(user_id, limit=max(1, int(limit)))
        return items[0] if items else ""

    def accessible_problem_slugs(self, user_id: int, *, limit: int = 1) -> list[str]:
        if self.user_is_system_admin(int(user_id)):
            return self._store.all_problem_slugs(limit=max(1, int(limit)))
        return self._store.user_problem_slugs(int(user_id), limit=max(1, int(limit)))

    def accessible_problem_slugs_by_leaf(self, user_id: int, leaf: str, *, limit: int = 20) -> list[str]:
        safe_leaf = str(leaf or "").strip()
        if not safe_leaf:
            return []
        if self.user_is_system_admin(int(user_id)):
            return self._store.problem_slugs_by_leaf(safe_leaf, limit=max(1, int(limit)))
        return self._store.user_problem_slugs_by_leaf(int(user_id), safe_leaf, limit=max(1, int(limit)))

    def problem_slugs_by_leaf(self, leaf: str, *, limit: int = 20) -> list[str]:
        safe_leaf = str(leaf or "").strip()
        if not safe_leaf:
            return []
        return self._store.problem_slugs_by_leaf(safe_leaf, limit=max(1, int(limit)))

    def participating_problem_rows(self, user_id: int, *, limit: int) -> list[dict[str, object]]:
        if self.user_is_system_admin(int(user_id)):
            return self._store.all_problem_rows(int(user_id), limit=max(1, int(limit)))
        return self._store.user_problem_rows(int(user_id), limit=max(1, int(limit)))

    def workspace_path(self, problem_id: int, workspace_id: int) -> str:
        return self._store.workspace_path(int(problem_id), int(workspace_id))

    def record_audit_event(
        self,
        *,
        actor_user_id: int | None,
        problem_id: int | None,
        action: str,
        details: dict[str, object],
    ) -> None:
        self._store.append_audit_event(
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            action=action,
            details=details,
        )

    @classmethod
    def _access_context_for_role(
        cls,
        role: str | None,
        *,
        system_admin: bool,
    ) -> dict[str, object]:
        if system_admin:
            return {
                "role": "admin",
                "can_read": True,
                "can_write": True,
                "can_manage": True,
                "read_block_reason": "",
                "write_block_reason": "",
                "manage_block_reason": "",
            }
        if role is None:
            return {
                "role": "none",
                "can_read": False,
                "can_write": False,
                "can_manage": False,
                "read_block_reason": "you do not have access to this problem",
                "write_block_reason": "write access required",
                "manage_block_reason": "owner or admin access required",
            }
        if role not in cls.ACCESS_ROLES:
            raise RuntimeError("invalid repo role")
        can_write = role in {"admin", "owner", "write"}
        return {
            "role": role,
            "can_read": True,
            "can_write": can_write,
            "can_manage": role in {"admin", "owner"},
            "read_block_reason": "",
            "write_block_reason": "" if can_write else "read-only access",
            "manage_block_reason": "" if role in {"admin", "owner"} else "owner or admin access required",
        }

    def access_context(self, problem_id: int, user_id: int) -> dict[str, object]:
        safe_user_id = int(user_id)
        return self._access_context_for_role(
            self._store.repo_role(int(problem_id), safe_user_id),
            system_admin=self.user_is_system_admin(safe_user_id),
        )

    def access_contexts(
        self,
        problem_ids: list[int],
        user_id: int,
    ) -> dict[int, dict[str, object]]:
        safe_problem_ids = list(
            dict.fromkeys(int(problem_id) for problem_id in problem_ids)
        )
        if not safe_problem_ids:
            return {}
        safe_user_id = int(user_id)
        system_admin = self.user_is_system_admin(safe_user_id)
        roles = {} if system_admin else self._store.repo_roles(safe_problem_ids, safe_user_id)
        return {
            problem_id: self._access_context_for_role(
                roles.get(problem_id),
                system_admin=system_admin,
            )
            for problem_id in safe_problem_ids
        }

    def access_entries(self, problem_id: int) -> list[dict[str, str]]:
        entries = self._store.problem_acl_entries(int(problem_id))
        for entry in entries:
            if entry["role"] not in self.REPO_ROLES:
                raise RuntimeError("invalid repo role")
        return entries

    def owner_count(self, problem_id: int) -> int:
        return self._store.problem_owner_count(int(problem_id))

    def set_repo_access_for_problem_id(self, problem_id: int, username: str, role: str) -> dict[str, object]:
        target_user = self.known_user(username)
        safe_role = self._normalize_repo_role(role)
        existing = self._store.repo_access_row(int(problem_id), int(target_user["id"]))
        existing_role = "" if existing is None else str(existing["role"])
        if safe_role == "owner" or existing_role == "owner":
            raise ValueError("owner access is fixed and cannot be transferred")
        self._store.upsert_repo_access(int(problem_id), int(target_user["id"]), safe_role)
        return {
            "target_user_id": int(target_user["id"]),
            "target_username": str(target_user["username"]),
            "previous_role": existing_role,
            "role": safe_role,
        }

    def revoke_repo_access_for_problem_id(self, problem_id: int, username: str) -> dict[str, object]:
        target_user = self.known_user(username)
        existing = self._store.repo_access_row(int(problem_id), int(target_user["id"]))
        if existing is None:
            raise ValueError("access entry not found")
        existing_role = str(existing["role"])
        if existing_role == "owner":
            raise ValueError("owner access is fixed and cannot be transferred")
        self._store.delete_repo_access(int(problem_id), int(target_user["id"]))
        return {
            "target_user_id": int(target_user["id"]),
            "target_username": str(target_user["username"]),
            "previous_role": existing_role,
        }

    def user_is_system_admin(self, user_id: int) -> bool:
        return self._store.is_system_admin(int(user_id))

    def _user_row(self, username: str):
        username = self._validate_identifier(username, "user")
        row = self.known_user(username)
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
    def _exclusive_lock_file(self, lock_path: Path, label: str, *, remove_after: bool = True):
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
        if remove_after:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _workspace_lock_path(workspace: Path) -> Path:
        lock_key = uuid.uuid5(uuid.NAMESPACE_URL, str(workspace.resolve(strict=False))).hex
        return workspace.parent / f".{lock_key}.workspace.lock"

    def ensure_workspace(self, problem: str, username: str, refresh_status: bool = True) -> Path:
        u = self.ensure_user(username)
        p = self._problem_row(problem)

        username_key = str(u["username"])
        workspace = self.settings.workspace_root / username_key / problem
        lock_path = self._workspace_lock_path(workspace)
        with self._exclusive_lock_file(lock_path, "workspace", remove_after=False):
            recover_workspace_swap(workspace)
        bare = self.settings.bare_root / p["repo_name"]
        workspace_id = self._store.workspace_id(int(p["id"]), int(u["id"]))

        # Steady-state fast path: avoid provisioning lock when workspace and DB row already exist.
        if workspace_id is not None and workspace.exists() and (workspace / ".git").is_dir():
            if refresh_status:
                with self.workspace_lock(workspace):
                    self._ensure_main_checkout(workspace)
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
                run_git(["git", "clone", str(bare), str(workspace)])
                if not (workspace / ".git").exists():
                    raise RuntimeError("workspace clone failed")
                self._ensure_main_checkout(workspace)
                self._seed_problem_repo(workspace)
            self._ensure_main_checkout(workspace)

            workspace_id = self._store.workspace_id(int(p["id"]), int(u["id"]))
            if workspace_id is None:
                try:
                    self._store.ensure_workspace_row(int(p["id"]), int(u["id"]), str(workspace))
                except sqlite3.IntegrityError:
                    pass
            else:
                self._store.update_workspace_path(int(p["id"]), int(u["id"]), str(workspace))

            if refresh_status:
                self._refresh_workspace_status_with_ids(workspace, int(p["id"]), int(u["id"]))
        return workspace

    def _ensure_main_checkout(self, workspace: Path) -> None:
        has_head = run_git(
            ["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"]
        ).returncode == 0
        has_local_main = run_git(
            ["git", "-C", str(workspace), "show-ref", "--verify", "--quiet", "refs/heads/main"]
        ).returncode == 0
        if has_local_main:
            run_git(["git", "-C", str(workspace), "switch", "--quiet", "main"])
            return

        has_origin_main = run_git(
            ["git", "-C", str(workspace), "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"]
        ).returncode == 0
        if (not has_origin_main) and (not has_head):
            run_git(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "fetch",
                    "--quiet",
                    "origin",
                    "refs/heads/main:refs/remotes/origin/main",
                ]
            )
            has_origin_main = run_git(
                ["git", "-C", str(workspace), "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"]
            ).returncode == 0
        if has_origin_main:
            if not has_head:
                # The initial unborn workspace contains seeded untracked files.
                # Once origin/main exists, remote Git history is authoritative.
                run_git(["git", "-C", str(workspace), "symbolic-ref", "HEAD", "refs/heads/main"])
                run_git(["git", "-C", str(workspace), "reset", "--hard", "origin/main"])
                return
            run_git(["git", "-C", str(workspace), "switch", "--quiet", "-C", "main", "--track", "origin/main"])
            return

        # Keep empty repositories on unborn main branch.
        run_git(["git", "-C", str(workspace), "symbolic-ref", "HEAD", "refs/heads/main"])

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
        testlib = workspace / "third_party/testlib/testlib.h"
        if not testlib.exists():
            source = maintained_testlib_header(repo_root=Path(__file__).resolve().parents[3])
            testlib.write_bytes(source.read_bytes())
        has_head = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"]).returncode == 0
        if not has_head:
            # Keep newly-created repositories at v0 without an automatic initial commit.
            run_git(["git", "-C", str(workspace), "symbolic-ref", "HEAD", "refs/heads/main"])

    def _refresh_workspace_status_with_ids(self, workspace: Path, problem_id: int, user_id: int) -> dict[str, str | int | None]:
        status = self.read_workspace_status(workspace)
        branch = branch if isinstance(branch := status.get("branch"), str) and branch else "main"
        head = head if isinstance(head := status.get("head_commit"), str) else ""
        dirty = 1 if bool(status.get("dirty")) else 0
        revision = workspace_revision_info(
            workspace,
            branch,
            workspace_head=head,
            workspace_dirty=bool(dirty),
        )
        self._store.update_workspace_status(
            problem_id,
            user_id,
            branch=branch,
            head_commit=head,
            dirty=dirty,
            revision_local=revision["local"],
            revision_upstream=revision["upstream"],
            revision_missing=1 if revision["missing"] else 0,
            revision_highlight=1 if revision["highlight"] else 0,
            revision_upstream_higher=1 if revision["upstream_higher"] else 0,
            revision_ahead_count=revision["ahead_count"],
            revision_behind_count=revision["behind_count"],
        )
        return {"branch": branch, "head_commit": head, "dirty": dirty}

    def refresh_workspace_status_with_ids(self, workspace: Path, problem_id: int, user_id: int) -> dict[str, str | int | None]:
        return self._refresh_workspace_status_with_ids(workspace, int(problem_id), int(user_id))

    def refresh_workspace_status_by_path(self, workspace: Path) -> dict[str, str | int | None] | None:
        safe_workspace = Path(workspace).resolve()
        if not safe_workspace.exists() or not safe_workspace.is_dir():
            return None
        git_dir = safe_workspace / ".git"
        if not git_dir.exists() or not git_dir.is_dir():
            return None
        identity = self._store.workspace_identity_by_path(str(safe_workspace))
        if identity is None:
            return None
        return self._refresh_workspace_status_with_ids(
            safe_workspace,
            int(identity["problem_id"]),
            int(identity["user_id"]),
        )

    def read_workspace_status(self, workspace: Path) -> dict[str, str | int | None]:
        status_v2 = run_git(["git", "-C", str(workspace), "status", "--porcelain=2", "--branch"])
        if status_v2.returncode == 0:
            branch, head, dirty = self._parse_status_v2(status_v2.stdout)
            return {"branch": branch, "head_commit": head, "dirty": dirty}

        status_out = run_git(["git", "-C", str(workspace), "status", "--short", "--branch"]).stdout
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
        head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
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

            if self._status_path_is_hidden(path):
                continue
            dirty = 1
            break
        return branch, head, dirty

    def workspace_context(self, problem: str, username: str, include_recent: bool = True) -> dict:
        p = self._problem_row(problem)
        u = self._user_row(username)
        ws = self._store.workspace_row(int(p["id"]), int(u["id"]))
        if ws is None:
            self.ensure_workspace(problem, username)
            ws = self._store.workspace_row(int(p["id"]), int(u["id"]))
        if ws is None:
            raise RuntimeError(f"workspace not available for {problem}/{username}")
        ws_path = Path(ws["path"]).resolve()
        expected_root = (self.settings.workspace_root / str(u["username"]) / problem).resolve()
        if ws_path != expected_root:
            raise RuntimeError(f"workspace path mismatch for {problem}/{username}")
        if not ws_path.exists() or not ws_path.is_dir():
            raise RuntimeError(f"workspace path missing for {problem}/{username}")
        git_dir = ws_path / ".git"
        if not git_dir.exists() or not git_dir.is_dir():
            raise RuntimeError(f"workspace git metadata missing for {problem}/{username}")
        latest_artifact_verification = None
        if include_recent:
            recent_verification = self._store.latest_workspace_artifact_verification(int(ws["id"]))
            if recent_verification is not None:
                latest_artifact_verification = {
                    "id": recent_verification["id"],
                    "status": recent_verification["status"],
                    "created_at": recent_verification["created_at"],
                }
        return {
            "problem": dict(p),
            "user": dict(u),
            "workspace": dict(ws),
            "latest_artifact_verification": latest_artifact_verification,
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
        ws_row = self._store.workspace_row(int(p["id"]), int(u["id"]))
        expected = self._workspace_expected_path(str(u["username"]), safe_problem)
        workspace_id = int(ws_row["id"]) if ws_row is not None else 0
        workspace_path = expected
        if ws_row is not None:
            row_path = Path(str(ws_row["path"] or "")).resolve()
            if row_path != expected:
                raise RuntimeError(f"workspace path mismatch for {safe_problem}/{safe_user}")
            workspace_path = row_path
        if workspace_id > 0:
            active_verification_status = self._store.latest_workspace_job_status(workspace_id, kind="verification")
            if self._is_active_status(active_verification_status):
                raise ValueError("workspace has active verifications")
            active_preview_status = self._store.latest_workspace_job_status(workspace_id, kind="preview")
            if self._is_active_status(active_preview_status):
                raise ValueError("workspace has active preview jobs")
            active_stage_status = self._store.latest_workspace_job_status(workspace_id, kind="all")
            if self._is_active_status(active_stage_status):
                raise ValueError("workspace has active verification jobs")

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
                self._store.reset_workspace_row(workspace_id, str(workspace_path))
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
        expected_repo_name = f"{safe_problem}.git"
        if (
            (not repo_name)
            or repo_name in {".", ".."}
            or Path(repo_name).is_absolute()
            or ("\\" in repo_name)
            or (repo_name != expected_repo_name)
        ):
            raise RuntimeError("problem repository name is unsafe")

        active_rows = self._store.problem_active_job_rows(problem_id, limit=64)
        for row in active_rows:
            if self._is_active_status(row["status"]):
                kind = str(row["kind"] or "").strip() or "job"
                raise ValueError(f"cannot delete problem while {kind} jobs are active")

        workspace_rows = self._store.workspace_rows_for_problem(problem_id)
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

        bare_repo_path = self._assert_safe_delete_target(
            (self.settings.bare_root / repo_name).resolve(),
            self.settings.bare_root,
            label=f"bare repo path for {safe_problem}",
        )

        run_ids = self._store.delete_problem_metadata(problem_id)
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
        proc = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", f"{commit_ref}^{{commit}}"])
        commit = proc.stdout.strip()
        if proc.returncode != 0 or not commit:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(detail or f"unable to resolve commit reference: {commit_ref}")
        return commit

    def _workspace_dirty(self, workspace: Path) -> bool:
        proc = run_git(["git", "-C", str(workspace), "status", "--porcelain"])
        return self._is_status_dirty(proc.stdout)

    def _is_status_dirty(self, status_output: str) -> bool:
        for raw in status_output.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## "):
                continue
            path = line[3:].strip() if len(line) >= 4 else line
            if self._status_path_is_hidden(path):
                continue
            return True
        return False

    @staticmethod
    def _status_path_is_hidden(raw: str) -> bool:
        payload = str(raw or "").strip().strip('"')
        if not payload:
            return False
        if " -> " in payload:
            return any(WorkspaceService._status_path_is_hidden(part) for part in payload.split(" -> "))
        parts = tuple(
            part
            for part in PurePosixPath(payload.replace("\\", "/")).parts
            if part not in {"", "."}
        )
        return is_hidden_workspace_path(parts)

    def create_snapshot(
        self,
        workspace: Path,
        commit: str | None,
        workspace_head: str | None = None,
        workspace_dirty: bool | None = None,
    ) -> Path:
        snapshot_id = f"snapshot-{uuid.uuid4().hex[:12]}"
        snap = self.settings.cache_root / "runtime" / "snapshots" / snapshot_id / "src"
        ensure_dir(snap.parent)
        if commit:
            self._extract_commit_snapshot(workspace, commit, snap)
        else:
            # Fast path for clean workspaces: archive current HEAD instead of copying entire tree.
            dirty = workspace_dirty if workspace_dirty is not None else self._workspace_dirty(workspace)
            if not dirty:
                head = (workspace_head or "").strip()
                if not head:
                    head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
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
        lock_path = self._workspace_lock_path(workspace)
        with self._exclusive_lock_file(lock_path, "workspace", remove_after=False):
            recover_workspace_swap(workspace)
            try:
                yield
            except Exception:
                try:
                    recover_workspace_swap(workspace)
                    self.refresh_workspace_status_by_path(workspace)
                except Exception:
                    pass
                raise
            self.refresh_workspace_status_by_path(workspace)
