from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.runtime_values import build_runtime_values
from app.services.sandbox.native_backend import NativeSandboxBackend
from app.services.toolchain_service import (
    TOOLCHAIN_JAVA_JAVAC_FLAGS,
    ToolchainService,
    apply_runtime_values,
)


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

    def test_compile_program_java_forwards_bounded_javac_vm_flags(self) -> None:
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
        captured: dict[str, object] = {}

        def _fake_compile_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return 0, "", ""

        self.service._compile_cmd = _fake_compile_cmd  # type: ignore[method-assign]
        ok, stdout_text, stderr_text, _digest = self.service.compile_program(source, output, include_dirs=[])

        self.assertTrue(ok, f"java compile failed\nstdout={stdout_text}\nstderr={stderr_text}")
        cmd = captured.get("cmd")
        self.assertIsInstance(cmd, list)
        self.assertTrue(cmd)
        self.assertEqual(str(cmd[0]), "javac")
        for flag in TOOLCHAIN_JAVA_JAVAC_FLAGS:
            self.assertIn(f"-J{flag}", cmd)

    def test_compile_command_executables_follow_runtime_config(self) -> None:
        source_py = self.root / "hello.py"
        source_py.write_text("print('OK')\n", encoding="utf-8")
        source_java = self.root / "Main.java"
        source_java.write_text("public class Main { public static void main(String[] args) {} }\n", encoding="utf-8")
        source_cpp = self.root / "main.cpp"
        source_cpp.write_text("int main(){return 0;}\n", encoding="utf-8")
        output = self.root / "run-bin"
        captures: list[list[str]] = []

        def _fake_compile_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
            captures.append(list(cmd))
            return 0, "", ""

        self.service._compile_cmd = _fake_compile_cmd  # type: ignore[method-assign]
        apply_runtime_values(
            build_runtime_values(
                {
                    "TOOLCHAIN_CPP_COMPILER": "cxx-custom",
                    "TOOLCHAIN_PYTHON_EXECUTABLE": "python-custom",
                    "TOOLCHAIN_JAVA_COMPILER": "javac-custom",
                }
            )
        )
        self.addCleanup(lambda: apply_runtime_values(build_runtime_values()))

        ok_py, _, _, _ = self.service.compile_program(source_py, output, include_dirs=[])
        ok_java, _, _, _ = self.service.compile_program(source_java, output, include_dirs=[])
        ok_cpp, _, _, _ = self.service.compile_program(source_cpp, output, include_dirs=[])
        self.assertTrue(ok_py)
        self.assertTrue(ok_java)
        self.assertTrue(ok_cpp)

        self.assertGreaterEqual(len(captures), 3)
        self.assertEqual(str(captures[0][0]), "python-custom")
        self.assertEqual(str(captures[1][0]), "javac-custom")
        self.assertEqual(str(captures[2][0]), "cxx-custom")


if __name__ == "__main__":
    unittest.main()
