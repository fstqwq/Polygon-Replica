from __future__ import annotations

import unittest
from typing import Any

from app.services.invocation_backend_service import InvocationBackendService


class _FakeRunService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_submission(
        self,
        problem: str,
        username: str,
        build_id: str,
        *,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
        upload_stream: object | None = None,
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        expected_behavior: str | None = None,
        invocation_source: str = "run.execute",
        force_recompile: bool = False,
    ) -> str:
        self.calls.append(
            {
                "problem": problem,
                "username": username,
                "build_id": build_id,
                "run_id": run_id,
                "force_recompile": bool(force_recompile),
            }
        )
        return str(run_id or "r-local")


class _FakeJudgehostTaskService:
    def __init__(self, *, enabled: bool = True, auth: bool = True) -> None:
        self._enabled = bool(enabled)
        self._auth = bool(auth)
        self.enqueued: list[dict[str, object]] = []
        self.waited: list[tuple[str, float | None]] = []

    def enabled(self) -> bool:
        return self._enabled

    def auth_token_configured(self) -> bool:
        return self._auth

    def enqueue_task(self, **kwargs: object) -> str:
        self.enqueued.append(dict(kwargs))
        return "t-judgehost-1"

    def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
        self.waited.append((str(task_id), timeout_sec))
        return "r-judgehost-1"

    def status(self) -> dict[str, object]:
        return {"queue": {"queued": 0, "leased": 0}}


class TestInvocationBackendService(unittest.TestCase):
    def test_status_lists_only_local_and_domjudge_judgehost(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehostTaskService(enabled=True, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        status = service.status()
        names = [str(item.get("name") or "") for item in (status.get("available") or []) if isinstance(item, dict)]
        self.assertIn("domjudge-judgehost", names)
        self.assertIn("local-sandbox", names)
        self.assertNotIn("domjudge-adapter", names)

    def test_configured_judgehost_selects_domjudge_judgehost(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehostTaskService(enabled=True, auth=True)
        service = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost,  # type: ignore[arg-type]
            configured_backend_name="domjudge-judgehost",
        )
        self.assertEqual(service.active_backend_name(), "domjudge-judgehost")

    def test_default_auto_prefers_judgehost_when_ready(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehostTaskService(enabled=True, auth=True)
        service = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost,  # type: ignore[arg-type]
        )
        self.assertEqual(service.active_backend_name(), "domjudge-judgehost")

    def test_auto_falls_back_to_local_when_judgehost_unavailable(self) -> None:
        run_service = _FakeRunService()
        judgehost_disabled = _FakeJudgehostTaskService(enabled=False, auth=True)
        service_disabled = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost_disabled,  # type: ignore[arg-type]
        )
        self.assertEqual(service_disabled.active_backend_name(), "local-sandbox")

        judgehost_missing_auth = _FakeJudgehostTaskService(enabled=True, auth=False)
        service_missing_auth = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost_missing_auth,  # type: ignore[arg-type]
        )
        self.assertEqual(service_missing_auth.active_backend_name(), "local-sandbox")

    def test_invalid_backend_name_falls_back_to_local(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehostTaskService(enabled=True, auth=True)
        service = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost,  # type: ignore[arg-type]
            configured_backend_name="domjudge-adapter",
        )
        self.assertEqual(service.active_backend_name(), "local-sandbox")

    def test_status_includes_configured_and_active_backend(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehostTaskService(enabled=True, auth=True)
        service = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost,  # type: ignore[arg-type]
        )
        status = service.status()
        self.assertEqual(str(status.get("configured") or ""), "auto")
        self.assertEqual(str(status.get("active") or ""), "domjudge-judgehost")

    def test_domjudge_judgehost_backend_dispatches_to_queue_service(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehostTaskService(enabled=True, auth=True)
        service = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost,  # type: ignore[arg-type]
            configured_backend_name="domjudge-judgehost",
        )
        run_id = service.run_submission(problem="p", username="u", build_id="b")
        self.assertEqual(run_id, "r-judgehost-1")
        self.assertFalse(run_service.calls)
        self.assertEqual(len(judgehost.enqueued), 1)
        self.assertEqual(len(judgehost.waited), 1)

    def test_domjudge_backend_forwards_force_recompile_flag(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehostTaskService(enabled=True, auth=True)
        service = InvocationBackendService(
            run_service,
            judgehost_task_service=judgehost,  # type: ignore[arg-type]
            configured_backend_name="domjudge-judgehost",
        )
        run_id = service.run_submission(
            problem="p",
            username="u",
            build_id="b",
            force_recompile=True,
        )
        self.assertEqual(run_id, "r-judgehost-1")
        self.assertEqual(len(judgehost.enqueued), 1)
        self.assertTrue(bool(judgehost.enqueued[0].get("force_recompile")))


if __name__ == "__main__":
    unittest.main()
