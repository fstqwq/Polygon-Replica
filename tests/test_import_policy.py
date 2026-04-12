from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.scripts import import_policy
ROOT = Path(__file__).resolve().parents[1]
IMPORT_POLICY_SCRIPT = ROOT / "tests" / "scripts" / "import_policy.py"


class TestImportPolicy(unittest.TestCase):
    def test_dynamic_reexport_detector_scoped_to_application_modules(self) -> None:
        source = """
def _export_public(namespace, module):
    for name in dir(module):
        namespace.setdefault(name, getattr(module, name))
_export_public(globals(), module)
"""
        self.assertTrue(import_policy._dynamic_reexport_detected("app.impl.workspace.context_ui", source))
        self.assertFalse(import_policy._dynamic_reexport_detected("tests.scripts.import_policy", source))

    def test_plural_segment_naming_detector(self) -> None:
        self.assertEqual(
            import_policy._plural_name_violations_for_module(
                "app.service.process.api",
                plural_exceptions=["process"],
            ),
            [],
        )
        self.assertEqual(import_policy._plural_name_violations_for_module("app.impl.auth.middleware"), [])

    def test_affix_cluster_detector(self) -> None:
        offenders = import_policy._affix_cluster_modules(
            "statement",
            [
                "statement_render",
                "statement_parse",
                "statement_tokenize",
                "context",
            ],
        )
        self.assertEqual(
            offenders,
            {"statement_render", "statement_parse", "statement_tokenize"},
        )

    def test_import_policy_audit_emits_machine_readable_inventory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="import-import_policy-audit-") as tmp:
            out_path = Path(tmp) / "audit.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(IMPORT_POLICY_SCRIPT),
                    "audit",
                    "--format",
                    "json",
                    "--output",
                    str(out_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertTrue(out_path.exists())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("violations", payload)
            self.assertIn("cycles", payload)
            self.assertIn("summary", payload)
            self.assertIsInstance(payload["violations"], list)
            self.assertIsInstance(payload["cycles"], list)
            first_wave_blockers = [
                row
                for row in payload["violations"]
                if bool(row.get("firstWave"))
                and str(row.get("rule") or "")
                in {"ALIAS_FROM_IMPORT", "MESH_RELATIVE_IMPORT", "WILDCARD_IMPORT", "REEXPORT_DYNAMIC"}
            ]
            self.assertEqual(first_wave_blockers, [])

if __name__ == "__main__":
    unittest.main()


