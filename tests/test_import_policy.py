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
        self.assertTrue(
            import_policy._dynamic_reexport_detected(
                "app.impl.workspace.context_ui",
                source,
            )
        )
        self.assertFalse(
            import_policy._dynamic_reexport_detected(
                "tests.scripts.import_policy",
                source,
            )
        )

    def test_layer_policy_is_derived_from_application_topology(self) -> None:
        self.assertIsNone(
            import_policy._layer_violation(
                "app.route.problem_route",
                "app.impl.problem",
            )
        )
        self.assertEqual(
            import_policy._layer_violation(
                "app.service.problem.readiness",
                "app.impl.problem",
            ),
            "layer `service` cannot import `app.impl.problem`",
        )

    def test_cycle_detector_covers_arbitrary_application_modules(self) -> None:
        self.assertEqual(
            import_policy._cycle_signatures(
                {
                    "app.new.alpha": {"app.new.beta"},
                    "app.new.beta": {"app.new.alpha"},
                    "app.unrelated": set(),
                }
            ),
            [["app.new.alpha", "app.new.beta"]],
        )

    def test_import_policy_audit_emits_complete_inventory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="import-policy-audit-") as temporary:
            output_path = Path(temporary) / "audit.json"
            process = subprocess.run(
                [
                    sys.executable,
                    str(IMPORT_POLICY_SCRIPT),
                    "audit",
                    "--format",
                    "json",
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 0, msg=process.stderr or process.stdout)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertIn("violations", payload)
            self.assertIn("cycles", payload)
            self.assertIn("summary", payload)
            self.assertNotIn("firstWave", payload)
            self.assertNotIn("boundaries", payload)
            self.assertEqual(
                int(payload["meta"]["applicationModuleCount"]),
                len(list((ROOT / "app").rglob("*.py"))),
            )

    def test_import_policy_check_needs_no_configuration(self) -> None:
        process = subprocess.run(
            [sys.executable, str(IMPORT_POLICY_SCRIPT), "check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, msg=process.stderr or process.stdout)
        self.assertIn("complete app graph is cycle-free", process.stdout)


if __name__ == "__main__":
    unittest.main()
