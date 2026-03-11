from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
from time import monotonic

from app.service.sandbox.base import ExecResult, ExecSpec, SandboxBackend


class NativeSandboxBackend(SandboxBackend):
    name = "native-sandbox"
    _SCMP_ACT_ALLOW = 0x7FFF0000
    _SCMP_ACT_ERRNO = 0x00050000
    _SANDBOX_ERROR_EXIT = 197
    _NETWORK_POLICY = "deny-all"
    _NETWORK_SYSCALLS = (
        "connect",
        "accept",
        "accept4",
        "bind",
        "listen",
        "sendto",
        "sendmsg",
        "sendmmsg",
        "recvfrom",
        "recvmsg",
        "recvmmsg",
        "shutdown",
    )
    _ROOT_SWITCH_SYSTEM_DIRS = (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc",
        # TeX Live keeps prebuilt formats (for example pdflatex.fmt) here.
        # Without this mount, pdflatex inside root-switch sandbox may fail
        # with "I can't find the format file `pdflatex.fmt'".
        "/var/lib/texmf",
    )
    _TEX_COMMAND_BASENAMES = {
        "latex",
        "pdflatex",
        "xelatex",
        "lualatex",
    }

    def __init__(self, *, root_switch_tool: str = "bwrap") -> None:
        self._seccomp = self._load_seccomp()
        if self._seccomp is None:
            raise RuntimeError("native-sandbox requires libseccomp (network policy: deny-all)")
        self._root_switch_mode = "required"
        self._root_switch_tool = ""
        self._configured_root_switch_tool = str(root_switch_tool or "").strip()
        self._root_switch_enabled = False
        self._root_switch_status = "disabled"
        self._configure_root_switch()

    def _load_seccomp(self):
        lib_path = ctypes.util.find_library("seccomp") or "libseccomp.so.2"
        try:
            lib = ctypes.CDLL(lib_path)
        except OSError:
            return None
        lib.seccomp_init.argtypes = [ctypes.c_uint32]
        lib.seccomp_init.restype = ctypes.c_void_p
        lib.seccomp_release.argtypes = [ctypes.c_void_p]
        lib.seccomp_release.restype = None
        lib.seccomp_load.argtypes = [ctypes.c_void_p]
        lib.seccomp_load.restype = ctypes.c_int
        lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        lib.seccomp_rule_add.restype = ctypes.c_int
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
        return lib

    def _install_network_filter(self) -> None:
        if self._seccomp is None:
            raise RuntimeError("libseccomp is unavailable")
        lib = self._seccomp
        ctx = lib.seccomp_init(self._SCMP_ACT_ALLOW)
        if not ctx:
            raise RuntimeError("seccomp_init failed")
        try:
            deny_action = self._SCMP_ACT_ERRNO | (errno.EPERM & 0xFFFF)
            for name in self._NETWORK_SYSCALLS:
                num = int(lib.seccomp_syscall_resolve_name(name.encode("ascii")))
                if num < 0:
                    continue
                rc = int(lib.seccomp_rule_add(ctx, deny_action, num, 0))
                if rc != 0:
                    raise RuntimeError(f"seccomp_rule_add failed for {name}: {rc}")
            rc = int(lib.seccomp_load(ctx))
            if rc != 0:
                raise RuntimeError(f"seccomp_load failed: {rc}")
        finally:
            lib.seccomp_release(ctx)

    def _result_details(self, *, root_switched: bool | None = None) -> dict[str, object]:
        details: dict[str, object] = {
            "network_enforced": True,
            "network_policy": self._NETWORK_POLICY,
            "root_switch_mode": self._root_switch_mode,
            "root_switch_enabled": self._root_switch_enabled,
        }
        if self._root_switch_tool:
            details["root_switch_tool"] = self._root_switch_tool
        if self._root_switch_status:
            details["root_switch_status"] = self._root_switch_status
        if root_switched is not None:
            details["root_switched"] = bool(root_switched)
        return details

    def _resolve_root_switch_tool(self) -> str:
        raw = str(self._configured_root_switch_tool or "").strip()
        if not raw:
            return ""
        if "/" in raw:
            tool = Path(raw)
            if tool.exists() and os.access(tool, os.X_OK):
                return str(tool)
            return ""
        found = shutil.which(raw)
        return str(found or "")

    def _probe_root_switch(self, tool: str) -> tuple[bool, str]:
        probe_cmd = [
            str(tool),
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--chdir",
            "/",
            "--",
            "/bin/sh",
            "-lc",
            "true",
        ]
        try:
            completed = subprocess.run(
                probe_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
            )
        except FileNotFoundError:
            return False, "tool not found"
        except Exception as exc:
            return False, str(exc)
        if int(completed.returncode) == 0:
            return True, "ok"
        stderr_text = str(completed.stderr or "").strip()
        if not stderr_text:
            stderr_text = f"exit {completed.returncode}"
        return False, stderr_text

    def _configure_root_switch(self) -> None:
        tool = self._resolve_root_switch_tool()
        if not tool:
            raise RuntimeError("native-sandbox root switch required: bwrap is not available in PATH")
        ok, detail = self._probe_root_switch(tool)
        if ok:
            self._root_switch_tool = tool
            self._root_switch_enabled = True
            self._root_switch_status = "enabled"
            return
        raise RuntimeError(f"native-sandbox root switch required: bwrap probe failed: {detail}")

    def _normalize_absolute_path(self, path: Path) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p)
        return Path(os.path.abspath(str(p)))

    def _nearest_existing_dir(self, path: Path) -> Path | None:
        candidate = self._normalize_absolute_path(path)
        if candidate.exists() and candidate.is_file():
            candidate = candidate.parent
        while True:
            try:
                if candidate.exists():
                    if candidate.is_file():
                        candidate = candidate.parent
                    if candidate.exists() and candidate.is_dir():
                        return candidate
            except OSError:
                pass
            parent = candidate.parent
            if parent == candidate:
                return None
            candidate = parent

    def _add_mount_path(self, mounts: dict[str, bool], path: Path, *, writable: bool) -> None:
        if not str(path).startswith("/"):
            return
        mount_dir = self._nearest_existing_dir(path)
        if mount_dir is None:
            return
        key = str(mount_dir)
        if key == "/":
            return
        mounts[key] = bool(mounts.get(key, False) or writable)

    def _collapse_mounts(self, mounts: dict[str, bool]) -> list[tuple[str, bool]]:
        entries = sorted(mounts.items(), key=lambda item: (item[0].count("/"), item[0]))
        result: list[tuple[str, bool]] = []
        for path, writable in entries:
            covered = False
            for parent, parent_writable in result:
                prefix = parent.rstrip("/") + "/"
                if path == parent or path.startswith(prefix):
                    if parent_writable or not writable:
                        covered = True
                    break
            if covered:
                continue
            result.append((path, writable))
        return result

    def _bwrap_dir_setup_args(self, mounts: list[tuple[str, bool]], working_dir: Path) -> list[str]:
        dirs: set[str] = set()
        for mount_path, _ in mounts:
            current = Path(mount_path).parent
            while str(current) != "/":
                dirs.add(str(current))
                current = current.parent
        current = Path(working_dir)
        while str(current) != "/":
            dirs.add(str(current))
            current = current.parent
        ordered = sorted(dirs, key=lambda item: (item.count("/"), item))
        args: list[str] = []
        for item in ordered:
            args.extend(["--dir", item])
        return args

    def _spec_mounts(self, spec: ExecSpec, working_dir: Path) -> list[tuple[str, bool]]:
        mounts: dict[str, bool] = {}
        for raw in self._ROOT_SWITCH_SYSTEM_DIRS:
            candidate = Path(raw)
            if candidate.exists() and candidate.is_dir():
                self._add_mount_path(mounts, candidate, writable=False)
        if self._is_tex_command(spec):
            for candidate in self._optional_tex_runtime_dirs(spec):
                self._add_mount_path(mounts, candidate, writable=False)
        self._add_mount_path(mounts, working_dir, writable=True)
        if spec.stdin_path is not None:
            self._add_mount_path(mounts, spec.stdin_path, writable=False)
        if spec.stdout_path is not None:
            self._add_mount_path(mounts, spec.stdout_path.parent, writable=True)
        for token in spec.command:
            raw = str(token or "").strip()
            if not raw.startswith("/"):
                continue
            candidate = self._normalize_absolute_path(Path(raw))
            # Executable paths only need read/execute access. Keep them read-only
            # to avoid granting accidental write access outside explicit work dirs.
            self._add_mount_path(mounts, candidate, writable=False)
        if spec.env:
            for key in ("FEEDBACK_DIR", "TMPDIR", "TEMP", "TMP"):
                raw = str(spec.env.get(key) or "").strip()
                if raw.startswith("/"):
                    self._add_mount_path(mounts, Path(raw), writable=True)
        return self._collapse_mounts(mounts)

    def _is_tex_command(self, spec: ExecSpec) -> bool:
        if not spec.command:
            return False
        first = str(spec.command[0] or "").strip()
        if not first:
            return False
        return Path(first).name.lower() in self._TEX_COMMAND_BASENAMES

    def _optional_tex_runtime_dirs(self, spec: ExecSpec) -> list[Path]:
        env_map: dict[str, str] = {}
        try:
            env_map.update({str(k): str(v) for k, v in os.environ.items()})
        except Exception:
            env_map = {}
        if spec.env:
            for k, v in spec.env.items():
                key = str(k or "").strip()
                if not key:
                    continue
                env_map[key] = str(v or "")
        candidates: list[Path] = []
        home = Path(os.path.expanduser("~"))
        candidates.append(home / "texmf")
        try:
            for item in sorted(home.glob(".texlive*")):
                if item.exists() and item.is_dir() and (not item.is_symlink()):
                    candidates.append(item)
        except OSError:
            pass
        for key in ("TEXMFHOME", "TEXMFCONFIG", "TEXMFVAR", "TEXMFCACHE"):
            raw = str(env_map.get(key) or "").strip()
            if not raw:
                continue
            for token in raw.split(os.pathsep):
                safe = str(token or "").strip()
                if not safe:
                    continue
                safe = safe.lstrip("!").strip("{}")
                if not safe.startswith("/"):
                    continue
                candidates.append(Path(safe))
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            try:
                if (not path.exists()) or (not path.is_dir()) or path.is_symlink():
                    continue
            except OSError:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _effective_working_dir(self, spec: ExecSpec) -> Path:
        if spec.cwd is not None:
            return self._normalize_absolute_path(spec.cwd)
        return self._normalize_absolute_path(Path.cwd())

    def _root_switched_command(self, spec: ExecSpec) -> tuple[list[str], Path]:
        if not self._root_switch_enabled or not self._root_switch_tool:
            raise RuntimeError("root switch is not enabled")
        working_dir = self._effective_working_dir(spec)
        mounts = self._spec_mounts(spec, working_dir)
        if not mounts:
            raise RuntimeError("root switch mount plan is empty")
        cmd = [
            self._root_switch_tool,
            "--die-with-parent",
            "--new-session",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            *self._bwrap_dir_setup_args(mounts, working_dir),
        ]
        for mount_path, writable in mounts:
            cmd.extend(["--bind" if writable else "--ro-bind", mount_path, mount_path])
        cmd.extend(["--chdir", str(working_dir), "--"])
        cmd.extend(str(x) for x in spec.command)
        return cmd, working_dir

    def _prepared_command(self, spec: ExecSpec) -> tuple[list[str], Path | None, bool]:
        try:
            wrapped, _working_dir = self._root_switched_command(spec)
            return wrapped, None, True
        except Exception as exc:
            raise RuntimeError(f"root switch required but failed to prepare command: {exc}") from exc

    def _uid_process_count(self, uid: int) -> int:
        total = 0
        try:
            with os.scandir("/proc") as it:
                for entry in it:
                    if not entry.name.isdigit():
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if int(st.st_uid) == int(uid):
                        task_dir = f"/proc/{entry.name}/task"
                        counted = 0
                        try:
                            with os.scandir(task_dir) as tasks:
                                for task_entry in tasks:
                                    if task_entry.name.isdigit():
                                        counted += 1
                        except OSError:
                            counted = 0
                        total += counted if counted > 0 else 1
        except OSError:
            return 0
        return total

    def _effective_nproc_limit(self, process_limit: int) -> int:
        requested = max(1, int(process_limit))
        try:
            current = self._uid_process_count(os.getuid())
        except Exception:
            current = 0
        if current <= 0:
            return requested
        # RLIMIT_NPROC is scoped to the whole UID. Preserve requested headroom for
        # this sandboxed process in addition to already-running UID processes.
        reserve = 8
        return max(requested, current + requested + reserve)

    def _preexec_for_spec(self, spec: ExecSpec):
        timeout_sec = max(1, int(spec.timeout_sec))
        memory_mb = int(spec.memory_mb) if spec.memory_mb is not None else None
        process_limit = int(spec.process_limit) if spec.process_limit is not None else None
        output_kb = int(spec.output_kb) if spec.output_kb is not None else None

        def _apply_limits() -> None:
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_CPU, (timeout_sec, timeout_sec + 1))
            if memory_mb is not None:
                as_limit = max(16, memory_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
            if process_limit is not None:
                nproc = self._effective_nproc_limit(process_limit)
                resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            if output_kb is not None:
                fsize = max(64, output_kb) * 1024
                resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
            # Network is always denied inside native-sandbox.
            try:
                self._install_network_filter()
            except Exception:
                os._exit(self._SANDBOX_ERROR_EXIT)

        return _apply_limits

    def _status_from_returncode(self, returncode: int | None) -> str:
        if returncode is None:
            return "re"
        if returncode == 0:
            return "ok"
        if returncode < 0:
            sig = -returncode
            if sig in {signal.SIGXCPU, signal.SIGKILL, signal.SIGALRM}:
                return "tle"
        return "re"

    def run(self, spec: ExecSpec) -> ExecResult:
        start = monotonic()
        proc: subprocess.Popen | None = None
        stdin_fh = None
        stdout_fh = None
        root_switched = False
        try:
            if spec.stdin_path is not None:
                stdin_fh = spec.stdin_path.open("rb")
            if spec.stdout_path is not None:
                spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_fh = spec.stdout_path.open("wb")
            try:
                command, host_cwd, root_switched = self._prepared_command(spec)
            except Exception as exc:
                elapsed = int((monotonic() - start) * 1000)
                return ExecResult(
                    backend=self.name,
                    status="sandbox_error",
                    returncode=None,
                    elapsed_ms=elapsed,
                    timed_out=False,
                    stdout="",
                    stderr=str(exc),
                    details=self._result_details(root_switched=False),
                )

            proc = subprocess.Popen(
                command,
                cwd=host_cwd,
                env=spec.env,
                stdin=stdin_fh,
                stdout=stdout_fh if stdout_fh is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                preexec_fn=self._preexec_for_spec(spec),
            )
            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=max(1, int(spec.timeout_sec)) + 1)
                elapsed = int((monotonic() - start) * 1000)
                stdout_text = "" if stdout_fh is not None else (stdout_raw or b"").decode("utf-8", errors="replace")
                stderr_text = (stderr_raw or b"").decode("utf-8", errors="replace")
                if int(proc.returncode or 0) == self._SANDBOX_ERROR_EXIT:
                    status = "sandbox_error"
                else:
                    status = self._status_from_returncode(proc.returncode)
                return ExecResult(
                    backend=self.name,
                    status=status,
                    returncode=proc.returncode,
                    elapsed_ms=elapsed,
                    timed_out=False,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    details=self._result_details(root_switched=root_switched),
                )
            except subprocess.TimeoutExpired:
                if proc is not None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
                    try:
                        proc.communicate(timeout=2)
                    except Exception:
                        pass
                elapsed = int((monotonic() - start) * 1000)
                return ExecResult(
                    backend=self.name,
                    status="tle",
                    returncode=None,
                    elapsed_ms=elapsed,
                    timed_out=True,
                    details=self._result_details(root_switched=root_switched),
                )
        finally:
            if proc is not None:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass
                try:
                    if proc.stderr is not None:
                        proc.stderr.close()
                except Exception:
                    pass
            if stdin_fh is not None:
                stdin_fh.close()
            if stdout_fh is not None:
                stdout_fh.close()

    def popen_command(self, spec: ExecSpec) -> list[str]:
        command, _host_cwd, _root_switched = self._prepared_command(spec)
        return command

    def popen(self, spec: ExecSpec, **kwargs) -> subprocess.Popen:
        command, host_cwd, _root_switched = self._prepared_command(spec)
        if "cwd" not in kwargs and host_cwd is not None:
            kwargs["cwd"] = host_cwd
        if "env" not in kwargs and spec.env is not None:
            kwargs["env"] = spec.env
        existing_preexec = kwargs.get("preexec_fn")
        sandbox_preexec = self._preexec_for_spec(spec)

        if existing_preexec is not None:
            def _combined_preexec() -> None:
                sandbox_preexec()
                existing_preexec()

            kwargs["preexec_fn"] = _combined_preexec
        else:
            kwargs["preexec_fn"] = sandbox_preexec

        return subprocess.Popen(command, **kwargs)
