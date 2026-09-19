import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient
from jinja2 import DictLoader
from starlette.responses import StreamingResponse

from app.impl.auth.middleware import AuthenticationMiddleware
from app.impl.auth.shared import render_template
from app.main import create_app
from app.runtime import build_runtime
from app.setting import Settings


class TestRuntimeComposition(unittest.TestCase):
    def test_create_app_installs_exact_runtime_and_serves_public_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                db_path=root / "var/metadata.db",
                bare_root=root / "git",
                workspace_root=root / "workspaces",
                artifacts_root=root / "artifacts",
                cache_root=root / "cache",
                contest_source_root=root / "contest-sources",
                backup_root=root / "backups",
            )
            runtime = build_runtime(settings)
            application = create_app(runtime)

            self.assertIs(application.state.runtime, runtime)
            for _lifespan in range(2):
                with TestClient(application) as client:
                    response = client.get("/login")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertGreaterEqual(int(response.headers["X-Backend-Render-Ms"]), 0)
                self.assertIn("application;dur=", response.headers["Server-Timing"])
                self.assertIn("template;dur=", response.headers["Server-Timing"])

            clock = {"wall": 10.0, "cpu": 1.0}

            def probe(value):
                clock["wall"] += 0.025
                clock["cpu"] += 0.004
                return value

            runtime.templates.env.loader = DictLoader({"timing.html": "{{ value | profile_probe }}"})
            runtime.templates.env.filters["profile_probe"] = probe

            @application.get("/timing-probe")
            def timed_page(request: Request):
                clock["wall"] += 0.010
                return render_template(request, "timing.html", {"value": "rendered"})

            with TestClient(application) as client, patch(
                "app.impl.auth.middleware.monotonic", side_effect=lambda: clock["wall"],
            ), patch("app.impl.auth.shared.monotonic", side_effect=lambda: clock["wall"]), patch(
                "app.impl.auth.shared.thread_time", side_effect=lambda: clock["cpu"],
            ):
                response = client.get("/timing-probe")
            self.assertEqual(response.text, "rendered")
            self.assertEqual(response.headers["X-Backend-Render-Ms"], "35")
            self.assertEqual(response.headers["Server-Timing"],
                             "application;dur=35.000, template;dur=25.000, template_cpu;dur=4.000")

    def test_timing_headers_are_sent_before_stream_body_is_consumed(self) -> None:
        messages = []

        async def body():
            self.assertEqual(messages[0]["type"], "http.response.start")
            headers = dict(messages[0]["headers"])
            self.assertIn(b"application;dur=", headers[b"server-timing"])
            self.assertIn(b"template;dur=0.000", headers[b"server-timing"])
            yield b"first"
            self.assertEqual(messages[-1]["body"], b"first")
            yield b"second"

        async def downstream(scope, receive, send):
            await StreamingResponse(body())(scope, receive, send)

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        asyncio.run(AuthenticationMiddleware(downstream)(
            {"type": "http", "path": "/stream", "asgi": {"version": "3.0", "spec_version": "2.4"}},
            receive, send,
        ))
        self.assertEqual(b"".join(message.get("body", b"") for message in messages), b"firstsecond")
