from __future__ import annotations

from .shared import (
    Path,
    RuntimeValues,
    _HOSTNAME_RE,
    _RUN_ID_RE,
    contextmanager,
    is_domjudge_sql,
    now_iso,
    secrets,
    task_status_counts,
    time,
)


class JudgehostCoreMixin:
    def _init_domdb_schema(self) -> None:
        with self._domdb_lock:
            conn = self._domdb
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS judgehost_domjudge_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL UNIQUE,
                    submit_id TEXT NOT NULL UNIQUE,
                    contest_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    work_root TEXT NOT NULL,
                    compile_hash TEXT NOT NULL,
                    run_hash TEXT NOT NULL,
                    compare_hash TEXT NOT NULL,
                    source_hash TEXT NOT NULL DEFAULT '',
                    compile_config_json TEXT NOT NULL,
                    run_config_json TEXT NOT NULL,
                    compare_config_json TEXT NOT NULL,
                    expected_behavior TEXT NOT NULL DEFAULT 'unknown',
                    invocation_source TEXT NOT NULL DEFAULT 'run.execute',
                    force_recompile INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    compile_success INTEGER,
                    compile_output_b64 TEXT,
                    compile_metadata_b64 TEXT,
                    debug_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'leased',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS judgehost_domjudge_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    test_name TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    testcase_id INTEGER,
                    testcase_hash TEXT NOT NULL,
                    testcase_input_hash TEXT NOT NULL DEFAULT '',
                    testcase_answer_hash TEXT NOT NULL DEFAULT '',
                    input_path TEXT NOT NULL,
                    answer_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT,
                    runresult TEXT,
                    runtime_sec REAL,
                    cpu_sec REAL,
                    wall_sec REAL,
                    memory_kb INTEGER,
                    output_run_rel TEXT,
                    output_error_rel TEXT,
                    output_system_rel TEXT,
                    output_diff_rel TEXT,
                    metadata_rel TEXT,
                    compare_metadata_rel TEXT,
                    team_message_rel TEXT,
                    score_text TEXT,
                    debug_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jh_jobs_task ON judgehost_domjudge_jobs(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jh_jobs_lease ON judgehost_domjudge_jobs(lease_owner,updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jh_cases_job ON judgehost_domjudge_cases(job_id,ordinal ASC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jh_cases_status ON judgehost_domjudge_cases(status,job_id,ordinal ASC)")
            conn.commit()

    @staticmethod
    def _is_domjudge_sql(sql: str) -> bool:
        return is_domjudge_sql(sql)

    def _db_fetch_one(self, sql: str, values: list[object] | tuple[object, ...] | None = None):
        params = [] if values is None else list(values)
        if self._is_domjudge_sql(sql):
            with self._domdb_lock:
                return self._domdb.execute(sql, params).fetchone()
        return self.db.fetch_one(sql, params)

    def _db_fetch_all(self, sql: str, values: list[object] | tuple[object, ...] | None = None):
        params = [] if values is None else list(values)
        if self._is_domjudge_sql(sql):
            with self._domdb_lock:
                return self._domdb.execute(sql, params).fetchall()
        return self.db.fetch_all(sql, params)

    def _db_execute(self, sql: str, values: list[object] | tuple[object, ...] | None = None):
        params = [] if values is None else list(values)
        if self._is_domjudge_sql(sql):
            with self._domdb_lock:
                cur = self._domdb.execute(sql, params)
                self._domdb.commit()
                return cur
        return self.db.execute(sql, params)

    @contextmanager
    def _domdb_conn(self):
        with self._domdb_lock:
            try:
                yield self._domdb
                self._domdb.commit()
            except Exception:
                self._domdb.rollback()
                raise

    def apply_runtime_values(self, constants: RuntimeValues) -> None:
        with self._lock:
            self._constants = constants
            self._enabled = bool(constants.JUDGEHOST_ENABLE)
            self._api_token = str(constants.JUDGEHOST_API_TOKEN or "").strip()
            self._api_username = str(getattr(constants, "JUDGEHOST_API_USERNAME", "judgehost") or "judgehost").strip()
            self._fetch_batch_size = max(1, min(128, int(constants.JUDGEHOST_FETCH_BATCH_SIZE)))
            self._lease_sec = max(5, min(86400, int(constants.JUDGEHOST_LEASE_SEC)))
            self._wait_timeout_sec = max(5, min(86400, int(constants.JUDGEHOST_WAIT_TIMEOUT_SEC)))
            self._wait_poll_sec = max(0.05, min(30.0, float(constants.JUDGEHOST_WAIT_POLL_SEC)))
            self._online_window_sec = max(5, min(86400, int(constants.JUDGEHOST_ONLINE_WINDOW_SEC)))
            self._max_source_bytes = max(1024, min(16 * 1024 * 1024, int(constants.JUDGEHOST_MAX_INLINE_SOURCE_BYTES)))
            self._max_tests_per_task = max(1, min(10000, int(constants.JUDGEHOST_MAX_TESTS_PER_TASK)))
            self._max_test_payload_bytes = max(
                1024,
                min(256 * 1024 * 1024, int(constants.JUDGEHOST_MAX_TEST_PAYLOAD_BYTES)),
            )
            self._include_build_payload = bool(constants.JUDGEHOST_INCLUDE_BUILD_PAYLOAD)
            self._max_binary_payload_bytes = max(
                1024, min(128 * 1024 * 1024, int(constants.JUDGEHOST_MAX_BINARY_PAYLOAD_BYTES))
            )

    def enabled(self) -> bool:
        return bool(self._enabled)

    def auth_token_configured(self) -> bool:
        return bool(self._api_token)

    def check_api_token(self, token: str) -> bool:
        expected = str(self._api_token or "").strip()
        provided = str(token or "").strip()
        if not expected or not provided:
            return False
        return secrets.compare_digest(expected, provided)

    def api_username(self) -> str:
        token = str(self._api_username or "").strip()
        return token or "judgehost"

    def check_api_basic(self, username: str, password: str) -> bool:
        expected_user = self.api_username()
        provided_user = str(username or "").strip()
        provided_pass = str(password or "").strip()
        if not provided_user or not provided_pass:
            return False
        if provided_user != expected_user:
            return False
        return self.check_api_token(provided_pass)

    def _normalize_run_id(self, run_id: str) -> str:
        token = str(run_id or "").strip()
        if not _RUN_ID_RE.fullmatch(token):
            raise RuntimeError("invalid run id for judgehost task")
        return token

    def _normalize_hostname(self, hostname: str) -> str:
        token = str(hostname or "").strip()
        if not _HOSTNAME_RE.fullmatch(token):
            return "judgehost"
        return token

    def _task_status_counts(self) -> dict[str, int]:
        with self._state_lock:
            return task_status_counts(
                self._tasks_by_id,
                queued=self.STATUS_QUEUED,
                leased=self.STATUS_LEASED,
                completed=self.STATUS_COMPLETED,
                failed=self.STATUS_FAILED,
            )

    def _task_by_id(self, task_id: str) -> dict[str, object] | None:
        with self._state_lock:
            row = self._tasks_by_id.get(str(task_id or "").strip())
            if row is None:
                return None
            return dict(row)

    def _task_payload(self, task_id: str) -> dict[str, object]:
        row = self._task_by_id(task_id)
        if row is None:
            return {}
        payload = row.get("payload")
        return dict(payload) if isinstance(payload, dict) else {}

    def _record_host_judging(self, hostname: str, *, label: str = "-", updated_at: str | None = None) -> None:
        safe_host = self._normalize_hostname(hostname)
        if not safe_host:
            return
        ts = time.time()
        now_text = str(updated_at or now_iso()).strip() or now_iso()
        with self._state_lock:
            events = self._host_judged_case_events.setdefault(safe_host, [])
            events.append(ts)
            cutoff = ts - (5 * 3600.0)
            while events and events[0] < cutoff:
                events.pop(0)
            self._host_last_judging[safe_host] = {"label": str(label or "-"), "updated_at": now_text}

    def _host_state_row(self, hostname: str) -> dict[str, object]:
        safe_host = self._normalize_hostname(hostname)
        now_text = now_iso()
        with self._state_lock:
            row = self._hosts_state.get(safe_host)
            if row is None:
                row = {
                    "hostname": safe_host,
                    "enabled": True,
                    "first_seen_at": now_text,
                    "last_seen_at": now_text,
                    "last_action": "",
                    "last_task_id": "",
                    "last_run_id": "",
                    "lease_expires_at": "",
                    "update_count": 0,
                }
                self._hosts_state[safe_host] = row
            return row

    def _safe_workspace_source(self, workspace_root: Path, submission_path: str) -> Path:
        workspace_resolved = workspace_root.resolve()
        rel = str(submission_path or "").strip().replace("\\", "/")
        if not rel:
            raise RuntimeError("submission source path is required")
        candidate = (workspace_resolved / rel).resolve()
        if candidate == workspace_resolved or workspace_resolved not in candidate.parents:
            raise RuntimeError("submission source escapes workspace")
        if candidate.is_symlink() or not candidate.exists() or (not candidate.is_file()):
            raise RuntimeError("submission source does not exist")
        return candidate

    def _safe_read_bytes(self, path: Path, *, max_bytes: int, label: str) -> bytes:
        size = int(path.stat().st_size)
        if size > max_bytes:
            raise RuntimeError(f"{label} exceeds payload limit: {path.name} ({size} bytes)")
        return path.read_bytes()

