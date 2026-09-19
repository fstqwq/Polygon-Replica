import shutil
import subprocess
import unittest
from pathlib import Path


class TestBrowserInteractions(unittest.TestCase):
    def test_real_modules_with_controlled_dom_and_fetch(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the executor browser harness")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "--experimental-vm-modules", "--test", "tests/browser/run_details.test.mjs"],
            cwd=root, text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
