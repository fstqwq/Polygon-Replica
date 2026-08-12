from __future__ import annotations

import json
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.db import DB, now_iso
from app.config import build_config_values
from app.service.judgehost.task_registry import JudgehostTaskRegistry
from app.service.platform.fs.layout import FsManager
from app.service.platform.maintenance import (
    ArtifactCleanupService,
    MaintenanceStart,
    MaintenanceCoordinator,
    validate_storage_layout,
)
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.platform.source_backup import SourceBackupService
from app.service.repository.workspace import WorkspaceService
from app.service.verification.execution_result import (
    execution_result_json,
    normalize_execution_result,
)
from app.service.verification.task_store import VerificationTaskStore
from app.setting import Settings
from tests.isolated_db_helpers import (
    isolated_db_connection,
    isolated_db_execute,
    isolated_db_fetch_all,
    isolated_db_fetch_one,
    isolated_db_write_transaction,
)


class _WorkerQueueStub:
    def __init__(self) -> None:
        self.queued = 0
        self.running = 0
        self.reset_count = 0

    def active_counts(self) -> dict[str, int]:
        return {"queued": self.queued, "running": self.running}

    def reset_runtime_history(self) -> None:
        self.reset_count += 1


class _JudgehostStub:
    def __init__(self) -> None:
        self.queued = 0
        self.leased = 0
        self.reporting = 0
        self.callbacks = 0
        self.reset_count = 0

    def busy_counts(self) -> dict[str, int]:
        return {
            "queued": self.queued,
            "leased": self.leased,
            "reporting": self.reporting,
            "callbacks": self.callbacks,
        }

    def reset_runtime_state(self) -> None:
        self.reset_count += 1


class TestArtifactCleanup(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="artifact-cleanup-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(
            db_path=self.root / "metadata.db",
            bare_root=self.root / "git",
            workspace_root=self.root / "workspaces",
            artifacts_root=self.root / "artifacts",
            cache_root=self.root / "cache",
            contest_source_root=self.root / "contest-sources",
            backup_root=self.root / "backups",
        )
        self.config_values = build_config_values()
        self.db = DB(self.settings.db_path, config_values=self.config_values)
        self.db.init()
        self.verification_task_store = VerificationTaskStore(self.db)
        self.workspace_service = WorkspaceService(
            self.db, self.settings,
            verification_task_store=self.verification_task_store, config_values=self.config_values,
        )
        self.workspace_service.ensure_problem("admin/sample")
        self.workspace_service.ensure_user("admin")
        workspace = self.workspace_service.ensure_workspace(
            "admin/sample",
            "admin",
            refresh_status=False,
        )
        context = self.workspace_service.workspace_context(
            "admin/sample",
            "admin",
            include_recent=False,
        )
        self.problem_id = int(context["problem"]["id"])
        self.workspace_id = int(context["workspace"]["id"])
        self.actor_user_id = int(context["user"]["id"])
        self.workspace = workspace
        self.workspace_service.grant_repo_access(
            "admin/sample",
            "admin",
            "owner",
        )
        self.fs_manager = FsManager(
            self.settings.cache_root,
            self.settings.artifacts_root,
        )
        self.runtime_blob_store = RuntimeBlobStore(self.fs_manager.runtime_root)
        self.runtime_cache_index = RuntimeCacheIndex(self.runtime_blob_store)
        self.worker_queue = _WorkerQueueStub()
        self.judgehost = _JudgehostStub()
        self.process_reset_count = 0
        self.cleanup = ArtifactCleanupService(
            self.db,
            self.settings,
            self.runtime_cache_index,
            self.runtime_blob_store,
            self.worker_queue,
            self.judgehost,
            self.verification_task_store,
            self._reset_process_tracking,
        )
        self.source_backup = SourceBackupService(self.db, self.settings)

    def _reset_process_tracking(self) -> None:
        self.process_reset_count += 1

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        isolated_db_execute(self.db, sql, params)

    def _count(self, table: str) -> int:
        row = isolated_db_fetch_one(
            self.db,
            f"SELECT COUNT(*) AS count FROM {table}",
        )
        assert row is not None
        return int(row["count"])

    def _run_source_backup(self, operation_id: str) -> dict[str, object]:
        started_at = now_iso()
        start_audit_id = self.source_backup.write_start_audit(
            actor_user_id=self.actor_user_id,
            operation_id=operation_id,
            started_at=started_at,
            roots=self.source_backup.configured_roots(),
        )
        return self.source_backup.run(
            actor_user_id=self.actor_user_id,
            operation_id=operation_id,
            start_audit_id=start_audit_id,
            started_at=started_at,
            set_stage=lambda _stage: None,
        )

    def _seed_generated_data(self) -> dict[str, bytes]:
        now = now_iso()
        self._execute(
            """
            INSERT INTO auth_sessions(
                id,user_id,token_hash,created_at,expires_at
            ) VALUES('auth-cleanup',?,'auth-token-cleanup',?,?)
            """,
            (self.actor_user_id, now, "2999-01-01T00:00:00+00:00"),
        )
        self._execute(
            """
            INSERT INTO verifications(
                id,problem_id,workspace_id,signature,source_commit,kind,status,created_at
            ) VALUES('ver-c1ea4',?,?,?,?,?,?,?)
            """,
            (
                self.problem_id,
                self.workspace_id,
                "commit",
                "commit",
                "all",
                "ok",
                now,
            ),
        )
        self._execute(
            "INSERT INTO verification_selected_tests VALUES('ver-c1ea4',0,'001.in')"
        )
        self._execute(
            "INSERT INTO verification_source_paths VALUES('ver-c1ea4',0,'solutions/ac.cpp')"
        )
        self._execute(
            """
            INSERT INTO verification_sanity_checks(
                verification_id,ordinal,check_name,status,checked_count
            ) VALUES('ver-c1ea4',0,'sample','passed',1)
            """
        )
        self._execute(
            """
            INSERT INTO verification_sanity_check_messages(
                verification_id,check_name,ordinal,severity,test_name,message
            ) VALUES('ver-c1ea4','sample',0,'info','001.in','ok')
            """
        )
        self._execute(
            """
            INSERT INTO verification_tests_meta(
                verification_id,ordinal,test_name
            ) VALUES('ver-c1ea4',0,'001.in')
            """
        )
        self._execute(
            """
            INSERT INTO verification_tasks(
                id,verification_id,task_kind,source_path,program_id,test_name,
                expected_behavior,final_status,result_json,created_at
            ) VALUES('task-cleanup','ver-c1ea4','run','solutions/ac.cpp',
                     'solution-0','001.in','accepted','ok',?,?)
            """,
            (
                execution_result_json(normalize_execution_result(verdict="AC")),
                now,
            ),
        )
        self._execute(
            """
            INSERT INTO verification_task_diagnostics(task_id,snapshot_json,updated_at)
            VALUES('task-cleanup','{"items":[]}',?)
            """,
            (now,),
        )
        self._execute(
            """
            INSERT INTO verification_artifact_refs(
                verification_id,test_name,input_ref,answer_ref,updated_at
            ) VALUES('ver-c1ea4','001.in','blob://input','blob://answer',?)
            """,
            (now,),
        )
        self._execute(
            """
            INSERT INTO previews(
                id,problem_id,workspace_id,verification_id,status,created_at
            ) VALUES('preview-cleanup',?,?, 'ver-c1ea4','ok',?)
            """,
            (self.problem_id, self.workspace_id, now),
        )
        self._execute(
            """
            INSERT INTO problem_package_materializations(
                id,problem_id,source_commit,revision_number,source_digest,
                archive_rel_path,archive_sha256,archive_size_bytes,verification_id,
                status,created_at,checked_at,unavailable_reason
            ) VALUES('pm-cleanup',? ,?,1,?,'materializations/native.zip',?,10,
                     'ver-c1ea4','available',?,?,'')
            """,
            (self.problem_id, "c" * 40, "d" * 64, "e" * 64, now, now),
        )
        self._execute(
            """
            INSERT INTO problem_package_builds(
                id,problem_id,source_commit,verification_id,phase,status,
                materialization_id,error,created_at,started_at,finished_at
            ) VALUES('pb-cleanup',?,?,?,'complete','succeeded','pm-cleanup','',?,?,?)
            """,
            (self.problem_id, "c" * 40, "ver-c1ea4", now, now, now),
        )
        self._execute(
            """
            INSERT INTO exports(
                id,problem_id,materialization_id,export_type,
                filename,archive_rel_path,sha256,size_bytes,source_commit,created_at
            ) VALUES('export-cleanup',?,'pm-cleanup','native','package.zip',
                     'materializations/native.zip',?,10,?,?)
            """,
            (self.problem_id, "e" * 64, "c" * 40, now),
        )
        self._execute(
            """
            INSERT INTO export_jobs(
                id,problem_id,actor_user_id,export_type,source_commit,status,
                materialization_id,export_id,error,created_at,started_at,finished_at
            ) VALUES('export-job-cleanup',?,?,'native',?,'succeeded',
                     'pm-cleanup','export-cleanup','',?,?,?)
            """,
            (
                self.problem_id,
                self.actor_user_id,
                "c" * 40,
                now,
                now,
                now,
            ),
        )
        cursor_id = isolated_db_write_transaction(
            self.db,
            lambda connection: int(
                connection.execute(
                    """
                    INSERT INTO contests(slug,title,owner_user_id,created_at)
                    VALUES('cleanup-contest','Cleanup Contest',?,?)
                    """,
                    (self.actor_user_id, now),
                ).lastrowid
            )
        )
        self.contest_id = int(cursor_id)
        self._execute(
            """
            INSERT INTO contest_members(contest_id,user_id,role,created_at)
            VALUES(?,?,'owner',?)
            """,
            (self.contest_id, self.actor_user_id, now),
        )
        self._execute(
            """
            INSERT INTO contest_problems(
                contest_id,position,label,problem_id,statement_folder,added_by_user_id,created_at
            ) VALUES(?,1,'A',?,'a',?,?)
            """,
            (
                self.contest_id,
                self.problem_id,
                self.actor_user_id,
                now,
            ),
        )
        contest_problem = isolated_db_fetch_one(
            self.db,
            "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
            (self.contest_id, self.problem_id),
        )
        self.assertIsNotNone(contest_problem)
        self._execute(
            """
            INSERT INTO contest_attachments(
                contest_id,key,rel_path,created_at,created_by_user_id
            ) VALUES(?,'statement','statement.pdf',?,?)
            """,
            (self.contest_id, now, self.actor_user_id),
        )
        self._execute(
            """
            INSERT INTO contest_jobs(
                id,contest_id,actor_user_id,job_type,status,source_generation,created_at,finished_at
            ) VALUES('contest-job-cleanup',? ,?,'build','ok',1,?,?)
            """,
            (self.contest_id, self.actor_user_id, now, now),
        )
        self._execute(
            """
            INSERT INTO contest_build_items(
                job_id,contest_problem_id,position,label,problem_id,statement_folder,
                source_commit,revision_number,materialization_id,archive_sha256
            ) VALUES('contest-job-cleanup',?,1,'A',?,'a',?,1,'pm-cleanup',?)
            """,
            (int(contest_problem["id"]), self.problem_id, "c" * 40, "e" * 64),
        )
        self._execute(
            """
            INSERT INTO contest_artifacts(
                id,contest_id,job_id,artifact_type,filename,created_at
            ) VALUES('contest-artifact-cleanup',?,'contest-job-cleanup',
                     'package','contest.zip',?)
            """,
            (self.contest_id, now),
        )
        self._execute(
            """
            INSERT INTO system_config(
                key,value_json,updated_at,updated_by_user_id
            ) VALUES('cleanup-test-setting','true',?,?)
            """,
            (now, self.actor_user_id),
        )
        self._execute(
            """
            INSERT INTO smtp_config(
                id,host,port,username,password_ciphertext,
                updated_at,updated_by_user_id
            ) VALUES(1,'smtp.example.test',2525,'mailer','ciphertext',?,?)
            """,
            (now, self.actor_user_id),
        )
        for index in range(600):
            self._execute(
                """
                INSERT INTO audit_log(
                    actor_user_id,problem_id,action,details_json,created_at
                ) VALUES(?,?,?, ?,?)
                """,
                (
                    self.actor_user_id,
                    self.problem_id,
                    f"old.audit.{index}",
                    json.dumps({"payload": "x" * 4096}),
                    now,
                ),
            )

        durable_files = {
            "git": b"git durable",
            "workspace": b"workspace durable",
            "contest": b"contest durable",
            "backup": b"backup durable",
        }
        durable_paths = {
            "git": self.settings.bare_root / "durable-marker",
            "workspace": self.workspace / "durable-marker",
            "contest": self.settings.contest_source_root / "cleanup-contest" / "statement.pdf",
            "backup": self.settings.backup_root / "contest-backup.tar.gz",
        }
        for label, path in durable_paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(durable_files[label])
        self.durable_paths = durable_paths
        artifact_file = self.settings.artifacts_root / "generated" / "artifact.bin"
        cache_file = self.settings.cache_root / "generated" / "cache.bin"
        artifact_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        artifact_file.write_bytes(b"artifact" * 1024)
        cache_file.write_bytes(b"cache" * 1024)
        return durable_files

    def test_usage_snapshot_matches_cleanup_scope(self) -> None:
        self._seed_generated_data()

        usage = self.cleanup.usage_snapshot()

        self.assertEqual(usage["artifacts_bytes"], len(b"artifact" * 1024))
        self.assertEqual(usage["artifacts_files"], 1)
        self.assertEqual(usage["cache_bytes"], len(b"cache" * 1024))
        self.assertEqual(usage["cache_files"], 1)
        self.assertEqual(
            usage["total_bytes"],
            len(b"artifact" * 1024) + len(b"cache" * 1024),
        )
        self.assertEqual(usage["total_files"], 2)
        expected_table_rows = {
            table: self._count(table)
            for table in usage["table_rows"]
        }
        self.assertEqual(usage["table_rows"], expected_table_rows)
        self.assertEqual(usage["artifact_rows"], sum(expected_table_rows.values()))
        self.assertEqual(usage["audit_rows"], self._count("audit_log"))
        self.assertEqual(
            usage["removable_rows"],
            usage["artifact_rows"] + usage["audit_rows"],
        )

    def test_cleanup_deletes_derived_epoch_and_preserves_durable_data(self) -> None:
        durable_files = self._seed_generated_data()
        with isolated_db_connection(self.db) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        database_size_before = self.settings.db_path.stat().st_size
        coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            self.source_backup,
        )

        started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)
        self.assertTrue(started.accepted, started.reason)
        self.assertFalse(coordinator.allow_new_work())
        worker = coordinator._worker
        self.assertIsNotNone(worker)
        assert worker is not None
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertTrue(coordinator.allow_new_work())

        for table in (
            "previews",
            "export_jobs",
            "exports",
            "problem_package_builds",
            "problem_package_materializations",
            "contest_build_items",
            "contest_artifacts",
            "contest_jobs",
            "verification_artifact_refs",
            "verification_selected_tests",
            "verification_source_paths",
            "verification_sanity_check_messages",
            "verification_sanity_checks",
            "verification_tests_meta",
            "verification_task_diagnostics",
            "verification_tasks",
            "verifications",
        ):
            self.assertEqual(self._count(table), 0, table)
        self.assertEqual(self._count("users"), 1)
        self.assertEqual(self._count("auth_sessions"), 1)
        self.assertEqual(self._count("problems"), 1)
        self.assertEqual(self._count("repo_acl"), 1)
        self.assertEqual(self._count("workspaces"), 1)
        self.assertEqual(self._count("contests"), 1)
        self.assertEqual(self._count("contest_members"), 1)
        self.assertEqual(self._count("contest_problems"), 1)
        self.assertEqual(self._count("contest_attachments"), 1)
        system_config = isolated_db_fetch_one(
            self.db,
            "SELECT value_json FROM system_config WHERE key='cleanup-test-setting'"
        )
        self.assertIsNotNone(system_config)
        self.assertEqual(str(system_config["value_json"]), "true")
        smtp_config = isolated_db_fetch_one(
            self.db,
            "SELECT host,port,password_ciphertext FROM smtp_config WHERE id=1"
        )
        self.assertIsNotNone(smtp_config)
        self.assertEqual(str(smtp_config["host"]), "smtp.example.test")
        self.assertEqual(int(smtp_config["port"]), 2525)
        self.assertEqual(str(smtp_config["password_ciphertext"]), "ciphertext")
        audit_actions = {
            str(row["action"])
            for row in isolated_db_fetch_all(self.db, "SELECT action FROM audit_log")
        }
        self.assertEqual(
            audit_actions,
            {"artifact_cleanup.start", "artifact_cleanup.succeeded"},
        )
        self.assertEqual(
            [path for path in self.settings.artifacts_root.rglob("*") if path.is_file()],
            [],
        )
        self.assertEqual(
            [path for path in self.settings.cache_root.rglob("*") if path.is_file()],
            [],
        )
        for label, path in self.durable_paths.items():
            self.assertEqual(path.read_bytes(), durable_files[label])
        self.assertEqual(self.worker_queue.reset_count, 1)
        self.assertEqual(self.judgehost.reset_count, 1)
        self.assertEqual(self.process_reset_count, 1)
        self.assertLess(self.settings.db_path.stat().st_size, database_size_before)

    def test_database_cleanup_replaces_tables_without_row_deletes(self) -> None:
        self._seed_generated_data()
        started_at = now_iso()
        start_audit_id = self.cleanup.write_start_audit(
            actor_user_id=self.actor_user_id,
            operation_id="cleanup-schema-reset",
            started_at=started_at,
            roots=self.cleanup.configured_roots(),
        )
        traced_sql: list[str] = []

        def install_trace(connection) -> None:
            connection.set_trace_callback(traced_sql.append)

        with patch.object(
            self.db,
            "_install_sql_trace",
            side_effect=install_trace,
        ):
            counts = self.cleanup._delete_metadata(
                start_audit_id=start_audit_id
            )

        statements = [" ".join(sql.upper().split()) for sql in traced_sql]
        self.assertIn("PRAGMA FOREIGN_KEYS=OFF", statements)
        self.assertIn("DROP TABLE VERIFICATION_TASKS", statements)
        self.assertNotIn("DELETE FROM VERIFICATION_TASKS", statements)
        self.assertFalse(
            any(
                statement.startswith("UPDATE VERIFICATION_TASKS")
                for statement in statements
            )
        )
        self.assertEqual(counts["verification_tasks"], 1)
        self.assertEqual(self._count("verification_tasks"), 0)

        with isolated_db_connection(self.db) as connection:
            self.assertEqual(
                int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                1,
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT rowid
                FROM verification_tasks
                WHERE predecessor_task_id=?
                """,
                ("task-id",),
            ).fetchall()
        self.assertEqual(len(plan), 1)
        self.assertIn("SEARCH", str(plan[0][3]))
        self.assertIn("idx_verification_tasks_predecessor", str(plan[0][3]))

    def test_schema_reset_failure_rolls_back_dropped_tables(self) -> None:
        self._seed_generated_data()
        start_audit_id = self.cleanup.write_start_audit(
            actor_user_id=self.actor_user_id,
            operation_id="cleanup-schema-reset-failure",
            started_at=now_iso(),
            roots=self.cleanup.configured_roots(),
        )

        with patch(
            "app.service.platform.maintenance.current_schema_statements_for_tables",
            return_value=("CREATE TABLE invalid schema",),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "syntax"):
                self.cleanup._delete_metadata(start_audit_id=start_audit_id)

        self.assertEqual(self._count("verification_tasks"), 1)
        self.assertEqual(self._count("verifications"), 1)
        self.assertEqual(self._count("exports"), 1)
        with isolated_db_connection(self.db) as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_busy_check_reopens_admission_without_writing_start_audit(self) -> None:
        coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            self.source_backup,
        )
        self.worker_queue.queued = 1
        self.judgehost.reporting = 2
        self.judgehost.callbacks = 1
        self.assertTrue(coordinator.enter_request())

        started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)

        self.assertFalse(started.accepted)
        self.assertEqual(started.reason, "busy")
        self.assertEqual(started.busy["worker_queued"], 1)
        self.assertEqual(started.busy["judgehost_reporting"], 2)
        self.assertEqual(started.busy["judgehost_callbacks"], 1)
        self.assertEqual(started.busy["inflight_requests"], 1)
        self.assertTrue(coordinator.allow_new_work())
        self.assertEqual(
            self._count("audit_log"),
            0,
        )
        coordinator.leave_request()

    def test_busy_count_failure_reopens_admission_without_start_audit(self) -> None:
        coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            self.source_backup,
        )

        with patch.object(
            self.worker_queue,
            "active_counts",
            side_effect=RuntimeError("forced admission count failure"),
        ):
            started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)

        self.assertFalse(started.accepted)
        self.assertIn("admission_failed", started.reason)
        self.assertTrue(coordinator.allow_new_work())
        self.assertEqual(self._count("audit_log"), 0)

    def test_start_audit_failure_reopens_admission_without_starting_cleanup(self) -> None:
        coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            self.source_backup,
        )

        with patch.object(
            self.cleanup,
            "write_start_audit",
            side_effect=RuntimeError("forced start audit failure"),
        ):
            started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)

        self.assertFalse(started.accepted)
        self.assertIn("audit_failed", started.reason)
        self.assertTrue(coordinator.allow_new_work())
        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["stage"], "start_audit")
        self.assertIn("forced start audit failure", str(snapshot["error"]))
        self.assertEqual(self._count("audit_log"), 0)

    def test_start_audit_write_does_not_hold_admission_lock(self) -> None:
        coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            self.source_backup,
        )
        audit_started = threading.Event()
        release_audit = threading.Event()
        start_results: list[MaintenanceStart] = []
        original_write_start = self.cleanup.write_start_audit

        def blocked_start_audit(**kwargs) -> int:
            audit_started.set()
            release_audit.wait(timeout=2)
            return original_write_start(**kwargs)

        with (
            patch.object(
                self.cleanup,
                "write_start_audit",
                side_effect=blocked_start_audit,
            ),
            patch.object(
                self.cleanup,
                "run",
                return_value={"finished_at": now_iso(), "duration_ms": 1},
            ),
        ):
            starter = threading.Thread(
                target=lambda: start_results.append(
                    coordinator.start_cleanup(actor_user_id=self.actor_user_id)
                )
            )
            starter.start()
            request_finished = threading.Event()
            request_results: list[bool] = []

            def enter_request() -> None:
                request_results.append(coordinator.enter_request())
                request_finished.set()

            request_thread = threading.Thread(target=enter_request)
            try:
                self.assertTrue(audit_started.wait(timeout=1))
                request_thread.start()
                self.assertTrue(request_finished.wait(timeout=0.5))
                self.assertEqual(request_results, [False])
            finally:
                release_audit.set()
                starter.join(timeout=2)
                if request_thread.ident is not None:
                    request_thread.join(timeout=2)

            self.assertFalse(starter.is_alive())
            if request_thread.ident is not None:
                self.assertFalse(request_thread.is_alive())
            self.assertEqual(len(start_results), 1)
            self.assertTrue(start_results[0].accepted)
            worker = coordinator._worker
            self.assertIsNotNone(worker)
            assert worker is not None
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

        self.assertTrue(coordinator.allow_new_work())

    def test_storage_layout_rejects_durable_root_inside_cleanup_root(self) -> None:
        invalid = replace(
            self.settings,
            backup_root=self.settings.cache_root / "backups",
        )

        with self.assertRaisesRegex(RuntimeError, "roots overlap"):
            validate_storage_layout(invalid)

    def test_cleanup_preflight_rejects_nested_symbolic_links(self) -> None:
        outside = self.root / "outside-cache"
        outside.mkdir()
        self.settings.cache_root.mkdir(parents=True, exist_ok=True)
        (self.settings.cache_root / "outside-link").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            self.cleanup.preflight()

    def test_repeated_cleanup_start_allows_only_one_background_operation(self) -> None:
        coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            self.source_backup,
        )
        running = threading.Event()
        release = threading.Event()

        def blocking_run(**_kwargs) -> dict[str, object]:
            running.set()
            release.wait(timeout=5)
            return {"finished_at": now_iso(), "duration_ms": 1}

        with patch.object(self.cleanup, "run", side_effect=blocking_run):
            first = coordinator.start_cleanup(actor_user_id=self.actor_user_id)
            self.assertTrue(first.accepted)
            self.assertTrue(running.wait(timeout=2))
            second = coordinator.start_cleanup(actor_user_id=self.actor_user_id)
            self.assertFalse(second.accepted)
            self.assertEqual(second.reason, "already_running")
            release.set()
            worker = coordinator._worker
            self.assertIsNotNone(worker)
            assert worker is not None
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        self.assertEqual(coordinator.snapshot()["status"], "succeeded")
        self.assertTrue(coordinator.allow_new_work())
        self.assertEqual(
            self._count("audit_log"),
            1,
        )

    def test_filesystem_failure_keeps_database_deletion_and_writes_failed_audit(self) -> None:
        self._seed_generated_data()
        artifact_file = (
            self.settings.artifacts_root
            / "generated"
            / "artifact.bin"
        )
        cache_file = self.settings.cache_root / "generated" / "cache.bin"
        operation_id = "cleanup-filesystem-failure"
        started_at = now_iso()
        start_audit_id = self.cleanup.write_start_audit(
            actor_user_id=self.actor_user_id,
            operation_id=operation_id,
            started_at=started_at,
            roots=self.cleanup.preflight(),
        )

        def partially_clear(_root: Path) -> int:
            artifact_file.unlink()
            raise OSError("forced filesystem failure")

        with patch.object(self.cleanup, "_clear_root", side_effect=partially_clear):
            with self.assertRaisesRegex(OSError, "forced filesystem failure"):
                self.cleanup.run(
                    actor_user_id=self.actor_user_id,
                    operation_id=operation_id,
                    start_audit_id=start_audit_id,
                    started_at=started_at,
                    set_stage=lambda _stage: None,
                )

        self.assertEqual(self._count("verifications"), 0)
        self.assertEqual(self._count("exports"), 0)
        self.assertFalse(artifact_file.exists())
        self.assertTrue(cache_file.is_file())
        actions = [
            str(row["action"])
            for row in isolated_db_fetch_all(
                self.db,
                "SELECT action FROM audit_log ORDER BY id"
            )
        ]
        self.assertEqual(
            actions,
            ["artifact_cleanup.start", "artifact_cleanup.failed"],
        )

    def test_database_transaction_failure_preserves_rows_and_files(self) -> None:
        self._seed_generated_data()
        artifact_file = (
            self.settings.artifacts_root
            / "generated"
            / "artifact.bin"
        )
        operation_id = "cleanup-database-failure"
        started_at = now_iso()
        start_audit_id = self.cleanup.write_start_audit(
            actor_user_id=self.actor_user_id,
            operation_id=operation_id,
            started_at=started_at,
            roots=self.cleanup.configured_roots(),
        )

        with patch.object(
            self.cleanup,
            "_delete_metadata",
            side_effect=RuntimeError("forced database transaction failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced database"):
                self.cleanup.run(
                    actor_user_id=self.actor_user_id,
                    operation_id=operation_id,
                    start_audit_id=start_audit_id,
                    started_at=started_at,
                    set_stage=lambda _stage: None,
                )

        self.assertEqual(self._count("verifications"), 1)
        self.assertEqual(self._count("exports"), 1)
        self.assertTrue(artifact_file.is_file())
        terminal = isolated_db_fetch_one(
            self.db,
            "SELECT action,details_json FROM audit_log ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(terminal)
        self.assertEqual(str(terminal["action"]), "artifact_cleanup.failed")
        self.assertIn("forced database transaction failure", str(terminal["details_json"]))

    def test_vacuum_failure_is_recorded_after_files_and_rows_are_removed(self) -> None:
        self._seed_generated_data()
        operation_id = "cleanup-vacuum-failure"
        started_at = now_iso()
        start_audit_id = self.cleanup.write_start_audit(
            actor_user_id=self.actor_user_id,
            operation_id=operation_id,
            started_at=started_at,
            roots=self.cleanup.preflight(),
        )

        with patch.object(
            self.cleanup,
            "_vacuum",
            side_effect=RuntimeError("forced vacuum failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced vacuum failure"):
                self.cleanup.run(
                    actor_user_id=self.actor_user_id,
                    operation_id=operation_id,
                    start_audit_id=start_audit_id,
                    started_at=started_at,
                    set_stage=lambda _stage: None,
                )

        self.assertEqual(self._count("verifications"), 0)
        self.assertEqual(
            [path for path in self.settings.artifacts_root.rglob("*") if path.is_file()],
            [],
        )
        terminal = isolated_db_fetch_one(
            self.db,
            "SELECT action,details_json FROM audit_log ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(terminal)
        self.assertEqual(str(terminal["action"]), "artifact_cleanup.failed")
        self.assertIn("forced vacuum failure", str(terminal["details_json"]))

        restarted_coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            self.source_backup,
        )
        retried = restarted_coordinator.start_cleanup(
            actor_user_id=self.actor_user_id
        )
        self.assertTrue(retried.accepted, retried.reason)
        retry_worker = restarted_coordinator._worker
        self.assertIsNotNone(retry_worker)
        assert retry_worker is not None
        retry_worker.join(timeout=20)
        self.assertFalse(retry_worker.is_alive())
        self.assertEqual(restarted_coordinator.snapshot()["status"], "succeeded")
        actions = [
            str(row["action"])
            for row in isolated_db_fetch_all(
                self.db,
                "SELECT action FROM audit_log ORDER BY id"
            )
        ]
        self.assertEqual(
            actions,
            ["artifact_cleanup.start", "artifact_cleanup.succeeded"],
        )

    def test_terminal_audit_failure_is_never_silently_ignored(self) -> None:
        self._seed_generated_data()
        operation_id = "cleanup-terminal-audit-failure"
        started_at = now_iso()
        start_audit_id = self.cleanup.write_start_audit(
            actor_user_id=self.actor_user_id,
            operation_id=operation_id,
            started_at=started_at,
            roots=self.cleanup.preflight(),
        )

        with patch.object(
            self.cleanup,
            "_write_audit",
            side_effect=RuntimeError("forced terminal audit failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "terminal audit failed"):
                self.cleanup.run(
                    actor_user_id=self.actor_user_id,
                    operation_id=operation_id,
                    start_audit_id=start_audit_id,
                    started_at=started_at,
                    set_stage=lambda _stage: None,
                )

        actions = [
            str(row["action"])
            for row in isolated_db_fetch_all(
                self.db,
                "SELECT action FROM audit_log ORDER BY id"
            )
        ]
        self.assertEqual(actions, ["artifact_cleanup.start"])

    def test_source_backup_contains_only_bare_repositories_and_workspaces(
        self,
    ) -> None:
        uncommitted = self.workspace / "uncommitted.txt"
        uncommitted.write_text("not committed\n", encoding="utf-8")
        excluded = {
            self.settings.artifacts_root / "artifact.bin",
            self.settings.cache_root / "cache.bin",
            self.settings.contest_source_root / "statement.tex",
            self.settings.backup_root / "contest-migration.tar.gz",
        }
        for path in excluded:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"excluded")

        result = self._run_source_backup("backup-boundary")

        self.assertEqual(result["completed_stage"], "publish")
        archive_path = self.source_backup.latest_archive_path()
        self.assertIsNotNone(archive_path)
        assert archive_path is not None
        with tarfile.open(archive_path, mode="r:gz") as archive:
            names = set(archive.getnames())
            top_level = {name.split("/", maxsplit=1)[0] for name in names}
            self.assertEqual(top_level, {"manifest.json", "bare", "workspaces"})
            workspace_relative = self.workspace.relative_to(
                self.settings.workspace_root
            )
            workspace_prefix = f"workspaces/{workspace_relative.as_posix()}"
            self.assertIn(f"{workspace_prefix}/uncommitted.txt", names)
            self.assertTrue(
                any(name.startswith(f"{workspace_prefix}/.git") for name in names)
            )
            self.assertTrue(
                any(name.startswith("bare/") and name.endswith("/HEAD") for name in names)
            )
            manifest_file = archive.extractfile("manifest.json")
            self.assertIsNotNone(manifest_file)
            assert manifest_file is not None
            manifest = json.loads(manifest_file.read().decode("ascii"))
        self.assertEqual(manifest["contents"], ["bare", "workspaces"])

    def test_source_backup_replaces_one_latest_archive(self) -> None:
        marker = self.workspace / "backup-marker.txt"
        marker.write_text("first\n", encoding="utf-8")
        self._run_source_backup("backup-first")
        first_bytes = self.source_backup.latest_path.read_bytes()

        marker.write_text("second\n", encoding="utf-8")
        self._run_source_backup("backup-second")

        self.assertNotEqual(self.source_backup.latest_path.read_bytes(), first_bytes)
        self.assertEqual(
            [path.name for path in self.source_backup.backup_directory.iterdir()],
            ["latest.tar.gz"],
        )
        with tarfile.open(self.source_backup.latest_path, mode="r:gz") as archive:
            relative = self.workspace.relative_to(self.settings.workspace_root)
            payload = archive.extractfile(
                f"workspaces/{relative.as_posix()}/backup-marker.txt"
            )
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload.read(), b"second\n")

    def test_source_backup_failure_restores_previous_latest(self) -> None:
        marker = self.workspace / "backup-marker.txt"
        marker.write_text("first\n", encoding="utf-8")
        self._run_source_backup("backup-stable")
        previous = self.source_backup.latest_path.read_bytes()
        marker.write_text("replacement\n", encoding="utf-8")
        original_write_audit = self.source_backup._write_audit

        def fail_success_audit(
            actor_user_id: int,
            action: str,
            details: dict[str, object],
        ) -> int:
            if action == "source_backup.succeeded":
                raise RuntimeError("forced success audit failure")
            return original_write_audit(actor_user_id, action, details)

        with patch.object(
            self.source_backup,
            "_write_audit",
            side_effect=fail_success_audit,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced success audit failure"):
                self._run_source_backup("backup-failing")

        self.assertEqual(self.source_backup.latest_path.read_bytes(), previous)
        actions = [
            str(row["action"])
            for row in isolated_db_fetch_all(
                self.db,
                "SELECT action FROM audit_log ORDER BY id",
            )
        ]
        self.assertEqual(
            actions,
            [
                "source_backup.start",
                "source_backup.succeeded",
                "source_backup.start",
                "source_backup.failed",
            ],
        )

    def test_source_backup_and_artifact_cleanup_are_mutually_exclusive(self) -> None:
        coordinator = MaintenanceCoordinator(
            self.cleanup,
            self.worker_queue,
            self.judgehost,
            source_backup_service=self.source_backup,
        )
        running = threading.Event()
        release = threading.Event()

        def blocking_backup(**_kwargs) -> dict[str, object]:
            running.set()
            release.wait(timeout=5)
            return {"finished_at": now_iso(), "duration_ms": 1}

        with patch.object(
            self.source_backup,
            "run",
            side_effect=blocking_backup,
        ):
            started = coordinator.start_source_backup(
                actor_user_id=self.actor_user_id
            )
            self.assertTrue(started.accepted)
            self.assertTrue(running.wait(timeout=2))
            self.assertFalse(coordinator.allow_new_work())
            snapshot = coordinator.snapshot()
            self.assertEqual(snapshot["operation"], "source_backup")
            blocked_cleanup = coordinator.start_cleanup(
                actor_user_id=self.actor_user_id
            )
            self.assertFalse(blocked_cleanup.accepted)
            self.assertEqual(blocked_cleanup.reason, "already_running")
            release.set()
            worker = coordinator._worker
            self.assertIsNotNone(worker)
            assert worker is not None
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(coordinator.snapshot()["status"], "succeeded")
        self.assertTrue(coordinator.allow_new_work())

    def test_task_registry_reports_reporting_separately_for_maintenance(self) -> None:
        registry = JudgehostTaskRegistry()
        registry.insert(
            {
                "id": "judge-task",
                "run_id": "judge-run",
                "problem_slug": "admin/sample",
                "verification_id": "ver-1ad6e",
                "status": "queued",
            }
        )
        registry.claim_reporting("judge-task", now_text=now_iso())

        self.assertEqual(
            registry.maintenance_counts(),
            {"queued": 0, "leased": 0, "reporting": 1},
        )


if __name__ == "__main__":
    unittest.main()
