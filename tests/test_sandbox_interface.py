from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app.services.sandbox import ExecSpec, NativeSandboxBackend


class TestSandboxInterface(unittest.TestCase):
    def _patched_backend(self) -> NativeSandboxBackend:
        with patch.object(NativeSandboxBackend, "_resolve_root_switch_tool", return_value="/usr/bin/bwrap"):
            with patch.object(NativeSandboxBackend, "_probe_root_switch", return_value=(True, "ok")):
                return NativeSandboxBackend()

    def _real_backend_or_skip(self) -> NativeSandboxBackend:
        try:
            return NativeSandboxBackend()
        except RuntimeError as exc:
            self.skipTest(f"native-sandbox unavailable in this environment: {exc}")

    def test_native_backend_default_name(self) -> None:
        with patch.object(NativeSandboxBackend, "_resolve_root_switch_tool", return_value="/usr/bin/bwrap"):
            with patch.object(NativeSandboxBackend, "_probe_root_switch", return_value=(True, "ok")):
                backend = NativeSandboxBackend()
        self.assertEqual(backend.name, "native-sandbox")

    def test_native_backend_root_switch_required_rejects_unavailable(self) -> None:
        with patch.object(NativeSandboxBackend, "_resolve_root_switch_tool", return_value="/usr/bin/bwrap"):
            with patch.object(NativeSandboxBackend, "_probe_root_switch", return_value=(False, "permission denied")):
                with self.assertRaises(RuntimeError):
                    NativeSandboxBackend()

    def test_native_backend_popen_command_uses_root_switch_wrapper(self) -> None:
        backend = self._patched_backend()
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir) / "work"
            work.mkdir(parents=True, exist_ok=True)
            input_path = work / "input.txt"
            input_path.write_text("hello\n", encoding="utf-8")
            output_path = work / "output.txt"
            cmd = backend.popen_command(
                ExecSpec(
                    command=["/bin/sh", "-lc", "cat"],
                    cwd=work,
                    stdin_path=input_path,
                    stdout_path=output_path,
                )
            )
        self.assertGreaterEqual(len(cmd), 4)
        self.assertEqual(cmd[0], "/usr/bin/bwrap")
        self.assertIn("--chdir", cmd)
        self.assertIn(str(work), cmd)
        self.assertIn("--", cmd)
        self.assertEqual(str(cmd[-3]), "/bin/sh")

    def test_native_backend_mount_plan_marks_command_path_read_only(self) -> None:
        backend = self._patched_backend()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work = root / "work"
            work.mkdir(parents=True, exist_ok=True)
            tools = root / "tools"
            tools.mkdir(parents=True, exist_ok=True)
            runner = tools / "runner.sh"
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runner.chmod(0o755)
            spec = ExecSpec(command=[str(runner)], cwd=work)
            mounts = dict(backend._spec_mounts(spec, work))
        self.assertIn(str(work), mounts)
        self.assertIn(str(tools), mounts)
        self.assertTrue(bool(mounts[str(work)]))
        self.assertFalse(bool(mounts[str(tools)]))

    def test_native_backend_mount_plan_tracks_stdio_access_modes(self) -> None:
        backend = self._patched_backend()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work = root / "work"
            io_dir = root / "io"
            out_dir = root / "out"
            work.mkdir(parents=True, exist_ok=True)
            io_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            input_path = io_dir / "case.in"
            input_path.write_text("1\n", encoding="utf-8")
            output_path = out_dir / "case.out"
            spec = ExecSpec(
                command=["/bin/sh", "-lc", "cat"],
                cwd=work,
                stdin_path=input_path,
                stdout_path=output_path,
            )
            mounts = dict(backend._spec_mounts(spec, work))
        self.assertIn(str(work), mounts)
        self.assertIn(str(io_dir), mounts)
        self.assertIn(str(out_dir), mounts)
        self.assertTrue(bool(mounts[str(work)]))
        self.assertFalse(bool(mounts[str(io_dir)]))
        self.assertTrue(bool(mounts[str(out_dir)]))

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
            with patch("app.services.sandbox.native_backend.os.path.expanduser", return_value=str(root / "home")):
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

    def test_native_backend_prepared_command_reports_root_switch(self) -> None:
        backend = self._patched_backend()
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir) / "work"
            work.mkdir(parents=True, exist_ok=True)
            command, host_cwd, root_switched = backend._prepared_command(
                ExecSpec(command=["/bin/sh", "-lc", "true"], cwd=work)
            )
        self.assertTrue(root_switched)
        self.assertIsNone(host_cwd)
        self.assertGreater(len(command), 4)
        self.assertEqual(command[0], "/usr/bin/bwrap")

    def test_native_backend_run_returns_sandbox_error_on_prepare_failure(self) -> None:
        backend = self._patched_backend()
        with patch.object(backend, "_prepared_command", side_effect=RuntimeError("mount plan failed")):
            result = backend.run(ExecSpec(command=["/bin/echo", "ok"], timeout_sec=1))
        self.assertEqual(result.status, "sandbox_error")
        self.assertIn("mount plan failed", result.stderr)
        self.assertFalse(bool(result.details.get("root_switched")))

    def test_native_backend_runs_and_times_out(self) -> None:
        backend = self._real_backend_or_skip()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out = root / "ok.out"
            in_file = root / "ok.in"
            in_file.write_text("hello\n", encoding="utf-8")
            ok = backend.run(
                ExecSpec(
                    command=["/bin/sh", "-lc", "cat"],
                    stdin_path=in_file,
                    stdout_path=out,
                    timeout_sec=2,
                )
            )
            self.assertFalse(ok.timed_out)
            self.assertEqual(ok.status, "ok")
            self.assertTrue(out.exists())

            slow = backend.run(
                ExecSpec(
                    command=["/bin/sh", "-lc", "sleep 2"],
                    timeout_sec=1,
                )
            )
            self.assertTrue(slow.timed_out)
            self.assertEqual(slow.status, "tle")

    def test_native_backend_status_mapping(self) -> None:
        backend = self._patched_backend()
        self.assertEqual(backend._status_from_returncode(0), "ok")
        self.assertEqual(backend._status_from_returncode(1), "re")

    def test_native_backend_nproc_limit_accounts_for_existing_uid_processes(self) -> None:
        backend = self._patched_backend()
        with patch.object(backend, "_uid_process_count", return_value=240):
            self.assertEqual(backend._effective_nproc_limit(8), 256)
        with patch.object(backend, "_uid_process_count", return_value=0):
            self.assertEqual(backend._effective_nproc_limit(8), 8)

    def test_native_backend_blocks_network_always(self) -> None:
        backend = self._real_backend_or_skip()
        probe = (
            "import errno, socket, sys\n"
            "try:\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    s.settimeout(0.2)\n"
            "    s.connect(('1.1.1.1', 53))\n"
            "except OSError as exc:\n"
            "    sys.exit(0 if exc.errno in (errno.EPERM, errno.EACCES) else 3)\n"
            "sys.exit(2)\n"
        )
        blocked = backend.run(
            ExecSpec(
                command=["python3", "-c", probe],
                timeout_sec=3,
            )
        )
        self.assertEqual(blocked.status, "ok")
        self.assertEqual(int(blocked.returncode or 0), 0)
        self.assertTrue(bool(blocked.details.get("network_enforced")))
        self.assertEqual(str(blocked.details.get("network_policy")), "deny-all")

        blocked_repeat = backend.run(
            ExecSpec(
                command=["python3", "-c", probe],
                timeout_sec=3,
            )
        )
        self.assertEqual(blocked_repeat.status, "ok")
        self.assertEqual(int(blocked_repeat.returncode or 0), 0)
        self.assertTrue(bool(blocked_repeat.details.get("network_enforced")))
        self.assertEqual(str(blocked_repeat.details.get("network_policy")), "deny-all")
