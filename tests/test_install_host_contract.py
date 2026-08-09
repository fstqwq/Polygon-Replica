from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


class TestInstallHostContract(unittest.TestCase):
    def test_systemd_unit_is_rendered_from_invocation_identity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install_host.sh").read_text(encoding="utf-8")
        unit = (root / "scripts" / "systemd" / "polygon-replica.service").read_text(encoding="utf-8")

        self.assertIn('RUNTIME_USER="$POLYGON_REPLICA_RUNTIME_USER"', installer)
        self.assertIn('RUNTIME_USER="$SUDO_USER"', installer)
        self.assertIn('RUNTIME_USER="$(id -un)"', installer)
        self.assertIn('id "$RUNTIME_USER"', installer)
        self.assertIn("@RUNTIME_USER@", unit)
        self.assertIn("@RUNTIME_GROUP@", unit)
        self.assertIn("@WORKING_DIRECTORY@", unit)
        self.assertIn("@UVICORN_EXECUTABLE@", unit)
        self.assertNotIn("User=judgehost", unit)
        self.assertIn('Refusing to run Polygon-Replica as root.', installer)
        self.assertIn('systemd-analyze verify "$TMP_SERVICE_UNIT"', installer)

    def test_rendered_unit_quotes_spaces_and_escapes_specifiers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        renderer = root / "scripts" / "render_systemd_unit.sh"
        template = root / "scripts" / "systemd" / "polygon-replica.service"
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "polygon-replica.service"
            subprocess.run(
                [
                    "bash",
                    str(renderer),
                    str(template),
                    str(output),
                    "runtime-user",
                    "runtime-group",
                    "/srv/Polygon Replica%prod",
                ],
                check=True,
            )
            rendered = output.read_text(encoding="utf-8")

        self.assertIn('User=runtime-user', rendered)
        self.assertIn('Group=runtime-group', rendered)
        self.assertIn('WorkingDirectory="/srv/Polygon Replica%%prod"', rendered)
        self.assertIn(
            'ExecStart="/srv/Polygon Replica%%prod/.venv/bin/uvicorn" app.main:app',
            rendered,
        )
        self.assertNotIn("@WORKING_DIRECTORY@", rendered)
        self.assertNotIn("@UVICORN_EXECUTABLE@", rendered)
