import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestInstallHostContract(unittest.TestCase):
    def test_installer_rejects_root_and_unknown_runtime_accounts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            commands = Path(tmpdir)
            (commands / "python3.14").symlink_to(sys.executable)
            # Stop before any host changes even if account validation regresses.
            for name in ("apt-get", "sudo"):
                command = commands / name
                command.write_text("#!/bin/sh\nexit 98\n", encoding="utf-8")
                command.chmod(0o755)
            for username in ("root", "polygon-no-such-runtime-account"):
                with self.subTest(username=username):
                    result = subprocess.run(
                        ["bash", str(root / "scripts/install_host.sh")],
                        env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
                             "POLYGON_REPLICA_RUNTIME_USER": username},
                        capture_output=True, text=True, check=False, timeout=10,
                    )
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertRegex(result.stderr, "Refusing to run.*as root|own account|does not exist")

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
        self.assertIn('UMask=0077', rendered)
        self.assertIn('WorkingDirectory="/srv/Polygon Replica%%prod"', rendered)
        self.assertIn(
            'ExecStart="/srv/Polygon Replica%%prod/.venv/bin/uvicorn" app.main:app',
            rendered,
        )
        self.assertNotIn("@WORKING_DIRECTORY@", rendered)
        self.assertNotIn("@UVICORN_EXECUTABLE@", rendered)

    def test_runtime_environment_renderer_preserves_operator_values(self) -> None:
        root = Path(__file__).resolve().parents[1]
        renderer = root / "scripts" / "render_runtime_env.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = Path(tmpdir) / "existing.env"
            output = Path(tmpdir) / "rendered.env"
            existing.write_text(
                "\n".join(
                    [
                        "# operator configuration",
                        "; systemd comment",
                        "export POLYGON_REPLICA_DB=/old/metadata.db",
                        "export POLYGON_REPLICA_ENCRYPTION_KEY=secret==",
                        'CUSTOM_SETTING="value with spaces"',
                        "CUSTOM_APOSTROPHE=it's",
                        'CUSTOM_QUOTE=abc"def',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["bash", str(renderer), str(existing), str(output)],
                check=True,
            )
            rendered = output.read_text(encoding="utf-8")

        self.assertIn(
            "POLYGON_REPLICA_DB=/var/lib/polygon-replica/metadata.db\n",
            rendered,
        )
        self.assertNotIn("/old/metadata.db", rendered)
        self.assertIn("POLYGON_REPLICA_ENCRYPTION_KEY=secret==\n", rendered)
        self.assertIn('CUSTOM_SETTING="value with spaces"\n', rendered)
        self.assertIn("CUSTOM_APOSTROPHE=it's\n", rendered)
        self.assertIn('CUSTOM_QUOTE=abc"def\n', rendered)
        self.assertIn("# operator configuration\n", rendered)
        self.assertIn("; systemd comment\n", rendered)
        self.assertNotIn("export ", rendered)

    def test_runtime_environment_renderer_rejects_invalid_or_duplicate_keys(self) -> None:
        root = Path(__file__).resolve().parents[1]
        renderer = root / "scripts" / "render_runtime_env.sh"
        invalid_payloads = (
            "NOT AN ASSIGNMENT\n",
            "CUSTOM=first\nexport CUSTOM=second\n",
            "CUSTOM='unterminated\n",
            'CUSTOM="unterminated\n',
            "CUSTOM=value\\\n",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            for index, payload in enumerate(invalid_payloads):
                with self.subTest(payload=payload):
                    existing = Path(tmpdir) / f"existing-{index}.env"
                    output = Path(tmpdir) / f"rendered-{index}.env"
                    existing.write_text(payload, encoding="utf-8")
                    completed = subprocess.run(
                        ["bash", str(renderer), str(existing), str(output)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(completed.returncode, 0)
