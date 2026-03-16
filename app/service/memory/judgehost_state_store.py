from __future__ import annotations

import sqlite3
import threading
from typing import TypedDict


class JudgehostJobRow(TypedDict):
    job_id: int
    task_id: str
    run_id: str
    submit_id: str
    contest_id: str
    mode: str
    source_name: str
    source_path: str
    work_root: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    source_hash: str
    compile_config_json: str
    run_config_json: str
    compare_config_json: str
    expected_behavior: str
    verification_source: str
    force_recompile: int
    lease_owner: str
    compile_success: int | None
    compile_output_b64: str
    compile_metadata_b64: str
    debug_text: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str


class JudgehostCaseRow(TypedDict):
    id: int
    job_id: int
    task_id: str
    run_id: str
    test_name: str
    ordinal: int
    testcase_id: int | None
    testcase_hash: str
    testcase_input_hash: str
    testcase_answer_hash: str
    input_path: str
    answer_path: str
    status: str
    lease_owner: str
    runresult: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    output_run_rel: str
    output_error_rel: str
    output_system_rel: str
    output_diff_rel: str
    metadata_rel: str
    compare_metadata_rel: str
    team_message_rel: str
    score_text: str
    debug_text: str
    created_at: str
    updated_at: str


class JudgehostStateStore:
    def __init__(self, lock: threading.RLock | None = None):
        self._lock = threading.RLock() if lock is None else lock
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn
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
                    verification_source TEXT NOT NULL DEFAULT 'run.execute',
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

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM judgehost_domjudge_cases")
            self._conn.execute("DELETE FROM judgehost_domjudge_jobs")
            self._conn.commit()

    def active_job_for_host(self, hostname: str) -> JudgehostJobRow | None:
        rows = self._fetch_all(
            """
            SELECT j.*
            FROM judgehost_domjudge_jobs j
            WHERE j.lease_owner=? AND j.status IN ('leased','queued')
            ORDER BY j.job_id ASC
            LIMIT 1
            """,
            [hostname],
        )
        return rows[0] if rows else None

    def shared_pending_job(self, hostname: str) -> JudgehostJobRow | None:
        rows = self._fetch_all(
            """
            SELECT j.*
            FROM judgehost_domjudge_jobs j
            WHERE (
                (j.lease_owner=? AND j.status IN ('leased','queued'))
                OR ((j.lease_owner IS NULL OR TRIM(j.lease_owner)='') AND j.status='queued')
            )
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
            [hostname, hostname],
        )
        return rows[0] if rows else None

    def cases_for_job(self, job_id: int, *, status: str | None = None) -> list[JudgehostCaseRow]:
        if status:
            return self._fetch_all(
                """
                SELECT *
                FROM judgehost_domjudge_cases
                WHERE job_id=? AND status=?
                ORDER BY ordinal ASC, id ASC
                """,
                [int(job_id), status],
            )
        return self._fetch_all(
            """
            SELECT *
            FROM judgehost_domjudge_cases
            WHERE job_id=?
            ORDER BY ordinal ASC, id ASC
            """,
            [int(job_id)],
        )

    def fetch_job(self, job_id: int) -> JudgehostJobRow | None:
        row = self._fetch_one("SELECT * FROM judgehost_domjudge_jobs WHERE job_id=? LIMIT 1", [int(job_id)])
        return None if row is None else row

    def job_for_task(self, task_id: str) -> JudgehostJobRow | None:
        row = self._fetch_one("SELECT * FROM judgehost_domjudge_jobs WHERE task_id=? LIMIT 1", [task_id])
        return None if row is None else row

    def fetch_case(self, case_id: int) -> JudgehostCaseRow | None:
        row = self._fetch_one("SELECT * FROM judgehost_domjudge_cases WHERE id=? LIMIT 1", [int(case_id)])
        return None if row is None else row

    def cases_for_run(self, run_id: str) -> list[JudgehostCaseRow]:
        return self._fetch_all(
            """
            SELECT *
            FROM judgehost_domjudge_cases
            WHERE run_id=?
            ORDER BY ordinal ASC, id ASC
            """,
            [run_id],
        )

    def source_file_job(self, submit_id: str, *, contest_id: str | None = None) -> dict[str, object] | None:
        if submit_id.isdigit():
            row = self._fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE job_id=?
                """,
                [int(submit_id)],
            )
            if row is not None:
                return row
        if contest_id is not None:
            row = self._fetch_one(
                """
                SELECT source_name,source_path
                FROM judgehost_domjudge_jobs
                WHERE submit_id=? AND contest_id=?
                """,
                [submit_id, contest_id],
            )
            if row is not None:
                return row
        return self._fetch_one(
            """
            SELECT source_name,source_path
            FROM judgehost_domjudge_jobs
            WHERE submit_id=?
            """,
            [submit_id],
        )

    def testcase_paths(self, testcase_id: int) -> tuple[dict[str, object] | None, str]:
        row = self._fetch_one(
            """
            SELECT input_path,answer_path
            FROM judgehost_domjudge_cases
            WHERE id=?
            LIMIT 1
            """,
            [int(testcase_id)],
        )
        if row is not None:
            return row, "case-id"
        row = self._fetch_one(
            """
            SELECT input_path,answer_path
            FROM judgehost_domjudge_cases
            WHERE testcase_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            [int(testcase_id)],
        )
        if row is not None:
            return row, "stored-testcase-id"
        return None, "missing"

    def work_root_for_job(self, job_id: int) -> str:
        row = self._fetch_one("SELECT work_root FROM judgehost_domjudge_jobs WHERE job_id=?", [int(job_id)])
        if row is None:
            return ""
        return str(row["work_root"])

    def jobs_for_script_hash(self, *, kind: str, script_hash: str) -> list[dict[str, object]]:
        field_map = {
            "compile": "compile_hash",
            "run": "run_hash",
            "compare": "compare_hash",
        }
        field = field_map.get(kind)
        if field is None:
            return []
        return self._fetch_all(
            f"""
            SELECT job_id,work_root
            FROM judgehost_domjudge_jobs
            WHERE {field}=?
            ORDER BY job_id ASC
            LIMIT 256
            """,
            [script_hash],
        )

    def job_finalize_row(self, job_id: int) -> dict[str, object] | None:
        return self._fetch_one(
            """
            SELECT task_id,run_id,status,compile_success,compile_output_b64,compile_metadata_b64,work_root,run_config_json,
                   compile_hash,run_hash,compare_hash
            FROM judgehost_domjudge_jobs
            WHERE job_id=?
            """,
            [int(job_id)],
        )

    def set_job_terminal_status(self, job_id: int, *, status: str, completed_at: str, updated_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE judgehost_domjudge_jobs SET status=?, completed_at=?, updated_at=? WHERE job_id=?",
                [status, completed_at, updated_at, int(job_id)],
            )
            self._conn.commit()

    def record_compile_result(
        self,
        job_id: int,
        *,
        compile_success: int,
        compile_output_b64: str,
        compile_metadata_b64: str,
        lease_owner: str,
        updated_at: str,
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET compile_success=?, compile_output_b64=?, compile_metadata_b64=?, lease_owner=?, updated_at=?
                WHERE job_id=? AND status IN ('queued','leased')
                """,
                [compile_success, compile_output_b64, compile_metadata_b64, lease_owner, updated_at, int(job_id)],
            )
            self._conn.commit()
        return int(cursor.rowcount or 0) > 0

    def mark_compile_failed_cases(self, job_id: int, *, updated_at: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='reported', runresult='compiler-error', runtime_sec=0, cpu_sec=0, wall_sec=0, memory_kb=0, updated_at=?
                WHERE job_id=? AND status<>'reported'
                """,
                [updated_at, int(job_id)],
            )
            self._conn.commit()

    def case_execution_row(self, case_id: int) -> dict[str, object] | None:
        return self._fetch_one(
            """
            SELECT
                c.id,c.job_id,c.task_id,c.test_name,c.testcase_hash,c.input_path,c.answer_path,c.status AS case_status,
                j.run_id,j.work_root,j.mode,j.source_name,j.source_path,j.status AS job_status,
                j.source_hash,j.compile_hash,j.run_hash,j.compare_hash,
                j.compile_config_json,j.run_config_json,j.compare_config_json,j.compile_success
            FROM judgehost_domjudge_cases c
            JOIN judgehost_domjudge_jobs j ON j.job_id=c.job_id
            WHERE c.id=?
            """,
            [int(case_id)],
        )

    def case_output_for_task(self, task_id: str, test_name: str) -> dict[str, object] | None:
        return self._fetch_one(
            """
            SELECT c.id, c.output_run_rel, j.work_root
            FROM judgehost_domjudge_cases c
            JOIN judgehost_domjudge_jobs j ON j.job_id = c.job_id
            WHERE c.task_id=? AND c.test_name=?
            ORDER BY c.id DESC
            LIMIT 1
            """,
            [task_id, test_name],
        )

    def report_case_result(
        self,
        case_id: int,
        *,
        lease_owner: str,
        runresult: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        output_run_rel: str,
        output_error_rel: str,
        output_system_rel: str,
        output_diff_rel: str,
        metadata_rel: str,
        compare_metadata_rel: str,
        team_message_rel: str,
        score_text: str,
        updated_at: str,
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='reported', lease_owner=?, runresult=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?,
                    output_run_rel=?, output_error_rel=?, output_system_rel=?, output_diff_rel=?, metadata_rel=?, compare_metadata_rel=?, team_message_rel=?, score_text=?, updated_at=?
                WHERE id=? AND status='leased'
                """,
                [
                    lease_owner,
                    runresult,
                    runtime_sec,
                    cpu_sec,
                    wall_sec,
                    memory_kb,
                    output_run_rel,
                    output_error_rel,
                    output_system_rel,
                    output_diff_rel,
                    metadata_rel,
                    compare_metadata_rel,
                    team_message_rel,
                    score_text,
                    updated_at,
                    int(case_id),
                ],
            )
            self._conn.commit()
        return int(cursor.rowcount or 0) > 0

    def case_debug_context(self, case_id: int) -> dict[str, object] | None:
        return self._fetch_one(
            """
            SELECT c.job_id,c.debug_text AS case_debug_text,j.debug_text AS job_debug_text
            FROM judgehost_domjudge_cases c
            JOIN judgehost_domjudge_jobs j ON j.job_id=c.job_id
            WHERE c.id=?
            """,
            [int(case_id)],
        )

    def job_debug_context(self, job_id: int) -> dict[str, object] | None:
        return self._fetch_one(
            "SELECT job_id,debug_text FROM judgehost_domjudge_jobs WHERE job_id=?",
            [int(job_id)],
        )

    def register_host_requeue(self, hostname: str, *, now_text: str, remap_seed: int) -> list[dict[str, object]]:
        with self._lock:
            unfinished: list[dict[str, object]] = []
            affected = self._conn.execute(
                """
                SELECT job_id,submit_id
                FROM judgehost_domjudge_jobs
                WHERE lease_owner=? AND status IN ('leased','queued')
                ORDER BY job_id ASC
                """,
                [hostname],
            ).fetchall()
            remap_step = 0
            for row in affected:
                remap_step += 1
                new_submit_id = str(remap_seed + remap_step)
                job_id = int(row["job_id"])
                self._conn.execute(
                    "UPDATE judgehost_domjudge_jobs SET submit_id=? WHERE job_id=?",
                    [new_submit_id, job_id],
                )
                unfinished.append({"jobid": job_id, "submitid": new_submit_id})
            self._conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=NULL, status='queued', updated_at=?
                WHERE lease_owner=? AND status IN ('leased','queued')
                """,
                [now_text, hostname],
            )
            self._conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='pending', lease_owner=NULL, updated_at=?
                WHERE lease_owner=? AND status='leased'
                """,
                [now_text, hostname],
            )
            self._conn.commit()
        return unfinished

    def create_job_with_cases(
        self,
        *,
        task_id: str,
        run_id: str,
        submit_id: str,
        contest_id: str,
        mode: str,
        source_name: str,
        source_path: str,
        work_root: str,
        compile_hash: str,
        run_hash: str,
        compare_hash: str,
        source_hash: str,
        compile_config_json: str,
        run_config_json: str,
        compare_config_json: str,
        expected_behavior: str,
        verification_source: str,
        force_recompile: int,
        lease_owner: str,
        status: str,
        created_at: str,
        case_rows: list[dict[str, object]],
    ) -> int:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO judgehost_domjudge_jobs(
                    task_id,run_id,submit_id,contest_id,mode,source_name,source_path,work_root,
                    compile_hash,run_hash,compare_hash,source_hash,compile_config_json,run_config_json,compare_config_json,
                    expected_behavior,verification_source,force_recompile,lease_owner,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    task_id,
                    run_id,
                    submit_id,
                    contest_id,
                    mode,
                    source_name,
                    source_path,
                    work_root,
                    compile_hash,
                    run_hash,
                    compare_hash,
                    source_hash,
                    compile_config_json,
                    run_config_json,
                    compare_config_json,
                    expected_behavior,
                    verification_source,
                    int(force_recompile),
                    lease_owner,
                    status,
                    created_at,
                    created_at,
                ],
            )
            row = self._conn.execute(
                "SELECT job_id FROM judgehost_domjudge_jobs WHERE task_id=?",
                [task_id],
            ).fetchone()
            if row is None:
                raise RuntimeError("failed to allocate DOMjudge compatibility job")
            job_id = int(row["job_id"])
            self._conn.execute(
                "UPDATE judgehost_domjudge_jobs SET submit_id=? WHERE job_id=?",
                [str(job_id), job_id],
            )
            for case_row in case_rows:
                self._conn.execute(
                    """
                    INSERT INTO judgehost_domjudge_cases(
                        job_id,task_id,run_id,test_name,ordinal,testcase_id,testcase_hash,testcase_input_hash,testcase_answer_hash,input_path,answer_path,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        job_id,
                        task_id,
                        run_id,
                        case_row["test_name"],
                        int(case_row["ordinal"]),
                        case_row["testcase_id"],
                        case_row["testcase_hash"],
                        case_row["testcase_input_hash"],
                        case_row["testcase_answer_hash"],
                        case_row["input_path"],
                        case_row["answer_path"],
                        case_row["status"],
                        created_at,
                        created_at,
                    ],
                )
            self._conn.commit()
        return job_id

    def release_prepared_job_for_queue(self, job_id: int, *, lease_owner: str, now_text: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=NULL, status='queued', updated_at=?
                WHERE job_id=? AND lease_owner=? AND status IN ('leased','queued')
                """,
                [now_text, int(job_id), lease_owner],
            )
            self._conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='pending', lease_owner=NULL, updated_at=?
                WHERE job_id=? AND status='leased' AND lease_owner=?
                """,
                [now_text, int(job_id), lease_owner],
            )
            self._conn.commit()

    def apply_cached_case_results(
        self,
        *,
        cached_rows: list[dict[str, object]],
        lease_owner: str,
        now_text: str,
    ) -> None:
        with self._lock:
            for cached in cached_rows:
                self._conn.execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='reported', lease_owner=?, runresult=?, runtime_sec=?, cpu_sec=?, wall_sec=?, memory_kb=?,
                        output_run_rel=?, output_error_rel=?, output_system_rel=?, output_diff_rel=?, metadata_rel=?, compare_metadata_rel=?, team_message_rel=?, score_text=?, updated_at=?
                    WHERE id=? AND status='pending'
                    """,
                    [
                        lease_owner,
                        cached["runresult"],
                        cached["runtime_sec"],
                        cached["cpu_sec"],
                        cached["wall_sec"],
                        cached["memory_kb"],
                        cached["output_run_rel"],
                        cached["output_error_rel"],
                        cached["output_system_rel"],
                        cached["output_diff_rel"],
                        cached["metadata_rel"],
                        cached["compare_metadata_rel"],
                        cached["team_message_rel"],
                        cached["score_text"],
                        now_text,
                        int(cached["case_id"]),
                    ],
                )
            self._conn.commit()

    def lease_cases(self, job_id: int, *, hostname: str, limit: int, now_text: str) -> list[JudgehostCaseRow]:
        cap = max(1, min(256, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM judgehost_domjudge_cases
                WHERE job_id=? AND status='pending'
                ORDER BY ordinal ASC, id ASC
                LIMIT ?
                """,
                [int(job_id), cap],
            ).fetchall()
            if not rows:
                return []
            self._conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=?, status='leased', updated_at=?
                WHERE job_id=?
                """,
                [hostname, now_text, int(job_id)],
            )
            leased: list[JudgehostCaseRow] = []
            for row in rows:
                case_id = int(row["id"])
                updated = self._conn.execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='leased', lease_owner=?, updated_at=?
                    WHERE id=? AND status='pending'
                    """,
                    [hostname, now_text, case_id],
                )
                if int(updated.rowcount or 0) <= 0:
                    continue
                leased.append({**dict(row), "status": "leased", "lease_owner": hostname, "updated_at": now_text})
            self._conn.commit()
        return leased

    def append_debug_text(
        self,
        *,
        case_id: int | None,
        job_id: int | None,
        debug_text: str,
        now_text: str,
    ) -> None:
        with self._lock:
            if case_id is not None:
                current_row = self._conn.execute(
                    "SELECT debug_text FROM judgehost_domjudge_cases WHERE id=?",
                    [int(case_id)],
                ).fetchone()
                current_text = "" if current_row is None else str(current_row["debug_text"] or "")
                merged_text = debug_text if not current_text else f"{current_text}\n{debug_text}"
                if len(merged_text) > 4000:
                    merged_text = merged_text[-4000:]
                self._conn.execute(
                    "UPDATE judgehost_domjudge_cases SET debug_text=?, updated_at=? WHERE id=?",
                    [merged_text, now_text, int(case_id)],
                )
            if job_id is not None:
                current_row = self._conn.execute(
                    "SELECT debug_text FROM judgehost_domjudge_jobs WHERE job_id=?",
                    [int(job_id)],
                ).fetchone()
                current_text = "" if current_row is None else str(current_row["debug_text"] or "")
                merged_text = debug_text if not current_text else f"{current_text}\n{debug_text}"
                if len(merged_text) > 4000:
                    merged_text = merged_text[-4000:]
                self._conn.execute(
                    "UPDATE judgehost_domjudge_jobs SET debug_text=?, updated_at=? WHERE job_id=?",
                    [merged_text, now_text, int(job_id)],
                )
            self._conn.commit()

    def case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        safe_run_ids = [run_id for run_id in run_ids if run_id]
        if not safe_run_ids:
            return {}
        placeholders = ",".join(("?" for _ in safe_run_ids))
        rows = self._fetch_all(
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
            [*safe_run_ids],
        )
        progress: dict[str, dict[str, int]] = {}
        for row in rows:
            run_id = str(row["run_id"])
            total = max(0, int(row["total_cases"] or 0))
            reported = max(0, int(row["reported_cases"] or 0))
            leased = max(0, int(row["leased_cases"] or 0))
            if total > 0 and reported > total:
                reported = total
            progress[run_id] = {"total": total, "reported": reported, "leased": leased}
        return progress

    def case_cells_for_runs(self, run_ids: list[str]) -> list[dict[str, object]]:
        safe_run_ids = [run_id for run_id in run_ids if run_id]
        if not safe_run_ids:
            return []
        placeholders = ",".join(("?" for _ in safe_run_ids))
        return self._fetch_all(
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
            [*safe_run_ids],
        )

    def aggregate_case_counts(self, run_ids: list[str]) -> dict[str, int]:
        safe_run_ids = [run_id for run_id in run_ids if run_id]
        if not safe_run_ids:
            return {"total": 0, "reported": 0}
        placeholders = ",".join(("?" for _ in safe_run_ids))
        rows = self._fetch_all(
            f"""
            SELECT COUNT(c.id) AS total_cases,
                   SUM(CASE WHEN c.status='reported' THEN 1 ELSE 0 END) AS reported_cases
            FROM judgehost_domjudge_jobs j
            JOIN judgehost_domjudge_cases c ON c.job_id=j.job_id
            WHERE j.run_id IN ({placeholders})
            """,
            [*safe_run_ids],
        )
        if not rows:
            return {"total": 0, "reported": 0}
        row = rows[0]
        total = max(0, int(row["total_cases"] or 0))
        reported = max(0, int(row["reported_cases"] or 0))
        if total > 0 and reported > total:
            reported = total
        return {"total": total, "reported": reported}

    def cancel_jobs_for_runs(self, run_ids: list[str], *, final_status: str, now_text: str) -> int:
        safe_run_ids = [run_id for run_id in run_ids if run_id]
        if not safe_run_ids:
            return 0
        placeholders = ",".join(("?" for _ in safe_run_ids))
        with self._lock:
            job_rows = self._conn.execute(
                f"SELECT job_id FROM judgehost_domjudge_jobs WHERE run_id IN ({placeholders}) AND status IN ('queued','leased')",
                [*safe_run_ids],
            ).fetchall()
            job_ids = [int(row["job_id"]) for row in job_rows if row is not None and row["job_id"] is not None]
            if not job_ids:
                return 0
            id_placeholders = ",".join(("?" for _ in job_ids))
            self._conn.execute(
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
                WHERE job_id IN ({id_placeholders}) AND status IN ('pending','leased')
                """,
                [now_text, *job_ids],
            )
            self._conn.execute(
                f"""
                UPDATE judgehost_domjudge_jobs
                SET status=?,
                    lease_owner=NULL,
                    completed_at=COALESCE(completed_at, ?),
                    updated_at=?
                WHERE job_id IN ({id_placeholders}) AND status IN ('queued','leased')
                """,
                [final_status, now_text, now_text, *job_ids],
            )
            self._conn.commit()
        return len(job_ids)

    def release_host_leases(self, hostname: str, *, now_text: str) -> tuple[int, int]:
        with self._lock:
            job_upd = self._conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET lease_owner=NULL, status='queued', updated_at=?
                WHERE lease_owner=? AND status IN ('leased','queued')
                """,
                [now_text, hostname],
            )
            case_upd = self._conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='pending', lease_owner=NULL, updated_at=?
                WHERE lease_owner=? AND status='leased'
                """,
                [now_text, hostname],
            )
            self._conn.commit()
        return (int(job_upd.rowcount or 0), int(case_upd.rowcount or 0))

    def forget_runs(self, run_ids: list[str]) -> int:
        safe_run_ids = [run_id for run_id in run_ids if run_id]
        if not safe_run_ids:
            return 0
        placeholders = ",".join(("?" for _ in safe_run_ids))
        with self._lock:
            self._conn.execute(
                f"DELETE FROM judgehost_domjudge_cases WHERE run_id IN ({placeholders})",
                [*safe_run_ids],
            )
            cur = self._conn.execute(
                f"DELETE FROM judgehost_domjudge_jobs WHERE run_id IN ({placeholders})",
                [*safe_run_ids],
            )
            self._conn.commit()
        return int(cur.rowcount or 0)

    def cancel_all_inflight(self, *, now_text: str) -> int:
        with self._lock:
            self._conn.execute(
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
            case_upd = self._conn.execute(
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
            self._conn.commit()
        return int(case_upd.rowcount or 0)

    def _fetch_one(self, sql: str, params: list[object]) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return dict(row)

    def _fetch_all(self, sql: str, params: list[object]) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
