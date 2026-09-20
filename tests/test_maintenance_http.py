import asyncio
import gzip
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from app.config.registry import build_config_values
from app.db import DB, SchemaRequirementsError
from app.main import MaintenanceAdmissionMiddleware, app, runtime
from app.route.maintenance_route import maintenance_page
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate
from app.service.platform.maintenance.coordinator import MaintenanceCoordinator
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.source_backup import SourceBackupService
from app.service.platform.worker_queue import WorkerQueueService
from tests.isolated_db_helpers import isolated_db_execute


class TestMaintenanceAdmissionMiddleware(unittest.IsolatedAsyncioTestCase):
    async def test_schema_gap_returns_actionable_raw_503_without_dispatch(self) -> None:
        downstream_called = False
        sent: list[Message] = []

        async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal downstream_called
            downstream_called = True

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/admin",
            "raw_path": b"/admin",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        middleware = MaintenanceAdmissionMiddleware(downstream, runtime)
        schema_error = SchemaRequirementsError(
            missing_tables=["system_config"],
            missing_columns=["exports.materialization_id"],
            missing_indexes=["idx_exports_materialization_created"],
        )

        with patch.object(runtime, "schema_error", schema_error):
            await middleware(scope, receive, send)

        self.assertFalse(downstream_called)
        self.assertEqual(sent[0]["status"], 503)
        headers = dict(sent[0]["headers"])
        self.assertEqual(headers[b"retry-after"], b"60")
        self.assertEqual(headers[b"cache-control"], b"no-store")
        body = bytes(sent[-1]["body"])
        self.assertIn(b"missing tables: system_config", body)
        self.assertIn(b"missing columns: exports.materialization_id", body)
        self.assertIn(b"missing indexes: idx_exports_materialization_created", body)
        self.assertNotIn(b"<html", body.lower())

    def test_raw_maintenance_page_tracks_backup_progress_and_completion(self) -> None:
        for fail_write in (False, True):
            with self.subTest(fail_write=fail_write), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                layout = StorageLayout(
                    database_path=root / "metadata.db",
                    bare_root=root / "bare", workspace_root=root / "workspaces",
                    contest_source_root=root / "contests", artifacts_root=root / "artifacts",
                    cache_root=root / "cache", backup_root=root / "backups",
                )
                for source in (layout.bare_root, layout.workspace_root, layout.contest_source_root):
                    source.mkdir()
                (layout.workspace_root / "fixture.txt").write_bytes(b"durable source\n")
                database = DB(layout.database_path, config_values=build_config_values())
                try:
                    isolated_db_execute(database, "CREATE TABLE fixture (value TEXT)")
                finally:
                    database.close_connections()
                gate = MaintenanceAdmissionGate()
                backup = SourceBackupService(layout)
                coordinator = MaintenanceCoordinator(
                    admission_gate=gate,
                    cleanup_service=runtime.artifact_cleanup_service,
                    source_backup_service=backup,
                    worker_queue_service=WorkerQueueService(),
                    judgehost_task_service=runtime.judgehost_task_service,
                )
                request = Request({"type": "http", "app": app})
                entered = threading.Event()
                release = threading.Event()
                original_write = gzip.GzipFile.write

                def write_archive(stream: gzip.GzipFile, data: bytes) -> int:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("backup archive write was not released")
                    if fail_write:
                        raise OSError("fixture archive write failed")
                    return original_write(stream, data)

                with (
                    patch.object(runtime, "maintenance_service", coordinator),
                    patch.object(gzip.GzipFile, "write", write_archive),
                ):
                    self.assertTrue(coordinator.begin_drain().accepted)
                    started = coordinator.start_source_backup(actor_user_id=1)
                    self.assertTrue(started.accepted, started)
                    try:
                        self.assertTrue(entered.wait(timeout=5))
                        running = maintenance_page(request)
                        self.assertEqual(running.status_code, 200)
                        self.assertEqual(running.headers["refresh"], "2")
                        self.assertIn("text/plain", running.headers["content-type"])
                        self.assertIn(b"stage: archive", running.body)
                        self.assertEqual(gate.state(), "closed")
                    finally:
                        release.set()
                        deadline = time.monotonic() + 5
                        while coordinator.snapshot()["status"] == "running" and time.monotonic() < deadline:
                            time.sleep(0.01)
                    completed = maintenance_page(request)
                    self.assertEqual(gate.state(), "open")
                    if fail_write:
                        self.assertEqual(completed.status_code, 200)
                        self.assertIn(b"completed_stage: database", completed.body)
                        self.assertIn(b"fixture archive write failed", completed.body)
                        self.assertIsNone(backup.latest_archive_path())
                    else:
                        self.assertEqual(completed.status_code, 303)
                        self.assertEqual(completed.headers["location"], "/admin?backup=success")
                        archive_path = backup.latest_archive_path()
                        self.assertIsNotNone(archive_path)
                        with tarfile.open(archive_path, "r:gz") as archive:
                            source = archive.extractfile("workspaces/fixture.txt")
                            self.assertIsNotNone(source)
                            self.assertEqual(source.read(), b"durable source\n")

    async def test_request_remains_counted_through_body_and_background_work(self) -> None:
        gate = MaintenanceAdmissionGate()
        body_finished = asyncio.Event()
        finish_background = asyncio.Event()
        sent: list[Message] = []

        async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"stream complete",
                    "more_body": False,
                }
            )
            body_finished.set()
            await finish_background.wait()

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/slow-response",
            "raw_path": b"/slow-response",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        middleware = MaintenanceAdmissionMiddleware(downstream, runtime)

        with patch.object(runtime, "maintenance_admission_gate", gate):
            request_task = asyncio.create_task(middleware(scope, receive, send))
            try:
                await asyncio.wait_for(body_finished.wait(), timeout=2)
                with gate.locked():
                    self.assertEqual(gate.active_requests_locked(), 1)
                self.assertFalse(request_task.done())
            finally:
                finish_background.set()
                await asyncio.wait_for(request_task, timeout=2)

        with gate.locked():
            self.assertEqual(gate.active_requests_locked(), 0)
        self.assertEqual(sent[-1]["type"], "http.response.body")

    async def test_closed_admission_returns_immediate_raw_503(self) -> None:
        gate = MaintenanceAdmissionGate()
        with gate.locked():
            gate.close_locked()
        downstream_called = False
        sent: list[Message] = []

        async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal downstream_called
            downstream_called = True

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/ordinary",
            "raw_path": b"/api/ordinary",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        middleware = MaintenanceAdmissionMiddleware(downstream, runtime)

        with patch.object(runtime, "maintenance_admission_gate", gate):
            await middleware(scope, receive, send)

        self.assertFalse(downstream_called)
        self.assertEqual(sent[0]["status"], 503)
        headers = dict(sent[0]["headers"])
        self.assertEqual(headers[b"retry-after"], b"5")
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertNotIn(b"<html", bytes(sent[-1]["body"]).lower())

    async def test_draining_keeps_admin_available_and_releases_its_count(self) -> None:
        gate = MaintenanceAdmissionGate()
        with gate.locked():
            gate.drain_locked()
        downstream_called = False

        async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal downstream_called
            downstream_called = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"admin"})

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "path": "/admin/judgehosts",
            "method": "GET",
        }
        middleware = MaintenanceAdmissionMiddleware(downstream, runtime)
        with patch.object(runtime, "maintenance_admission_gate", gate):
            await middleware(scope, receive, send)

        self.assertTrue(downstream_called)
        self.assertEqual(sent[0]["status"], 200)
        with gate.locked():
            self.assertEqual(gate.active_requests_locked(), 0)


if __name__ == "__main__":
    unittest.main()
