from __future__ import annotations

import os
import resource
import shutil
import subprocess
from pathlib import Path
from time import monotonic

from app.service.sandbox.base import ExecResult, ExecSpec, SandboxBackend


class TexSandboxBackend(SandboxBackend):
    name = "tex-local-sandbox"
    _ROOT_SWITCH_SYSTEM_DIRS = (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc",
        "/var/lib/texmf",
    )

    def __init__(self, *, root_switch_tool: str = "bwrap") -> None:
        self._configured_root_switch_tool = str(root_switch_tool or "").strip()
        self._root_switch_tool = ""
        self._configure_root_switch()

    def _resolve_root_switch_tool(self) -> str:
        raw = str(self._configured_root_switch_tool or "").strip()
        if not raw:
            return ""
        if "/" in raw:
            tool = Path(raw)
            if tool.exists() and os.access(tool, os.X_OK):
                return str(tool)
            return ""
        return str(shutil.which(raw) or "")

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
                timeout=5,
                check=False,
            )
        except FileNotFoundError:
            return False, "tool not found"
        except Exception as exc:
            return False, str(exc)
        if int(completed.returncode or 0) == 0:
            return True, "ok"
        return False, str(completed.stderr or "").strip() or f"exit {completed.returncode}"

    def _configure_root_switch(self) -> None:
        tool = self._resolve_root_switch_tool()
        if not tool:
            raise RuntimeError("tex sandbox requires bwrap in PATH")
        ok, detail = self._probe_root_switch(tool)
        if not ok:
            raise RuntimeError(f"tex sandbox root switch probe failed: {detail}")
        self._root_switch_tool = tool

    def _normalize_absolute_path(self, path: Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return Path(os.path.abspath(str(candidate)))

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
        mount_dir = self._nearest_existing_dir(path)
        if mount_dir is None:
            return
        key = str(mount_dir)
        if key == "/":
            return
        mounts[key] = bool(mounts.get(key, False) or writable)

    def _collapse_mounts(self, mounts: dict[str, bool]) -> list[tuple[str, bool]]:
        ordered = sorted(mounts.items(), key=lambda item: (item[0].count("/"), item[0]))
        result: list[tuple[str, bool]] = []
        for path, writable in ordered:
            covered = False
            for parent, parent_writable in result:
                prefix = parent.rstrip("/") + "/"
                if path == parent or path.startswith(prefix):
                    if parent_writable or (not writable):
                        covered = True
                    break
            if not covered:
                result.append((path, writable))
        return result

    def _optional_tex_runtime_dirs(self, spec: ExecSpec) -> list[Path]:
        env_map: dict[str, str] = {}
        try:
            env_map.update({str(k): str(v) for k, v in os.environ.items()})
        except Exception:
            env_map = {}
        if spec.env:
            for k, v in spec.env.items():
                key = str(k).strip()
                if key:
                    env_map[key] = str(v)
        home = Path(os.path.expanduser("~"))
        candidates: list[Path] = [home / "texmf"]
        try:
            for item in sorted(home.glob(".texlive*")):
                if item.exists() and item.is_dir() and (not item.is_symlink()):
                    candidates.append(item)
        except OSError:
            pass
        for key in ("TEXMFHOME", "TEXMFCONFIG", "TEXMFVAR", "TEXMFCACHE"):
            raw = raw.strip() if isinstance(raw := env_map.get(key), str) else ""
            if not raw:
                continue
            for token in raw.split(os.pathsep):
                value = str(token or "").strip().lstrip("!").strip("{}")
                if value.startswith("/"):
                    candidates.append(Path(value))
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                if (not candidate.exists()) or (not candidate.is_dir()) or candidate.is_symlink():
                    continue
            except OSError:
                continue
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return unique

    def _spec_mounts(self, spec: ExecSpec, working_dir: Path) -> list[tuple[str, bool]]:
        mounts: dict[str, bool] = {}
        for raw in self._ROOT_SWITCH_SYSTEM_DIRS:
            candidate = Path(raw)
            if candidate.exists() and candidate.is_dir():
                self._add_mount_path(mounts, candidate, writable=False)
        for candidate in self._optional_tex_runtime_dirs(spec):
            self._add_mount_path(mounts, candidate, writable=False)
        self._add_mount_path(mounts, working_dir, writable=True)
        if spec.stdin_path is not None:
            self._add_mount_path(mounts, spec.stdin_path, writable=False)
        if spec.stdout_path is not None:
            self._add_mount_path(mounts, spec.stdout_path.parent, writable=True)
        return self._collapse_mounts(mounts)

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
        args: list[str] = []
        for item in sorted(dirs, key=lambda value: (value.count("/"), value)):
            args.extend(["--dir", item])
        return args

    def _prepared_command(self, spec: ExecSpec) -> tuple[list[str], Path]:
        working_dir = self._normalize_absolute_path(spec.cwd or Path.cwd())
        mounts = self._spec_mounts(spec, working_dir)
        if not mounts:
            raise RuntimeError("tex sandbox mount plan is empty")
        command = [
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
            command.extend(["--bind" if writable else "--ro-bind", mount_path, mount_path])
        command.extend(["--chdir", str(working_dir), "--"])
        command.extend(str(token) for token in spec.command)
        return command, working_dir

    def _effective_nproc_limit(self, process_limit: int) -> int:
        requested = max(1, int(process_limit))
        return max(requested, requested + 8)

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

        return _apply_limits

    def run(self, spec: ExecSpec) -> ExecResult:
        started = monotonic()
        command, host_cwd = self._prepared_command(spec)
        try:
            proc = subprocess.run(
                command,
                cwd=host_cwd,
                env=spec.env,
                capture_output=True,
                text=False,
                timeout=max(1, int(spec.timeout_sec)) + 1,
                check=False,
                preexec_fn=self._preexec_for_spec(spec),
            )
            status = "ok" if int(proc.returncode or 0) == 0 else "error"
            return ExecResult(
                backend=self.name,
                status=status,
                returncode=int(proc.returncode or 0),
                elapsed_ms=int((monotonic() - started) * 1000),
                timed_out=False,
                stdout=self._decode_output(proc.stdout, output_kb=spec.output_kb),
                stderr=self._decode_output(proc.stderr, output_kb=spec.output_kb),
                details={"root_switched": True, "root_switch_tool": self._root_switch_tool},
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                backend=self.name,
                status="tle",
                returncode=None,
                elapsed_ms=int((monotonic() - started) * 1000),
                timed_out=True,
                stdout=self._decode_output(exc.stdout, output_kb=spec.output_kb),
                stderr=self._decode_output(exc.stderr, output_kb=spec.output_kb),
                details={"root_switched": True, "root_switch_tool": self._root_switch_tool},
            )

    @staticmethod
    def _decode_output(raw: bytes | str | None, *, output_kb: int | None) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            text = raw
        else:
            text = raw.decode("utf-8", errors="replace")
        max_chars = max(1024, int(output_kb or 0) * 1024) if output_kb is not None else 1024 * 1024
        if len(text) > max_chars:
            return text[:max_chars]
        return text
