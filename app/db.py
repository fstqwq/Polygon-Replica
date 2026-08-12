"""SQLite schema and connection helpers."""

# The canonical DDL and its validation manifest must remain reviewable together.
# pylint: disable=too-many-lines

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TypeVar

from app.config.model import ConfigValues
from app.main_util import is_sqlite_locked_error, summarize_traced_sql
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)


_TxResult = TypeVar("_TxResult")


class SchemaRequirementsError(RuntimeError):
    """Report current schema objects required before runtime may start."""

    def __init__(
        self,
        *,
        missing_tables: list[str],
        missing_columns: list[str],
        missing_indexes: list[str],
    ) -> None:
        self.missing_tables = tuple(missing_tables)
        self.missing_columns = tuple(missing_columns)
        self.missing_indexes = tuple(missing_indexes)
        parts: list[str] = []
        if missing_tables:
            parts.append(f"missing tables: {', '.join(missing_tables)}")
        if missing_columns:
            parts.append(f"missing columns: {', '.join(missing_columns)}")
        if missing_indexes:
            parts.append(f"missing indexes: {', '.join(missing_indexes)}")
        super().__init__("; ".join(parts))


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA auto_vacuum=INCREMENTAL;

CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    repo_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    email_normalized TEXT NOT NULL DEFAULT '',
    email_verified_at TEXT,
    password_hash TEXT,
    password_salt TEXT,
    password_iters INTEGER,
    password_updated_at TEXT,
    is_system_admin INTEGER NOT NULL DEFAULT 0,
    is_banned INTEGER NOT NULL DEFAULT 0,
    banned_at TEXT,
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

CREATE TABLE IF NOT EXISTS pending_registrations (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    email TEXT NOT NULL,
    email_normalized TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    password_iters INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    request_ip TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    terms_accepted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    bucket_key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    window_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_registration_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    identity_hash TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    desktop_id TEXT NOT NULL,
    init_ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(user_id, identity_hash),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS agent_access_requests (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    delivered_at TEXT,
    token_id TEXT NOT NULL DEFAULT '',
    delivery_token TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(agent_session_id) REFERENCES agent_sessions(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id)
);

CREATE TABLE IF NOT EXISTS agent_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    agent_session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    problem_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY(agent_session_id) REFERENCES agent_sessions(id),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id)
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
    revision_local INTEGER,
    revision_upstream INTEGER,
    revision_missing INTEGER NOT NULL DEFAULT 1,
    revision_highlight INTEGER NOT NULL DEFAULT 1,
    revision_upstream_higher INTEGER NOT NULL DEFAULT 0,
    revision_ahead_count INTEGER,
    revision_behind_count INTEGER,
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
    status TEXT NOT NULL DEFAULT 'draft',
    source_generation INTEGER NOT NULL DEFAULT 1,
    location TEXT NOT NULL DEFAULT '',
    date_text TEXT NOT NULL DEFAULT '',
    statement_default_language TEXT NOT NULL DEFAULT 'english',
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
    position INTEGER NOT NULL,
    label TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    statement_folder TEXT NOT NULL DEFAULT '',
    added_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contest_id, problem_id),
    UNIQUE(contest_id, position),
    UNIQUE(contest_id, label),
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
    source_generation INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(actor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS contest_build_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    contest_problem_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    label TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    statement_folder TEXT NOT NULL DEFAULT '',
    source_commit TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    materialization_id TEXT,
    archive_sha256 TEXT,
    UNIQUE(job_id,contest_problem_id),
    FOREIGN KEY(job_id) REFERENCES contest_jobs(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(materialization_id) REFERENCES problem_package_materializations(id)
);

CREATE TABLE IF NOT EXISTS contest_artifacts (
    id TEXT PRIMARY KEY,
    contest_id INTEGER NOT NULL,
    job_id TEXT,
    artifact_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(job_id) REFERENCES contest_jobs(id)
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
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY(verification_id) REFERENCES verifications(id)
);

CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    workspace_id INTEGER,
    signature TEXT NOT NULL DEFAULT '',
    source_commit TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','ok','failed','cancelled')),
    fail_reason TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'pass-fail',
    pass_limit INTEGER NOT NULL DEFAULT 1,
    run_config_json TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    failed_step TEXT NOT NULL DEFAULT '',
    failed_check TEXT NOT NULL DEFAULT '',
    failed_test TEXT NOT NULL DEFAULT '',
    sanity_status TEXT NOT NULL DEFAULT '',
    sanity_checked_count INTEGER NOT NULL DEFAULT 0,
    validation_status TEXT NOT NULL DEFAULT '',
    validated_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS verification_selected_tests (
    verification_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    test_name TEXT NOT NULL,
    PRIMARY KEY(verification_id, ordinal),
    UNIQUE(verification_id, test_name),
    FOREIGN KEY(verification_id) REFERENCES verifications(id)
);

CREATE TABLE IF NOT EXISTS verification_source_paths (
    verification_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY(verification_id, ordinal),
    FOREIGN KEY(verification_id) REFERENCES verifications(id)
);

CREATE TABLE IF NOT EXISTS verification_sanity_checks (
    verification_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    checked_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(verification_id, ordinal),
    FOREIGN KEY(verification_id) REFERENCES verifications(id)
);

CREATE TABLE IF NOT EXISTS verification_sanity_check_messages (
    verification_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    severity TEXT NOT NULL,
    test_name TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    PRIMARY KEY(verification_id, check_name, ordinal),
    FOREIGN KEY(verification_id) REFERENCES verifications(id)
);

CREATE TABLE IF NOT EXISTS verification_tests_meta (
    verification_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    test_name TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    is_sample INTEGER NOT NULL DEFAULT 0,
    sample_input_custom INTEGER NOT NULL DEFAULT 0,
    sample_output_custom INTEGER NOT NULL DEFAULT 0,
    sample_output_validate INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    command_text TEXT NOT NULL DEFAULT '',
    payload_source_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(verification_id, ordinal),
    UNIQUE(verification_id, test_name),
    FOREIGN KEY(verification_id) REFERENCES verifications(id)
);

CREATE TABLE IF NOT EXISTS verification_tasks (
    id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL,
    predecessor_task_id TEXT,
    task_kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    program_id TEXT NOT NULL,
    test_name TEXT NOT NULL,
    expected_behavior TEXT NOT NULL,
    final_status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    finished_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(verification_id) REFERENCES verifications(id),
    FOREIGN KEY(predecessor_task_id) REFERENCES verification_tasks(id)
);

CREATE TABLE IF NOT EXISTS verification_artifact_refs (
    verification_id TEXT NOT NULL,
    test_name TEXT NOT NULL,
    input_ref TEXT NOT NULL DEFAULT '',
    answer_ref TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(verification_id, test_name),
    FOREIGN KEY(verification_id) REFERENCES verifications(id)
);

CREATE TABLE IF NOT EXISTS verification_task_diagnostics (
    task_id TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL DEFAULT '{"items":[]}',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES verification_tasks(id)
);

CREATE TABLE IF NOT EXISTS problem_package_materializations (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    source_commit TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    source_digest TEXT NOT NULL,
    archive_rel_path TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    archive_size_bytes INTEGER NOT NULL,
    verification_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','unavailable')),
    created_at TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    unavailable_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(problem_id,source_commit),
    FOREIGN KEY(problem_id) REFERENCES problems(id)
);

CREATE TABLE IF NOT EXISTS problem_package_builds (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    source_commit TEXT NOT NULL,
    verification_id TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    materialization_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(problem_id,source_commit),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(materialization_id) REFERENCES problem_package_materializations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    materialization_id TEXT NOT NULL,
    export_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    archive_rel_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_commit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(materialization_id,export_type),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(materialization_id) REFERENCES problem_package_materializations(id)
);

CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL,
    actor_user_id INTEGER NOT NULL,
    export_type TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    materialization_id TEXT,
    export_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(actor_user_id) REFERENCES users(id),
    FOREIGN KEY(materialization_id) REFERENCES problem_package_materializations(id) ON DELETE SET NULL,
    FOREIGN KEY(export_id) REFERENCES exports(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_user_id INTEGER,
    FOREIGN KEY(updated_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS smtp_config (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 587,
    username TEXT NOT NULL DEFAULT '',
    password_ciphertext TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by_user_id INTEGER,
    FOREIGN KEY(updated_by_user_id) REFERENCES users(id)
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_workspaces_problem_user ON workspaces(problem_id, user_id);
CREATE INDEX IF NOT EXISTS idx_repo_acl_user_problem ON repo_acl(user_id, problem_id);
CREATE INDEX IF NOT EXISTS idx_contests_slug ON contests(slug);
CREATE INDEX IF NOT EXISTS idx_contests_owner ON contests(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_members_user ON contest_members(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_members_contest ON contest_members(contest_id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contest_members_single_owner ON contest_members(contest_id) WHERE role='owner';
CREATE INDEX IF NOT EXISTS idx_contest_problems_contest ON contest_problems(contest_id, position);
CREATE INDEX IF NOT EXISTS idx_contest_problems_problem ON contest_problems(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_jobs_contest_created ON contest_jobs(contest_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_jobs_actor_created ON contest_jobs(actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_build_items_job_position ON contest_build_items(job_id,position);
CREATE INDEX IF NOT EXISTS idx_contest_build_items_materialization ON contest_build_items(materialization_id);
CREATE INDEX IF NOT EXISTS idx_contest_artifacts_contest_created ON contest_artifacts(contest_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_artifacts_job_created ON contest_artifacts(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contest_attachments_contest_created ON contest_attachments(contest_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_problem_created ON previews(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_problem_workspace_created ON previews(problem_id, workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_workspace_created ON previews(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_problem_source_status_created ON previews(problem_id, source_commit, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_verification_created ON previews(verification_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_problem_created ON verifications(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_problem_workspace_created ON verifications(problem_id, workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_problem_signature_created ON verifications(problem_id, signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_problem_workspace_signature_created ON verifications(problem_id, workspace_id, signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifications_kind_status ON verifications(kind, status);
CREATE INDEX IF NOT EXISTS idx_verifications_workspace_kind_created ON verifications(workspace_id, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verification_selected_tests_verification_ordinal ON verification_selected_tests(verification_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_verification_source_paths_verification_ordinal ON verification_source_paths(verification_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_verification_sanity_checks_verification_ordinal ON verification_sanity_checks(verification_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_verification_sanity_check_messages_verification_check ON verification_sanity_check_messages(verification_id, check_name, ordinal);
CREATE INDEX IF NOT EXISTS idx_verification_tests_meta_verification_ordinal ON verification_tests_meta(verification_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_verification_tasks_verification_task ON verification_tasks(verification_id, task_kind, source_path, test_name, id);
CREATE INDEX IF NOT EXISTS idx_verification_tasks_verification_predecessor ON verification_tasks(verification_id, predecessor_task_id);
CREATE INDEX IF NOT EXISTS idx_verification_tasks_predecessor ON verification_tasks(predecessor_task_id);
CREATE INDEX IF NOT EXISTS idx_verification_tasks_verification_final ON verification_tasks(verification_id, final_status, task_kind);
CREATE INDEX IF NOT EXISTS idx_verification_artifact_refs_verification_updated ON verification_artifact_refs(verification_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_verification_task_diagnostics_updated ON verification_task_diagnostics(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_problem_package_materializations_problem_revision ON problem_package_materializations(problem_id,revision_number DESC);
CREATE INDEX IF NOT EXISTS idx_problem_package_materializations_status ON problem_package_materializations(status,checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_problem_package_builds_status_created ON problem_package_builds(status,created_at);
CREATE INDEX IF NOT EXISTS idx_exports_problem_created ON exports(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exports_materialization_created ON exports(materialization_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_export_jobs_actor_created ON export_jobs(actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_export_jobs_problem_created ON export_jobs(problem_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_export_jobs_export ON export_jobs(export_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_normalized_unique ON users(email_normalized) WHERE email_normalized <> '';
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_created ON auth_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sudo_sessions_user_created ON sudo_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sudo_sessions_expires ON sudo_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_pending_registrations_token ON pending_registrations(token_hash);
CREATE INDEX IF NOT EXISTS idx_pending_registrations_expires ON pending_registrations(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_rate_limits_expires ON auth_rate_limits(window_expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_registration_codes_expires ON agent_registration_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_user_revoked_seen ON agent_sessions(user_id, revoked_at, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_access_requests_session_status_created ON agent_access_requests(agent_session_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_user_problem_active ON agent_tokens(user_id, problem_id, revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_system_config_updated ON system_config(updated_at DESC);
"""


def _split_sql_statements(script: str) -> tuple[str, ...]:
    """Split a trusted SQLite schema script without executing implicit commits."""

    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise RuntimeError("incomplete SQLite schema statement")
    return tuple(statements)


def _table_name_from_create(statement: str) -> str | None:
    prefix = "CREATE TABLE IF NOT EXISTS "
    if not statement.upper().startswith(prefix):
        return None
    return statement[len(prefix) :].split("(", 1)[0].strip()


def _table_name_from_create_index(statement: str) -> str:
    marker = " ON "
    marker_index = statement.upper().find(marker)
    if marker_index < 0:
        raise RuntimeError(f"invalid SQLite index statement: {statement!r}")
    return statement[marker_index + len(marker) :].split("(", 1)[0].strip()


def _index_name_from_create_index(statement: str) -> str:
    upper = statement.upper()
    prefix = (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        if upper.startswith("CREATE UNIQUE INDEX IF NOT EXISTS ")
        else "CREATE INDEX IF NOT EXISTS "
    )
    if not upper.startswith(prefix):
        raise RuntimeError(f"invalid SQLite index statement: {statement!r}")
    return statement[len(prefix) :].split(" ON ", 1)[0].strip()


def _current_table_statements() -> dict[str, str]:
    statements: dict[str, str] = {}
    for statement in _split_sql_statements(SCHEMA):
        schema_table_name = _table_name_from_create(statement)
        if schema_table_name is not None:
            statements[schema_table_name] = statement
    return statements


_CURRENT_TABLE_STATEMENTS = _current_table_statements()
_CURRENT_INDEX_STATEMENTS = tuple(_split_sql_statements(SCHEMA_INDEXES))
_CURRENT_INDEX_NAMES = tuple(
    _index_name_from_create_index(statement)
    for statement in _CURRENT_INDEX_STATEMENTS
)


def current_schema_statements_for_tables(
    table_names: Iterable[str],
) -> tuple[str, ...]:
    """Return current CREATE TABLE statements in canonical schema order."""

    requested = frozenset(table_names)
    missing = sorted(requested.difference(_CURRENT_TABLE_STATEMENTS))
    if missing:
        raise RuntimeError(
            f"tables are absent from the current SQLite schema: {', '.join(missing)}"
        )
    return tuple(
        statement
        for table_name, statement in _CURRENT_TABLE_STATEMENTS.items()
        if table_name in requested
    )


def current_index_statements_for_tables(
    table_names: Iterable[str],
) -> tuple[str, ...]:
    """Return current CREATE INDEX statements for the selected tables."""

    requested = frozenset(table_names)
    return tuple(
        statement
        for statement in _CURRENT_INDEX_STATEMENTS
        if _table_name_from_create_index(statement) in requested
    )

CURRENT_SCHEMA_COLUMNS: dict[str, tuple[str, ...]] = {
    "problems": ("id", "slug", "repo_name", "created_at"),
    "users": (
        "id",
        "username",
        "email",
        "email_normalized",
        "email_verified_at",
        "password_hash",
        "password_salt",
        "password_iters",
        "password_updated_at",
        "is_system_admin",
        "is_banned",
        "banned_at",
        "created_at",
    ),
    "auth_sessions": (
        "id",
        "user_id",
        "token_hash",
        "created_at",
        "expires_at",
        "revoked_at",
    ),
    "sudo_sessions": (
        "id",
        "user_id",
        "scope",
        "token_hash",
        "created_at",
        "expires_at",
        "revoked_at",
    ),
    "pending_registrations": (
        "id",
        "username",
        "email",
        "email_normalized",
        "password_hash",
        "password_salt",
        "password_iters",
        "token_hash",
        "request_ip",
        "user_agent",
        "terms_accepted",
        "created_at",
        "expires_at",
        "used_at",
    ),
    "auth_rate_limits": (
        "bucket_key",
        "count",
        "window_expires_at",
        "updated_at",
    ),
    "agent_registration_codes": ("code", "user_id", "created_at", "expires_at", "used_at"),
    "agent_sessions": (
        "id",
        "user_id",
        "identity_hash",
        "agent_name",
        "desktop_id",
        "init_ts",
        "created_at",
        "last_seen_at",
        "revoked_at",
    ),
    "agent_access_requests": (
        "id",
        "agent_session_id",
        "problem_id",
        "status",
        "created_at",
        "expires_at",
        "resolved_at",
        "delivered_at",
        "token_id",
        "delivery_token",
    ),
    "agent_tokens": (
        "id",
        "token_hash",
        "agent_session_id",
        "user_id",
        "problem_id",
        "scope",
        "created_at",
        "expires_at",
        "revoked_at",
    ),
    "repo_acl": ("id", "problem_id", "user_id", "role", "created_at"),
    "workspaces": (
        "id",
        "problem_id",
        "user_id",
        "path",
        "branch",
        "head_commit",
        "dirty",
        "revision_local",
        "revision_upstream",
        "revision_missing",
        "revision_highlight",
        "revision_upstream_higher",
        "revision_ahead_count",
        "revision_behind_count",
        "updated_at",
    ),
    "contests": (
        "id", "slug", "title", "owner_user_id", "status", "source_generation",
        "location", "date_text", "statement_default_language", "created_at",
    ),
    "contest_members": ("id", "contest_id", "user_id", "role", "created_at"),
    "contest_problems": (
        "id", "contest_id", "position", "label", "problem_id", "statement_folder",
        "added_by_user_id", "created_at",
    ),
    "contest_jobs": (
        "id",
        "contest_id",
        "actor_user_id",
        "job_type",
        "status",
        "source_generation",
        "created_at",
        "finished_at",
    ),
    "contest_build_items": (
        "id",
        "job_id",
        "contest_problem_id",
        "position",
        "label",
        "problem_id",
        "statement_folder",
        "source_commit",
        "revision_number",
        "materialization_id",
        "archive_sha256",
    ),
    "contest_artifacts": (
        "id",
        "contest_id",
        "job_id",
        "artifact_type",
        "filename",
        "sha256",
        "size_bytes",
        "created_at",
    ),
    "contest_attachments": (
        "id",
        "contest_id",
        "key",
        "rel_path",
        "created_at",
        "created_by_user_id",
    ),
    "previews": (
        "id",
        "problem_id",
        "workspace_id",
        "verification_id",
        "source_commit",
        "source_ref",
        "status",
        "summary_json",
        "created_at",
        "finished_at",
    ),
    "verification_artifact_refs": (
        "verification_id",
        "test_name",
        "input_ref",
        "answer_ref",
        "updated_at",
    ),
    "verification_task_diagnostics": (
        "task_id",
        "snapshot_json",
        "updated_at",
    ),
    "verification_selected_tests": (
        "verification_id",
        "ordinal",
        "test_name",
    ),
    "verification_source_paths": (
        "verification_id",
        "ordinal",
        "source_path",
    ),
    "verification_sanity_checks": (
        "verification_id",
        "ordinal",
        "check_name",
        "status",
        "checked_count",
    ),
    "verification_sanity_check_messages": (
        "verification_id",
        "check_name",
        "ordinal",
        "severity",
        "test_name",
        "message",
    ),
    "verification_tests_meta": (
        "verification_id",
        "ordinal",
        "test_name",
        "source_kind",
        "source_id",
        "is_sample",
        "sample_input_custom",
        "sample_output_custom",
        "sample_output_validate",
        "description",
        "source_path",
        "command_text",
        "payload_source_path",
    ),
    "verifications": (
        "id",
        "problem_id",
        "workspace_id",
        "signature",
        "source_commit",
        "kind",
        "status",
        "fail_reason",
        "mode",
        "pass_limit",
        "run_config_json",
        "error",
        "failed_step",
        "failed_check",
        "failed_test",
        "sanity_status",
        "sanity_checked_count",
        "validation_status",
        "validated_count",
        "created_at",
        "finished_at",
    ),
    "verification_tasks": (
        "id",
        "verification_id",
        "predecessor_task_id",
        "task_kind",
        "source_path",
        "program_id",
        "test_name",
        "expected_behavior",
        "final_status",
        "result_json",
        "finished_at",
        "created_at",
    ),
    "problem_package_materializations": (
        "id",
        "problem_id",
        "source_commit",
        "revision_number",
        "source_digest",
        "archive_rel_path",
        "archive_sha256",
        "archive_size_bytes",
        "verification_id",
        "status",
        "created_at",
        "checked_at",
        "unavailable_reason",
    ),
    "problem_package_builds": (
        "id",
        "problem_id",
        "source_commit",
        "verification_id",
        "phase",
        "status",
        "materialization_id",
        "error",
        "created_at",
        "started_at",
        "finished_at",
    ),
    "exports": (
        "id",
        "problem_id",
        "materialization_id",
        "export_type",
        "filename",
        "archive_rel_path",
        "sha256",
        "size_bytes",
        "source_commit",
        "created_at",
    ),
    "export_jobs": (
        "id",
        "problem_id",
        "actor_user_id",
        "export_type",
        "source_commit",
        "status",
        "materialization_id",
        "export_id",
        "error",
        "created_at",
        "started_at",
        "finished_at",
    ),
    "system_config": ("key", "value_json", "updated_at", "updated_by_user_id"),
    "smtp_config": (
        "id",
        "host",
        "port",
        "username",
        "password_ciphertext",
        "updated_at",
        "updated_by_user_id",
    ),
}

def now_iso() -> str:
    """Return the current UTC timestamp as ISO 8601 text."""

    return datetime.now(timezone.utc).isoformat()


@dataclass
class DB:
    """Small SQLite wrapper used by services and request handlers."""

    path: Path
    config_values: ConfigValues
    _database_was_present: bool = field(init=False, repr=False)

    LOCK_RETRY_ATTEMPTS = 3
    LOCK_RETRY_BASE_SEC = 0.05
    SQLITE_BUSY_TIMEOUT_MS = 5000
    SQL_TRACE_TEXT_LIMIT = 256

    def __post_init__(self) -> None:
        self._database_was_present = self._db_file_exists()

    def init(self) -> None:
        """Initialize a new database or validate an existing one without mutation."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                self._init_current_schema()
                return
            except sqlite3.OperationalError as exc:
                if is_sqlite_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise

    def _init_current_schema(self) -> None:
        if self._database_was_present:
            database_uri = f"{self.path.absolute().resolve().as_uri()}?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as conn:
                self._prepare_connection(conn)
                self._validate_existing_schema(conn)
            return
        with sqlite3.connect(self.path) as conn:
            self._prepare_connection(conn)
            conn.executescript(SCHEMA)
            conn.executescript(SCHEMA_INDEXES)
            self._validate_existing_schema(conn)
            conn.commit()
        self._database_was_present = True

    def _db_file_exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def _prepare_connection(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._install_sql_trace(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.SQLITE_BUSY_TIMEOUT_MS)}")

    def _validate_existing_schema(self, conn: sqlite3.Connection) -> None:
        table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {str(row[0]) for row in table_rows}
        missing_tables: list[str] = []
        missing_columns: list[str] = []
        index_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        indexes = {str(row[0]) for row in index_rows}
        missing_indexes = sorted(set(_CURRENT_INDEX_NAMES).difference(indexes))
        for table_name, expected_columns in CURRENT_SCHEMA_COLUMNS.items():
            if table_name not in tables:
                missing_tables.append(table_name)
                continue
            actual_columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            for column_name in expected_columns:
                if column_name not in actual_columns:
                    missing_columns.append(f"{table_name}.{column_name}")
        if missing_tables or missing_columns or missing_indexes:
            raise SchemaRequirementsError(
                missing_tables=sorted(missing_tables),
                missing_columns=sorted(missing_columns),
                missing_indexes=missing_indexes,
            )

    @contextmanager
    def conn(self):
        """Open a configured SQLite connection."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        self._prepare_connection(conn)
        try:
            yield conn
        finally:
            conn.close()

    def _install_sql_trace(self, conn: sqlite3.Connection) -> None:
        snapshot = self.config_values.snapshot()
        enabled = snapshot["DB_SQL_TRACE_ENABLED"]
        if not bool(enabled):
            return
        conn_id = id(conn)
        pid = os.getpid()

        def _trace(statement: str) -> None:
            text = summarize_traced_sql(statement, text_limit=self.SQL_TRACE_TEXT_LIMIT)
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
        transaction_fn: Callable[[sqlite3.Connection], _TxResult],
    ) -> _TxResult:
        """Run a write transaction with lock retry handling."""

        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = transaction_fn(conn)
                    except Exception:
                        conn.rollback()
                        raise
                    conn.commit()
                    return result
            except sqlite3.OperationalError as exc:
                if is_sqlite_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise
        raise RuntimeError("write transaction failed")

    def write_schema_reset_transaction(
        self,
        transaction_fn: Callable[[sqlite3.Connection], _TxResult],
    ) -> _TxResult:
        """Atomically replace tables, validating foreign keys before commit.

        SQLite implements ``DROP TABLE`` as an implicit row delete while foreign
        keys are enabled. A cleanup that replaces whole tables therefore needs a
        dedicated connection with enforcement disabled before its transaction
        begins. Every normal connection still enables enforcement in
        :meth:`_prepare_connection`.
        """

        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()
                    if foreign_keys is None or int(foreign_keys[0]) != 0:
                        raise RuntimeError(
                            "could not disable SQLite foreign keys for schema reset"
                        )
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = transaction_fn(conn)
                        violations = conn.execute(
                            "PRAGMA foreign_key_check"
                        ).fetchmany(10)
                        if violations:
                            details = [tuple(row) for row in violations]
                            raise RuntimeError(
                                "foreign key violations after schema reset: "
                                f"{details!r}"
                            )
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    conn.execute("PRAGMA foreign_keys=ON")
                    return result
            except sqlite3.OperationalError as exc:
                if is_sqlite_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise
        raise RuntimeError("schema reset transaction failed")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        """Execute a write statement with lock retry handling."""

        values = tuple(params)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    conn.execute(sql, values)
                    conn.commit()
                    return
            except sqlite3.OperationalError as exc:
                if is_sqlite_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        """Fetch one row with lock retry handling."""

        values = tuple(params)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    cursor = conn.execute(sql, values)
                    row = cursor.fetchone()
                    if row is None:
                        return None
                    return row
            except sqlite3.OperationalError as exc:
                if is_sqlite_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise
        return None

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Fetch all rows with lock retry handling."""

        values = tuple(params)
        for attempt in range(max(1, int(self.LOCK_RETRY_ATTEMPTS))):
            try:
                with self.conn() as conn:
                    cursor = conn.execute(sql, values)
                    return list(cursor.fetchall())
            except sqlite3.OperationalError as exc:
                if is_sqlite_locked_error(exc) and attempt + 1 < int(self.LOCK_RETRY_ATTEMPTS):
                    time.sleep(self.LOCK_RETRY_BASE_SEC * float(attempt + 1))
                    continue
                raise
        return []
