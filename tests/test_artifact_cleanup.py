import json
import os
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
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate
from app.service.platform.maintenance.artifact import ArtifactCleanupService
from app.service.platform.maintenance.coordinator import MaintenanceCoordinator
from app.service.platform.maintenance.database import ArtifactCleanupDatabase
from app.service.platform.maintenance.filesystem import ArtifactCleanupFilesystem
from app.service.platform.maintenance.plan import (
    ARTIFACT_TABLES,
    CLEANUP_FILESYSTEM_CLASSES,
    REDUNDANT_DATABASE_INDEXES,
)
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.platform.source_backup import SourceBackupService
from app.service.repository.workspace import WorkspaceService
from app.service.access.query import AccessQuery
from app.service.execution.codec import execution_result_json
from app.service.execution.policy import normalize_execution_result
from app.service.verification.task_store import VerificationTaskStore
from app.setting import Settings
from tests.isolated_db_helpers import (
    isolated_db_connection,
    isolated_db_execute,
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
        self.storage_layout = StorageLayout.from_settings(self.settings)
        self.db = DB(self.settings.db_path, config_values=self.config_values)
        self.db.init()
        self.verification_task_store = VerificationTaskStore(self.db)
        self.access_query = AccessQuery(self.db)
        self.workspace_service = WorkspaceService(
            self.db,
            self.storage_layout,
            access_query=self.access_query,
            verification_task_store=self.verification_task_store,
            config_values=self.config_values,
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
        self.runtime_blob_store = RuntimeBlobStore(
            self.storage_layout.runtime_blob_root
        )
        self.runtime_cache_index = RuntimeCacheIndex(self.runtime_blob_store)
        self.worker_queue = _WorkerQueueStub()
        self.judgehost = _JudgehostStub()
        self.process_reset_count = 0
        self.cleanup_database = ArtifactCleanupDatabase(
            self.db,
            self.storage_layout.database_path,
        )
        self.cleanup_filesystem = ArtifactCleanupFilesystem(
            self.storage_layout,
        )
        self.cleanup = ArtifactCleanupService(
            self.cleanup_database,
            self.cleanup_filesystem,
            self.runtime_cache_index,
            self.runtime_blob_store,
            self.worker_queue,
            self.judgehost,
            self.verification_task_store,
            self._reset_process_tracking,
        )
        self.source_backup = SourceBackupService(self.storage_layout)

    def _coordinator(self) -> MaintenanceCoordinator:
        self.maintenance_gate = MaintenanceAdmissionGate()
        return MaintenanceCoordinator(
            admission_gate=self.maintenance_gate,
            cleanup_service=self.cleanup,
            source_backup_service=self.source_backup,
            worker_queue_service=self.worker_queue,
            judgehost_task_service=self.judgehost,
        )

    def _begin_drain(self, coordinator: MaintenanceCoordinator) -> None:
        drained = coordinator.begin_drain()
        self.assertTrue(drained.accepted, drained.reason)
        self.assertEqual(self.maintenance_gate.state(), "draining")

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
        return self.source_backup.run(
            operation_id=operation_id,
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
        self._execute("""
            INSERT INTO verification_sanity_checks(
                verification_id,ordinal,check_name,status,checked_count
            ) VALUES('ver-c1ea4',0,'sample','passed',1)
            """)
        self._execute("""
            INSERT INTO verification_sanity_check_messages(
                verification_id,check_name,ordinal,severity,test_name,message
            ) VALUES('ver-c1ea4','sample',0,'info','001.in','ok')
            """)
        self._execute("""
            INSERT INTO verification_tests_meta(
                verification_id,ordinal,test_name
            ) VALUES('ver-c1ea4',0,'001.in')
            """)
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
            INSERT INTO verification_task_artifacts(
                verification_id,task_id,test_name,pass_number,role,
                artifact_ref,download_filename
            ) VALUES('ver-c1ea4','task-cleanup','001.in',0,
                     'generated-input','blob://input','001.in')
            """,
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
            ),
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
        durable_files = {
            "git": b"git durable",
            "workspace": b"workspace durable",
            "contest": b"contest durable",
            "backup": b"backup durable",
        }
        durable_paths = {
            "git": self.settings.bare_root / "durable-marker",
            "workspace": self.workspace / "durable-marker",
            "contest": self.settings.contest_source_root
            / "cleanup-contest"
            / "statement.pdf",
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
            table: self._count(table) for table in usage["table_rows"]
        }
        self.assertEqual(usage["table_rows"], expected_table_rows)
        self.assertEqual(usage["artifact_rows"], sum(expected_table_rows.values()))
        self.assertEqual(usage["removable_rows"], usage["artifact_rows"])

    def test_cleanup_deletes_derived_epoch_and_preserves_durable_data(self) -> None:
        durable_files = self._seed_generated_data()
        with isolated_db_connection(self.db) as connection:
            redundant_index_statements = (
                "CREATE INDEX idx_workspaces_problem_user "
                "ON workspaces(problem_id,user_id)",
                "CREATE INDEX idx_contests_slug ON contests(slug)",
                "CREATE INDEX idx_contest_members_contest "
                "ON contest_members(contest_id,user_id)",
                "CREATE INDEX idx_contest_problems_contest "
                "ON contest_problems(contest_id,position)",
                "CREATE INDEX idx_verification_selected_tests_verification_ordinal "
                "ON verification_selected_tests(verification_id,ordinal)",
                "CREATE INDEX idx_verification_source_paths_verification_ordinal "
                "ON verification_source_paths(verification_id,ordinal)",
                "CREATE INDEX idx_verification_sanity_checks_verification_ordinal "
                "ON verification_sanity_checks(verification_id,ordinal)",
                "CREATE INDEX idx_verification_sanity_check_messages_verification_check "
                "ON verification_sanity_check_messages(verification_id,check_name,ordinal)",
                "CREATE INDEX idx_verification_tests_meta_verification_ordinal "
                "ON verification_tests_meta(verification_id,ordinal)",
                "CREATE INDEX idx_pending_registrations_token "
                "ON pending_registrations(token_hash)",
            )
            for statement in redundant_index_statements:
                connection.execute(statement)
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        coordinator = self._coordinator()
        self._begin_drain(coordinator)

        started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)
        self.assertTrue(started.accepted, started.reason)
        self.assertFalse(self.maintenance_gate.is_open())
        worker = coordinator._worker
        self.assertIsNotNone(worker)
        assert worker is not None
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertTrue(self.maintenance_gate.is_open())

        for table in (
            "previews",
            "export_jobs",
            "exports",
            "problem_package_builds",
            "problem_package_materializations",
            "contest_build_items",
            "contest_artifacts",
            "contest_jobs",
            "verification_task_artifacts",
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
        with isolated_db_connection(self.db) as connection:
            remaining_indexes = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        self.assertTrue(set(REDUNDANT_DATABASE_INDEXES).isdisjoint(remaining_indexes))
        system_config = isolated_db_fetch_one(
            self.db,
            "SELECT value_json FROM system_config WHERE key='cleanup-test-setting'",
        )
        self.assertIsNotNone(system_config)
        self.assertEqual(str(system_config["value_json"]), "true")
        smtp_config = isolated_db_fetch_one(
            self.db, "SELECT host,port,password_ciphertext FROM smtp_config WHERE id=1"
        )
        self.assertIsNotNone(smtp_config)
        self.assertEqual(str(smtp_config["host"]), "smtp.example.test")
        self.assertEqual(int(smtp_config["port"]), 2525)
        self.assertEqual(str(smtp_config["password_ciphertext"]), "ciphertext")
        self.assertEqual(
            [
                path
                for path in self.settings.artifacts_root.rglob("*")
                if path.is_file()
            ],
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
        with isolated_db_connection(self.db) as connection:
            freelist_count = int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
        self.assertEqual(freelist_count, 0)

    def test_database_cleanup_replaces_tables_without_row_deletes(self) -> None:
        self._seed_generated_data()
        traced_sql: list[str] = []

        def install_trace(connection) -> None:
            connection.set_trace_callback(traced_sql.append)

        with patch.object(
            self.db,
            "_install_sql_trace",
            side_effect=install_trace,
        ):
            counts = self.cleanup_database.reset_tables(
                ARTIFACT_TABLES,
                drop_indexes=REDUNDANT_DATABASE_INDEXES,
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

        with patch(
            "app.service.platform.maintenance.database.current_schema_statements_for_tables",
            return_value=("CREATE TABLE invalid schema",),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "syntax"):
                self.cleanup_database.reset_tables(
                    ARTIFACT_TABLES,
                    drop_indexes=REDUNDANT_DATABASE_INDEXES,
                )

        self.assertEqual(self._count("verification_tasks"), 1)
        self.assertEqual(self._count("verifications"), 1)
        self.assertEqual(self._count("exports"), 1)
        with isolated_db_connection(self.db) as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_busy_check_keeps_explicit_drain(self) -> None:
        coordinator = self._coordinator()
        self.worker_queue.queued = 1
        self.judgehost.reporting = 2
        self.judgehost.callbacks = 1
        self.assertTrue(self.maintenance_gate.enter_request())
        self._begin_drain(coordinator)

        started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)

        self.assertFalse(started.accepted)
        self.assertEqual(started.reason, "busy")
        self.assertEqual(started.busy["worker_queued"], 1)
        self.assertEqual(started.busy["judgehost_reporting"], 2)
        self.assertEqual(started.busy["judgehost_callbacks"], 1)
        self.assertEqual(started.busy["inflight_requests"], 1)
        self.assertEqual(self.maintenance_gate.state(), "draining")
        self.maintenance_gate.leave_request()

    def test_cleanup_requires_explicit_drain(self) -> None:
        coordinator = self._coordinator()

        started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)

        self.assertFalse(started.accepted)
        self.assertEqual(started.reason, "drain_required")
        self.assertEqual(self.maintenance_gate.state(), "open")

    def test_drain_rejects_new_work_until_admin_resumes(self) -> None:
        coordinator = self._coordinator()
        self.worker_queue.running = 1

        started = coordinator.begin_drain()

        self.assertTrue(started.accepted)
        self.assertEqual(self.maintenance_gate.state(), "draining")
        self.assertFalse(self.maintenance_gate.enter_request())
        self.assertTrue(self.maintenance_gate.enter_control_request()[0])
        self.maintenance_gate.leave_request()
        self.assertEqual(started.busy["worker_running"], 1)

        resumed = coordinator.cancel_drain()
        self.assertTrue(resumed.accepted)
        self.assertEqual(self.maintenance_gate.state(), "open")

    def test_restart_requires_an_idle_explicit_drain(self) -> None:
        exited = threading.Event()
        self.maintenance_gate = MaintenanceAdmissionGate()
        coordinator = MaintenanceCoordinator(
            admission_gate=self.maintenance_gate,
            cleanup_service=self.cleanup,
            source_backup_service=self.source_backup,
            worker_queue_service=self.worker_queue,
            judgehost_task_service=self.judgehost,
            restart_process=exited.set,
        )

        not_drained = coordinator.restart_when_idle(actor_user_id=self.actor_user_id)
        self.assertEqual(not_drained.reason, "drain_required")
        coordinator.begin_drain()
        self.judgehost.leased = 1
        busy = coordinator.restart_when_idle(actor_user_id=self.actor_user_id)
        self.assertEqual(busy.reason, "busy")
        self.judgehost.leased = 0

        started = coordinator.restart_when_idle(actor_user_id=self.actor_user_id)
        self.assertTrue(started.accepted)
        self.assertEqual(self.maintenance_gate.state(), "closed")
        self.assertTrue(exited.wait(timeout=2))

    def test_busy_count_failure_keeps_explicit_drain(self) -> None:
        coordinator = self._coordinator()
        self._begin_drain(coordinator)

        with patch.object(
            self.worker_queue,
            "active_counts",
            side_effect=RuntimeError("forced admission count failure"),
        ):
            started = coordinator.start_cleanup(actor_user_id=self.actor_user_id)

        self.assertFalse(started.accepted)
        self.assertIn("admission_failed", started.reason)
        self.assertEqual(self.maintenance_gate.state(), "draining")

    def test_storage_layout_rejects_durable_root_inside_cleanup_root(self) -> None:
        invalid = replace(
            self.settings,
            backup_root=self.settings.cache_root / "backups",
        )

        with self.assertRaisesRegex(RuntimeError, "roots overlap"):
            StorageLayout.from_settings(invalid).validate()

    def test_cleanup_preflight_rejects_nested_symbolic_links(self) -> None:
        outside = self.root / "outside-cache"
        outside.mkdir()
        self.settings.cache_root.mkdir(parents=True, exist_ok=True)
        (self.settings.cache_root / "outside-link").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            self.cleanup_filesystem.preflight(CLEANUP_FILESYSTEM_CLASSES)

    def test_repeated_cleanup_start_allows_only_one_background_operation(self) -> None:
        coordinator = self._coordinator()
        self._begin_drain(coordinator)
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
        self.assertTrue(self.maintenance_gate.is_open())

    def test_filesystem_failure_keeps_database_deletion(self) -> None:
        self._seed_generated_data()
        artifact_file = self.settings.artifacts_root / "generated" / "artifact.bin"
        cache_file = self.settings.cache_root / "generated" / "cache.bin"
        operation_id = "cleanup-filesystem-failure"
        started_at = now_iso()

        def partially_clear(_root: Path) -> int:
            artifact_file.unlink()
            raise OSError("forced filesystem failure")

        with patch.object(
            self.cleanup_filesystem,
            "clear_root",
            side_effect=partially_clear,
        ):
            with self.assertRaisesRegex(OSError, "forced filesystem failure"):
                self.cleanup.run(
                    operation_id=operation_id,
                    started_at=started_at,
                    set_stage=lambda _stage: None,
                )

        self.assertEqual(self._count("verifications"), 0)
        self.assertEqual(self._count("exports"), 0)
        self.assertFalse(artifact_file.exists())
        self.assertTrue(cache_file.is_file())

    def test_database_transaction_failure_preserves_rows_and_files(self) -> None:
        self._seed_generated_data()
        artifact_file = self.settings.artifacts_root / "generated" / "artifact.bin"
        operation_id = "cleanup-database-failure"
        started_at = now_iso()
        with patch.object(
            self.cleanup_database,
            "reset_tables",
            side_effect=RuntimeError("forced database transaction failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced database"):
                self.cleanup.run(
                    operation_id=operation_id,
                    started_at=started_at,
                    set_stage=lambda _stage: None,
                )

        self.assertEqual(self._count("verifications"), 1)
        self.assertEqual(self._count("exports"), 1)
        self.assertTrue(artifact_file.is_file())

    def test_vacuum_failure_allows_safe_rerun_after_files_and_rows_are_removed(
        self,
    ) -> None:
        self._seed_generated_data()
        operation_id = "cleanup-vacuum-failure"
        started_at = now_iso()
        with patch.object(
            self.cleanup_database,
            "vacuum",
            side_effect=RuntimeError("forced vacuum failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced vacuum failure"):
                self.cleanup.run(
                    operation_id=operation_id,
                    started_at=started_at,
                    set_stage=lambda _stage: None,
                )

        self.assertEqual(self._count("verifications"), 0)
        self.assertEqual(
            [
                path
                for path in self.settings.artifacts_root.rglob("*")
                if path.is_file()
            ],
            [],
        )
        restarted_coordinator = self._coordinator()
        self._begin_drain(restarted_coordinator)
        retried = restarted_coordinator.start_cleanup(actor_user_id=self.actor_user_id)
        self.assertTrue(retried.accepted, retried.reason)
        retry_worker = restarted_coordinator._worker
        self.assertIsNotNone(retry_worker)
        assert retry_worker is not None
        retry_worker.join(timeout=20)
        self.assertFalse(retry_worker.is_alive())
        self.assertEqual(restarted_coordinator.snapshot()["status"], "succeeded")

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
                any(
                    name.startswith("bare/") and name.endswith("/HEAD")
                    for name in names
                )
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
            sorted(path.name for path in self.source_backup.backup_directory.iterdir()),
            ["latest.tar.gz", "latest.tar.gz.sha256"],
        )
        with tarfile.open(self.source_backup.latest_path, mode="r:gz") as archive:
            relative = self.workspace.relative_to(self.settings.workspace_root)
            payload = archive.extractfile(
                f"workspaces/{relative.as_posix()}/backup-marker.txt"
            )
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload.read(), b"second\n")

    def test_source_backup_publication_failure_restores_previous_pair(self) -> None:
        marker = self.workspace / "backup-marker.txt"
        marker.write_text("first\n", encoding="utf-8")
        self._run_source_backup("backup-stable")
        previous = self.source_backup.latest_path.read_bytes()
        previous_sidecar = self.source_backup.sidecar_path.read_bytes()
        marker.write_text("replacement\n", encoding="utf-8")
        original_replace = os.replace
        failed = False

        def fail_sidecar_publish(source: Path, destination: Path) -> None:
            nonlocal failed
            if Path(destination) == self.source_backup.sidecar_path and not failed:
                failed = True
                raise OSError("forced sidecar publication failure")
            original_replace(source, destination)

        with patch.object(
            os,
            "replace",
            side_effect=fail_sidecar_publish,
        ):
            with self.assertRaisesRegex(OSError, "sidecar publication"):
                self._run_source_backup("backup-failing")

        self.assertEqual(self.source_backup.latest_path.read_bytes(), previous)
        self.assertEqual(self.source_backup.sidecar_path.read_bytes(), previous_sidecar)
        self.assertEqual(
            self.source_backup.latest_archive_path(), self.source_backup.latest_path
        )

    def test_source_backup_summary_is_metadata_only_but_download_verifies(
        self,
    ) -> None:
        self._run_source_backup("backup-verified")
        self.source_backup.sidecar_path.write_text(
            f"{'0' * 64}  latest.tar.gz\n",
            encoding="ascii",
        )

        self.assertIsNone(self.source_backup.latest_archive_path())
        # The overview only reports that a published regular-file pair exists;
        # opening the download remains the strict integrity boundary.
        self.assertTrue(self.source_backup.latest_summary()["available"])

    def test_source_backup_and_artifact_cleanup_are_mutually_exclusive(self) -> None:
        coordinator = self._coordinator()
        self._begin_drain(coordinator)
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
            started = coordinator.start_source_backup(actor_user_id=self.actor_user_id)
            self.assertTrue(started.accepted)
            self.assertTrue(running.wait(timeout=2))
            self.assertFalse(self.maintenance_gate.is_open())
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
        self.assertTrue(self.maintenance_gate.is_open())

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
