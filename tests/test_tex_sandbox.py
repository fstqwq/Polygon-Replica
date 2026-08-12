import resource
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from app.service.sandbox.base import ExecSpec
from app.service.sandbox.tex_backend import TexSandboxBackend


@unittest.skipUnless(shutil.which("bwrap") and shutil.which("pdflatex"), "bwrap and pdflatex are required")
class TestTexSandbox(unittest.TestCase):
    def test_preexec_for_spec_does_not_set_nproc_limit(self) -> None:
        backend = object.__new__(TexSandboxBackend)
        spec = ExecSpec(
            command=["true"],
            timeout_sec=20,
            memory_mb=1024,
            process_limit=64,
            output_kb=131072,
        )
        preexec = backend._preexec_for_spec(spec)
        with patch("app.service.sandbox.tex_backend.os.setsid") as setsid_mock, patch(
            "app.service.sandbox.tex_backend.resource.setrlimit"
        ) as setrlimit_mock:
            preexec()
        setsid_mock.assert_called_once_with()
        self.assertEqual(
            setrlimit_mock.call_args_list,
            [
                call(resource.RLIMIT_CORE, (0, 0)),
                call(resource.RLIMIT_CPU, (20, 21)),
                call(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024)),
                call(resource.RLIMIT_FSIZE, (131072 * 1024, 131072 * 1024)),
            ],
        )

    def test_tex_sandbox_compiles_in_root_switched_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="polygon-replica-tex-sandbox-") as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "main.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "ok\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            backend = TexSandboxBackend()
            result = backend.run(
                ExecSpec(
                    command=["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                    cwd=workdir,
                    timeout_sec=20,
                    memory_mb=1024,
                    process_limit=64,
                    output_kb=131072,
                )
            )
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.returncode, 0)
            self.assertTrue((workdir / "main.pdf").is_file())
            self.assertTrue(bool(result.details.get("root_switched")))

    def test_tex_sandbox_blocks_parent_include_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="polygon-replica-tex-sandbox-") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir(parents=True, exist_ok=True)
            (root / "secret.tex").write_text("ESCAPED\n", encoding="utf-8")
            (workdir / "main.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "\\input{../secret.tex}\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            backend = TexSandboxBackend()
            result = backend.run(
                ExecSpec(
                    command=["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                    cwd=workdir,
                    timeout_sec=20,
                    memory_mb=1024,
                    process_limit=64,
                    output_kb=131072,
                )
            )
            self.assertEqual(result.status, "error")
            self.assertFalse((workdir / "main.pdf").exists())
            merged = f"{result.stdout}\n{result.stderr}".lower()
            self.assertIn("secret.tex", merged)

    def test_tex_sandbox_blocks_parent_include_command_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="polygon-replica-tex-sandbox-") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir(parents=True, exist_ok=True)
            (root / "secret.tex").write_text("ESCAPED\n", encoding="utf-8")
            (workdir / "main.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "\\include{../secret}\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            backend = TexSandboxBackend()
            result = backend.run(
                ExecSpec(
                    command=["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                    cwd=workdir,
                    timeout_sec=20,
                    memory_mb=1024,
                    process_limit=64,
                    output_kb=131072,
                )
            )
            self.assertEqual(result.status, "error")
            self.assertFalse((workdir / "main.pdf").exists())
            merged = f"{result.stdout}\n{result.stderr}".lower()
            self.assertIn("secret", merged)

    def test_tex_sandbox_allows_parent_include_with_explicit_extra_mount(self) -> None:
        with tempfile.TemporaryDirectory(prefix="polygon-replica-tex-sandbox-") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir(parents=True, exist_ok=True)
            (root / "shared.tex").write_text("VISIBLE\n", encoding="utf-8")
            (workdir / "main.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "\\input{../shared.tex}\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            backend = TexSandboxBackend()
            result = backend.run(
                ExecSpec(
                    command=["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                    cwd=workdir,
                    extra_mounts=(root,),
                    timeout_sec=20,
                    memory_mb=1024,
                    process_limit=64,
                    output_kb=131072,
                )
            )
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.returncode, 0)
            self.assertTrue((workdir / "main.pdf").is_file())
