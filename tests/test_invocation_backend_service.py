from __future__ import annotations

import io
import unittest
from typing import Any

from app.service.runtime.invocation_backend import InvocationBackendService


class _FakeRunService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


class _FakeJudgehost:
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
    def test_status_lists_only_domjudge_judgehost(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=True, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        status = service.status()
        available = [item for item in (status.get("available") or []) if isinstance(item, dict)]
        self.assertEqual(len(available), 1)
        self.assertEqual(str(available[0].get("name") or ""), "domjudge-judgehost")
        self.assertEqual(str(status.get("configured") or ""), "domjudge-judgehost")
        self.assertEqual(str(status.get("active") or ""), "domjudge-judgehost")

    def test_active_backend_name_is_fixed_to_domjudge_judgehost(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=False, auth=False)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        self.assertEqual(service.active_backend_name(), "domjudge-judgehost")

    def test_domjudge_backend_dispatches_to_queue_service(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=True, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        run_id = service.run_submission(problem="p", username="u", build_id="b")
        self.assertEqual(run_id, "r-judgehost-1")
        self.assertEqual(len(judgehost.enqueued), 1)
        self.assertEqual(len(judgehost.waited), 1)

    def test_domjudge_backend_forwards_force_recompile_flag(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=True, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        run_id = service.run_submission(
            problem="p",
            username="u",
            build_id="b",
            force_recompile=True,
        )
        self.assertEqual(run_id, "r-judgehost-1")
        self.assertEqual(len(judgehost.enqueued), 1)
        self.assertTrue(bool(judgehost.enqueued[0].get("force_recompile")))

    def test_domjudge_backend_rejects_upload_stream(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=True, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        with self.assertRaises(RuntimeError):
            service.run_submission(
                problem="p",
                username="u",
                build_id="b",
                upload_stream=io.BytesIO(b"test"),
            )

    def test_domjudge_backend_requires_enabled_service(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=False, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        with self.assertRaises(RuntimeError) as cm:
            service.run_submission(problem="p", username="u", build_id="b")
        self.assertIn("disabled", str(cm.exception).lower())

    def test_domjudge_backend_requires_auth_token(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=True, auth=False)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        with self.assertRaises(RuntimeError) as cm:
            service.run_submission(problem="p", username="u", build_id="b")
        self.assertIn("token", str(cm.exception).lower())

    def test_domjudge_backend_routes_buildsolve_and_verification_sources(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=True, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        service.run_submission(
            problem="p",
            username="u",
            build_id="b",
            invocation_source="build.solve",
        )
        service.run_submission(
            problem="p",
            username="u",
            build_id="b",
            invocation_source="verification.start",
        )
        self.assertEqual(len(judgehost.enqueued), 2)
        self.assertEqual(str(judgehost.enqueued[0].get("invocation_source") or ""), "build.solve")
        self.assertEqual(str(judgehost.enqueued[1].get("invocation_source") or ""), "verification.start")

    def test_domjudge_backend_routes_generation_and_validation_sources(self) -> None:
        run_service = _FakeRunService()
        judgehost = _FakeJudgehost(enabled=True, auth=True)
        service = InvocationBackendService(run_service, judgehost_task_service=judgehost)  # type: ignore[arg-type]
        invocation_sources = [
            "build.generate-input",
            "build.generate-output",
            "build.validate-tests",
        ]
        for source in invocation_sources:
            service.run_submission(
                problem="p",
                username="u",
                build_id="b",
                invocation_source=source,
            )
        self.assertEqual(
            [str(item.get("invocation_source") or "") for item in judgehost.enqueued],
            invocation_sources,
        )


if __name__ == "__main__":
    unittest.main()
