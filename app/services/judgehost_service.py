from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import shlex
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import DB, now_iso
from app.runtime_values import RuntimeValues
from app.services.hashing import (
    canonical_json,
    compile_command_digest,
    domjudge_executable_hash,
    sha256_hex_bytes,
    sha256_hex_json,
    sha256_hex_text,
    sha256_hex_of_hashes,
)
from app.services.judge_fs_index_service import JudgeFsIndexService
from app.services.run_service import RUN_TEST_NAME_RE, RunService
from app.settings import Settings


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_DOMJUDGE_SUBMIT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOMJUDGE_CONTEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOMJUDGE_PROTOCOL_TRACE_RE = re.compile(r"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")
_DOMJUDGE_CACHE_BLOB_REF_RE = re.compile(
    r"^cache://(?P<kind>[a-z-]+)/(?P<key>[0-9a-f]{64})/(?P<sig>[0-9a-f]{64})/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)
_DOMJUDGE_CACHE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

logger = logging.getLogger(__name__)


def _now_iso_after(seconds: float) -> str:
    sec = max(0.0, float(seconds))
    return (datetime.now(timezone.utc) + timedelta(seconds=sec)).isoformat()


def _parse_iso_utc(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class JudgehostTaskService:
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    CASE_CACHE_KIND = JudgeFsIndexService.KIND_CASE
    SOLVE_OUTPUT_CACHE_KIND = JudgeFsIndexService.KIND_SOLVE_OUTPUT

    def __init__(
        self,
        db: DB,
        run_service: RunService,
        settings: Settings,
        constants: RuntimeValues,
        judge_fs_index_service: JudgeFsIndexService | None = None,
    ) -> None:
        self.db = db
        self._run_service = run_service
        self._settings = settings
        self._constants = constants
        self._lock = threading.Lock()
        self._enabled = False
        self._api_token = ""
        self._api_username = "judgehost"
        self._fetch_batch_size = 1
        self._lease_sec = 120
        self._wait_timeout_sec = 900
        self._wait_poll_sec = 0.5
        self._online_window_sec = 120
        self._max_source_bytes = 262144
        self._max_tests_per_task = 512
        self._max_test_payload_bytes = 1048576
        self._include_build_payload = True
        self._max_binary_payload_bytes = 8388608
        self._lease_requeue_lock = threading.Lock()
        self._lease_requeue_next_ts = 0.0
        self._testcase_registry_lock = threading.RLock()
        self._testcase_registry_next_id = 1
        self._testcase_registry_by_hash: dict[str, dict[str, object]] = {}
        self._testcase_registry_by_id: dict[int, dict[str, object]] = {}
        self._state_lock = threading.RLock()
        self._tasks_by_id: dict[str, dict[str, object]] = {}
        self._task_id_by_run: dict[str, str] = {}
        self._hosts_state: dict[str, dict[str, object]] = {}
        self._host_judged_case_events: dict[str, list[float]] = {}
        self._host_last_judging: dict[str, dict[str, str]] = {}
        self._domdb_lock = threading.RLock()
        self._domdb = sqlite3.connect(":memory:", check_same_thread=False)
        self._domdb.row_factory = sqlite3.Row
        self._init_domdb_schema()
        self._judge_fs_index_service = judge_fs_index_service
        self.apply_runtime_values(constants)

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
        token = str(sql or "").lower()
        return ("judgehost_domjudge_jobs" in token) or ("judgehost_domjudge_cases" in token)

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
        out = {
            self.STATUS_QUEUED: 0,
            self.STATUS_LEASED: 0,
            self.STATUS_COMPLETED: 0,
            self.STATUS_FAILED: 0,
        }
        with self._state_lock:
            for row in self._tasks_by_id.values():
                token = str(row.get("status") or "").strip().lower()
                if token in out:
                    out[token] = int(out[token]) + 1
        return out

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

    def _collect_build_payload(
        self,
        *,
        problem: str,
        build_id: str,
        workspace: Path,
        mode: str,
        selected_tests: list[str],
    ) -> dict[str, object]:
        if not self._include_build_payload:
            return {}
        build_row = self._db_fetch_one("SELECT build_ref FROM builds WHERE id=?", [str(build_id or "").strip()])
        build_ref = str(build_row["build_ref"] or "").strip().lower() if build_row is not None else ""
        if not build_ref:
            return {}
        artifact_root = self._run_service.fs_manager.build_paths(build_ref).root.resolve()
        if not artifact_root.exists() or (not artifact_root.is_dir()):
            return {}
        tests_dir = (artifact_root / "tests").resolve()
        ans_dir = (artifact_root / "ans").resolve()
        logs_dir = (artifact_root / "logs").resolve()
        bin_dir = (artifact_root / "bin").resolve()

        wanted_tests: list[str] = []
        if selected_tests:
            for raw in selected_tests:
                token = Path(str(raw or "").strip()).name
                if not RUN_TEST_NAME_RE.fullmatch(token):
                    continue
                if token in wanted_tests:
                    continue
                wanted_tests.append(token)
        else:
            if tests_dir.exists():
                for p in sorted(tests_dir.glob("*.in")):
                    token = p.name
                    if not RUN_TEST_NAME_RE.fullmatch(token):
                        continue
                    wanted_tests.append(token)
                    if len(wanted_tests) >= self._max_tests_per_task:
                        break

        tests_payload: list[dict[str, object]] = []
        for test_name in wanted_tests:
            test_file = (tests_dir / test_name).resolve()
            if not test_file.exists() or (not test_file.is_file()):
                continue
            test_bytes = self._safe_read_bytes(
                test_file,
                max_bytes=self._max_test_payload_bytes,
                label="test payload",
            )
            ans_name = f"{Path(test_name).stem}.ans"
            ans_file = (ans_dir / ans_name).resolve()
            ans_bytes = b""
            if ans_file.exists() and ans_file.is_file():
                ans_bytes = self._safe_read_bytes(
                    ans_file,
                    max_bytes=self._max_test_payload_bytes,
                    label="answer payload",
                )
            tests_payload.append(
                {
                    "name": test_name,
                    "input_b64": base64.b64encode(test_bytes).decode("ascii"),
                    "answer_name": ans_name,
                    "answer_b64": base64.b64encode(ans_bytes).decode("ascii"),
                }
            )

        run_config_text = ""
        run_cfg_obj: dict[str, object] = {}
        run_cfg_path = (logs_dir / "run_config.json").resolve()
        if run_cfg_path.exists() and run_cfg_path.is_file():
            run_cfg_bytes = self._safe_read_bytes(
                run_cfg_path,
                max_bytes=self._max_test_payload_bytes,
                label="run config payload",
            )
            run_config_text = run_cfg_bytes.decode("utf-8", errors="replace")
            try:
                parsed_cfg = json.loads(run_config_text)
                if isinstance(parsed_cfg, dict):
                    run_cfg_obj = parsed_cfg
            except Exception:
                run_cfg_obj = {}

        binaries: dict[str, str] = {}
        for name in ("checker", "interactor"):
            p = (bin_dir / name).resolve()
            if not p.exists() or (not p.is_file()):
                continue
            blob = self._safe_read_bytes(
                p,
                max_bytes=self._max_binary_payload_bytes,
                label=f"{name} payload",
            )
            binaries[name] = base64.b64encode(blob).decode("ascii")

        workspace_resolved = workspace.resolve()

        def _safe_workspace_rel_file(rel_path: str) -> Path | None:
            token = str(rel_path or "").strip().replace("\\", "/")
            if not token:
                return None
            candidate = (workspace_resolved / token).resolve()
            if candidate == workspace_resolved or workspace_resolved not in candidate.parents:
                return None
            if candidate.is_symlink() or (not candidate.exists()) or (not candidate.is_file()):
                return None
            return candidate

        def _first_cpp_under(rel_dir: str) -> Path | None:
            base = (workspace_resolved / rel_dir).resolve()
            if base == workspace_resolved or workspace_resolved not in base.parents:
                return None
            if (not base.exists()) or (not base.is_dir()):
                return None
            for path in sorted(base.glob("*.cpp")):
                resolved = path.resolve()
                if resolved.is_symlink() or (not resolved.is_file()):
                    continue
                return resolved
            return None

        build_cfg_obj: dict[str, object] = {}
        build_cfg_path = _safe_workspace_rel_file("config/build.json")
        if build_cfg_path is not None:
            try:
                parsed_build_cfg = json.loads(build_cfg_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(parsed_build_cfg, dict):
                    build_cfg_obj = parsed_build_cfg
            except Exception:
                build_cfg_obj = {}
        problem_cfg_obj: dict[str, object] = {}
        problem_cfg_path = _safe_workspace_rel_file("config/problem.json")
        if problem_cfg_path is not None:
            try:
                parsed_problem_cfg = json.loads(problem_cfg_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(parsed_problem_cfg, dict):
                    problem_cfg_obj = parsed_problem_cfg
            except Exception:
                problem_cfg_obj = {}
        try:
            problem_time_limit_ms = int(problem_cfg_obj.get("time_limit_ms", 0))
        except Exception:
            problem_time_limit_ms = 0
        try:
            problem_memory_limit_mb = int(problem_cfg_obj.get("memory_limit_mb", 0))
        except Exception:
            problem_memory_limit_mb = 0
        if problem_time_limit_ms < 0:
            problem_time_limit_ms = 0
        if problem_memory_limit_mb < 0:
            problem_memory_limit_mb = 0

        checker_source: Path | None = None
        checker_standard = str(run_cfg_obj.get("checker_standard") or build_cfg_obj.get("checker_standard") or "").strip()
        if checker_standard:
            token = checker_standard[5:] if checker_standard.startswith("std::") else checker_standard
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", token):
                std_root = (Path(__file__).resolve().parents[2] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
                source = (std_root / token).resolve()
                if source.exists() and source.is_file():
                    checker_source = source
        if checker_source is None:
            checker_source = _safe_workspace_rel_file(str(build_cfg_obj.get("checker_source") or ""))
        if checker_source is None:
            checker_source = _safe_workspace_rel_file("checkers/checker.cpp")
        if checker_source is None:
            checker_source = _first_cpp_under("checkers")

        interactive_mode = str(mode or "").strip().lower() in {"interactive", "multi-pass"}
        interactor_source: Path | None = None
        if interactive_mode:
            interactor_source = _safe_workspace_rel_file(str(build_cfg_obj.get("interactor_source") or ""))
            if interactor_source is None:
                interactor_source = _safe_workspace_rel_file("interactors/interactor.cpp")
            if interactor_source is None:
                interactor_source = _first_cpp_under("interactors")

        source_files: dict[str, Path] = {}
        if checker_source is not None:
            source_files["checker.cpp"] = checker_source
        if interactor_source is not None:
            source_files["interactor.cpp"] = interactor_source
        if source_files:
            testlib_source = _safe_workspace_rel_file("third_party/testlib/testlib.h")
            if testlib_source is None:
                upstream_testlib = (Path(__file__).resolve().parents[2] / "third_party" / "upstream" / "testlib" / "testlib.h").resolve()
                if upstream_testlib.exists() and upstream_testlib.is_file():
                    testlib_source = upstream_testlib
            if testlib_source is not None:
                source_files["testlib.h"] = testlib_source

        sources_payload: dict[str, str] = {}
        for name, source_path in source_files.items():
            blob = self._safe_read_bytes(
                source_path,
                max_bytes=self._max_binary_payload_bytes,
                label=f"{name} payload",
            )
            sources_payload[name] = base64.b64encode(blob).decode("ascii")

        return {
            "tests": tests_payload,
            "run_config_json": run_config_text,
            "problem_limits": {
                "time_limit_ms": int(problem_time_limit_ms),
                "memory_limit_mb": int(problem_memory_limit_mb),
            },
            "binaries_b64": binaries,
            "sources_b64": sources_payload,
        }

    def _build_task_payload(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_filename: str | None,
        selected_tests: list[str],
        invocation_id: str,
        invocation_run_ids: list[str],
        expected_behavior: str,
        invocation_source: str,
        run_id: str,
        force_recompile: bool = False,
    ) -> dict[str, object]:
        ctx = self._run_service.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(str(ctx["workspace"]["path"]))

        source_bytes: bytes
        source_name: str
        source_label: str
        if isinstance(upload_content, (bytes, bytearray)):
            source_bytes = bytes(upload_content)
            source_name = str(upload_filename or "submission.cpp").strip() or "submission.cpp"
            source_label = source_name
        else:
            source_path = self._safe_workspace_source(workspace, str(submission_path or ""))
            source_bytes = self._safe_read_bytes(
                source_path,
                max_bytes=self._max_source_bytes,
                label="submission payload",
            )
            source_name = source_path.name
            source_label = str(submission_path or source_name)

        build_payload = self._collect_build_payload(
            problem=problem,
            build_id=build_id,
            workspace=workspace,
            mode=mode,
            selected_tests=selected_tests,
        )
        return {
            "type": "invocation.run",
            "run_id": run_id,
            "problem": problem,
            "username": username,
            "build_id": build_id,
            "mode": mode,
            "submission_path": str(submission_path or ""),
            "source_name": source_name,
            "source_label": source_label,
            "source_b64": base64.b64encode(source_bytes).decode("ascii"),
            "selected_tests": list(selected_tests),
            "invocation_id": invocation_id,
            "invocation_run_ids": list(invocation_run_ids),
            "expected_behavior": expected_behavior,
            "invocation_source": invocation_source,
            "force_recompile": bool(force_recompile),
            "build_payload": build_payload,
            "enqueued_at": now_iso(),
        }

    def _domjudge_precomputed_fields_from_payload(self, payload: dict[str, object]) -> dict[str, object]:
        source_name = Path(str(payload.get("source_name") or "submission.cpp").strip() or "submission.cpp").name
        source_bytes = self._domjudge_b64_decode(payload.get("source_b64"))
        if not source_bytes:
            raise RuntimeError("submission source payload is empty")
        build_payload = payload.get("build_payload")
        if not isinstance(build_payload, dict):
            raise RuntimeError("build payload is required for DOMjudge compatibility")
        run_cfg_obj: dict[str, object] = {}
        run_cfg_raw = str(build_payload.get("run_config_json") or "").strip()
        if run_cfg_raw:
            try:
                parsed = json.loads(run_cfg_raw)
                if isinstance(parsed, dict):
                    run_cfg_obj = parsed
            except Exception:
                run_cfg_obj = {}
        problem_limits_obj = build_payload.get("problem_limits")
        if not isinstance(problem_limits_obj, dict):
            problem_limits_obj = {}
        checker_args_raw = run_cfg_obj.get("checker_args")
        checker_args: list[str] = []
        if isinstance(checker_args_raw, list):
            checker_args = [str(item or "").strip() for item in checker_args_raw if str(item or "").strip()]
        mode = str(payload.get("mode") or "pass-fail").strip().lower()
        invocation_source = str(payload.get("invocation_source") or "").strip().lower()
        solve_mode = invocation_source == "build.solve"
        configured_max_passes = max(
            1,
            self._domjudge_parse_int(
                run_cfg_obj.get("max_passes"),
                self._domjudge_parse_int(problem_limits_obj.get("max_passes"), 16),
            ),
        )
        max_passes = configured_max_passes if mode == "multi-pass" else 1
        compile_timeout = max(1, int(getattr(self._constants, "TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120) or 120))
        compile_mem_mb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048))
        compile_output_kb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536))
        run_output_kb = max(64, int(getattr(self._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536))
        run_process_limit = max(1, int(getattr(self._constants, "RUN_EXEC_PROCESS_LIMIT", 64) or 64))
        default_cfg = getattr(self._constants, "GENERAL_CONFIG_DEFAULTS", {}) or {}
        run_tl_ms = self._domjudge_parse_int(
            run_cfg_obj.get("time_limit_ms"),
            self._domjudge_parse_int(
                problem_limits_obj.get("time_limit_ms"),
                self._domjudge_parse_int(default_cfg.get("time_limit_ms", 2000), 2000),
            ),
        )
        run_mem_mb = self._domjudge_parse_int(
            run_cfg_obj.get("memory_limit_mb"),
            self._domjudge_parse_int(
                problem_limits_obj.get("memory_limit_mb"),
                self._domjudge_parse_int(default_cfg.get("memory_limit_mb", 1024), 1024),
            ),
        )
        run_tl_ms = max(100, run_tl_ms)
        run_mem_mb = max(16, run_mem_mb)
        run_tl_sec = max(0.1, float(run_tl_ms) / 1000.0)
        pass_fail_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_PASS_FAIL_SEC", 1) or 1))
        multi_pass_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_MULTI_PASS_SEC", 15) or 15))
        interactive_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_INTERACTIVE_SEC", 15) or 15))
        run_overshoot_sec = pass_fail_slack
        if mode == "interactive":
            run_overshoot_sec = interactive_slack
        elif mode == "multi-pass":
            run_overshoot_sec = multi_pass_slack
        run_mem_kb = max(16 * 1024, int(run_mem_mb * 1024))
        binaries_b64 = build_payload.get("binaries_b64")
        binaries_obj = binaries_b64 if isinstance(binaries_b64, dict) else {}
        checker_bytes = self._domjudge_b64_decode(binaries_obj.get("checker"))
        interactor_bytes = self._domjudge_b64_decode(binaries_obj.get("interactor"))
        sources_b64 = build_payload.get("sources_b64")
        sources_obj = sources_b64 if isinstance(sources_b64, dict) else {}
        checker_source_bytes = self._domjudge_b64_decode(sources_obj.get("checker.cpp"))
        interactor_source_bytes = self._domjudge_b64_decode(sources_obj.get("interactor.cpp"))
        testlib_header_bytes = self._domjudge_b64_decode(sources_obj.get("testlib.h"))
        if checker_source_bytes:
            checker_source_bytes = self._domjudge_force_cpp_define(checker_source_bytes)
        if interactor_source_bytes:
            interactor_source_bytes = self._domjudge_force_cpp_define(interactor_source_bytes)
        if checker_source_bytes:
            checker_bytes = b""
        if interactor_source_bytes:
            interactor_bytes = b""
        interactive = mode == "interactive" or (mode == "multi-pass" and bool(interactor_bytes or interactor_source_bytes))
        if mode == "interactive" and not (interactor_bytes or interactor_source_bytes):
            raise RuntimeError("interactive mode requires interactor payload")

        compile_files: list[tuple[str, bytes, bool]] = [("run", self._domjudge_compile_script(source_name), True)]
        run_files: list[tuple[str, bytes, bool]] = []
        compare_files: list[tuple[str, bytes, bool]] = []
        if interactive:
            if interactor_source_bytes:
                run_files.append(("interactor.cpp", interactor_source_bytes, False))
                if testlib_header_bytes:
                    run_files.append(("testlib.h", testlib_header_bytes, False))
            elif interactor_bytes:
                run_files.append(("run", interactor_bytes, True))
            else:
                raise RuntimeError("interactive mode requires interactor payload")
            compare_files.append(("run", self._domjudge_compare_script(solve_mode=solve_mode), True))
        else:
            run_files.append(("run", self._domjudge_run_script(False, solve_mode=solve_mode), True))
            if solve_mode:
                compare_files.append(("run", self._domjudge_compare_script(solve_mode=True), True))
            elif checker_source_bytes:
                compare_files.append(("checker.cpp", checker_source_bytes, False))
                if testlib_header_bytes:
                    compare_files.append(("testlib.h", testlib_header_bytes, False))
            elif checker_bytes:
                compare_files.append(("run", checker_bytes, True))
            else:
                compare_files.append(("run", self._domjudge_compare_script(solve_mode=False), True))

        source_hash = self._domjudge_source_hash(source_name, source_bytes)
        compile_hash = domjudge_executable_hash(compile_files)
        run_hash = domjudge_executable_hash(run_files)
        compare_hash = domjudge_executable_hash(compare_files)
        toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(source_name)
        compare_script_timelimit = max(1, int(run_tl_sec))
        if checker_source_bytes:
            compare_script_timelimit = max(compare_script_timelimit, min(compile_timeout, 120))
        compile_config = {
            "hash": compile_hash,
            "toolchain_cmd_digest": toolchain_cmd_digest,
            "filter_compiler_files": False,
            "language_extensions": list(self._domjudge_language_extensions(source_name)[1]),
            "script_timelimit": compile_timeout,
            "script_memory_limit": int(compile_mem_mb * 1024),
            "script_filesize_limit": int(compile_output_kb * 1024),
        }
        run_config = {
            "hash": run_hash,
            "time_limit": run_tl_sec,
            "overshoot": run_overshoot_sec,
            "memory_limit": run_mem_kb,
            "output_limit": int(run_output_kb * 1024),
            "process_limit": run_process_limit,
            "entry_point": None,
            "pass_limit": max_passes,
            "language_id": self._domjudge_language_extensions(source_name)[0],
        }
        compare_config = {
            "hash": compare_hash,
            "combined_run_compare": bool(interactive),
            "compare_args": " ".join(checker_args),
            "script_timelimit": int(compare_script_timelimit),
            "script_memory_limit": run_mem_kb,
            "script_filesize_limit": int(run_output_kb * 1024),
        }
        return {
            "source_hash": source_hash,
            "compile_hash": compile_hash,
            "run_hash": run_hash,
            "compare_hash": compare_hash,
            "toolchain_cmd_digest": toolchain_cmd_digest,
            "compile_config": compile_config,
            "run_config": run_config,
            "compare_config": compare_config,
        }

    def prepare_enqueue_payload(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_filename: str | None,
        run_id: str,
        selected_tests: list[str] | None,
        invocation_id: str,
        invocation_run_ids: list[str] | None,
        expected_behavior: str,
        invocation_source: str,
        force_recompile: bool = False,
    ) -> dict[str, object]:
        selected = [str(item or "").strip() for item in (selected_tests or [])]
        selected = [item for item in selected if RUN_TEST_NAME_RE.fullmatch(item)]
        selected = list(dict.fromkeys(selected))
        inv_run_ids = [str(item or "").strip() for item in (invocation_run_ids or [])]
        inv_run_ids = [item for item in inv_run_ids if _RUN_ID_RE.fullmatch(item)]
        inv_run_ids = list(dict.fromkeys(inv_run_ids))
        safe_run_id = self._normalize_run_id(run_id)
        payload = self._build_task_payload(
            problem=problem,
            username=username,
            build_id=build_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_filename=upload_filename,
            selected_tests=selected,
            invocation_id=invocation_id,
            invocation_run_ids=inv_run_ids,
            expected_behavior=expected_behavior,
            invocation_source=invocation_source,
            run_id=safe_run_id,
            force_recompile=bool(force_recompile),
        )
        payload["domjudge_precomputed"] = self._domjudge_precomputed_fields_from_payload(payload)
        return payload

    def _initial_summary(
        self,
        *,
        run_id: str,
        task_id: str,
        mode: str,
        source_label: str,
        selected_tests: list[str],
        invocation_id: str,
        invocation_run_ids: list[str],
        expected_behavior: str,
        invocation_source: str,
    ) -> dict[str, object]:
        summary: dict[str, object] = {
            "mode": mode,
            "source": source_label,
            "selected_tests": list(selected_tests),
            "selected_tests_count": len(selected_tests),
            "invocation_source": str(invocation_source or "").strip() or "run.execute",
            "tests": [],
            "compile_log": "",
            "compile_diagnostics": [],
            "toolchain_digest": "judgehost",
            "sandbox_backend": self._run_service.sandbox.name,
            "invocation_backend": "domjudge-judgehost",
            "limits": {},
            "usage": {},
            "judgehost": {
                "task_id": task_id,
                "status": self.STATUS_QUEUED,
            },
        }
        safe_invocation_id = str(invocation_id or "").strip()
        if _INVOCATION_ID_RE.fullmatch(safe_invocation_id):
            safe_ids: list[str] = []
            for raw in invocation_run_ids:
                token = str(raw or "").strip()
                if not _RUN_ID_RE.fullmatch(token):
                    continue
                if token in safe_ids:
                    continue
                safe_ids.append(token)
            if run_id not in safe_ids:
                safe_ids.append(run_id)
            summary["invocation"] = {
                "id": safe_invocation_id,
                "source": str(invocation_source or "run.execute").strip() or "run.execute",
                "run_ids": safe_ids,
                "expected_behavior": str(expected_behavior or "unknown").strip() or "unknown",
                "completed": False,
            }
        return summary

    def _ensure_run_row(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        run_id: str,
        mode: str,
        summary: dict[str, object],
    ) -> str:
        ctx = self._run_service.workspace_service.workspace_context(problem, username, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_row = self._db_fetch_one("SELECT build_ref FROM builds WHERE id=?", [str(build_id or "").strip()])
        build_ref = str(build_row["build_ref"] or "").strip().lower() if build_row is not None else ""
        run_root = self._run_service.fs_manager.prepare_run_root(run_id).resolve()
        now_text = now_iso()
        existing = self._db_fetch_one("SELECT id FROM runs WHERE id=?", [run_id])
        encoded = self._run_service._summary_for_db(summary)
        if existing is None:
            self._db_execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    build_ref,
                    mode,
                    "running",
                    encoded,
                    str(run_root),
                    now_text,
                ],
            )
        else:
            self._db_execute(
                """
                UPDATE runs
                SET problem_id=?,workspace_id=?,build_id=?,build_ref=?,mode=?,status=?,summary_json=?,artifact_path=?,finished_at=NULL
                WHERE id=?
                """,
                [
                    problem_id,
                    workspace_id,
                    build_id,
                    build_ref,
                    mode,
                    "running",
                    encoded,
                    str(run_root),
                    run_id,
                ],
            )
        return str(run_root)

    def enqueue_task(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_filename: str | None,
        run_id: str,
        selected_tests: list[str] | None,
        invocation_id: str,
        invocation_run_ids: list[str] | None,
        expected_behavior: str,
        invocation_source: str,
        force_recompile: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        safe_run_id = self._normalize_run_id(run_id)
        selected = [str(item or "").strip() for item in (selected_tests or [])]
        selected = [item for item in selected if RUN_TEST_NAME_RE.fullmatch(item)]
        selected = list(dict.fromkeys(selected))
        inv_run_ids = [str(item or "").strip() for item in (invocation_run_ids or [])]
        inv_run_ids = [item for item in inv_run_ids if _RUN_ID_RE.fullmatch(item)]
        inv_run_ids = list(dict.fromkeys(inv_run_ids))
        if isinstance(prepared_payload, dict):
            payload = dict(prepared_payload)
            payload["run_id"] = safe_run_id
            payload["problem"] = problem
            payload["username"] = username
            payload["build_id"] = build_id
            payload["mode"] = mode
            payload["submission_path"] = str(submission_path or "")
            payload["selected_tests"] = list(selected)
            payload["invocation_id"] = invocation_id
            payload["invocation_run_ids"] = list(inv_run_ids)
            payload["expected_behavior"] = expected_behavior
            payload["invocation_source"] = invocation_source
            payload["force_recompile"] = bool(force_recompile)
        else:
            payload = self._build_task_payload(
                problem=problem,
                username=username,
                build_id=build_id,
                mode=mode,
                submission_path=submission_path,
                upload_content=upload_content,
                upload_filename=upload_filename,
                selected_tests=selected,
                invocation_id=invocation_id,
                invocation_run_ids=inv_run_ids,
                expected_behavior=expected_behavior,
                invocation_source=invocation_source,
                run_id=safe_run_id,
                force_recompile=bool(force_recompile),
            )
        task_id = ""
        summary: dict[str, object] | None = None
        while True:
            with self._state_lock:
                existing_task_id = str(self._task_id_by_run.get(safe_run_id) or "").strip()
                if existing_task_id:
                    existing_task = self._tasks_by_id.get(existing_task_id)
                    if existing_task is None:
                        self._task_id_by_run.pop(safe_run_id, None)
                    else:
                        existing_status = str(existing_task.get("status") or "").strip().lower()
                        if existing_status != self.STATUS_ENQUEUING:
                            return existing_task_id
                if not existing_task_id or existing_task_id not in self._tasks_by_id:
                    task_id = f"jt-{uuid.uuid4().hex[:12]}"
                    source_label = str(payload.get("source_label") or payload.get("source_name") or "upload")
                    summary = self._initial_summary(
                        run_id=safe_run_id,
                        task_id=task_id,
                        mode=mode,
                        source_label=source_label,
                        selected_tests=selected,
                        invocation_id=invocation_id,
                        invocation_run_ids=inv_run_ids,
                        expected_behavior=expected_behavior,
                        invocation_source=invocation_source,
                    )
                    now_text = now_iso()
                    self._tasks_by_id[task_id] = {
                        "id": task_id,
                        "run_id": safe_run_id,
                        "problem_slug": str(problem),
                        "username": str(username),
                        "build_id": str(build_id),
                        "mode": str(mode),
                        "status": self.STATUS_ENQUEUING,
                        "payload": dict(payload),
                        "result": {},
                        "error_text": "",
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "created_at": now_text,
                        "updated_at": now_text,
                        "completed_at": "",
                        "attempt_count": 0,
                    }
                    self._task_id_by_run[safe_run_id] = task_id
                    break
            # Another thread is creating the same run task; wait for terminal enqueue step.
            time.sleep(0.01)

        if summary is None or not task_id:
            raise RuntimeError("failed to allocate judgehost task")

        try:
            self._ensure_run_row(
                problem=problem,
                username=username,
                build_id=build_id,
                run_id=safe_run_id,
                mode=mode,
                summary=summary,
            )
        except Exception:
            with self._state_lock:
                row = self._tasks_by_id.get(task_id)
                if row is not None and str(row.get("status") or "").strip().lower() == self.STATUS_ENQUEUING:
                    self._tasks_by_id.pop(task_id, None)
                    if self._task_id_by_run.get(safe_run_id) == task_id:
                        self._task_id_by_run.pop(safe_run_id, None)
            raise

        self._domjudge_try_prequeue_cache_finalize(
            task_id=task_id,
            run_id=safe_run_id,
            payload=dict(payload),
        )
        with self._state_lock:
            row = self._tasks_by_id.get(task_id)
            if row is not None and str(row.get("status") or "").strip().lower() == self.STATUS_ENQUEUING:
                row["status"] = self.STATUS_QUEUED
                row["updated_at"] = now_iso()
        return task_id

    def _claim_lease_requeue_slot(self, *, interval_sec: float = 0.75) -> bool:
        now_mono = time.monotonic()
        with self._lease_requeue_lock:
            if now_mono < float(self._lease_requeue_next_ts):
                return False
            self._lease_requeue_next_ts = now_mono + max(0.05, float(interval_sec))
            return True

    def _requeue_expired_leases(self, conn=None, *, force: bool = False) -> None:
        if (not force) and (not self._claim_lease_requeue_slot()):
            return
        now_dt = datetime.now(timezone.utc)
        now_text = now_dt.isoformat()
        with self._state_lock:
            for task in self._tasks_by_id.values():
                if str(task.get("status") or "").strip().lower() != self.STATUS_LEASED:
                    continue
                lease_exp = _parse_iso_utc(task.get("lease_expires_at"))
                if lease_exp is None or lease_exp >= now_dt:
                    continue
                task["status"] = self.STATUS_QUEUED
                task["lease_owner"] = ""
                task["lease_expires_at"] = ""
                task["updated_at"] = now_text

    def _record_host_event_conn(
        self,
        conn=None,
        *,
        hostname: str,
        action: str,
        task_id: str = "",
        run_id: str = "",
        lease_expires_at: str = "",
    ) -> None:
        safe_host = self._normalize_hostname(hostname)
        safe_action = str(action or "").strip().lower() or "event"
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
            row["last_seen_at"] = now_text
            row["last_action"] = safe_action
            row["last_task_id"] = str(task_id or "").strip()
            row["last_run_id"] = str(run_id or "").strip()
            row["lease_expires_at"] = str(lease_expires_at or "").strip()
            row["update_count"] = int(row.get("update_count") or 0) + 1

    def _host_enabled_conn(self, conn=None, hostname: str = "") -> bool:
        safe_host = self._normalize_hostname(hostname)
        if not safe_host:
            return True
        with self._state_lock:
            row = self._hosts_state.get(safe_host)
            if row is None:
                return True
            return bool(row.get("enabled", True))

    def fetch_work(self, hostname: str, limit: int | None = None) -> list[dict[str, object]]:
        safe_host = self._normalize_hostname(hostname)
        cap = self._fetch_batch_size if limit is None else max(1, min(256, int(limit)))
        tasks: list[dict[str, object]] = []
        self._requeue_expired_leases()
        event_action = "fetch"
        event_task_id = ""
        event_run_id = ""
        event_lease_expires_at = ""
        with self._state_lock:
            host_row = self._hosts_state.get(safe_host)
            if host_row is not None and not bool(host_row.get("enabled", True)):
                event_action = "disabled"
            else:
                queued = [
                    dict(row)
                    for row in self._tasks_by_id.values()
                    if str(row.get("status") or "").strip().lower() == self.STATUS_QUEUED
                ]
                queued.sort(key=lambda item: str(item.get("created_at") or ""))
                lease_until = _now_iso_after(self._lease_sec)
                now_text = now_iso()
                for row in queued[:cap]:
                    task_id = str(row.get("id") or "").strip()
                    task = self._tasks_by_id.get(task_id)
                    if task is None:
                        continue
                    if str(task.get("status") or "").strip().lower() != self.STATUS_QUEUED:
                        continue
                    task["status"] = self.STATUS_LEASED
                    task["lease_owner"] = safe_host
                    task["lease_expires_at"] = lease_until
                    task["updated_at"] = now_text
                    task["attempt_count"] = int(task.get("attempt_count") or 0) + 1
                    payload_obj = task.get("payload")
                    tasks.append(
                        {
                            "task_id": task_id,
                            "run_id": str(task.get("run_id") or ""),
                            "problem": str(task.get("problem_slug") or ""),
                            "username": str(task.get("username") or ""),
                            "build_id": str(task.get("build_id") or ""),
                            "mode": str(task.get("mode") or ""),
                            "lease_expires_at": lease_until,
                            "payload": dict(payload_obj) if isinstance(payload_obj, dict) else {},
                        }
                    )
                if tasks:
                    tail = tasks[-1]
                    event_action = "lease"
                    event_task_id = str(tail.get("task_id") or "")
                    event_run_id = str(tail.get("run_id") or "")
                    event_lease_expires_at = lease_until
        self._record_host_event_conn(
            hostname=safe_host,
            action=event_action,
            task_id=event_task_id,
            run_id=event_run_id,
            lease_expires_at=event_lease_expires_at,
        )
        return tasks

    def renew_lease(self, task_id: str, hostname: str) -> bool:
        safe_host = self._normalize_hostname(hostname)
        token = str(task_id or "").strip()
        if not token:
            return False
        with self._state_lock:
            task = self._tasks_by_id.get(token)
            if task is None:
                self._record_host_event_conn(hostname=safe_host, action="heartbeat", task_id=token)
                return False
            if str(task.get("status") or "").strip().lower() != self.STATUS_LEASED:
                self._record_host_event_conn(hostname=safe_host, action="heartbeat", task_id=token)
                return False
            if str(task.get("lease_owner") or "").strip() != safe_host:
                self._record_host_event_conn(hostname=safe_host, action="heartbeat", task_id=token)
                return False
            now_text = now_iso()
            lease_until = _now_iso_after(self._lease_sec)
            task["lease_expires_at"] = lease_until
            task["updated_at"] = now_text
            self._record_host_event_conn(
                hostname=safe_host,
                action="heartbeat",
                task_id=token,
                lease_expires_at=lease_until,
            )
            return True

    def _load_run_summary(self, run_id: str) -> dict[str, object]:
        row = self._db_fetch_one("SELECT summary_json FROM runs WHERE id=?", [run_id])
        if row is None:
            return {}
        raw = str(row["summary_json"] or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

    def report_result(self, *, task_id: str, hostname: str, payload: dict[str, object]) -> dict[str, object]:
        safe_task_id = str(task_id or "").strip()
        if not safe_task_id:
            raise RuntimeError("task_id is required")
        safe_host = self._normalize_hostname(hostname)
        payload_obj = dict(payload or {})
        run_status_raw = str(
            payload_obj.get("run_status")
            or payload_obj.get("status")
            or payload_obj.get("result")
            or ""
        ).strip().lower()
        if run_status_raw in {"ok", "accepted", "pass", "passed", "success", "completed"}:
            run_status = "ok"
            task_status = self.STATUS_COMPLETED
        else:
            run_status = "failed"
            task_status = self.STATUS_FAILED
        error_text = str(payload_obj.get("error") or "").strip()

        with self._state_lock:
            row = self._tasks_by_id.get(safe_task_id)
            if row is None:
                raise RuntimeError("judgehost task not found")
            current_status = str(row.get("status") or "").strip().lower()
            lease_owner = str(row.get("lease_owner") or "").strip()
            if current_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                raise RuntimeError(f"judgehost task is not reportable (status={current_status})")
            if lease_owner and lease_owner != safe_host:
                raise RuntimeError("judgehost task lease owner mismatch")
            run_id = str(row.get("run_id") or "").strip()
            prev_status = current_status
            prev_lease_owner = lease_owner
            prev_lease_expires_at = str(row.get("lease_expires_at") or "").strip()
            row["status"] = self.STATUS_REPORTING
            row["updated_at"] = now_iso()

        try:
            existing_summary = self._load_run_summary(run_id)
            summary = payload_obj.get("summary")
            if isinstance(summary, dict):
                summary_obj: dict[str, object] = dict(summary)
            else:
                summary_obj = dict(existing_summary)
            if "tests" not in summary_obj:
                summary_obj["tests"] = list(existing_summary.get("tests") or [])
            if "source" not in summary_obj:
                summary_obj["source"] = str(existing_summary.get("source") or "upload")
            if "mode" not in summary_obj:
                summary_obj["mode"] = str(existing_summary.get("mode") or "pass-fail")
            if "limits" not in summary_obj:
                summary_obj["limits"] = dict(existing_summary.get("limits") or {})
            if "usage" not in summary_obj:
                summary_obj["usage"] = dict(existing_summary.get("usage") or {})
            if "invocation_backend" not in summary_obj:
                summary_obj["invocation_backend"] = "domjudge-judgehost"
            if run_status != "ok":
                if error_text:
                    summary_obj["error"] = error_text
                elif "error" not in summary_obj:
                    summary_obj["error"] = "judgehost reported failure"

            invocation_obj = existing_summary.get("invocation")
            if isinstance(invocation_obj, dict):
                inv = dict(invocation_obj)
                inv["completed"] = True
                summary_obj.setdefault("invocation", inv)
                if isinstance(summary_obj.get("invocation"), dict):
                    summary_obj["invocation"]["completed"] = True

            judgehost_block = dict(summary_obj.get("judgehost") or {})
            judgehost_block["task_id"] = safe_task_id
            judgehost_block["hostname"] = safe_host
            judgehost_block["status"] = task_status
            summary_obj["judgehost"] = judgehost_block

            encoded_summary = self._run_service._summary_for_db(summary_obj)
            finished_at = now_iso()
            self._db_execute(
                """
                UPDATE runs
                SET status=?, summary_json=?, finished_at=?
                WHERE id=?
                """,
                [run_status, encoded_summary, finished_at, run_id],
            )
        except Exception:
            with self._state_lock:
                row = self._tasks_by_id.get(safe_task_id)
                if row is not None and str(row.get("status") or "").strip().lower() == self.STATUS_REPORTING:
                    row["status"] = prev_status
                    row["lease_owner"] = prev_lease_owner
                    row["lease_expires_at"] = prev_lease_expires_at
                    row["updated_at"] = now_iso()
            raise

        with self._state_lock:
            row = self._tasks_by_id.get(safe_task_id)
            if row is not None:
                row["status"] = task_status
                row["result"] = dict(payload_obj)
                row["error_text"] = error_text
                row["lease_owner"] = safe_host
                row["lease_expires_at"] = ""
                row["updated_at"] = finished_at
                row["completed_at"] = finished_at
        self._record_host_event_conn(
            hostname=safe_host,
            action="report",
            task_id=safe_task_id,
            run_id=run_id,
        )
        try:
            artifact_path = self._run_service.fs_manager.prepare_run_root(run_id).resolve()
            artifact_path.mkdir(parents=True, exist_ok=True)
            (artifact_path / "summary.json").write_text(
                json.dumps(summary_obj, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("failed to persist judgehost summary artifact for run %s: %s", run_id, exc)
        return {"task_id": safe_task_id, "run_id": run_id, "status": run_status}

    def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
        safe_task_id = str(task_id or "").strip()
        if not safe_task_id:
            raise RuntimeError("judgehost task id is required")
        timeout = self._wait_timeout_sec if timeout_sec is None else max(1.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        while True:
            row = self._task_by_id(safe_task_id)
            if row is None:
                raise RuntimeError("judgehost task disappeared")
            status = str(row.get("status") or "").strip().lower()
            run_id = str(row.get("run_id") or "").strip()
            if status == self.STATUS_COMPLETED:
                return run_id
            if status == self.STATUS_FAILED:
                detail = str(row.get("error_text") or "").strip() or "judgehost task failed"
                raise RuntimeError(detail)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"judgehost task timed out after {int(timeout)}s")
            time.sleep(self._wait_poll_sec)

    def _host_status_rows(self) -> tuple[list[dict[str, object]], int]:
        now_dt = datetime.now(timezone.utc)
        now_ts = time.time()
        active_by_host: dict[str, int] = {}
        cases_5m: dict[str, int] = {}
        cases_15m: dict[str, int] = {}
        cases_1h: dict[str, int] = {}
        cases_5h: dict[str, int] = {}
        with self._state_lock:
            for task in self._tasks_by_id.values():
                if str(task.get("status") or "").strip().lower() != self.STATUS_LEASED:
                    continue
                host = self._normalize_hostname(str(task.get("lease_owner") or "").strip())
                if not host:
                    continue
                lease_expires_at = _parse_iso_utc(task.get("lease_expires_at"))
                if lease_expires_at is None or lease_expires_at < now_dt:
                    continue
                active_by_host[host] = int(active_by_host.get(host, 0)) + 1
            for host, events in self._host_judged_case_events.items():
                keep: list[float] = []
                c5 = 0
                c15 = 0
                c1h = 0
                c5h = 0
                for ts in events:
                    age = now_ts - float(ts)
                    if age <= 5 * 3600:
                        keep.append(ts)
                        c5h += 1
                        if age <= 3600:
                            c1h += 1
                            if age <= 900:
                                c15 += 1
                                if age <= 300:
                                    c5 += 1
                self._host_judged_case_events[host] = keep
                cases_5m[host] = c5
                cases_15m[host] = c15
                cases_1h[host] = c1h
                cases_5h[host] = c5h
            last_judging_by_host = {k: dict(v) for k, v in self._host_last_judging.items()}
            host_rows = sorted(
                (dict(row) for row in self._hosts_state.values()),
                key=lambda item: (str(item.get("last_seen_at") or ""), str(item.get("hostname") or "")),
                reverse=True,
            )
        rows_out: list[dict[str, object]] = []
        online_count = 0
        for row in host_rows:
            hostname = self._normalize_hostname(str(row.get("hostname") or "").strip())
            if not hostname:
                continue
            enabled_flag = bool(row.get("enabled", True))
            last_seen = str(row.get("last_seen_at") or "").strip()
            last_seen_dt = _parse_iso_utc(last_seen)
            age_sec: int | None = None
            is_online = False
            if last_seen_dt is not None:
                delta = max(0.0, (now_dt - last_seen_dt).total_seconds())
                age_sec = int(delta)
                is_online = delta <= float(self._online_window_sec)
            if is_online and enabled_flag:
                online_count += 1
            count_5m = int(cases_5m.get(hostname, 0))
            count_15m = int(cases_15m.get(hostname, 0))
            count_1h = int(cases_1h.get(hostname, 0))
            count_5h = int(cases_5h.get(hostname, 0))
            last_judging = dict(last_judging_by_host.get(hostname, {}))
            rows_out.append(
                {
                    "hostname": hostname,
                    "enabled": enabled_flag,
                    "online": is_online,
                    "age_sec": age_sec,
                    "last_seen_at": last_seen,
                    "first_seen_at": str(row.get("first_seen_at") or "").strip(),
                    "last_action": str(row.get("last_action") or "").strip(),
                    "last_task_id": str(row.get("last_task_id") or "").strip(),
                    "last_run_id": str(row.get("last_run_id") or "").strip(),
                    "lease_expires_at": str(row.get("lease_expires_at") or "").strip(),
                    "active_leases": int(active_by_host.get(hostname, 0)),
                    "update_count": int(row.get("update_count") or 0),
                    "load_5m": float(count_5m / 300.0),
                    "load_15m": float(count_15m / 900.0),
                    "load_1h": float(count_1h / 3600.0),
                    "load_5h": float(count_5h / 18000.0),
                    "judged_cases_5m": count_5m,
                    "judged_cases_15m": count_15m,
                    "judged_cases_1h": count_1h,
                    "judged_cases_5h": count_5h,
                    "last_judging": str(last_judging.get("label") or "-"),
                    "last_judging_at": str(last_judging.get("updated_at") or ""),
                }
            )
        return rows_out, online_count

    def set_host_enabled(self, hostname: str, enabled: bool) -> dict[str, int]:
        safe_host = self._normalize_hostname(hostname)
        now_text = now_iso()
        safe_enabled = bool(enabled)
        released_tasks = 0
        with self._state_lock:
            row = self._host_state_row(safe_host)
            row["enabled"] = safe_enabled
            row["last_seen_at"] = now_text
            row["last_action"] = "enabled" if safe_enabled else "disabled"
            row["update_count"] = int(row.get("update_count") or 0) + 1
            if not safe_enabled:
                for task in self._tasks_by_id.values():
                    if str(task.get("lease_owner") or "").strip() != safe_host:
                        continue
                    task_status = str(task.get("status") or "").strip().lower()
                    if task_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                        continue
                    task["status"] = self.STATUS_QUEUED
                    task["lease_owner"] = ""
                    task["lease_expires_at"] = ""
                    task["updated_at"] = now_text
                    released_tasks += 1
        released_jobs = 0
        released_cases = 0
        if not safe_enabled:
            with self._domdb_conn() as conn:
                job_upd = conn.execute(
                    """
                    UPDATE judgehost_domjudge_jobs
                    SET lease_owner=NULL, status='queued', updated_at=?
                    WHERE lease_owner=? AND status IN ('leased','queued')
                    """,
                    [now_text, safe_host],
                )
                released_jobs = int(job_upd.rowcount or 0)
                case_upd = conn.execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='pending', lease_owner=NULL, updated_at=?
                    WHERE lease_owner=? AND status='leased'
                    """,
                    [now_text, safe_host],
                )
                released_cases = int(case_upd.rowcount or 0)
        return {
            "released_tasks": released_tasks,
            "released_jobs": released_jobs,
            "released_cases": released_cases,
        }

    def status(self) -> dict[str, object]:
        counts = self._task_status_counts()
        host_rows, online_count = self._host_status_rows()
        return {
            "enabled": bool(self._enabled),
            "auth_configured": bool(self._api_token),
            "auth_username": self.api_username(),
            "fetch_batch_size": self._fetch_batch_size,
            "lease_sec": self._lease_sec,
            "wait_timeout_sec": self._wait_timeout_sec,
            "wait_poll_sec": self._wait_poll_sec,
            "online_window_sec": self._online_window_sec,
            "hosts_total": len(host_rows),
            "hosts_online": int(online_count),
            "hosts": host_rows,
            "queue": {
                "queued": int(counts.get(self.STATUS_QUEUED, 0)),
                "leased": int(counts.get(self.STATUS_LEASED, 0)),
                "completed": int(counts.get(self.STATUS_COMPLETED, 0)),
                "failed": int(counts.get(self.STATUS_FAILED, 0)),
            },
        }

    def cancel_tasks_for_runs(self, run_ids: list[str], *, reason: str) -> int:
        safe_reason = str(reason or "").strip() or "verification cancelled by user"
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return 0
        now_text = now_iso()
        affected = 0
        with self._state_lock:
            for run_id in safe_ids:
                task_id = str(self._task_id_by_run.get(run_id) or "").strip()
                if not task_id:
                    continue
                row = self._tasks_by_id.get(task_id)
                if row is None:
                    continue
                status = str(row.get("status") or "").strip().lower()
                if status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    continue
                row["status"] = self.STATUS_FAILED
                row["result"] = {"cancelled": True, "reason": safe_reason, "error": safe_reason}
                row["error_text"] = safe_reason
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["updated_at"] = now_text
                row["completed_at"] = now_text
                affected += 1
        return affected

    def active_task_count_for_build(self, build_id: str) -> int:
        safe_build = str(build_id or "").strip()
        if not safe_build:
            return 0
        count = 0
        with self._state_lock:
            for row in self._tasks_by_id.values():
                if str(row.get("build_id") or "").strip() != safe_build:
                    continue
                status = str(row.get("status") or "").strip().lower()
                if status in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    count += 1
        return count

    def startup_cancel_inflight_tasks(self, *, reason: str) -> list[str]:
        safe_reason = str(reason or "").strip() or "startup reset"
        now_text = now_iso()
        run_ids: list[str] = []
        with self._state_lock:
            for row in self._tasks_by_id.values():
                status = str(row.get("status") or "").strip().lower()
                if status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    continue
                run_id = str(row.get("run_id") or "").strip()
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
                row["status"] = self.STATUS_FAILED
                row["result"] = {"cancelled": True, "reason": safe_reason, "error": safe_reason}
                row["error_text"] = safe_reason
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["updated_at"] = now_text
                row["completed_at"] = now_text
        return run_ids

    def forget_problem_tasks(self, problem_slug: str) -> int:
        safe_problem = str(problem_slug or "").strip()
        if not safe_problem:
            return 0
        removed = 0
        with self._state_lock:
            remove_ids = [
                task_id
                for task_id, row in self._tasks_by_id.items()
                if str(row.get("problem_slug") or "").strip() == safe_problem
            ]
            for task_id in remove_ids:
                row = self._tasks_by_id.pop(task_id, None)
                if row is None:
                    continue
                run_id = str(row.get("run_id") or "").strip()
                if run_id and self._task_id_by_run.get(run_id) == task_id:
                    self._task_id_by_run.pop(run_id, None)
                removed += 1
        return removed

    def cancel_domjudge_jobs_for_runs(self, run_ids: list[str], *, final_status: str = "failed") -> int:
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return 0
        placeholders = ",".join(("?" for _ in safe_ids))
        now_text = now_iso()
        with self._domdb_conn() as conn:
            job_rows = conn.execute(
                f"SELECT job_id FROM judgehost_domjudge_jobs WHERE run_id IN ({placeholders}) AND status IN ('queued','leased')",
                [*safe_ids],
            ).fetchall()
            job_ids = [int(row["job_id"]) for row in job_rows if row is not None and row["job_id"] is not None]
            if not job_ids:
                return 0
            jph = ",".join(("?" for _ in job_ids))
            conn.execute(
                f"""
                UPDATE judgehost_domjudge_cases
                SET status='reported',
                    lease_owner=NULL,
                    runresult=CASE WHEN runresult IS NULL OR TRIM(runresult)='' THEN 'internal-error' ELSE runresult END,
                    runtime_sec=COALESCE(runtime_sec, 0),
                    cpu_sec=COALESCE(cpu_sec, 0),
                    wall_sec=COALESCE(wall_sec, 0),
                    memory_kb=COALESCE(memory_kb, 0),
                    updated_at=?
                WHERE job_id IN ({jph}) AND status IN ('pending','leased')
                """,
                [now_text, *job_ids],
            )
            conn.execute(
                f"""
                UPDATE judgehost_domjudge_jobs
                SET status=?,
                    lease_owner=NULL,
                    completed_at=COALESCE(completed_at, ?),
                    updated_at=?
                WHERE job_id IN ({jph}) AND status IN ('queued','leased')
                """,
                [str(final_status or "failed"), now_text, now_text, *job_ids],
            )
            return len(job_ids)

    def cancel_all_domjudge_inflight(self) -> int:
        now_text = now_iso()
        with self._domdb_conn() as conn:
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET status='failed',
                    lease_owner=NULL,
                    updated_at=?,
                    completed_at=COALESCE(completed_at, ?)
                WHERE status IN ('queued','leased')
                """,
                [now_text, now_text],
            )
            case_upd = conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='reported',
                    lease_owner=NULL,
                    runresult=CASE WHEN runresult IS NULL OR TRIM(runresult)='' THEN 'internal-error' ELSE runresult END,
                    updated_at=?
                WHERE status IN ('pending','leased')
                """,
                [now_text],
            )
            try:
                return int(case_upd.rowcount or 0)
            except Exception:
                return 0

    def domjudge_case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return {}
        placeholders = ",".join(("?" for _ in safe_ids))
        rows = self._db_fetch_all(
            f"""
            SELECT j.run_id AS run_id,
                   COUNT(c.id) AS total_cases,
                   SUM(CASE WHEN c.status='reported' THEN 1 ELSE 0 END) AS reported_cases,
                   SUM(CASE WHEN c.status='leased' THEN 1 ELSE 0 END) AS leased_cases
            FROM judgehost_domjudge_jobs j
            JOIN judgehost_domjudge_cases c ON c.job_id=j.job_id
            WHERE j.run_id IN ({placeholders})
            GROUP BY j.run_id
            """,
            [*safe_ids],
        )
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            run_id = str(row["run_id"] or "").strip()
            if not run_id:
                continue
            total = max(0, int(row["total_cases"] or 0))
            reported = max(0, int(row["reported_cases"] or 0))
            leased = max(0, int(row["leased_cases"] or 0))
            out[run_id] = {"total": total, "reported": min(total, reported) if total > 0 else reported, "leased": leased}
        return out

    def domjudge_case_cells_for_runs(self, run_ids: list[str]) -> list[dict[str, object]]:
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return []
        placeholders = ",".join(("?" for _ in safe_ids))
        rows = self._db_fetch_all(
            f"""
            SELECT j.run_id AS run_id,
                   c.test_name AS test_name,
                   c.status AS status,
                   c.runresult AS runresult,
                   c.cpu_sec AS cpu_sec,
                   c.runtime_sec AS runtime_sec,
                   c.wall_sec AS wall_sec,
                   c.memory_kb AS memory_kb
            FROM judgehost_domjudge_jobs j
            JOIN judgehost_domjudge_cases c ON c.job_id=j.job_id
            WHERE j.run_id IN ({placeholders})
            ORDER BY j.run_id ASC, c.ordinal ASC, c.id ASC
            """,
            [*safe_ids],
        )
        return [dict(row) for row in rows]

    def domjudge_buildsolve_progress(self, build_id: str) -> dict[str, int]:
        safe_build = str(build_id or "").strip()
        if not safe_build:
            return {"total": 0, "reported": 0}
        run_ids: list[str] = []
        with self._state_lock:
            for row in self._tasks_by_id.values():
                if str(row.get("build_id") or "").strip() != safe_build:
                    continue
                run_id = str(row.get("run_id") or "").strip()
                if (not run_id) or (not run_id.startswith("r-buildsolve-")):
                    continue
                if run_id not in run_ids:
                    run_ids.append(run_id)
        if not run_ids:
            return {"total": 0, "reported": 0}
        placeholders = ",".join(("?" for _ in run_ids))
        row = self._db_fetch_one(
            f"""
            SELECT COUNT(c.id) AS total_cases,
                   SUM(CASE WHEN c.status='reported' THEN 1 ELSE 0 END) AS reported_cases
            FROM judgehost_domjudge_jobs j
            JOIN judgehost_domjudge_cases c ON c.job_id=j.job_id
            WHERE j.run_id IN ({placeholders})
            """,
            [*run_ids],
        )
        if row is None:
            return {"total": 0, "reported": 0}
        total = max(0, int(row["total_cases"] or 0))
        reported = max(0, int(row["reported_cases"] or 0))
        if total > 0 and reported > total:
            reported = total
        return {"total": total, "reported": reported}

    def forget_domjudge_runs(self, run_ids: list[str]) -> int:
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return 0
        placeholders = ",".join(("?" for _ in safe_ids))
        with self._domdb_conn() as conn:
            conn.execute(
                f"DELETE FROM judgehost_domjudge_cases WHERE run_id IN ({placeholders})",
                [*safe_ids],
            )
            cur = conn.execute(
                f"DELETE FROM judgehost_domjudge_jobs WHERE run_id IN ({placeholders})",
                [*safe_ids],
            )
            try:
                return int(cur.rowcount or 0)
            except Exception:
                return 0

    @staticmethod
    def _domjudge_parse_float(raw: object, default: float = 0.0) -> float:
        try:
            value = float(str(raw or "").strip())
        except Exception:
            return float(default)
        if value < 0:
            return float(default)
        return value

    @staticmethod
    def _domjudge_parse_int(raw: object, default: int = 0) -> int:
        try:
            return int(raw)
        except Exception:
            return int(default)

    def _domjudge_run_time_limit_sec(self, run_cfg_obj: dict[str, object]) -> float:
        cfg = run_cfg_obj if isinstance(run_cfg_obj, dict) else {}
        tl_sec = self._domjudge_parse_float(cfg.get("time_limit"), 0.0)
        if tl_sec > 0:
            return float(tl_sec)
        tl_ms = self._domjudge_parse_int(cfg.get("time_limit_ms"), 0)
        if tl_ms > 0:
            return float(max(0.0, float(tl_ms) / 1000.0))
        return 0.0

    def _domjudge_rewrite_untrusted_runresult(
        self,
        runresult: str,
        *,
        cpu_sec: float,
        run_cfg_obj: dict[str, object],
    ) -> str:
        token = str(runresult or "").strip().lower()
        if token not in {"wrong-answer", "run-error", "no-output"}:
            return token
        tl_sec = self._domjudge_run_time_limit_sec(run_cfg_obj)
        if tl_sec <= 0:
            return token
        cpu_total_sec = self._domjudge_parse_float(cpu_sec, 0.0)
        if cpu_total_sec <= float(tl_sec) * 2.0:
            return token
        return "timelimit"

    @staticmethod
    def _domjudge_parse_meta_text(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in str(text or "").splitlines():
            token = str(line or "").strip()
            if not token or ":" not in token:
                continue
            key, value = token.split(":", 1)
            safe_key = str(key or "").strip().lower()
            if not safe_key:
                continue
            out[safe_key] = str(value or "").strip()
        return out

    @staticmethod
    def _domjudge_bool(raw: object, default: bool = False) -> bool:
        text = str(raw or "").strip().lower()
        if not text:
            return bool(default)
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n"}:
            return False
        return bool(default)

    @staticmethod
    def _domjudge_feedback_line_from_text(text: str, *, max_chars: int = 240) -> str:
        for raw_line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = " ".join(str(raw_line or "").split())
            if not line:
                continue
            if len(line) <= max_chars:
                return line
            return line[:max_chars].rstrip() + "..."
        return ""

    @staticmethod
    def _domjudge_feedback_line_from_bytes(blob: bytes, *, max_chars: int = 240) -> str:
        return JudgehostTaskService._domjudge_feedback_line_from_text(
            bytes(blob or b"").decode("utf-8", errors="replace"),
            max_chars=max_chars,
        )

    @staticmethod
    def _domjudge_submit_id_from_run_id(run_id: str) -> str:
        token = str(run_id or "").strip()
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", token).strip("-")
        if not safe:
            safe = f"r-{uuid.uuid4().hex[:12]}"
        if not _DOMJUDGE_SUBMIT_ID_RE.fullmatch(safe):
            safe = f"s-{uuid.uuid4().hex[:12]}"
        return safe[:64]

    @staticmethod
    def _domjudge_contest_id(raw: object) -> str:
        token = str(raw or "").strip()
        if not _DOMJUDGE_CONTEST_ID_RE.fullmatch(token):
            return "local"
        return token

    def _domjudge_work_root(self, task_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task_id or "").strip()).strip("-")
        if not safe:
            safe = f"task-{uuid.uuid4().hex[:8]}"
        return (self._settings.run_root / "judgehost-domjudge" / safe).resolve()

    @staticmethod
    def _domjudge_b64_decode(text: object) -> bytes:
        raw = str(text or "").strip()
        if not raw:
            return b""
        try:
            return base64.b64decode(raw, validate=False)
        except Exception:
            return b""

    @staticmethod
    def _domjudge_json_hash(payload: object) -> str:
        encoded = canonical_json(payload, ensure_ascii=False)
        return sha256_hex_text(encoded)

    @staticmethod
    def _domjudge_source_hash(source_name: str, source_bytes: bytes) -> str:
        blob = bytes(source_bytes or b"")
        name = str(source_name or "").strip()
        payload = blob + b"\x00" + name.encode("utf-8", errors="replace")
        return sha256_hex_bytes(payload)

    @staticmethod
    def _domjudge_manifest_digest(rows: list[dict[str, object]]) -> str:
        canonical_rows = sorted(
            [
                {
                    "path": str(item.get("path") or "").strip(),
                    "blob_key": str(item.get("blob_key") or "").strip(),
                    "sha256": str(item.get("sha256") or "").strip().lower(),
                    "size": int(item.get("size") or 0),
                    "mode": str(item.get("mode") or "").strip(),
                }
                for item in rows
                if isinstance(item, dict)
            ],
            key=lambda item: (item["path"], item["blob_key"], item["sha256"], item["size"], item["mode"]),
        )
        return sha256_hex_json(canonical_rows, ensure_ascii=False)

    def _domjudge_manifest_from_files(self, files: dict[str, bytes]) -> tuple[list[dict[str, object]], str]:
        rows: list[dict[str, object]] = []
        for raw_name, raw_blob in sorted(files.items(), key=lambda item: str(item[0] or "")):
            path = Path(str(raw_name or "").strip()).name
            if (not path) or (_DOMJUDGE_CACHE_NAME_RE.fullmatch(path) is None):
                continue
            blob = bytes(raw_blob or b"")
            sha256_text = sha256_hex_bytes(blob)
            size_value = int(len(blob))
            mode = "0644"
            blob_key = f"{sha256_text}:{size_value}:{mode}"
            rows.append(
                {
                    "path": path,
                    "blob_key": blob_key,
                    "sha256": sha256_text,
                    "size": size_value,
                    "mode": mode,
                }
            )
        return rows, self._domjudge_manifest_digest(rows)

    def _domjudge_validate_cache_entry(
        self,
        *,
        kind: str,
        key_hash: str,
        signature: str,
        entry: dict[str, object],
    ) -> bool:
        value_obj = entry.get("value")
        files_obj = entry.get("files")
        value_map = value_obj if isinstance(value_obj, dict) else {}
        files_map = files_obj if isinstance(files_obj, dict) else {}
        manifest_raw = value_map.get("manifest")
        if not isinstance(manifest_raw, list):
            return False
        manifest_rows: list[dict[str, object]] = []
        for raw in manifest_raw:
            if not isinstance(raw, dict):
                return False
            path = Path(str(raw.get("path") or "").strip()).name
            if (not path) or (_DOMJUDGE_CACHE_NAME_RE.fullmatch(path) is None):
                return False
            sha = str(raw.get("sha256") or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", sha) is None:
                return False
            mode = str(raw.get("mode") or "").strip() or "0644"
            try:
                size = max(0, int(raw.get("size") or 0))
            except Exception:
                return False
            blob_key = str(raw.get("blob_key") or "").strip()
            if not blob_key:
                return False
            manifest_rows.append(
                {
                    "path": path,
                    "blob_key": blob_key,
                    "sha256": sha,
                    "size": size,
                    "mode": mode,
                }
            )
        declared_digest = str(value_map.get("manifest_digest") or "").strip().lower()
        computed_digest = self._domjudge_manifest_digest(manifest_rows)
        if (not declared_digest) or (declared_digest != computed_digest):
            return False
        manifest_paths = {str(item["path"]) for item in manifest_rows}
        file_paths = {str(Path(str(name or "").strip()).name) for name in files_map.keys()}
        if manifest_paths != file_paths:
            return False
        seen_blob: dict[str, tuple[str, int]] = {}
        for row in manifest_rows:
            path = str(row["path"])
            file_meta = files_map.get(path)
            if not isinstance(file_meta, dict):
                return False
            meta_sha = str(file_meta.get("sha256") or "").strip().lower()
            try:
                meta_size = max(0, int(file_meta.get("size") or 0))
            except Exception:
                return False
            if meta_sha != str(row["sha256"]) or meta_size != int(row["size"]):
                return False
            blob_key = str(row["blob_key"])
            expected = (str(row["sha256"]), int(row["size"]))
            blob = self._domjudge_cache_read_blob(
                kind=kind,
                key_hash=key_hash,
                signature=signature,
                name=path,
            )
            if blob is None:
                return False
            blob_sha = sha256_hex_bytes(blob)
            blob_size = int(len(blob))
            if blob_sha != expected[0] or blob_size != expected[1]:
                return False
            existing = seen_blob.get(blob_key)
            if existing is not None:
                if existing != (blob_sha, blob_size):
                    return False
                continue
            seen_blob[blob_key] = (blob_sha, blob_size)
        return True

    @staticmethod
    def _domjudge_safe_hash(raw: str) -> str:
        token = str(raw or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", token):
            return token
        return sha256_hex_text(token, errors="replace")

    def _domjudge_case_cache_ref(
        self,
        *,
        source_hash: str,
        compile_hash: str,
        run_hash: str,
        compare_hash: str,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
        testcase_hash: str,
    ) -> tuple[str, str]:
        safe_testcase_hash = self._domjudge_safe_hash(testcase_hash)
        signature = JudgeFsIndexService.signature(
            {
                "schema": "v2",
                "source_hash": str(source_hash or "").strip().lower(),
                "compile_hash": str(compile_hash or "").strip().lower(),
                "run_hash": str(run_hash or "").strip().lower(),
                "compare_hash": str(compare_hash or "").strip().lower(),
                "compile_config_hash": str(compile_config_hash or "").strip().lower(),
                "run_config_hash": str(run_config_hash or "").strip().lower(),
                "compare_config_hash": str(compare_config_hash or "").strip().lower(),
                "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
            }
        )
        return (safe_testcase_hash, signature)

    def _domjudge_solve_output_cache_ref(
        self,
        *,
        source_hash: str,
        compile_hash: str,
        run_hash: str,
        compile_config_hash: str,
        run_config_hash: str,
        toolchain_cmd_digest: str,
        testcase_input_hash: str,
    ) -> tuple[str, str]:
        safe_input_hash = self._domjudge_safe_hash(testcase_input_hash)
        signature = JudgeFsIndexService.signature(
            {
                "schema": "v2",
                "source_hash": str(source_hash or "").strip().lower(),
                "compile_hash": str(compile_hash or "").strip().lower(),
                "run_hash": str(run_hash or "").strip().lower(),
                "compile_config_hash": str(compile_config_hash or "").strip().lower(),
                "run_config_hash": str(run_config_hash or "").strip().lower(),
                "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
            }
        )
        return (safe_input_hash, signature)

    def _domjudge_cache_get(self, kind: str, key_hash: str, signature: str) -> dict[str, object] | None:
        service = self._judge_fs_index_service
        if service is None:
            return None
        entry = service.get(kind=kind, key_hash=key_hash, signature=signature)
        if not isinstance(entry, dict):
            return None
        value_obj = entry.get("value")
        tags_obj = entry.get("tags")
        files_obj = entry.get("files")
        resolved = {
            "key_hash": key_hash,
            "signature": signature,
            "value": dict(value_obj) if isinstance(value_obj, dict) else {},
            "tags": dict(tags_obj) if isinstance(tags_obj, dict) else {},
            "files": dict(files_obj) if isinstance(files_obj, dict) else {},
            "created_at": str(entry.get("created_at") or "").strip(),
            "updated_at": str(entry.get("updated_at") or "").strip(),
        }
        if not self._domjudge_validate_cache_entry(
            kind=kind,
            key_hash=key_hash,
            signature=signature,
            entry=resolved,
        ):
            self._domjudge_cache_delete(kind=kind, key_hash=key_hash, signature=signature)
            return None
        return resolved

    def _domjudge_cache_put(
        self,
        kind: str,
        key_hash: str,
        signature: str,
        value: dict[str, object],
        *,
        files: dict[str, bytes] | None = None,
        tags: dict[str, object] | None = None,
    ) -> str:
        service = self._judge_fs_index_service
        if service is None:
            return ""
        service.put(
            kind=kind,
            key_hash=key_hash,
            signature=signature,
            value=value,
            files=files,
            tags=tags,
        )
        return signature

    def _domjudge_cache_delete(self, kind: str, key_hash: str, signature: str) -> None:
        service = self._judge_fs_index_service
        if service is None:
            return
        service.delete(kind=kind, key_hash=key_hash, signature=signature)

    def _domjudge_cache_read_blob(self, kind: str, key_hash: str, signature: str, name: str) -> bytes | None:
        service = self._judge_fs_index_service
        if service is None:
            return None
        return service.read_blob(kind=kind, key_hash=key_hash, signature=signature, name=name)

    @staticmethod
    def _domjudge_cache_blob_ref(*, kind: str, key_hash: str, signature: str, name: str) -> str:
        safe_kind = str(kind or "").strip().lower()
        safe_key = str(key_hash or "").strip().lower()
        safe_sig = str(signature or "").strip().lower()
        safe_name = Path(str(name or "").strip()).name
        return f"cache://{safe_kind}/{safe_key}/{safe_sig}/{safe_name}"

    @staticmethod
    def _domjudge_parse_cache_blob_ref(token: str) -> tuple[str, str, str, str] | None:
        match = _DOMJUDGE_CACHE_BLOB_REF_RE.fullmatch(str(token or "").strip())
        if match is None:
            return None
        return (
            str(match.group("kind") or "").strip().lower(),
            str(match.group("key") or "").strip().lower(),
            str(match.group("sig") or "").strip().lower(),
            str(match.group("name") or "").strip(),
        )

    def _domjudge_materialize_cached_case(
        self,
        *,
        cache_kind: str,
        cache_key_hash: str,
        cache_signature: str,
        cache_value: dict[str, object],
        cache_files: dict[str, object] | None = None,
    ) -> dict[str, object]:
        mapping = {
            "program.out": "output_run_rel",
            "program.err": "output_error_rel",
            "system.out": "output_system_rel",
            "judgemessage.txt": "output_diff_rel",
            "program.meta": "metadata_rel",
            "compare.meta": "compare_metadata_rel",
            "teammessage.txt": "team_message_rel",
        }
        rel_map: dict[str, str] = {}
        files_map = dict(cache_files) if isinstance(cache_files, dict) else {}
        for blob_name, rel_key in mapping.items():
            if blob_name not in files_map:
                continue
            rel_map[rel_key] = self._domjudge_cache_blob_ref(
                kind=cache_kind,
                key_hash=cache_key_hash,
                signature=cache_signature,
                name=blob_name,
            )
        return {
            "runresult": str(cache_value.get("runresult") or "correct").strip().lower(),
            "runtime_sec": self._domjudge_parse_float(cache_value.get("runtime_sec"), 0.0),
            "cpu_sec": self._domjudge_parse_float(cache_value.get("cpu_sec"), 0.0),
            "wall_sec": self._domjudge_parse_float(cache_value.get("wall_sec"), 0.0),
            "memory_kb": max(0, self._domjudge_parse_int(cache_value.get("memory_kb"), 0)),
            "score_text": str(cache_value.get("score_text") or "").strip(),
            **rel_map,
        }

    @staticmethod
    def _domjudge_sha256_bytes(blob: bytes) -> str:
        return sha256_hex_bytes(blob)

    @staticmethod
    def _domjudge_hash_of_hashes(hex_hashes: list[str]) -> str:
        return sha256_hex_of_hashes(hex_hashes)

    def _domjudge_set_hash_from_blobs(self, blobs: list[bytes]) -> str:
        parts = [self._domjudge_sha256_bytes(blob) for blob in blobs]
        return self._domjudge_hash_of_hashes(parts)

    def _domjudge_read_artifact_blob(self, work_root: Path, token: str) -> bytes | None:
        safe_token = str(token or "").strip()
        if not safe_token:
            return None
        parsed = self._domjudge_parse_cache_blob_ref(safe_token)
        if parsed is not None:
            kind, key_hash, signature, name = parsed
            return self._domjudge_cache_read_blob(kind=kind, key_hash=key_hash, signature=signature, name=name)
        source = (work_root / safe_token).resolve()
        if source == work_root or work_root not in source.parents:
            return None
        if (not source.exists()) or (not source.is_file()) or source.is_symlink():
            return None
        try:
            return source.read_bytes()
        except OSError:
            return None

    def resolve_artifact_blob(self, token: str, *, work_root: str | Path | None = None) -> bytes | None:
        safe_token = str(token or "").strip()
        if not safe_token:
            return None
        parsed = self._domjudge_parse_cache_blob_ref(safe_token)
        if parsed is not None:
            kind, key_hash, signature, name = parsed
            return self._domjudge_cache_read_blob(kind=kind, key_hash=key_hash, signature=signature, name=name)
        if work_root is None:
            return None
        root = Path(str(work_root)).resolve()
        return self._domjudge_read_artifact_blob(root, safe_token)

    def _domjudge_store_case_cache(
        self,
        *,
        key_parts: dict[str, object],
        tags: dict[str, object],
        runresult: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        score_text: str,
        files: dict[str, bytes],
    ) -> None:
        manifest_rows, manifest_digest = self._domjudge_manifest_from_files(files)
        key_hash = self._domjudge_cache_put(
            self.CASE_CACHE_KIND,
            str(key_parts.get("key_hash") or ""),
            str(key_parts.get("signature") or ""),
            {
                "runresult": str(runresult or "").strip().lower(),
                "runtime_sec": float(max(0.0, runtime_sec)),
                "cpu_sec": float(max(0.0, cpu_sec)),
                "wall_sec": float(max(0.0, wall_sec)),
                "memory_kb": int(max(0, memory_kb)),
                "score_text": str(score_text or "").strip(),
                "manifest": manifest_rows,
                "manifest_digest": manifest_digest,
            },
            files=files,
            tags=tags,
        )
        if not key_hash:
            return

    def _domjudge_store_solve_output_cache(
        self,
        *,
        key_parts: dict[str, object],
        tags: dict[str, object],
        output_hash: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        files: dict[str, bytes],
    ) -> None:
        manifest_rows, manifest_digest = self._domjudge_manifest_from_files(files)
        key_hash = self._domjudge_cache_put(
            self.SOLVE_OUTPUT_CACHE_KIND,
            str(key_parts.get("key_hash") or ""),
            str(key_parts.get("signature") or ""),
            {
                "output_hash": str(output_hash or "").strip().lower(),
                "runtime_sec": float(max(0.0, runtime_sec)),
                "cpu_sec": float(max(0.0, cpu_sec)),
                "wall_sec": float(max(0.0, wall_sec)),
                "memory_kb": int(max(0, memory_kb)),
                "runresult": "correct",
                "manifest": manifest_rows,
                "manifest_digest": manifest_digest,
            },
            files=files,
            tags=tags,
        )
        if not key_hash:
            return

    @staticmethod
    def _domjudge_strip_protocol_trace(raw: bytes) -> bytes:
        payload = bytes(raw or b"")
        if not payload:
            return b""
        text = payload.decode("utf-8", errors="replace")
        kept: list[str] = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if _DOMJUDGE_PROTOCOL_TRACE_RE.search(line):
                continue
            kept.append(line)
        while kept and (not kept[0].strip()):
            kept.pop(0)
        while kept and (not kept[-1].strip()):
            kept.pop()
        if not kept:
            return b""
        return ("\n".join(kept) + "\n").encode("utf-8")

    @staticmethod
    def _domjudge_force_cpp_define(source_bytes: bytes) -> bytes:
        payload = bytes(source_bytes or b"")
        if not payload:
            return b""
        if b"#define DOMJUDGE" in payload or b"# define DOMJUDGE" in payload:
            return payload
        return b"#ifndef DOMJUDGE\n#define DOMJUDGE 1\n#endif\n" + payload

    @staticmethod
    def _domjudge_ensure_bytes_file(path: Path, content: bytes, *, executable: bool = False) -> None:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(content))
        if executable:
            try:
                mode = int(target.stat().st_mode)
                target.chmod(mode | 0o755)
            except Exception as exc:
                logger.debug("failed to set executable bit on %s: %s", target, exc)

    def _domjudge_testcase_cache_root(self) -> Path:
        root = (self._settings.cache_root / "judgehost-domjudge-testcases").resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _domjudge_testcase_marker_root(self) -> Path:
        root = self._domjudge_testcase_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def clear_testcase_registry(self) -> None:
        with self._testcase_registry_lock:
            self._testcase_registry_next_id = 1
            self._testcase_registry_by_hash.clear()
            self._testcase_registry_by_id.clear()

    def _domjudge_testcase_cache_paths(self, testcase_hash: str) -> tuple[Path, Path]:
        token = str(testcase_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            token = sha256_hex_text(token, errors="replace")
        case_root = (self._domjudge_testcase_cache_root() / token[:2] / token).resolve()
        return ((case_root / "input.in").resolve(), (case_root / "answer.ans").resolve())

    def _domjudge_register_cached_testcase(
        self,
        conn: sqlite3.Connection,
        *,
        testcase_hash: str,
        in_bytes: bytes,
        ans_bytes: bytes,
    ) -> tuple[int, str, str]:
        _ = conn
        safe_hash = str(testcase_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", safe_hash):
            safe_hash = self._domjudge_set_hash_from_blobs([bytes(in_bytes), bytes(ans_bytes)])
        now_text = now_iso()
        in_path, ans_path = self._domjudge_testcase_cache_paths(safe_hash)
        self._domjudge_ensure_bytes_file(in_path, bytes(in_bytes), executable=False)
        self._domjudge_ensure_bytes_file(ans_path, bytes(ans_bytes), executable=False)
        with self._testcase_registry_lock:
            entry = self._testcase_registry_by_hash.get(safe_hash) or {}
            testcase_id = 0
            try:
                testcase_id = int(entry.get("id") or 0)
            except Exception:
                testcase_id = 0
            if testcase_id <= 0:
                testcase_id = max(1, int(self._testcase_registry_next_id))
                while testcase_id in self._testcase_registry_by_id:
                    testcase_id += 1
                self._testcase_registry_next_id = int(testcase_id + 1)
            record = {
                "id": int(testcase_id),
                "hash": safe_hash,
                "input_path": str(in_path),
                "answer_path": str(ans_path),
                "updated_at": now_text,
            }
            self._testcase_registry_by_hash[safe_hash] = dict(record)
            self._testcase_registry_by_id[int(testcase_id)] = dict(record)
            marker_dir = self._domjudge_testcase_marker_root()
            marker = (marker_dir / safe_hash).resolve()
            if marker.parent == marker_dir:
                marker.write_bytes(b"")
        return (int(testcase_id), str(in_path), str(ans_path))

    @staticmethod
    def _domjudge_language_extensions(source_name: str) -> tuple[str, list[str]]:
        name = str(source_name or "").strip().lower()
        if name.endswith(".java"):
            return ("java", ["java"])
        if name.endswith(".py"):
            return ("py", ["py"])
        if name.endswith(".c"):
            return ("c", ["c"])
        return ("cpp", ["cpp", "cc", "cxx", "c++"])

    @staticmethod
    def _domjudge_shell_words(raw: object) -> str:
        token = str(raw or "").strip()
        if not token:
            return ""
        try:
            parts = shlex.split(token)
        except ValueError:
            parts = token.split()
        safe_parts = [shlex.quote(str(part or "")) for part in parts if str(part or "")]
        return " ".join(safe_parts)

    @staticmethod
    def _domjudge_shell_tokens(raw: object) -> list[str]:
        token = str(raw or "").strip()
        if not token:
            return []
        try:
            parts = shlex.split(token)
        except ValueError:
            parts = token.split()
        return [str(part or "").strip() for part in parts if str(part or "").strip()]

    def _domjudge_toolchain_cmd_digest(self, source_name: str) -> str:
        language, _exts = self._domjudge_language_extensions(source_name)
        if language == "java":
            command = str(getattr(self._constants, "TOOLCHAIN_JAVA_COMPILER", "javac") or "javac").strip() or "javac"
            flags = self._domjudge_shell_tokens(getattr(self._constants, "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS", ""))
            return compile_command_digest(command, flags)
        if language == "py":
            command = (
                str(getattr(self._constants, "TOOLCHAIN_PYTHON_EXECUTABLE", "python3") or "python3").strip() or "python3"
            )
            flags = self._domjudge_shell_tokens(getattr(self._constants, "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS", ""))
            return compile_command_digest(command, [*flags, "-m", "py_compile"])
        if language == "c":
            return compile_command_digest("gcc", ["-O2", "-std=gnu11", "-pipe", "-lm"])
        command = str(getattr(self._constants, "TOOLCHAIN_CPP_COMPILER", "g++") or "g++").strip() or "g++"
        flags = self._domjudge_shell_tokens(
            getattr(self._constants, "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS", "-x c++ -Wall -O2 -std=gnu++20 -static -pipe")
        )
        return compile_command_digest(command, flags)

    def _domjudge_compile_script(self, source_name: str) -> bytes:
        compiler = str(getattr(self._constants, "TOOLCHAIN_CPP_COMPILER", "g++") or "g++").strip() or "g++"
        java_compiler = str(getattr(self._constants, "TOOLCHAIN_JAVA_COMPILER", "javac") or "javac").strip() or "javac"
        python_exec = (
            str(getattr(self._constants, "TOOLCHAIN_PYTHON_EXECUTABLE", "python3") or "python3").strip() or "python3"
        )
        cpp_compile_flags = self._domjudge_shell_words(
            getattr(self._constants, "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS", "-O2 -std=gnu++20 -pipe -DDOMJUDGE")
        )
        java_compile_flags = self._domjudge_shell_words(
            getattr(self._constants, "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS", "")
        )
        python_compile_flags = self._domjudge_shell_words(
            getattr(self._constants, "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS", "")
        )
        cpp_compile_cmd = shlex.quote(compiler)
        if cpp_compile_flags:
            cpp_compile_cmd += f" {cpp_compile_flags}"
        java_compile_cmd = shlex.quote(java_compiler)
        if java_compile_flags:
            java_compile_cmd += f" {java_compile_flags}"
        python_compile_flag_suffix = f" {python_compile_flags}" if python_compile_flags else ""
        text = (
            "#!/bin/sh\n"
            "set -eu\n"
            "DEST=\"$1\"\n"
            "MEMLIMIT=\"$2\"\n"
            "shift 2\n"
            "if [ \"$#\" -lt 1 ]; then\n"
            "  echo \"no source file\" >&2\n"
            "  exit 1\n"
            "fi\n"
            "MAIN=\"$1\"\n"
            "case \"$MAIN\" in\n"
            "  *.cpp|*.cc|*.cxx|*.c++)\n"
            f"    exec {cpp_compile_cmd} \"$@\" -o \"$DEST\"\n"
            "    ;;\n"
            "  *.c)\n"
            "    exec gcc -O2 -std=gnu11 -pipe \"$@\" -o \"$DEST\" -lm\n"
            "    ;;\n"
            "  *.java)\n"
            "    CLASS=\"$(sed -n 's/^[[:space:]]*public[[:space:]]\\+class[[:space:]]\\+\\([A-Za-z_][A-Za-z0-9_]*\\).*/\\1/p' \"$MAIN\" | head -n1)\"\n"
            "    if [ -z \"$CLASS\" ]; then\n"
            "      CLASS=\"$(basename \"$MAIN\" .java)\"\n"
            "    fi\n"
            "    SRC=\"$MAIN\"\n"
            "    if [ \"$(basename \"$MAIN\")\" != \"$CLASS.java\" ]; then\n"
            "      cp \"$MAIN\" \"$CLASS.java\"\n"
            "      SRC=\"$CLASS.java\"\n"
            "    fi\n"
            f"    {java_compile_cmd} \"$SRC\"\n"
            "    cat >\"$DEST\" <<EOF\n"
            "#!/bin/sh\n"
            "HERE=\"\\$(CDPATH= cd -- \"\\$(dirname \"\\$0\")\" && pwd)\"\n"
            "exec java -cp \"\\$HERE\" \"$CLASS\" \"\\$@\"\n"
            "EOF\n"
            "    chmod +x \"$DEST\"\n"
            "    ;;\n"
            "  *.py)\n"
            f"    PREFERRED_PY={json.dumps(python_exec)}\n"
            "    PY=\"\"\n"
            "    if [ -n \"$PREFERRED_PY\" ] && command -v \"$PREFERRED_PY\" >/dev/null 2>&1; then\n"
            "      PY=\"$PREFERRED_PY\"\n"
            "    elif command -v python3 >/dev/null 2>&1; then\n"
            "      PY=\"python3\"\n"
            "    elif command -v python >/dev/null 2>&1; then\n"
            "      PY=\"python\"\n"
            "    elif command -v pypy3 >/dev/null 2>&1; then\n"
            "      PY=\"pypy3\"\n"
            "    fi\n"
            "    if [ -z \"$PY\" ]; then\n"
            "      echo \"python interpreter not found\" >&2\n"
            "      exit 1\n"
            "    fi\n"
            f"    \"$PY\"{python_compile_flag_suffix} -m py_compile \"$@\"\n"
            "    EXITCODE=$?\n"
            "    [ \"$EXITCODE\" -ne 0 ] && exit \"$EXITCODE\"\n"
            "    rm -f -- ./*.pyc\n"
            "    rm -rf -- __pycache__\n"
            "    if [ ! -r \"$MAIN\" ]; then\n"
            "      echo \"main source file '$MAIN' is not readable\" >&2\n"
            "      exit 1\n"
            "    fi\n"
            "    cp \"$MAIN\" \"$DEST.py\"\n"
            "    cat >\"$DEST\" <<EOF\n"
            "#!/bin/sh\n"
            "HERE=\"\\$(CDPATH= cd -- \"\\$(dirname \"\\$0\")\" && pwd)\"\n"
            "SCRIPT_NAME=\"\\$(basename \"\\$0\").py\"\n"
            "export HOME=/does/not/exist\n"
            "exec \"$PY\" \"\\$HERE/\\$SCRIPT_NAME\" \"\\$@\"\n"
            "EOF\n"
            "    chmod +x \"$DEST\"\n"
            "    ;;\n"
            "  *)\n"
            "    echo \"unsupported language: $MAIN\" >&2\n"
            "    exit 1\n"
            "    ;;\n"
            "esac\n"
        )
        return text.encode("utf-8")

    @staticmethod
    def _domjudge_run_script(interactive: bool, *, solve_mode: bool = False) -> bytes:
        if interactive:
            text = (
                "#!/bin/sh\n"
                "set -eu\n"
                "TESTIN=\"$1\";  shift\n"
                "PROGOUT=\"$1\"; shift\n"
                "TESTOUT=\"$1\"; shift\n"
                "META=\"$1\"; shift\n"
                "FEEDBACK=\"$1\"; shift\n"
                "MYDIR=\"$(dirname \"$0\")\"\n"
                "exec ../../dj-bin/runpipe ${DEBUG:+-v} -M \"$META\" -o \"$PROGOUT\" \"$MYDIR/runjury\" \"$TESTIN\" \"$TESTOUT\" \"$FEEDBACK\" = \"$@\"\n"
            )
            return text.encode("utf-8")
        text = (
            "#!/bin/sh\n"
            "set -eu\n"
            "TESTIN=\"$1\"\n"
            "PROGOUT=\"$2\"\n"
            "shift 2\n"
            "if [ \"$#\" -eq 0 ]; then\n"
            "  echo \"missing submission command\" >&2\n"
            "  exit 43\n"
            "fi\n"
            "exec \"$@\" <\"$TESTIN\" >\"$PROGOUT\"\n"
        )
        return text.encode("utf-8")

    @staticmethod
    def _domjudge_compare_script(*, solve_mode: bool = False) -> bytes:
        solve_flag = "1" if bool(solve_mode) else "0"
        text = (
            "#!/bin/sh\n"
            "set -eu\n"
            "TESTIN=\"$1\"\n"
            "TESTANS=\"$2\"\n"
            "FEEDBACK=\"$3\"\n"
            "shift 3\n"
            "HERE=\"$(CDPATH= cd -- \"$(dirname \"$0\")\" && pwd)\"\n"
            "mkdir -p \"$FEEDBACK\"\n"
            "TEAMOUT=\"$FEEDBACK/team.out\"\n"
            "TEAMOUT_SRC=\"\"\n"
            "for SRC_TEAMOUT in \"$@\"; do\n"
            "  [ -n \"$SRC_TEAMOUT\" ] || continue\n"
            "  [ \"$SRC_TEAMOUT\" = \"$FEEDBACK\" ] && continue\n"
            "  if [ -e \"$SRC_TEAMOUT\" ] && [ -e \"$TESTIN\" ] && [ \"$SRC_TEAMOUT\" -ef \"$TESTIN\" ] 2>/dev/null; then\n"
            "    continue\n"
            "  fi\n"
            "  if [ -e \"$SRC_TEAMOUT\" ] && [ -e \"$TESTANS\" ] && [ \"$SRC_TEAMOUT\" -ef \"$TESTANS\" ] 2>/dev/null; then\n"
            "    continue\n"
            "  fi\n"
            "  if [ -r \"$SRC_TEAMOUT\" ] && [ ! -d \"$SRC_TEAMOUT\" ]; then\n"
            "    TEAMOUT_SRC=\"$SRC_TEAMOUT\"\n"
            "    if [ -s \"$SRC_TEAMOUT\" ]; then\n"
            "      break\n"
            "    fi\n"
            "  fi\n"
            "done\n"
            "if [ -z \"$TEAMOUT_SRC\" ] && [ -r \"$FEEDBACK/program.out\" ] && [ ! -d \"$FEEDBACK/program.out\" ]; then\n"
            "  TEAMOUT_SRC=\"$FEEDBACK/program.out\"\n"
            "fi\n"
            "if [ -n \"$TEAMOUT_SRC\" ]; then\n"
            "  cat \"$TEAMOUT_SRC\" >\"$TEAMOUT\" 2>/dev/null || true\n"
            "fi\n"
            "if [ ! -s \"$TEAMOUT\" ] && [ -r \"$FEEDBACK/program.out\" ] && [ ! -d \"$FEEDBACK/program.out\" ] && [ \"$TEAMOUT_SRC\" != \"$FEEDBACK/program.out\" ]; then\n"
            "  cat \"$FEEDBACK/program.out\" >\"$TEAMOUT\" 2>/dev/null || true\n"
            "fi\n"
            "if [ ! -s \"$TEAMOUT\" ]; then\n"
            "  cat >\"$TEAMOUT\" || true\n"
            "fi\n"
            f"if [ \"{solve_flag}\" = \"1\" ]; then\n"
            "  echo \"build solve mode\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "  exit 42\n"
            "fi\n"
            "CHECKER_BIN=\"$HERE/checker\"\n"
            "CHECKER_SRC=\"$HERE/checker.cpp\"\n"
            "compile_checker() {\n"
            "  [ -f \"$CHECKER_SRC\" ] || return 1\n"
            "  command -v g++ >/dev/null 2>&1 || return 1\n"
            "  CACHE_ROOT=\"${TMPDIR:-/tmp}/polygonlike-checker-cache\"\n"
            "  mkdir -p \"$CACHE_ROOT\" 2>/dev/null || true\n"
            "  HASH_HDR=\"$HERE/testlib.h\"\n"
            "  CHECKER_KEY=\"\"\n"
            "  if command -v sha256sum >/dev/null 2>&1; then\n"
            "    if [ -f \"$HASH_HDR\" ]; then\n"
            "      CHECKER_KEY=\"$(cat \"$CHECKER_SRC\" \"$HASH_HDR\" | sha256sum | awk '{print $1}')\"\n"
            "    else\n"
            "      CHECKER_KEY=\"$(sha256sum \"$CHECKER_SRC\" | awk '{print $1}')\"\n"
            "    fi\n"
            "  fi\n"
            "  if [ -z \"$CHECKER_KEY\" ]; then\n"
            "    CHECKER_KEY=\"checker-$(basename \"$CHECKER_SRC\")\"\n"
            "  fi\n"
            "  TARGET=\"$CACHE_ROOT/$CHECKER_KEY\"\n"
            "  if [ -x \"$TARGET\" ]; then\n"
            "    CHECKER_BIN=\"$TARGET\"\n"
            "    return 0\n"
            "  fi\n"
            "  TMP_TARGET=\"$TARGET.tmp.$$\"\n"
            "  g++ -O2 -std=gnu++20 -pipe -DDOMJUDGE -I\"$HERE\" \"$CHECKER_SRC\" -o \"$TMP_TARGET\" >\"$FEEDBACK/checker.build.log\" 2>&1 || { rm -f \"$TMP_TARGET\"; return 1; }\n"
            "  chmod +x \"$TMP_TARGET\" || true\n"
            "  mv -f \"$TMP_TARGET\" \"$TARGET\" 2>/dev/null || cp -f \"$TMP_TARGET\" \"$TARGET\"\n"
            "  rm -f \"$TMP_TARGET\" 2>/dev/null || true\n"
            "  CHECKER_BIN=\"$TARGET\"\n"
            "  return 0\n"
            "}\n"
            "run_checker_once() {\n"
            "  set +e\n"
            "  FEEDBACK_DIR=\"$FEEDBACK\" \"$CHECKER_BIN\" \"$TESTIN\" \"$TESTANS\" \"$FEEDBACK/\" \"$@\" <\"$TEAMOUT\" >\"$FEEDBACK/checker.log\" 2>&1\n"
            "  CHECKER_RC=$?\n"
            "  set -e\n"
            "  return 0\n"
            "}\n"
            "if [ ! -x \"$CHECKER_BIN\" ] && [ -f \"$CHECKER_SRC\" ]; then\n"
            "  if ! compile_checker; then\n"
            "    if [ -f \"$FEEDBACK/checker.build.log\" ] && [ -s \"$FEEDBACK/checker.build.log\" ]; then\n"
            "      cat \"$FEEDBACK/checker.build.log\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "    else\n"
            "      echo \"checker compile failed\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "    fi\n"
            "    exit 43\n"
            "  fi\n"
            "fi\n"
            "if [ -x \"$CHECKER_BIN\" ]; then\n"
            "  run_checker_once \"$@\"\n"
            "  RC=$CHECKER_RC\n"
            "  if [ \"$RC\" -ne 42 ] && [ \"$RC\" -ne 43 ] && [ -f \"$CHECKER_SRC\" ] && grep -Eq \"GLIBC_|GLIBCXX_\" \"$FEEDBACK/checker.log\" 2>/dev/null; then\n"
            "    if compile_checker; then\n"
            "      run_checker_once \"$@\"\n"
            "      RC=$CHECKER_RC\n"
            "    fi\n"
            "  fi\n"
            "  if [ \"$RC\" -ne 42 ] && [ \"$RC\" -ne 43 ]; then\n"
            "    if [ -f \"$FEEDBACK/checker.log\" ] && [ -s \"$FEEDBACK/checker.log\" ]; then\n"
            "      cat \"$FEEDBACK/checker.log\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "    else\n"
            "      echo \"checker returned unexpected exit code: $RC\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "    fi\n"
            "    exit 43\n"
            "  fi\n"
            "  if [ -f \"$FEEDBACK/checker.log\" ] && [ -s \"$FEEDBACK/checker.log\" ]; then\n"
            "    cat \"$FEEDBACK/checker.log\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "  fi\n"
            "  exit \"$RC\"\n"
            "fi\n"
            "if diff -q \"$TEAMOUT\" \"$TESTANS\" >/dev/null 2>&1; then\n"
            "  echo \"ok\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "  exit 42\n"
            "fi\n"
            "echo \"wrong answer\" >\"$FEEDBACK/judgemessage.txt\"\n"
            "exit 43\n"
        )
        return text.encode("utf-8")

    def domjudge_config(self) -> dict[str, object]:
        compile_timeout = max(1, int(getattr(self._constants, "TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120) or 120))
        compile_mem_mb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048))
        compile_output_kb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536))
        output_kb = max(64, int(getattr(self._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536))
        return {
            "diskspace_error": 1048576,
            "output_storage_limit": int(output_kb * 1024),
            "script_timelimit": compile_timeout,
            "script_memory_limit": int(compile_mem_mb * 1024),
            "script_filesize_limit": int(compile_output_kb * 1024),
            "timelimit_overshoot": "1s",
        }

    @staticmethod
    def domjudge_languages() -> list[dict[str, object]]:
        return [
            {"id": "c", "extensions": ["c"]},
            {"id": "cpp", "extensions": ["cpp", "cc", "cxx", "c++"]},
            {"id": "java", "extensions": ["java"]},
            {"id": "py", "extensions": ["py"]},
        ]

    def domjudge_list_hosts(self) -> list[dict[str, object]]:
        with self._state_lock:
            rows = sorted(
                (dict(row) for row in self._hosts_state.values()),
                key=lambda item: (str(item.get("last_seen_at") or ""), str(item.get("hostname") or "")),
                reverse=True,
            )
        out: list[dict[str, object]] = []
        for row in rows:
            token = str(row.get("hostname") or "").strip()
            if not token:
                continue
            out.append(
                {
                    "hostname": token,
                    "enabled": bool(row.get("enabled", True)),
                    "polltime": str(row.get("last_seen_at") or "").strip(),
                }
            )
        return out

    @staticmethod
    def _domjudge_script_ids(job_id: int) -> tuple[int, int, int]:
        base = int(job_id) * 10
        return (base + 1, base + 2, base + 3)

    @staticmethod
    def _domjudge_parse_script_id(raw_id: object) -> tuple[int, int]:
        try:
            token = int(str(raw_id or "").strip())
        except Exception as exc:
            raise RuntimeError("invalid script id") from exc
        if token <= 0:
            raise RuntimeError("invalid script id")
        job_id = token // 10
        offset = token % 10
        if job_id <= 0 or offset not in {1, 2, 3}:
            raise RuntimeError("invalid script id")
        return (job_id, offset)

    @staticmethod
    def _domjudge_script_hash_field(kind: str) -> str:
        token = str(kind or "").strip().lower()
        mapping = {
            "compile": "compile_hash",
            "run": "run_hash",
            "compare": "compare_hash",
        }
        field = mapping.get(token)
        if not field:
            raise RuntimeError("invalid script kind")
        return field

    @staticmethod
    def _domjudge_script_dir_has_files(work_root: Path, kind: str) -> bool:
        try:
            base = (Path(work_root).resolve() / "scripts" / str(kind or "").strip().lower()).resolve()
        except Exception:
            return False
        if not base.exists() or not base.is_dir():
            return False
        try:
            for child in base.iterdir():
                if child.is_file():
                    return True
        except Exception:
            return False
        return False

    def _domjudge_script_provider_job_id(self, *, kind: str, script_hash: str, default_job_id: int) -> int:
        safe_default = max(1, int(default_job_id))
        safe_hash = str(script_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", safe_hash):
            return safe_default
        try:
            field = self._domjudge_script_hash_field(kind)
        except Exception:
            return safe_default
        rows = self._db_fetch_all(
            f"""
            SELECT job_id,work_root
            FROM judgehost_domjudge_jobs
            WHERE {field}=?
            ORDER BY job_id ASC
            LIMIT 256
            """,
            [safe_hash],
        )
        for row in rows:
            try:
                candidate_job_id = int(row["job_id"])
            except Exception:
                continue
            if candidate_job_id <= 0:
                continue
            work_root = Path(str(row["work_root"] or ""))
            if self._domjudge_script_dir_has_files(work_root, kind):
                return candidate_job_id
        return safe_default

    def domjudge_register_host(self, hostname: str) -> list[dict[str, object]]:
        safe_host = self._normalize_hostname(hostname)
        now_text = now_iso()
        with self._domdb_conn() as conn:
            unfinished: list[dict[str, object]] = []
            self._requeue_expired_leases(conn, force=True)
            self._record_host_event_conn(conn, hostname=safe_host, action="register")
            affected = conn.execute(
                """
                SELECT job_id,submit_id
                FROM judgehost_domjudge_jobs
                WHERE lease_owner=? AND status IN ('leased','queued')
                ORDER BY job_id ASC
                """,
                [safe_host],
            ).fetchall()
            remap_submit_ids: list[tuple[int, str]] = []
            remap_seed = int(time.time() * 1000)
            remap_step = 0
            for row in affected:
                job_id = int(row["job_id"])
                # Re-registration after transient disconnect can make judgedaemon retry
                # unfinished runs in an existing working directory. Allocate a fresh
                # numeric submitid to force a clean judgedaemon working path.
                remap_step += 1
                new_submitid = str(remap_seed + remap_step)
                remap_submit_ids.append((job_id, new_submitid))
                unfinished.append({"jobid": job_id, "submitid": new_submitid})
            for job_id, new_submitid in remap_submit_ids:
                conn.execute(
                    "UPDATE judgehost_domjudge_jobs SET submit_id=? WHERE job_id=?",
                    [new_submitid, job_id],
                )
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=NULL, status='queued', updated_at=?
                WHERE lease_owner=? AND status IN ('leased','queued')
                """,
                [now_text, safe_host],
            )
            conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='pending', lease_owner=NULL, updated_at=?
                WHERE lease_owner=? AND status='leased'
                """,
                [now_text, safe_host],
            )
            with self._state_lock:
                for task in self._tasks_by_id.values():
                    if str(task.get("lease_owner") or "").strip() != safe_host:
                        continue
                    if str(task.get("status") or "").strip().lower() != self.STATUS_LEASED:
                        continue
                    task["status"] = self.STATUS_QUEUED
                    task["lease_owner"] = ""
                    task["lease_expires_at"] = ""
                    task["updated_at"] = now_text
            return unfinished

    def _domjudge_active_job_for_host(self, hostname: str) -> sqlite3.Row | None:
        rows = self._db_fetch_all(
            """
            SELECT j.*
            FROM judgehost_domjudge_jobs j
            WHERE j.lease_owner=? AND j.status IN ('leased','queued')
            ORDER BY j.job_id ASC
            LIMIT 1
            """,
            [hostname],
        )
        if not rows:
            return None
        return rows[0]

    def _domjudge_shared_pending_job(self, hostname: str) -> sqlite3.Row | None:
        rows = self._db_fetch_all(
            """
            SELECT j.*
            FROM judgehost_domjudge_jobs j
            WHERE j.status IN ('leased','queued')
              AND EXISTS (
                SELECT 1
                FROM judgehost_domjudge_cases c
                WHERE c.job_id=j.job_id AND c.status='pending'
              )
            ORDER BY
              CASE WHEN j.lease_owner=? THEN 0 ELSE 1 END,
              CASE WHEN j.status='leased' THEN 0 ELSE 1 END,
              j.created_at ASC,
              j.job_id ASC
            LIMIT 1
            """,
            [hostname],
        )
        if not rows:
            return None
        return rows[0]

    def _domjudge_cases_for_job(self, job_id: int, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self._db_fetch_all(
                """
                SELECT *
                FROM judgehost_domjudge_cases
                WHERE job_id=? AND status=?
                ORDER BY ordinal ASC, id ASC
                """,
                [int(job_id), str(status)],
            )
        return self._db_fetch_all(
            """
            SELECT *
            FROM judgehost_domjudge_cases
            WHERE job_id=?
            ORDER BY ordinal ASC, id ASC
            """,
            [int(job_id)],
        )

    def _domjudge_prepare_job(self, hostname: str, task: dict[str, object]) -> int:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError("missing task_id for DOMjudge compatibility")
        run_id = str(task.get("run_id") or "").strip()
        payload = task.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("judgehost task payload is missing")
        existing = self._db_fetch_one("SELECT job_id FROM judgehost_domjudge_jobs WHERE task_id=?", [task_id])
        if existing is not None:
            job_id = int(existing["job_id"])
            self._db_execute(
                "UPDATE judgehost_domjudge_jobs SET lease_owner=?, status='leased', updated_at=? WHERE job_id=?",
                [hostname, now_iso(), job_id],
            )
            return job_id

        source_name = Path(str(payload.get("source_name") or "submission.cpp").strip() or "submission.cpp").name
        if not source_name:
            source_name = "submission.cpp"
        source_b64 = str(payload.get("source_b64") or "").strip()
        source_bytes = self._domjudge_b64_decode(source_b64)
        if not source_bytes:
            raise RuntimeError("submission source payload is empty")
        build_payload = payload.get("build_payload")
        if not isinstance(build_payload, dict):
            raise RuntimeError("build payload is required for DOMjudge compatibility")
        tests_payload = build_payload.get("tests")
        tests_rows = [row for row in (tests_payload if isinstance(tests_payload, list) else []) if isinstance(row, dict)]
        if not tests_rows:
            raise RuntimeError("no tests in judgehost payload")
        run_cfg_obj: dict[str, object] = {}
        run_cfg_raw = str(build_payload.get("run_config_json") or "").strip()
        if run_cfg_raw:
            try:
                parsed = json.loads(run_cfg_raw)
                if isinstance(parsed, dict):
                    run_cfg_obj = parsed
            except Exception:
                run_cfg_obj = {}
        problem_limits_obj = build_payload.get("problem_limits")
        if not isinstance(problem_limits_obj, dict):
            problem_limits_obj = {}
        checker_args_raw = run_cfg_obj.get("checker_args")
        checker_args: list[str] = []
        if isinstance(checker_args_raw, list):
            checker_args = [str(item or "").strip() for item in checker_args_raw if str(item or "").strip()]
        mode = str(payload.get("mode") or "pass-fail").strip().lower()
        configured_max_passes = max(
            1,
            self._domjudge_parse_int(
                run_cfg_obj.get("max_passes"),
                self._domjudge_parse_int(problem_limits_obj.get("max_passes"), 16),
            ),
        )
        max_passes = configured_max_passes if mode == "multi-pass" else 1
        invocation_source = str(payload.get("invocation_source") or "").strip().lower()
        solve_mode = invocation_source == "build.solve"
        expected_behavior = str(payload.get("expected_behavior") or "").strip().lower()
        force_recompile = self._domjudge_bool(payload.get("force_recompile"), default=False)
        contest_id = self._domjudge_contest_id(payload.get("problem"))
        submit_id = self._domjudge_submit_id_from_run_id(run_id)
        language_id, language_exts = self._domjudge_language_extensions(source_name)
        source_hash = self._domjudge_source_hash(source_name, source_bytes)

        compile_timeout = max(1, int(getattr(self._constants, "TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120) or 120))
        compile_mem_mb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048))
        compile_output_kb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536))
        run_output_kb = max(64, int(getattr(self._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536))
        run_process_limit = max(1, int(getattr(self._constants, "RUN_EXEC_PROCESS_LIMIT", 64) or 64))
        default_cfg = getattr(self._constants, "GENERAL_CONFIG_DEFAULTS", {}) or {}
        run_tl_ms = self._domjudge_parse_int(
            run_cfg_obj.get("time_limit_ms"),
            self._domjudge_parse_int(
                problem_limits_obj.get("time_limit_ms"),
                self._domjudge_parse_int(default_cfg.get("time_limit_ms", 2000), 2000),
            ),
        )
        run_mem_mb = self._domjudge_parse_int(
            run_cfg_obj.get("memory_limit_mb"),
            self._domjudge_parse_int(
                problem_limits_obj.get("memory_limit_mb"),
                self._domjudge_parse_int(default_cfg.get("memory_limit_mb", 1024), 1024),
            ),
        )
        run_tl_ms = max(100, run_tl_ms)
        run_mem_mb = max(16, run_mem_mb)
        run_tl_sec = max(0.1, float(run_tl_ms) / 1000.0)
        pass_fail_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_PASS_FAIL_SEC", 1) or 1))
        multi_pass_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_MULTI_PASS_SEC", 15) or 15))
        interactive_slack = max(0.0, float(getattr(self._constants, "RUN_WALL_TIME_SLACK_INTERACTIVE_SEC", 15) or 15))
        run_overshoot_sec = pass_fail_slack
        if mode == "interactive":
            run_overshoot_sec = interactive_slack
        elif mode == "multi-pass":
            run_overshoot_sec = multi_pass_slack
        run_mem_kb = max(16 * 1024, int(run_mem_mb * 1024))

        binaries_b64 = build_payload.get("binaries_b64")
        binaries_obj = binaries_b64 if isinstance(binaries_b64, dict) else {}
        checker_bytes = self._domjudge_b64_decode(binaries_obj.get("checker"))
        interactor_bytes = self._domjudge_b64_decode(binaries_obj.get("interactor"))
        sources_b64 = build_payload.get("sources_b64")
        sources_obj = sources_b64 if isinstance(sources_b64, dict) else {}
        checker_source_bytes = self._domjudge_b64_decode(sources_obj.get("checker.cpp"))
        interactor_source_bytes = self._domjudge_b64_decode(sources_obj.get("interactor.cpp"))
        testlib_header_bytes = self._domjudge_b64_decode(sources_obj.get("testlib.h"))
        if checker_source_bytes:
            checker_source_bytes = self._domjudge_force_cpp_define(checker_source_bytes)
        if interactor_source_bytes:
            interactor_source_bytes = self._domjudge_force_cpp_define(interactor_source_bytes)
        # Prefer source payloads over host-built binaries to avoid libc/libstdc++ ABI
        # mismatch between producer and judgehost runtime.
        if checker_source_bytes:
            checker_bytes = b""
        if interactor_source_bytes:
            interactor_bytes = b""
        interactive = mode == "interactive" or (mode == "multi-pass" and bool(interactor_bytes or interactor_source_bytes))
        if mode == "interactive" and not (interactor_bytes or interactor_source_bytes):
            raise RuntimeError("interactive mode requires interactor payload")

        compile_files: list[tuple[str, bytes, bool]] = [("run", self._domjudge_compile_script(source_name), True)]
        run_files: list[tuple[str, bytes, bool]] = []
        compare_files: list[tuple[str, bytes, bool]] = []
        if interactive:
            # Follow official DOMjudge combined run/compare flow:
            # provide jury program as "run" executable payload, and let
            # judgedaemon wrap it with run-interactive.sh + runpipe.
            if interactor_source_bytes:
                run_files.append(("interactor.cpp", interactor_source_bytes, False))
                if testlib_header_bytes:
                    run_files.append(("testlib.h", testlib_header_bytes, False))
            elif interactor_bytes:
                run_files.append(("run", interactor_bytes, True))
            else:
                raise RuntimeError("interactive mode requires interactor payload")
            # combined_run_compare=true means compare executable is not fetched.
            compare_files.append(("run", self._domjudge_compare_script(solve_mode=solve_mode), True))
        else:
            run_files.append(("run", self._domjudge_run_script(False, solve_mode=solve_mode), True))
            if solve_mode:
                # build.solve must accept without requiring canonical answer files.
                compare_files.append(("run", self._domjudge_compare_script(solve_mode=True), True))
            elif checker_source_bytes:
                compare_files.append(("checker.cpp", checker_source_bytes, False))
                if testlib_header_bytes:
                    compare_files.append(("testlib.h", testlib_header_bytes, False))
            elif checker_bytes:
                compare_files.append(("run", checker_bytes, True))
            else:
                compare_files.append(("run", self._domjudge_compare_script(solve_mode=False), True))

        precomputed_raw = payload.get("domjudge_precomputed")
        precomputed = precomputed_raw if isinstance(precomputed_raw, dict) else {}
        precompile_hash = str(precomputed.get("compile_hash") or "").strip().lower()
        prerun_hash = str(precomputed.get("run_hash") or "").strip().lower()
        precompare_hash = str(precomputed.get("compare_hash") or "").strip().lower()
        presource_hash = str(precomputed.get("source_hash") or "").strip().lower()
        precompile_config = precomputed.get("compile_config")
        prerun_config = precomputed.get("run_config")
        precompare_config = precomputed.get("compare_config")
        use_precomputed = (
            bool(re.fullmatch(r"[0-9a-f]{32}", precompile_hash))
            and bool(re.fullmatch(r"[0-9a-f]{32}", prerun_hash))
            and bool(re.fullmatch(r"[0-9a-f]{32}", precompare_hash))
            and bool(re.fullmatch(r"[0-9a-f]{64}", presource_hash))
            and isinstance(precompile_config, dict)
            and isinstance(prerun_config, dict)
            and isinstance(precompare_config, dict)
        )
        if use_precomputed:
            source_hash = presource_hash
            compile_hash = precompile_hash
            run_hash = prerun_hash
            compare_hash = precompare_hash
            compile_config = dict(precompile_config)
            run_config = dict(prerun_config)
            compare_config = dict(precompare_config)
        else:
            compile_hash = domjudge_executable_hash(compile_files)
            run_hash = domjudge_executable_hash(run_files)
            compare_hash = domjudge_executable_hash(compare_files)
            toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(source_name)
            compare_script_timelimit = max(1, int(run_tl_sec))
            if checker_source_bytes:
                # compare script may need one-time local checker rebuild when host binary
                # is ABI-incompatible with judgehost runtime; reserve enough wall time.
                compare_script_timelimit = max(compare_script_timelimit, min(compile_timeout, 120))

            compile_config = {
                "hash": compile_hash,
                "toolchain_cmd_digest": toolchain_cmd_digest,
                "filter_compiler_files": False,
                "language_extensions": list(language_exts),
                "script_timelimit": compile_timeout,
                "script_memory_limit": int(compile_mem_mb * 1024),
                "script_filesize_limit": int(compile_output_kb * 1024),
            }
            run_config = {
                "hash": run_hash,
                "time_limit": run_tl_sec,
                "overshoot": run_overshoot_sec,
                "memory_limit": run_mem_kb,
                "output_limit": int(run_output_kb * 1024),
                "process_limit": run_process_limit,
                "entry_point": None,
                "pass_limit": max_passes,
                "language_id": language_id,
            }
            compare_config = {
                "hash": compare_hash,
                "combined_run_compare": bool(interactive),
                "compare_args": " ".join(checker_args),
                "script_timelimit": int(compare_script_timelimit),
                "script_memory_limit": run_mem_kb,
                "script_filesize_limit": int(run_output_kb * 1024),
            }
        compile_config_hash = self._domjudge_json_hash(compile_config)
        run_config_hash = self._domjudge_json_hash(run_config)
        compare_config_hash = self._domjudge_json_hash(compare_config)

        work_key = self._domjudge_json_hash(
            {
                "schema": "v1",
                "source_hash": source_hash,
                "source_name": source_name,
                "compile_hash": compile_hash,
                "run_hash": run_hash,
                "compare_hash": compare_hash,
                "compile_config_hash": compile_config_hash,
                "run_config_hash": run_config_hash,
                "compare_config_hash": compare_config_hash,
            }
        )
        work_root = self._domjudge_work_root(f"job-{work_key[:32]}")
        source_dir = (work_root / "source").resolve()
        scripts_compile_dir = (work_root / "scripts" / "compile").resolve()
        scripts_run_dir = (work_root / "scripts" / "run").resolve()
        scripts_compare_dir = (work_root / "scripts" / "compare").resolve()
        for directory in (source_dir, scripts_compile_dir, scripts_run_dir, scripts_compare_dir):
            directory.mkdir(parents=True, exist_ok=True)
        source_path = (source_dir / source_name).resolve()
        self._domjudge_ensure_bytes_file(source_path, source_bytes, executable=False)
        for name, content, is_exec in compile_files:
            self._domjudge_ensure_bytes_file(scripts_compile_dir / name, content, executable=is_exec)
        for name, content, is_exec in run_files:
            self._domjudge_ensure_bytes_file(scripts_run_dir / name, content, executable=is_exec)
        for name, content, is_exec in compare_files:
            self._domjudge_ensure_bytes_file(scripts_compare_dir / name, content, executable=is_exec)

        now_text = now_iso()
        with self._domdb_conn() as conn:
            conn.execute(
                """
                INSERT INTO judgehost_domjudge_jobs(
                    task_id,run_id,submit_id,contest_id,mode,source_name,source_path,work_root,
                    compile_hash,run_hash,compare_hash,source_hash,compile_config_json,run_config_json,compare_config_json,
                    expected_behavior,invocation_source,force_recompile,
                    lease_owner,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    task_id,
                    run_id,
                    submit_id,
                    contest_id,
                    mode,
                    source_name,
                    str(source_path),
                    str(work_root),
                    compile_hash,
                    run_hash,
                    compare_hash,
                    source_hash,
                    json.dumps(compile_config, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(run_config, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(compare_config, ensure_ascii=False, separators=(",", ":")),
                    expected_behavior,
                    invocation_source,
                    1 if force_recompile else 0,
                    hostname,
                    "leased",
                    now_text,
                    now_text,
                ],
            )
            job_row = conn.execute("SELECT job_id FROM judgehost_domjudge_jobs WHERE task_id=?", [task_id]).fetchone()
            if job_row is None:
                raise RuntimeError("failed to allocate DOMjudge compatibility job")
            job_id = int(job_row["job_id"])
            # Official judgedaemon validates submitid as integer.
            submit_id = str(job_id)
            conn.execute(
                "UPDATE judgehost_domjudge_jobs SET submit_id=? WHERE job_id=?",
                [submit_id, job_id],
            )
            ordinal = 0
            for entry in tests_rows:
                ordinal += 1
                raw_name = str(entry.get("name") or "").strip()
                test_name = raw_name if RUN_TEST_NAME_RE.fullmatch(raw_name) else f"{ordinal:03}.in"
                in_bytes = self._domjudge_b64_decode(entry.get("input_b64"))
                ans_bytes = self._domjudge_b64_decode(entry.get("answer_b64"))
                testcase_input_hash = self._domjudge_sha256_bytes(in_bytes)
                testcase_answer_hash = self._domjudge_sha256_bytes(ans_bytes)
                # build.solve must not depend on pre-existing answers:
                # use input hash as testcase key so cache identity is
                # (main_correct/source signature + input_hash).
                testcase_hash = (
                    str(testcase_input_hash)
                    if solve_mode
                    else self._domjudge_set_hash_from_blobs([in_bytes, ans_bytes])
                )
                testcase_id, in_path_text, ans_path_text = self._domjudge_register_cached_testcase(
                    conn,
                    testcase_hash=testcase_hash,
                    in_bytes=in_bytes,
                    ans_bytes=ans_bytes,
                )
                conn.execute(
                    """
                    INSERT INTO judgehost_domjudge_cases(
                        job_id,task_id,run_id,test_name,ordinal,testcase_id,testcase_hash,testcase_input_hash,testcase_answer_hash,input_path,answer_path,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        job_id,
                        task_id,
                        run_id,
                        test_name,
                        ordinal,
                        testcase_id,
                        testcase_hash,
                        testcase_input_hash,
                        testcase_answer_hash,
                        str(in_path_text),
                        str(ans_path_text),
                        "pending",
                        now_text,
                        now_text,
                    ],
                )
            conn.commit()
        return job_id

    def _domjudge_try_cache_shortcut(
        self,
        *,
        hostname: str,
        job_row: sqlite3.Row,
        case_row: sqlite3.Row,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
    ) -> dict[str, object] | None:
        source_hash = str(job_row["source_hash"] or "").strip().lower()
        compile_hash = str(job_row["compile_hash"] or "").strip().lower()
        run_hash = str(job_row["run_hash"] or "").strip().lower()
        compare_hash = str(job_row["compare_hash"] or "").strip().lower()
        testcase_hash = str(case_row["testcase_hash"] or "").strip().lower()
        testcase_input_hash = str(case_row["testcase_input_hash"] or "").strip().lower()
        testcase_answer_hash = str(case_row["testcase_answer_hash"] or "").strip().lower()
        answer_path = Path(str(case_row["answer_path"] or "")).resolve()
        input_path = Path(str(case_row["input_path"] or "")).resolve()
        if (not testcase_input_hash) and input_path.exists() and input_path.is_file():
            testcase_input_hash = self._domjudge_sha256_bytes(input_path.read_bytes())
        if (not testcase_answer_hash) and answer_path.exists() and answer_path.is_file():
            testcase_answer_hash = self._domjudge_sha256_bytes(answer_path.read_bytes())

        force_recompile = bool(int(job_row["force_recompile"] or 0))
        expected_behavior = str(job_row["expected_behavior"] or "unknown").strip().lower()
        invocation_source = str(job_row["invocation_source"] or "").strip().lower()
        solve_mode = invocation_source == "build.solve"

        case_key_hash, case_signature = self._domjudge_case_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            compare_config_hash=compare_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_hash=testcase_hash,
        )
        solve_key_hash, solve_signature = self._domjudge_solve_output_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_input_hash=testcase_input_hash,
        )
        if force_recompile:
            self._domjudge_cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
            self._domjudge_cache_delete(self.SOLVE_OUTPUT_CACHE_KIND, solve_key_hash, solve_signature)
            return None
        run_cfg_obj: dict[str, object] = {}
        try:
            parsed_run_cfg = json.loads(str(job_row["run_config_json"] or "{}"))
            if isinstance(parsed_run_cfg, dict):
                run_cfg_obj = parsed_run_cfg
        except Exception:
            run_cfg_obj = {}

        cached_exact = self._domjudge_cache_get(self.CASE_CACHE_KIND, case_key_hash, case_signature)
        if isinstance(cached_exact, dict):
            cached_value = cached_exact.get("value")
            cached_obj = cached_value if isinstance(cached_value, dict) else {}
            cached_runresult = str(cached_obj.get("runresult") or "").strip().lower()
            cached_runresult = self._domjudge_rewrite_untrusted_runresult(
                cached_runresult,
                cpu_sec=self._domjudge_parse_float(cached_obj.get("cpu_sec"), self._domjudge_parse_float(cached_obj.get("runtime_sec"), 0.0)),
                run_cfg_obj=run_cfg_obj,
            )
            cached_verdict = self._domjudge_verdict_from_runresult(cached_runresult)
            if cached_verdict == "FL":
                self._domjudge_cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                return None
            # Build answer generation and "expected accepted" runs must not reuse
            # non-OK cached outcomes; otherwise a transient WA/TL can poison future runs.
            if (solve_mode or expected_behavior == "accepted") and cached_verdict != "OK":
                return None
            materialized = self._domjudge_materialize_cached_case(
                cache_kind=self.CASE_CACHE_KIND,
                cache_key_hash=case_key_hash,
                cache_signature=case_signature,
                cache_value=dict(cached_obj),
                cache_files=dict(cached_exact.get("files") or {}),
            )
            output_run_rel = str(materialized.get("output_run_rel") or "").strip()
            if cached_verdict == "OK":
                # Cached OK result must carry a resolvable output artifact.
                if not output_run_rel:
                    self._domjudge_cache_delete(self.CASE_CACHE_KIND, case_key_hash, case_signature)
                    return None
            return {
                "lease_owner": hostname,
                "runresult": cached_runresult,
                "runtime_sec": float(materialized.get("runtime_sec") or 0.0),
                "cpu_sec": float(materialized.get("cpu_sec") or 0.0),
                "wall_sec": float(materialized.get("wall_sec") or 0.0),
                "memory_kb": int(materialized.get("memory_kb") or 0),
                "output_run_rel": output_run_rel,
                "output_error_rel": str(materialized.get("output_error_rel") or ""),
                "output_system_rel": str(materialized.get("output_system_rel") or ""),
                "output_diff_rel": str(materialized.get("output_diff_rel") or ""),
                "metadata_rel": str(materialized.get("metadata_rel") or ""),
                "compare_metadata_rel": str(materialized.get("compare_metadata_rel") or ""),
                "team_message_rel": str(materialized.get("team_message_rel") or ""),
                "score_text": str(materialized.get("score_text") or ""),
            }

        if solve_mode or expected_behavior != "accepted":
            return None
        cached_solve = self._domjudge_cache_get(self.SOLVE_OUTPUT_CACHE_KIND, solve_key_hash, solve_signature)
        if not isinstance(cached_solve, dict):
            return None
        solve_value = cached_solve.get("value")
        solve_obj = solve_value if isinstance(solve_value, dict) else {}
        output_hash = str(solve_obj.get("output_hash") or "").strip().lower()
        if (not output_hash) or (not testcase_answer_hash) or output_hash != testcase_answer_hash:
            return None
        materialized = self._domjudge_materialize_cached_case(
            cache_kind=self.SOLVE_OUTPUT_CACHE_KIND,
            cache_key_hash=solve_key_hash,
            cache_signature=solve_signature,
            cache_value=dict(solve_obj),
            cache_files=dict(cached_solve.get("files") or {}),
        )
        output_run_rel = str(materialized.get("output_run_rel") or "").strip()
        if not output_run_rel:
            self._domjudge_cache_delete(self.SOLVE_OUTPUT_CACHE_KIND, solve_key_hash, solve_signature)
            return None
        return {
            "lease_owner": hostname,
            "runresult": "correct",
            "runtime_sec": float(materialized.get("runtime_sec") or 0.0),
            "cpu_sec": float(materialized.get("cpu_sec") or 0.0),
            "wall_sec": float(materialized.get("wall_sec") or 0.0),
            "memory_kb": int(materialized.get("memory_kb") or 0),
            "output_run_rel": output_run_rel,
            "output_error_rel": str(materialized.get("output_error_rel") or ""),
            "output_system_rel": str(materialized.get("output_system_rel") or ""),
            "output_diff_rel": str(materialized.get("output_diff_rel") or ""),
            "metadata_rel": str(materialized.get("metadata_rel") or ""),
            "compare_metadata_rel": str(materialized.get("compare_metadata_rel") or ""),
            "team_message_rel": str(materialized.get("team_message_rel") or ""),
            "score_text": str(materialized.get("score_text") or ""),
        }

    def _domjudge_release_prepared_job_for_queue(self, job_id: int) -> None:
        now_text = now_iso()
        with self._domdb_conn() as conn:
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=NULL, status='queued', updated_at=?
                WHERE job_id=? AND status IN ('leased','queued')
                """,
                [now_text, int(job_id)],
            )
            conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='pending', lease_owner=NULL, updated_at=?
                WHERE job_id=? AND status='leased'
                """,
                [now_text, int(job_id)],
            )

    def _domjudge_try_prequeue_cache_finalize(self, *, task_id: str, run_id: str, payload: dict[str, object]) -> None:
        safe_task_id = str(task_id or "").strip()
        if not safe_task_id:
            return
        task_payload = dict(payload or {})
        build_payload = task_payload.get("build_payload")
        if not isinstance(build_payload, dict):
            return
        tests_payload = build_payload.get("tests")
        if not isinstance(tests_payload, list):
            return
        if not any(isinstance(row, dict) for row in tests_payload):
            return

        prequeue_host = self._normalize_hostname("prequeue-cache")
        job_id = 0
        try:
            job_id = int(
                self._domjudge_prepare_job(
                    prequeue_host,
                    {
                        "task_id": safe_task_id,
                        "run_id": str(run_id or "").strip(),
                        "payload": task_payload,
                    },
                )
            )
            job_row = self._db_fetch_one(
                """
                SELECT submit_id,contest_id,task_id,source_name,compile_config_json,run_config_json,compare_config_json,
                       compile_hash,run_hash,compare_hash,source_hash,expected_behavior,invocation_source,force_recompile,work_root,run_id
                FROM judgehost_domjudge_jobs
                WHERE job_id=?
                """,
                [int(job_id)],
            )
            if job_row is None:
                return
            rows = self._db_fetch_all(
                """
                SELECT *
                FROM judgehost_domjudge_cases
                WHERE job_id=? AND status='pending'
                ORDER BY ordinal ASC, id ASC
                """,
                [int(job_id)],
            )
            if not rows:
                self._domjudge_finalize_if_ready(int(job_id))
                return

            compile_cfg: dict[str, object] = {}
            run_cfg: dict[str, object] = {}
            compare_cfg: dict[str, object] = {}
            try:
                parsed = json.loads(str(job_row["compile_config_json"] or "{}"))
                if isinstance(parsed, dict):
                    compile_cfg = parsed
            except Exception:
                compile_cfg = {}
            try:
                parsed = json.loads(str(job_row["run_config_json"] or "{}"))
                if isinstance(parsed, dict):
                    run_cfg = parsed
            except Exception:
                run_cfg = {}
            try:
                parsed = json.loads(str(job_row["compare_config_json"] or "{}"))
                if isinstance(parsed, dict):
                    compare_cfg = parsed
            except Exception:
                compare_cfg = {}
            compile_config_hash = self._domjudge_json_hash(compile_cfg)
            run_config_hash = self._domjudge_json_hash(run_cfg)
            compare_config_hash = self._domjudge_json_hash(compare_cfg)
            toolchain_cmd_digest = str(compile_cfg.get("toolchain_cmd_digest") or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
                toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(str(job_row["source_name"] or ""))

            now_text = now_iso()
            work_root = Path(str(job_row["work_root"] or "")).resolve()
            cached_rows: list[tuple[sqlite3.Row, dict[str, object]]] = []
            pending_rows = 0
            for row in rows:
                shortcut = self._domjudge_try_cache_shortcut(
                    hostname=prequeue_host,
                    job_row=job_row,
                    case_row=row,
                    compile_config_hash=compile_config_hash,
                    run_config_hash=run_config_hash,
                    compare_config_hash=compare_config_hash,
                    toolchain_cmd_digest=toolchain_cmd_digest,
                )
                if isinstance(shortcut, dict):
                    cached_rows.append((row, dict(shortcut)))
                else:
                    pending_rows += 1

            if cached_rows:
                with self._domdb_conn() as conn:
                    for row, cached in cached_rows:
                        case_id = int(row["id"])
                        conn.execute(
                            """
                            UPDATE judgehost_domjudge_cases
                            SET status='reported', lease_owner=?, runresult=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?,
                                output_run_rel=?, output_error_rel=?, output_system_rel=?, output_diff_rel=?, metadata_rel=?, compare_metadata_rel=?, team_message_rel=?, score_text=?, updated_at=?
                            WHERE id=? AND status='pending'
                            """,
                            [
                                str(cached.get("lease_owner") or prequeue_host),
                                str(cached.get("runresult") or ""),
                                cached.get("runtime_sec"),
                                cached.get("cpu_sec"),
                                cached.get("wall_sec"),
                                cached.get("memory_kb"),
                                str(cached.get("output_run_rel") or ""),
                                str(cached.get("output_error_rel") or ""),
                                str(cached.get("output_system_rel") or ""),
                                str(cached.get("output_diff_rel") or ""),
                                str(cached.get("metadata_rel") or ""),
                                str(cached.get("compare_metadata_rel") or ""),
                                str(cached.get("team_message_rel") or ""),
                                str(cached.get("score_text") or ""),
                                now_text,
                                int(case_id),
                            ],
                        )
            if pending_rows > 0:
                self._domjudge_release_prepared_job_for_queue(int(job_id))
                return

            with self._state_lock:
                row = self._tasks_by_id.get(safe_task_id)
                if row is not None and str(row.get("status") or "").strip().lower() == self.STATUS_ENQUEUING:
                    row["status"] = self.STATUS_QUEUED
                    row["updated_at"] = now_iso()
            self._domjudge_finalize_if_ready(int(job_id))
        except Exception as exc:
            if job_id > 0:
                try:
                    self._domjudge_release_prepared_job_for_queue(int(job_id))
                except Exception:
                    pass
            logger.warning("prequeue cache consumption failed task_id=%s: %s", safe_task_id, exc)

    def _domjudge_lease_cases(self, job_id: int, hostname: str, max_batchsize: int) -> list[dict[str, object]]:
        cap = max(1, min(256, int(max_batchsize)))
        now_text = now_iso()
        job_row = self._db_fetch_one(
            """
            SELECT submit_id,contest_id,task_id,compile_config_json,run_config_json,compare_config_json,compile_hash,run_hash,compare_hash
            FROM judgehost_domjudge_jobs
            WHERE job_id=?
            """,
            [int(job_id)],
        )
        if job_row is None:
            return []
        rows = self._db_fetch_all(
            """
            SELECT *
            FROM judgehost_domjudge_cases
            WHERE job_id=? AND status='pending'
            ORDER BY ordinal ASC, id ASC
            LIMIT ?
            """,
            [int(job_id), int(cap)],
        )
        if not rows:
            return []
        compile_provider_job_id = self._domjudge_script_provider_job_id(
            kind="compile",
            script_hash=str(job_row["compile_hash"] or ""),
            default_job_id=int(job_id),
        )
        run_provider_job_id = self._domjudge_script_provider_job_id(
            kind="run",
            script_hash=str(job_row["run_hash"] or ""),
            default_job_id=int(job_id),
        )
        compare_provider_job_id = self._domjudge_script_provider_job_id(
            kind="compare",
            script_hash=str(job_row["compare_hash"] or ""),
            default_job_id=int(job_id),
        )
        compile_id = int(self._domjudge_script_ids(compile_provider_job_id)[0])
        run_id_num = int(self._domjudge_script_ids(run_provider_job_id)[1])
        compare_id = int(self._domjudge_script_ids(compare_provider_job_id)[2])
        raw_submit_id = str(job_row["submit_id"] or "").strip()
        safe_submit_id = str(int(raw_submit_id))
        safe_task_id = str(job_row["task_id"] or "")
        out: list[dict[str, object]] = []
        with self._domdb_conn() as conn:
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=?, status='leased', updated_at=?
                WHERE job_id=?
                """,
                [hostname, now_text, int(job_id)],
            )
            for row in rows:
                case_id = int(row["id"])
                testcase_id = 0
                try:
                    testcase_id = int(row["testcase_id"] or 0)
                except Exception:
                    testcase_id = 0
                if testcase_id <= 0:
                    testcase_id = case_id
                updated = conn.execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='leased', lease_owner=?, updated_at=?
                    WHERE id=? AND status='pending'
                    """,
                    [hostname, now_text, case_id],
                )
                if int(updated.rowcount or 0) <= 0:
                    continue
                out.append(
                    {
                        "type": "judging_run",
                        "judgetaskid": case_id,
                        "jobid": int(job_id),
                        "uuid": safe_task_id,
                        "submitid": safe_submit_id,
                        "contestid": str(job_row["contest_id"] or "local"),
                        "compile_script_id": str(int(compile_id)),
                        "run_script_id": str(int(run_id_num)),
                        "compare_script_id": str(int(compare_id)),
                        "testcase_id": str(int(testcase_id)),
                        "testcase_hash": str(row["testcase_hash"] or ""),
                        "compile_config": str(job_row["compile_config_json"] or "{}"),
                        "run_config": str(job_row["run_config_json"] or "{}"),
                        "compare_config": str(job_row["compare_config_json"] or "{}"),
                    }
                )
        self.renew_lease(safe_task_id, hostname)
        return out

    def domjudge_fetch_work(self, hostname: str, max_batchsize: int | None = None) -> list[dict[str, object]]:
        safe_host = self._normalize_hostname(hostname)
        if not self._host_enabled_conn(hostname=safe_host):
            self._record_host_event_conn(hostname=safe_host, action="disabled")
            return []
        cap = self._fetch_batch_size if max_batchsize is None else max(1, min(256, int(max_batchsize)))
        max_attempts = max(1, min(32, cap * 4))

        for _ in range(max_attempts):
            active = self._domjudge_active_job_for_host(safe_host)
            if active is not None:
                active_job_id = int(active["job_id"])
                leased_cases = self._domjudge_lease_cases(active_job_id, safe_host, cap)
                if leased_cases:
                    return leased_cases
                # No pending cases for the active job; attempt finalization and retry.
                self._domjudge_finalize_if_ready(active_job_id)
                refreshed = self._domjudge_active_job_for_host(safe_host)
                if refreshed is not None and int(refreshed["job_id"]) == active_job_id:
                    return []
                continue

            leased = self.fetch_work(safe_host, limit=1)
            if not leased:
                shared_job = self._domjudge_shared_pending_job(safe_host)
                if shared_job is not None:
                    shared_job_id = int(shared_job["job_id"])
                    leased_cases = self._domjudge_lease_cases(shared_job_id, safe_host, cap)
                    if leased_cases:
                        return leased_cases
                    self._domjudge_finalize_if_ready(shared_job_id)
                return []
            leased_task = leased[0] if isinstance(leased[0], dict) else {}
            task_id = str(leased_task.get("task_id") or "").strip()
            try:
                active_job_id = self._domjudge_prepare_job(safe_host, leased_task)
            except Exception as exc:
                error_text = str(exc or "invalid judgehost task payload").strip() or "invalid judgehost task payload"
                logger.warning("invalid judgehost task dropped task_id=%s host=%s: %s", task_id, safe_host, error_text)
                if task_id:
                    try:
                        self.report_result(
                            task_id=task_id,
                            hostname=safe_host,
                            payload={
                                "run_status": "failed",
                                "error": error_text,
                                "summary": {
                                    "error": error_text,
                                    "invocation_backend": "domjudge-judgehost",
                                },
                            },
                        )
                    except Exception as report_exc:
                        logger.warning("failed to mark invalid judgehost task as failed task_id=%s: %s", task_id, report_exc)
                continue

            leased_cases = self._domjudge_lease_cases(active_job_id, safe_host, cap)
            if leased_cases:
                return leased_cases
            self._domjudge_finalize_if_ready(active_job_id)

        return []

    def domjudge_get_source_files(self, submit_id: str, contest_id: str | None = None) -> list[dict[str, object]]:
        safe_submit = str(submit_id or "").strip()
        if not safe_submit:
            raise RuntimeError("source files not found")
        row = None
        if safe_submit.isdigit():
            row = self._db_fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE job_id=?
                """,
                [int(safe_submit)],
            )
        if row is None and contest_id is not None:
            safe_contest = self._domjudge_contest_id(contest_id)
            row = self._db_fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE submit_id=? AND contest_id=?
                """,
                [safe_submit, safe_contest],
            )
        if row is None:
            row = self._db_fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE submit_id=?
                """,
                [safe_submit],
            )
        if row is None:
            raise RuntimeError("source files not found")
        source_path = Path(str(row["source_path"] or "")).resolve()
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError("source files not found")
        content = base64.b64encode(source_path.read_bytes()).decode("ascii")
        return [{"filename": str(row["source_name"] or source_path.name), "content": content}]

    def domjudge_get_testcase_files(self, testcase_id: int) -> list[dict[str, object]]:
        token = int(testcase_id)
        row = None
        with self._testcase_registry_lock:
            record = self._testcase_registry_by_id.get(int(token))
            if isinstance(record, dict):
                row = {
                    "input_path": str(record.get("input_path") or ""),
                    "answer_path": str(record.get("answer_path") or ""),
                }
        if row is None:
            row = self._db_fetch_one(
                """
                SELECT input_path,answer_path
                FROM judgehost_domjudge_cases
                WHERE testcase_id=? OR id=?
                ORDER BY id ASC
                LIMIT 1
                """,
                [token, token],
            )
        if row is None:
            raise RuntimeError("testcase files not found")
        in_path = Path(str(row["input_path"] or "")).resolve()
        ans_path = Path(str(row["answer_path"] or "")).resolve()
        if not in_path.exists() or not ans_path.exists():
            raise RuntimeError("testcase files not found")
        return [
            {"filename": "input", "content": base64.b64encode(in_path.read_bytes()).decode("ascii")},
            {"filename": "output", "content": base64.b64encode(ans_path.read_bytes()).decode("ascii")},
        ]

    def domjudge_get_executable_files(self, kind: str, script_id: object) -> list[dict[str, object]]:
        job_id, offset = self._domjudge_parse_script_id(script_id)
        expected = {"compile": 1, "run": 2, "compare": 3}
        token = str(kind or "").strip().lower()
        if token not in expected or expected[token] != offset:
            raise RuntimeError("script id/type mismatch")
        job_row = self._db_fetch_one("SELECT work_root FROM judgehost_domjudge_jobs WHERE job_id=?", [job_id])
        if job_row is None:
            raise RuntimeError("script files not found")
        base = (Path(str(job_row["work_root"] or "")).resolve() / "scripts" / token).resolve()
        if not base.exists() or not base.is_dir():
            raise RuntimeError("script files not found")
        rows: list[dict[str, object]] = []
        for file in sorted(base.iterdir(), key=lambda item: item.name):
            if not file.is_file():
                continue
            st_mode = int(file.stat().st_mode)
            rows.append(
                {
                    "filename": file.name,
                    "content": base64.b64encode(file.read_bytes()).decode("ascii"),
                    "is_executable": bool(st_mode & 0o111),
                }
            )
        if not rows:
            raise RuntimeError("script files not found")
        return rows

    def domjudge_get_version_commands(self, judgetask_id: int) -> dict[str, object]:
        _ = int(judgetask_id)
        return {}

    def domjudge_check_versions(
        self,
        judgetask_id: int,
        *,
        hostname: str,
        compiler: str = "",
        runner: str = "",
    ) -> dict[str, object]:
        _ = int(judgetask_id)
        _ = str(hostname or "")
        _ = str(compiler or "")
        _ = str(runner or "")
        return {}

    @staticmethod
    def _domjudge_verdict_from_runresult(raw: str) -> str:
        token = str(raw or "").strip().lower()
        mapping = {
            "correct": "OK",
            "compiler-error": "CE",
            "timelimit": "TL",
            "run-error": "RE",
            "wrong-answer": "WA",
            "no-output": "WA",
            "output-limit": "FL",
            "compare-error": "FL",
            "internal-error": "FL",
        }
        return mapping.get(token, "FL")

    def _domjudge_task_lease_owner(self, task_id: str) -> str:
        row = self._task_by_id(task_id)
        if row is None:
            return "judgehost"
        token = str(row.get("lease_owner") or "").strip()
        if token:
            return token
        return "judgehost"

    def _domjudge_finalize_if_ready(self, job_id: int, *, force_failed: bool = False, error_text: str = "") -> None:
        job_row = self._db_fetch_one(
            """
            SELECT task_id,run_id,status,compile_success,compile_output_b64,compile_metadata_b64,work_root,run_config_json
            FROM judgehost_domjudge_jobs
            WHERE job_id=?
            """,
            [int(job_id)],
        )
        if job_row is None:
            return
        current_status = str(job_row["status"] or "").strip().lower()
        if current_status in {"completed", "failed"}:
            return
        cases = self._domjudge_cases_for_job(int(job_id))
        if not cases:
            return
        compile_success_raw = job_row["compile_success"]
        compile_success = None
        if compile_success_raw is not None:
            try:
                compile_success = int(compile_success_raw)
            except Exception:
                compile_success = None
        ready = force_failed or compile_success == 0
        if not ready:
            ready = all(str(row["status"] or "").strip().lower() == "reported" for row in cases)
        if not ready:
            return

        tests: list[dict[str, object]] = []
        usage_time_user = 0
        usage_time_wall = 0
        usage_mem_peak = 0
        work_root = Path(str(job_row["work_root"] or "")).resolve()
        for row in cases:
            test_name = str(row["test_name"] or "").strip() or f"{int(row['ordinal']):03}.in"
            test_stem = Path(test_name).stem
            runresult = str(row["runresult"] or "").strip()
            verdict = self._domjudge_verdict_from_runresult(runresult)
            if compile_success == 0:
                verdict = "CE"
            cpu_sec = self._domjudge_parse_float(row["cpu_sec"], self._domjudge_parse_float(row["runtime_sec"], 0.0))
            wall_sec = self._domjudge_parse_float(row["wall_sec"], cpu_sec)
            memory_kb = max(0, self._domjudge_parse_int(row["memory_kb"], 0))
            cpu_ms = max(0, int(round(cpu_sec * 1000)))
            wall_ms = max(0, int(round(wall_sec * 1000)))
            usage_time_user += cpu_ms
            usage_time_wall += wall_ms
            usage_mem_peak = max(usage_mem_peak, memory_kb)
            feedback_files: list[str] = []
            feedback_text = ""
            has_output_diff = False
            has_team_message = False
            for key in ("output_diff_rel", "team_message_rel"):
                token = str(row[key] or "").strip()
                if token:
                    if key == "output_diff_rel":
                        has_output_diff = True
                    elif key == "team_message_rel":
                        has_team_message = True
                    if not feedback_text:
                        blob = self._domjudge_read_artifact_blob(work_root, token)
                        if blob is not None:
                            feedback_text = self._domjudge_feedback_line_from_bytes(blob)
            if test_stem:
                for filename, present in (("judgemessage.txt", has_output_diff), ("teammessage.txt", has_team_message)):
                    if not present:
                        continue
                    feedback_files.append(f"feedback_dir/{test_stem}/{filename}")
            final_pass = {
                "verdict": verdict,
                "time_ms": cpu_ms,
                "time_user_ms": cpu_ms,
                "time_wall_ms": wall_ms,
                "memory_kb": memory_kb,
            }
            output_ref = str(row["output_run_rel"] or "").strip()
            if output_ref:
                final_pass["output_ref"] = output_ref
            if feedback_text:
                final_pass["feedback"] = feedback_text
            tests.append(
                {
                    "test": test_name,
                    "passes": [final_pass],
                    "verdict": verdict,
                    "time_ms": cpu_ms,
                    "time_user_ms": cpu_ms,
                    "time_wall_ms": wall_ms,
                    "memory_kb": memory_kb,
                    "feedback_files": feedback_files,
                }
            )

        compile_log = ""
        compile_diag: list[dict[str, object]] = []
        if compile_success == 0:
            compile_log = "compile.log"
            compile_text = self._domjudge_b64_decode(job_row["compile_output_b64"]).decode("utf-8", errors="replace")
            message = "compilation failed"
            if compile_text.strip():
                message = compile_text.strip()
            compile_diag.append(
                {
                    "level": "error",
                    "message": message,
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "can_link": False,
                }
            )

        task_id = str(job_row["task_id"] or "").strip()
        run_status = "failed" if force_failed else "ok"
        summary = self._load_run_summary(str(job_row["run_id"] or "").strip())
        summary = dict(summary or {})
        summary["tests"] = tests
        summary["compile_log"] = compile_log
        summary["compile_diagnostics"] = compile_diag
        summary["usage"] = {
            "tests": len(tests),
            "time_ms_total": usage_time_user,
            "time_user_ms_total": usage_time_user,
            "time_wall_ms_total": usage_time_wall,
            "memory_kb_peak": usage_mem_peak,
        }
        summary.setdefault("invocation_backend", "domjudge-judgehost")
        if force_failed and error_text:
            summary["error"] = str(error_text)
        result_payload: dict[str, object] = {"run_status": run_status, "summary": summary}
        if force_failed and error_text:
            result_payload["error"] = str(error_text)
        try:
            self.report_result(
                task_id=task_id,
                hostname=self._domjudge_task_lease_owner(task_id),
                payload=result_payload,
            )
        except RuntimeError as exc:
            logger.warning("failed to finalize DOMjudge job %s via report_result: %s", int(job_id), exc)
        self._db_execute(
            "UPDATE judgehost_domjudge_jobs SET status=?, completed_at=?, updated_at=? WHERE job_id=?",
            ["failed" if force_failed else "completed", now_iso(), now_iso(), int(job_id)],
        )

    def domjudge_update_judging(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> None:
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        case_row = self._db_fetch_one("SELECT id,job_id FROM judgehost_domjudge_cases WHERE id=?", [case_id])
        if case_row is None:
            raise RuntimeError("unknown judging run id")
        job_id = int(case_row["job_id"])
        compile_success = None
        if "compile_success" in payload:
            compile_success = 1 if self._domjudge_bool(payload.get("compile_success"), default=False) else 0
        compile_output = str(payload.get("output_compile") or "").strip()
        compile_meta = str(payload.get("compile_metadata") or "").strip()
        if compile_success is not None:
            self._db_execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET compile_success=?, compile_output_b64=?, compile_metadata_b64=?, lease_owner=?, updated_at=?
                WHERE job_id=?
                """,
                [compile_success, compile_output, compile_meta, safe_host, now_iso(), job_id],
            )
            if compile_success == 0:
                self._db_execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='reported', runresult='compiler-error', runtime_sec=0, cpu_sec=0, wall_sec=0, memory_kb=0, updated_at=?
                    WHERE job_id=? AND status<>'reported'
                    """,
                    [now_iso(), job_id],
                )
                self._domjudge_finalize_if_ready(job_id)

    def domjudge_add_judging_run(self, hostname: str, judgetask_id: int, payload: dict[str, object]) -> int:
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        row = self._db_fetch_one(
            """
            SELECT
                c.id,c.job_id,c.task_id,c.test_name,c.testcase_hash,c.input_path,c.answer_path,
                j.run_id,j.work_root,j.mode,j.source_name,j.source_path,
                j.compile_hash,j.run_hash,j.compare_hash,
                j.compile_config_json,j.run_config_json,j.compare_config_json
            FROM judgehost_domjudge_cases c
            JOIN judgehost_domjudge_jobs j ON j.job_id=c.job_id
            WHERE c.id=?
            """,
            [case_id],
        )
        if row is None:
            raise RuntimeError("unknown judging run id")
        job_id = int(row["job_id"])
        safe_task_id = str(row["task_id"] or "").strip()
        test_name = str(row["test_name"] or "").strip() or f"{case_id}.in"
        work_root = Path(str(row["work_root"] or "")).resolve()
        mode = str(row["mode"] or "").strip().lower()
        result_root = (work_root / "results" / f"{case_id}").resolve()
        result_root.mkdir(parents=True, exist_ok=True)

        def _write_b64(
            name: str,
            value: object,
            *,
            strip_protocol: bool = False,
            allow_empty: bool = False,
        ) -> str:
            if value is None:
                return ""
            raw = self._domjudge_b64_decode(value)
            if strip_protocol:
                raw = self._domjudge_strip_protocol_trace(raw)
            if (not raw) and (not allow_empty):
                return ""
            target = (result_root / name).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            return str(target.relative_to(work_root).as_posix())

        output_run_rel = _write_b64(
            "program.out",
            payload.get("output_run"),
            strip_protocol=mode in {"interactive", "multi-pass"},
            allow_empty=True,
        )
        output_err_rel = _write_b64("program.err", payload.get("output_error"))
        output_sys_rel = _write_b64("system.out", payload.get("output_system"))
        output_diff_rel = _write_b64("judgemessage.txt", payload.get("output_diff"))
        metadata_rel = _write_b64("program.meta", payload.get("metadata"))
        compare_meta_rel = _write_b64("compare.meta", payload.get("compare_metadata"))
        team_message_rel = _write_b64("teammessage.txt", payload.get("team_message"))

        runtime_sec = self._domjudge_parse_float(payload.get("runtime"), 0.0)
        cpu_sec = runtime_sec
        wall_sec = runtime_sec
        memory_kb = 0
        if metadata_rel:
            meta_path = (work_root / metadata_rel).resolve()
            if meta_path.exists() and meta_path.is_file():
                meta = self._domjudge_parse_meta_text(meta_path.read_text(encoding="utf-8", errors="replace"))
                cpu_total_sec = self._domjudge_parse_float(meta.get("cpu-time"), runtime_sec)
                wall_sec = self._domjudge_parse_float(meta.get("wall-time"), cpu_total_sec)
                cpu_sec = cpu_total_sec
                runtime_sec = cpu_sec
                mem_bytes = self._domjudge_parse_int(meta.get("memory-bytes"), 0)
                memory_kb = max(0, int(mem_bytes // 1024))

        score_text = str(payload.get("score") or "").strip()
        feedback_text = ""
        for rel in (output_diff_rel, team_message_rel):
            token = str(rel or "").strip()
            if (not token) or feedback_text:
                continue
            blob = self._domjudge_read_artifact_blob(work_root, token)
            if blob is not None:
                feedback_text = self._domjudge_feedback_line_from_bytes(blob)

        task_payload_obj: dict[str, object] = {}
        if safe_task_id:
            task_payload_obj = self._task_payload(safe_task_id)
        invocation_source = str(task_payload_obj.get("invocation_source") or "").strip().lower()

        def _load_json_object(raw: object) -> dict[str, object]:
            text = str(raw or "").strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        source_name = str(row["source_name"] or "").strip()
        source_bytes = b""
        source_path = Path(str(row["source_path"] or "")).resolve()
        try:
            if source_path.exists() and source_path.is_file() and (not source_path.is_symlink()):
                source_bytes = source_path.read_bytes()
        except OSError:
            source_bytes = b""
        source_hash = self._domjudge_source_hash(source_name, source_bytes)

        input_bytes = b""
        input_path = Path(str(row["input_path"] or "")).resolve()
        try:
            if input_path.exists() and input_path.is_file() and (not input_path.is_symlink()):
                input_bytes = input_path.read_bytes()
        except OSError:
            input_bytes = b""

        answer_bytes = b""
        answer_path = Path(str(row["answer_path"] or "")).resolve()
        try:
            if answer_path.exists() and answer_path.is_file() and (not answer_path.is_symlink()):
                answer_bytes = answer_path.read_bytes()
        except OSError:
            answer_bytes = b""

        testcase_hash = str(row["testcase_hash"] or "").strip().lower()
        testcase_input_hash = self._domjudge_sha256_bytes(input_bytes)
        testcase_answer_hash = self._domjudge_sha256_bytes(answer_bytes)
        if not re.fullmatch(r"[0-9a-f]{64}", testcase_hash):
            if invocation_source == "build.solve":
                testcase_hash = testcase_input_hash
            else:
                testcase_hash = self._domjudge_set_hash_from_blobs([input_bytes, answer_bytes])

        compile_hash = str(row["compile_hash"] or "").strip().lower()
        run_hash = str(row["run_hash"] or "").strip().lower()
        compare_hash = str(row["compare_hash"] or "").strip().lower()
        compile_cfg_obj = _load_json_object(row["compile_config_json"])
        run_cfg_obj = _load_json_object(row["run_config_json"])
        compare_cfg_obj = _load_json_object(row["compare_config_json"])
        runresult = str(payload.get("runresult") or "").strip().lower() or "internal-error"
        runresult = self._domjudge_rewrite_untrusted_runresult(
            runresult,
            cpu_sec=cpu_sec,
            run_cfg_obj=run_cfg_obj,
        )
        verdict = self._domjudge_verdict_from_runresult(runresult)
        if verdict == "OK" and (not str(output_run_rel or "").strip()):
            target = (result_root / "program.out").resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
            output_run_rel = str(target.relative_to(work_root).as_posix())
        compile_config_hash = self._domjudge_json_hash(compile_cfg_obj)
        run_config_hash = self._domjudge_json_hash(run_cfg_obj)
        compare_config_hash = self._domjudge_json_hash(compare_cfg_obj)
        toolchain_cmd_digest = str(compile_cfg_obj.get("toolchain_cmd_digest") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", toolchain_cmd_digest) is None:
            toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(source_name)

        cache_files: dict[str, bytes] = {}

        def _read_rel_blob(rel_path: str) -> bytes | None:
            return self._domjudge_read_artifact_blob(work_root, rel_path)

        for rel, blob_name in (
            (output_run_rel, "program.out"),
            (output_err_rel, "program.err"),
            (output_sys_rel, "system.out"),
            (output_diff_rel, "judgemessage.txt"),
            (metadata_rel, "program.meta"),
            (compare_meta_rel, "compare.meta"),
            (team_message_rel, "teammessage.txt"),
        ):
            blob = _read_rel_blob(str(rel or ""))
            if blob is not None:
                cache_files[blob_name] = blob

        case_key_hash, case_signature = self._domjudge_case_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            compare_config_hash=compare_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_hash=testcase_hash,
        )
        if verdict != "FL":
            self._domjudge_store_case_cache(
                key_parts={"key_hash": case_key_hash, "signature": case_signature},
                tags={
                    "source_hash": source_hash,
                    "testcase_hash": testcase_hash,
                    "invocation_source": invocation_source,
                },
                runresult=runresult,
                runtime_sec=runtime_sec,
                cpu_sec=cpu_sec,
                wall_sec=wall_sec,
                memory_kb=memory_kb,
                score_text=score_text,
                files=cache_files,
            )

        output_run_token = str(output_run_rel or "").strip()
        output_err_token = str(output_err_rel or "").strip()
        output_sys_token = str(output_sys_rel or "").strip()
        output_diff_token = str(output_diff_rel or "").strip()
        metadata_token = str(metadata_rel or "").strip()
        compare_meta_token = str(compare_meta_rel or "").strip()
        team_message_token = str(team_message_rel or "").strip()
        if verdict != "FL" and self._judge_fs_index_service is not None:
            refs_by_name = {
                name: self._domjudge_cache_blob_ref(
                    kind=self.CASE_CACHE_KIND,
                    key_hash=case_key_hash,
                    signature=case_signature,
                    name=name,
                )
                for name in cache_files.keys()
            }
            output_run_token = str(refs_by_name.get("program.out") or output_run_token)
            output_err_token = str(refs_by_name.get("program.err") or output_err_token)
            output_sys_token = str(refs_by_name.get("system.out") or output_sys_token)
            output_diff_token = str(refs_by_name.get("judgemessage.txt") or output_diff_token)
            metadata_token = str(refs_by_name.get("program.meta") or metadata_token)
            compare_meta_token = str(refs_by_name.get("compare.meta") or compare_meta_token)
            team_message_token = str(refs_by_name.get("teammessage.txt") or team_message_token)

        if invocation_source == "build.solve" and runresult == "correct":
            solve_key_hash, solve_signature = self._domjudge_solve_output_cache_ref(
                source_hash=source_hash,
                compile_hash=compile_hash,
                run_hash=run_hash,
                compile_config_hash=compile_config_hash,
                run_config_hash=run_config_hash,
                toolchain_cmd_digest=toolchain_cmd_digest,
                testcase_input_hash=testcase_input_hash,
            )
            output_bytes = cache_files.get("program.out", b"")
            output_hash = self._domjudge_sha256_bytes(output_bytes)
            self._domjudge_store_solve_output_cache(
                key_parts={"key_hash": solve_key_hash, "signature": solve_signature},
                tags={
                    "source_hash": source_hash,
                    "testcase_input_hash": testcase_input_hash,
                    "testcase_answer_hash": testcase_answer_hash,
                },
                output_hash=output_hash,
                runtime_sec=runtime_sec,
                cpu_sec=cpu_sec,
                wall_sec=wall_sec,
                memory_kb=memory_kb,
                files=cache_files,
            )

        now_text = now_iso()
        self._db_execute(
            """
            UPDATE judgehost_domjudge_cases
            SET status='reported', lease_owner=?, runresult=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?,
                output_run_rel=?, output_error_rel=?, output_system_rel=?, output_diff_rel=?, metadata_rel=?, compare_metadata_rel=?, team_message_rel=?, score_text=?, updated_at=?
            WHERE id=?
            """,
            [
                safe_host,
                runresult,
                runtime_sec,
                cpu_sec,
                wall_sec,
                memory_kb,
                output_run_token,
                output_err_token,
                output_sys_token,
                output_diff_token,
                metadata_token,
                compare_meta_token,
                team_message_token,
                score_text,
                now_text,
                case_id,
            ],
        )
        self._record_host_judging(safe_host, label=f"j{job_id}", updated_at=now_text)

        self._domjudge_finalize_if_ready(job_id)
        return 1

    def domjudge_internal_error(self, *, description: str, judgetask_id: int | None = None) -> int:
        safe_desc = str(description or "").strip() or "judgehost internal error"
        if judgetask_id is None:
            return 0
        row = self._db_fetch_one("SELECT job_id FROM judgehost_domjudge_cases WHERE id=?", [int(judgetask_id)])
        if row is None:
            return 0
        self._domjudge_finalize_if_ready(int(row["job_id"]), force_failed=True, error_text=safe_desc)
        return int(judgetask_id)

    def domjudge_add_debug_info(self, *, hostname: str, judgetask_id: int, payload: dict[str, object] | None = None) -> None:
        safe_host = self._normalize_hostname(hostname)
        case_id = int(judgetask_id)
        row = self._db_fetch_one(
            "SELECT task_id,run_id FROM judgehost_domjudge_cases WHERE id=?",
            [case_id],
        )
        safe_task_id = str(row["task_id"] or "").strip() if row is not None else ""
        safe_run_id = str(row["run_id"] or "").strip() if row is not None else ""
        debug_payload = payload if isinstance(payload, dict) else {}
        if debug_payload:
            logger.debug(
                "domjudge debug info host=%s judgetask_id=%s payload_keys=%s",
                safe_host,
                case_id,
                sorted(str(key) for key in debug_payload.keys()),
            )
        with self._domdb_conn() as conn:
            self._record_host_event_conn(
                conn,
                hostname=safe_host,
                action="debug",
                task_id=safe_task_id,
                run_id=safe_run_id,
            )



