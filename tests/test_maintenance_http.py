from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.main import MaintenanceAdmissionMiddleware, config
from app.route.maintenance_route import maintenance_page
from app.service.platform.admission import MaintenanceAdmissionGate


class _AdmissionStub:
    def __init__(self) -> None:
        self.gate = MaintenanceAdmissionGate()
        self.active_requests = 0

    @staticmethod
    def is_exempt(_path: str) -> bool:
        return False

    def enter_request(self) -> bool:
        with self.gate.locked():
            if not self.gate.is_open_locked():
                return False
            self.active_requests += 1
            return True

    def leave_request(self) -> None:
        with self.gate.locked():
            self.active_requests -= 1


class TestMaintenanceAdmissionMiddleware(unittest.IsolatedAsyncioTestCase):
    def test_raw_maintenance_page_reports_running_success_and_failure(self) -> None:
        running_state = {
            "status": "running",
            "operation_id": "cleanup-running",
            "stage": "filesystem",
            "started_at": "2026-08-08T00:00:00Z",
        }
        with patch.object(
            config,
            "maintenance_service",
            SimpleNamespace(snapshot=lambda: running_state),
        ):
            running = maintenance_page()
        self.assertEqual(running.status_code, 200)
        self.assertEqual(running.headers.get("refresh"), "2")
        self.assertIn("text/plain", running.headers.get("content-type", ""))
        self.assertIn(b"stage: filesystem", running.body)

        succeeded_state = {"status": "succeeded"}
        with patch.object(
            config,
            "maintenance_service",
            SimpleNamespace(snapshot=lambda: succeeded_state),
        ):
            succeeded = maintenance_page()
        self.assertEqual(succeeded.status_code, 303)
        self.assertEqual(
            succeeded.headers.get("location"),
            "/admin?cleanup=success",
        )

        failed_state = {
            "status": "failed",
            "operation_id": "cleanup-failed",
            "stage": "vacuum",
            "error": "disk full",
            "result": {"completed_stage": "runtime"},
        }
        with patch.object(
            config,
            "maintenance_service",
            SimpleNamespace(snapshot=lambda: failed_state),
        ):
            failed = maintenance_page()
        self.assertEqual(failed.status_code, 200)
        self.assertIn(b"completed_stage: runtime", failed.body)
        self.assertIn(b"disk full", failed.body)

    async def test_request_remains_counted_through_body_and_background_work(self) -> None:
        stub = _AdmissionStub()
        body_finished = asyncio.Event()
        finish_background = asyncio.Event()
        sent: list[dict[str, object]] = []

        async def downstream(scope, receive, send) -> None:
            _ = (scope, receive)
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

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
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
        middleware = MaintenanceAdmissionMiddleware(downstream)

        with patch.object(config, "maintenance_service", stub):
            request_task = asyncio.create_task(middleware(scope, receive, send))
            await asyncio.wait_for(body_finished.wait(), timeout=2)
            self.assertEqual(stub.active_requests, 1)
            self.assertFalse(request_task.done())
            finish_background.set()
            await asyncio.wait_for(request_task, timeout=2)

        self.assertEqual(stub.active_requests, 0)
        self.assertEqual(sent[-1]["type"], "http.response.body")

    async def test_closed_admission_returns_immediate_raw_503(self) -> None:
        stub = _AdmissionStub()
        with stub.gate.locked():
            stub.gate.close_locked()
        downstream_called = False
        sent: list[dict[str, object]] = []

        async def downstream(scope, receive, send) -> None:
            nonlocal downstream_called
            _ = (scope, receive, send)
            downstream_called = True

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
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
        middleware = MaintenanceAdmissionMiddleware(downstream)

        with patch.object(config, "maintenance_service", stub):
            await middleware(scope, receive, send)

        self.assertFalse(downstream_called)
        self.assertEqual(sent[0]["status"], 503)
        headers = dict(sent[0]["headers"])
        self.assertEqual(headers[b"retry-after"], b"5")
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertNotIn(b"<html", bytes(sent[-1]["body"]).lower())


if __name__ == "__main__":
    unittest.main()
