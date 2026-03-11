from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app.service.sandbox.base import ExecSpec
from app.service.sandbox.native_backend import NativeSandboxBackend


class TestSandboxInterface(unittest.TestCase):
    def _patched_backend(self) -> NativeSandboxBackend:
        with patch.object(NativeSandboxBackend, "_resolve_root_switch_tool", return_value="/usr/bin/bwrap"):
            with patch.object(NativeSandboxBackend, "_probe_root_switch", return_value=(True, "ok")):
                return NativeSandboxBackend()

    def test_native_backend_mount_plan_includes_system_texmf_path_read_only(self) -> None:
        backend = self._patched_backend()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work = root / "work"
            texmf = root / "var" / "lib" / "texmf"
            work.mkdir(parents=True, exist_ok=True)
            texmf.mkdir(parents=True, exist_ok=True)
            backend._ROOT_SWITCH_SYSTEM_DIRS = (str(texmf),)
            mounts = dict(backend._spec_mounts(ExecSpec(command=["/bin/sh", "-lc", "true"], cwd=work), work))
        self.assertIn(str(texmf), mounts)
        self.assertFalse(bool(mounts[str(texmf)]))
        self.assertIn(str(work), mounts)
        self.assertTrue(bool(mounts[str(work)]))

    def test_native_backend_mount_plan_includes_texmfhome_for_pdflatex(self) -> None:
        backend = self._patched_backend()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work = root / "work"
            texmfhome = root / "home" / "texmf"
            work.mkdir(parents=True, exist_ok=True)
            texmfhome.mkdir(parents=True, exist_ok=True)
            with patch("app.service.sandbox.native_backend.os.path.expanduser", return_value=str(root / "home")):
                mounts = dict(
                    backend._spec_mounts(
                        ExecSpec(command=["pdflatex", "main.tex"], cwd=work),
                        work,
                    )
                )
        self.assertIn(str(texmfhome), mounts)
        self.assertFalse(bool(mounts[str(texmfhome)]))
        self.assertIn(str(work), mounts)
        self.assertTrue(bool(mounts[str(work)]))

    def test_native_backend_mount_plan_uses_tex_env_paths_for_pdflatex(self) -> None:
        backend = self._patched_backend()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work = root / "work"
            texmfhome = root / "custom" / "texmfhome"
            work.mkdir(parents=True, exist_ok=True)
            texmfhome.mkdir(parents=True, exist_ok=True)
            mounts = dict(
                backend._spec_mounts(
                    ExecSpec(
                        command=["pdflatex", "main.tex"],
                        cwd=work,
                        env={"TEXMFHOME": str(texmfhome)},
                    ),
                    work,
                )
            )
        self.assertIn(str(texmfhome), mounts)
        self.assertFalse(bool(mounts[str(texmfhome)]))


if __name__ == "__main__":
    unittest.main()
