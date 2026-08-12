import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

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
            with TestClient(application) as client:
                response = client.get("/login")
            self.assertEqual(response.status_code, 200)
