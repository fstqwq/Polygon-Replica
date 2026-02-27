from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services.sandbox.native_backend import NativeSandboxBackend
from app.services.toolchain_service import ToolchainService


class TestToolchainLanguages(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = ToolchainService(self.root, sandbox_backend=NativeSandboxBackend())
        self.service.cache_cleanup_interval_sec = 0

    def test_compile_program_python_creates_runnable_launcher(self) -> None:
        source = self.root / "hello.py"
        source.write_text("print('PY_OK')\n", encoding="utf-8")
        output = self.root / "run-python"
        ok, stdout_text, stderr_text, _digest = self.service.compile_program(source, output, include_dirs=[])
        self.assertTrue(ok, f"python compile failed\nstdout={stdout_text}\nstderr={stderr_text}")
        self.assertTrue(output.exists())
        self.assertTrue(bool(output.stat().st_mode & 0o111))
        run = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("PY_OK", run.stdout)

    def test_compile_program_java_creates_runnable_launcher(self) -> None:
        if shutil.which("javac") is None or shutil.which("java") is None:
            self.skipTest("javac/java is not available")
        source = self.root / "Main.java"
        source.write_text(
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            "        System.out.println(\"JAVA_OK\");\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        output = self.root / "run-java"
        ok, stdout_text, stderr_text, _digest = self.service.compile_program(source, output, include_dirs=[])
        self.assertTrue(ok, f"java compile failed\nstdout={stdout_text}\nstderr={stderr_text}")
        self.assertTrue(output.exists())
        self.assertTrue(bool(output.stat().st_mode & 0o111))
        run = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("JAVA_OK", run.stdout)
        constrained = subprocess.run(
            ["bash", "-lc", f"ulimit -v 1048576; {str(output)}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(constrained.returncode, 0, constrained.stderr)
        self.assertIn("JAVA_OK", constrained.stdout)


if __name__ == "__main__":
    unittest.main()
