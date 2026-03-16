from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TypeVar

logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)


_TxResult = TypeVar("_TxResult")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA auto_vacuum=INCREMENTAL;

CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    password_salt TEXT,
    password_iters INTEGER,
    password_updated_at TEXT,
    is_system_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS sudo_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS repo_acl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(problem_id, user_id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    branch TEXT,
    head_commit TEXT,
    dirty INTEGER NOT NULL DEFAULT 0,
    recent_verification_status TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(problem_id, user_id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS contests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS contest_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contest_id, user_id),
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS contest_problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER NOT NULL,
    idx TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    added_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contest_id, problem_id),
    UNIQUE(contest_id, idx),
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(added_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS contest_jobs (
    id TEXT PRIMARY KEY,
    contest_id INTEGER NOT NULL,
    actor_user_id INTEGER NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(actor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS contest_artifacts (
    id TEXT PRIMARY KEY,
    contest_id INTEGER NOT NULL,
    job_id TEXT,
    artifact_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(job_id) REFERENCES contest_jobs(id)
);

CREATE TABLE IF NOT EXISTS contest_properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_user_id INTEGER NOT NULL,
    UNIQUE(contest_id, key),
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(updated_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS contest_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL,
    UNIQUE(contest_id, key),
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(created_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS previews (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    workspace_id INTEGER,
    verification_id TEXT,
    source_commit TEXT,
    source_ref TEXT,
    status TEXT NOT NULL,
    summary_json TEXT,
    artifact_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    workspace_id INTEGER,
    source_commit TEXT,
    source_ref TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT,
    artifact_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    verification_id TEXT NOT NULL,
    workspace_id INTEGER,
    export_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_commit TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(problem_id) REFERENCES problems(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    problem_id INTEGER,
    action TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(actor_user_id) REFERENCES users(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id)
);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_user_id INTEGER,
    FOREIGN KEY(updated_by_user_id) REFERENCES users(id)
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_workspaces_problem_user ON workspaces(problem_id, user_id);
CREATE INDEX IF NOT EXISTS idx_contests_slug ON contests(slug);
CREATE INDEX IF NOT EXISTS idx_contests_owner ON contests(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_members_user ON contest_members(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_members_contest ON contest_members(contest_id, user_id);
CREATE INDEX IF NOT EXISTS idx_contest_problems_contest ON contest_problems(contest_id, problem_id);
CREATE INDEX IF NOT EXISTS idx_contest_problems_problem ON contest_problems(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_jobs_contest_created ON contest_jobs(contest_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_jobs_actor_created ON contest_jobs(actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_artifacts_contest_created ON contest_artifacts(contest_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_artifacts_job_created ON contest_artifacts(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_properties_contest_updated ON contest_properties(contest_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_attachments_contest_created ON contest_attachments(contest_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_problem_created ON previews(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_problem_workspace_created ON previews(problem_id, workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_workspace_created ON previews(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_problem_source_status_created ON previews(problem_id, source_commit, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_verification_created ON previews(verification_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_problem_created ON verifications(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_problem_workspace_created ON verifications(problem_id, workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_kind_status ON verifications(kind, status);
CREATE INDEX IF NOT EXISTS idx_verifications_workspace_kind_created ON verifications(workspace_id, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exports_problem_created ON exports(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exports_verification_created ON exports(verification_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_problem_created ON audit_log(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_created ON auth_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sudo_sessions_user_created ON sudo_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sudo_sessions_expires ON sudo_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_system_config_updated ON system_config(updated_at DESC);
"""

CURRENT_SCHEMA_COLUMNS: dict[str, tuple[str, ...]] = {
    "problems": ("id", "slug", "name", "repo_name", "created_at"),
    "users": (
        "id",
        "username",
        "password_hash",
        "password_salt",
        "password_iters",
        "password_updated_at",
        "is_system_admin",
        "created_at",
    ),
    "auth_sessions": ("id", "user_id", "token_hash", "created_at", "expires_at", "revoked_at"),
    "sudo_sessions": ("id", "user_id", "scope", "token_hash", "created_at", "expires_at", "revoked_at"),
    "repo_acl": ("id", "problem_id", "user_id", "role", "created_at"),
    "workspaces": (
        "id",
        "problem_id",
        "user_id",
        "path",
        "branch",
        "head_commit",
        "dirty",
        "recent_verification_status",
        "updated_at",
    ),
    "contests": ("id", "slug", "title", "owner_user_id", "created_at"),
    "contest_members": ("id", "contest_id", "user_id", "role", "created_at"),
    "contest_problems": ("id", "contest_id", "idx", "problem_id", "added_by_user_id", "created_at"),
    "contest_jobs": ("id", "contest_id", "actor_user_id", "job_type", "status", "summary_json", "created_at", "finished_at"),
    "contest_artifacts": (
        "id",
        "contest_id",
        "job_id",
        "artifact_type",
        "filename",
        "artifact_path",
        "sha256",
        "size_bytes",
        "created_at",
    ),
    "contest_properties": ("id", "contest_id", "key", "value_json", "updated_at", "updated_by_user_id"),
    "contest_attachments": ("id", "contest_id", "key", "rel_path", "created_at", "created_by_user_id"),
    "previews": (
        "id",
        "problem_id",
        "workspace_id",
        "verification_id",
        "source_commit",
        "source_ref",
        "status",
        "summary_json",
        "artifact_path",
        "created_at",
        "finished_at",
    ),
    "verifications": (
        "id",
        "problem_id",
        "workspace_id",
        "source_commit",
        "source_ref",
        "kind",
        "status",
        "summary_json",
        "artifact_path",
        "created_at",
        "finished_at",
    ),
    "exports": (
        "id",
        "problem_id",
        "verification_id",
        "workspace_id",
        "export_type",
        "filename",
        "sha256",
        "size_bytes",
        "source_commit",
        "created_at",
    ),
    "audit_log": ("id", "actor_user_id", "problem_id", "action", "details_json", "created_at"),
    "system_config": ("key", "value_json", "updated_at", "updated_by_user_id"),
}

LEGACY_SCHEMA_TABLES = ("runs", "builds")


class _IncompatibleSchemaError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DB:
    path: Path
    LOCK_RETRY_ATTEMPTS = 3
    LOCK_RETRY_BASE_SEC = 0.05
    SQLITE_BUSY_TIMEOUT_MS = 5000
    SQL_TRACE_ENABLED = False
    SQL_TRACE_TEXT_LIMIT = 256
    SQL_TRACE_JSON_FIELDS = ("summary_json", "details_json", "value_json")

    @staticmethod
    def _coerce_bool(value: object, default: bool = False) -> bool:
        if value is True:
            return True
        if value is False:
            return False
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n"}:
            return False
        return bool(default)

    def apply_runtime_values(self, values: object) -> None:
        enabled = getattr(values, "DB_SQL_TRACE_ENABLED", self.SQL_TRACE_ENABLED)
        self.sql_trace_enabled = self._coerce_bool(enabled, default=bool(self.SQL_TRACE_ENABLED))

    @staticmethod
    def _trace_sql_verb(text: str) -> str:
        match = re.match(r"^\s*([A-Za-z]+)", str(text or ""))
        return str(match.group(1) if match else "SQL").upper()

    @staticmethod
    def _trace_sql_table(text: str) -> str:
        raw = str(text or "")
        patterns = (
            r"^\s*UPDATE\s+([A-Za-z_][A-Za-z0-9_\.]*)",
            r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_\.]*)",
            r"^\s*DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_\.]*)",
            r"^\s*SELECT\b.*?\bFROM\s+([A-Za-z_][A-Za-z0-9_\.]*)",
            r"^\s*PRAGMA\s+([A-Za-z_][A-Za-z0-9_\.]*)",
        )
        for pattern in patterns:
            match = re.search(pattern, raw, flags=re.IGNORECASE)
            if match is not None:
                return str(match.group(1) or "").strip()
        return ""

    @classmethod
    def _truncate_trace_text(cls, text: str, *, limit: int | None = None) -> str:
        safe_text = str(text or "").strip()
        cap = max(64, int(limit or cls.SQL_TRACE_TEXT_LIMIT))
        if len(safe_text) <= cap:
            return safe_text
        return f"{safe_text[:cap].rstrip()}... [truncated; len={len(safe_text)}]"

    @classmethod
    def _summarize_traced_sql(cls, statement: str) -> str:
        text = " ".join(str(statement or "").strip().split())
        if not text:
            return ""
        lowered = text.lower()
        verb = cls._trace_sql_verb(text)
        table = cls._trace_sql_table(text)
        json_fields = [field for field in cls.SQL_TRACE_JSON_FIELDS if field in lowered]
        if json_fields:
            field_positions = [lowered.find(field) for field in json_fields if lowered.find(field) >= 0]
            prefix_end = min(field_positions) if field_positions else len(text)
            prefix = text[:prefix_end].rstrip(" ,")
            if not prefix:
                prefix = f"{verb} {table}".strip()
            prefix = cls._truncate_trace_text(prefix, limit=max(96, cls.SQL_TRACE_TEXT_LIMIT // 2))
            table_part = table or "?"
            fields_part = ",".join(json_fields)
            return f"{verb} {table_part} [json_fields={fields_part} len={len(text)}] {prefix} <redacted-json>"
        return cls._truncate_trace_text(text)

    @staticmethod
    def _should_retry_after_init(exc: sqlite3.OperationalError) -> bool:
        msg = str(exc or "").strip().lower()
        if not msg:
            return False
        return (
            "no such table" in msg
            or "unable to open database file" in msg
            or "disk i/o error" in msg
        )

    @staticmethod
    def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
        msg = str(exc or "").strip().lower()
        if not msg:
            return False
        return "database is locked" in msg or "database table is locked" in msg

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                self._init_current_schema()
                return
            except sqlite3.OperationalError as exc:
                if self._is_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise

    def _init_current_schema(self) -> None:
        if self._db_file_exists():
            try:
                with sqlite3.connect(self.path) as conn:
                    self._prepare_connection(conn)
                    self._validate_existing_schema(conn)
                    conn.executescript(SCHEMA_INDEXES)
                    conn.commit()
                    return
            except _IncompatibleSchemaError as exc:
                backup_path = self._backup_bad_db()
                logger.warning(
                    "db.init replaced incompatible db path=%s backup=%s error=%s",
                    self.path,
                    backup_path,
                    exc,
                )
            except sqlite3.DatabaseError as exc:
                if self._is_locked_error(exc):
                    raise
                backup_path = self._backup_bad_db()
                logger.warning(
                    "db.init replaced incompatible db path=%s backup=%s error=%s",
                    self.path,
                    backup_path,
                    exc,
                )
        with sqlite3.connect(self.path) as conn:
            self._prepare_connection(conn)
            conn.executescript(SCHEMA)
            self._validate_existing_schema(conn)
            conn.executescript(SCHEMA_INDEXES)
            conn.commit()

    def _db_file_exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def _prepare_connection(self, conn: sqlite3.Connection) -> None:
        self._install_sql_trace(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.SQLITE_BUSY_TIMEOUT_MS)}")

    def _validate_existing_schema(self, conn: sqlite3.Connection) -> None:
        table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {str(row[0]) for row in table_rows}
        legacy_tables = sorted(table for table in LEGACY_SCHEMA_TABLES if table in tables)
        if legacy_tables:
            raise _IncompatibleSchemaError(f"legacy tables present: {', '.join(legacy_tables)}")
        missing_tables: list[str] = []
        missing_columns: list[str] = []
        for table_name, expected_columns in CURRENT_SCHEMA_COLUMNS.items():
            if table_name not in tables:
                missing_tables.append(table_name)
                continue
            actual_columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
            for column_name in expected_columns:
                if column_name not in actual_columns:
                    missing_columns.append(f"{table_name}.{column_name}")
        if missing_tables or missing_columns:
            parts: list[str] = []
            if missing_tables:
                parts.append(f"missing tables: {', '.join(missing_tables)}")
            if missing_columns:
                parts.append(f"missing columns: {', '.join(missing_columns)}")
            raise _IncompatibleSchemaError("; ".join(parts))

    def _backup_bad_db(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self._unique_backup_path(self.path, timestamp)
        self.path.replace(backup_path)
        for suffix in ("-wal", "-shm"):
            sidecar_path = Path(f"{self.path}{suffix}")
            if sidecar_path.exists():
                sidecar_backup = self._unique_backup_path(sidecar_path, timestamp)
                sidecar_path.replace(sidecar_backup)
        return backup_path

    @staticmethod
    def _unique_backup_path(path: Path, timestamp: str) -> Path:
        candidate = path.with_name(f"{path.name}.{timestamp}.backup")
        seq = 1
        while candidate.exists():
            candidate = path.with_name(f"{path.name}.{timestamp}.{seq}.backup")
            seq += 1
        return candidate

    @contextmanager
    def conn(self):
        conn = sqlite3.connect(self.path)
        self._prepare_connection(conn)
        try:
            yield conn
        finally:
            conn.close()

    def _install_sql_trace(self, conn: sqlite3.Connection) -> None:
        enabled = getattr(self, "sql_trace_enabled", self.SQL_TRACE_ENABLED)
        if not bool(enabled):
            return
        conn_id = id(conn)
        pid = os.getpid()

        def _trace(statement: str) -> None:
            text = self._summarize_traced_sql(statement)
            if not text:
                return
            logger.info(
                "db.sql pid=%s tid=%s conn=%s sql=%s",
                pid,
                threading.get_ident(),
                conn_id,
                text,
            )

        conn.set_trace_callback(_trace)

    def write_transaction(
        self,
        fn: Callable[[sqlite3.Connection], _TxResult],
    ) -> _TxResult:
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(conn)
                    except Exception:
                        conn.rollback()
                        raise
                    conn.commit()
                    return result
            except sqlite3.OperationalError as exc:
                if attempt == 0 and self._should_retry_after_init(exc):
                    self.init()
                    continue
                if self._is_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise
        raise RuntimeError("write transaction failed")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        values = tuple(params)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    conn.execute(sql, values)
                    conn.commit()
                    return
            except sqlite3.OperationalError as exc:
                if attempt == 0 and self._should_retry_after_init(exc):
                    self.init()
                    continue
                if self._is_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        values = tuple(params)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    return conn.execute(sql, values).fetchone()
            except sqlite3.OperationalError as exc:
                if attempt == 0 and self._should_retry_after_init(exc):
                    self.init()
                    continue
                if self._is_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise
        return None

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        values = tuple(params)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    return conn.execute(sql, values).fetchall()
            except sqlite3.OperationalError as exc:
                if attempt == 0 and self._should_retry_after_init(exc):
                    self.init()
                    continue
                if self._is_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise
        return []
