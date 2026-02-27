from __future__ import annotations

import fcntl
import hashlib
import os
import shlex
import shutil
import threading
import time
import uuid
from pathlib import Path

from app.runtime_values import RuntimeValues, build_runtime_values
from app.services.sandbox import ExecSpec, SandboxBackend, create_sandbox_backend
from app.services.util import sha256_file

TOOLCHAIN_CACHE_CLEANUP_LOCK: str = ".cleanup.lock"
TOOLCHAIN_CPP_CXXFLAGS: tuple[str, ...] = ("-O2", "-std=c++20", "-pipe", "-static")
TOOLCHAIN_INCLUDE_RE = None
TOOLCHAIN_JAVA_MAIN_CLASS_RE = None
TOOLCHAIN_JAVA_JAVAC_FLAGS: tuple[str, ...] = ("-XX:CompressedClassSpaceSize=128m",)
TOOLCHAIN_JAVA_RUNTIME_FLAGS: tuple[str, ...] = (
    "-XX:+UseSerialGC",
    "-XX:TieredStopAtLevel=1",
    "-XX:ActiveProcessorCount=1",
    "-Xss256k",
    "-XX:-UseCompressedClassPointers",
)
TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB = 256
TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB = 64
TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB = 16


def _apply_runtime_values(values: RuntimeValues) -> None:
    global TOOLCHAIN_CACHE_CLEANUP_LOCK
    global TOOLCHAIN_CPP_CXXFLAGS
    global TOOLCHAIN_INCLUDE_RE
    global TOOLCHAIN_JAVA_MAIN_CLASS_RE
    global TOOLCHAIN_JAVA_JAVAC_FLAGS
    global TOOLCHAIN_JAVA_RUNTIME_FLAGS
    global TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB
    global TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB
    global TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB
    TOOLCHAIN_CACHE_CLEANUP_LOCK = str(values.TOOLCHAIN_CACHE_CLEANUP_LOCK)
    TOOLCHAIN_CPP_CXXFLAGS = tuple(str(x) for x in values.TOOLCHAIN_CPP_CXXFLAGS)
    TOOLCHAIN_INCLUDE_RE = values.TOOLCHAIN_INCLUDE_RE
    TOOLCHAIN_JAVA_MAIN_CLASS_RE = values.TOOLCHAIN_JAVA_MAIN_CLASS_RE
    TOOLCHAIN_JAVA_JAVAC_FLAGS = tuple(str(x) for x in values.TOOLCHAIN_JAVA_JAVAC_FLAGS)
    TOOLCHAIN_JAVA_RUNTIME_FLAGS = tuple(str(x) for x in values.TOOLCHAIN_JAVA_RUNTIME_FLAGS)
    TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB = int(values.TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB)
    TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB = int(values.TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB)
    TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB = int(values.TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB)


def configure_runtime_values(values: RuntimeValues) -> None:
    _apply_runtime_values(values)


_apply_runtime_values(build_runtime_values())


class ToolchainService:
    def __init__(self, cache_root: Path, sandbox_backend: SandboxBackend | None = None):
        self.cache_root = cache_root / "compile"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.sandbox = sandbox_backend or create_sandbox_backend()
        self.compile_timeout_sec = self._env_int("POLYGONLIKE_COMPILE_TIMEOUT_SEC", default=120, min_value=5, max_value=1800)
        self.compile_memory_mb = self._env_int("POLYGONLIKE_COMPILE_MEMORY_MB", default=2048, min_value=64, max_value=262144)
        # RLIMIT_NPROC is UID-scoped and can cause compiler fork failures on busy hosts.
        # Keep compile process limits opt-in; runtime execution limits are enforced separately.
        self.compile_process_limit = self._env_int("POLYGONLIKE_COMPILE_PROCESS_LIMIT", default=0, min_value=0, max_value=4096)
        self.compile_output_kb = self._env_int("POLYGONLIKE_COMPILE_OUTPUT_KB", default=262144, min_value=64, max_value=1048576)
        self.cache_cleanup_interval_sec = self._env_int(
            "POLYGONLIKE_CACHE_CLEANUP_INTERVAL_SEC",
            default=600,
            min_value=0,
            max_value=86400,
        )
        self.cache_ttl_sec = self._env_int(
            "POLYGONLIKE_CACHE_TTL_SEC",
            default=604800,
            min_value=0,
            max_value=315360000,
        )
        self.cache_max_bytes = self._env_int(
            "POLYGONLIKE_CACHE_MAX_BYTES",
            default=2147483648,
            min_value=0,
            max_value=1125899906842624,
        )
        self._cleanup_state_lock = threading.Lock()
        self._last_cleanup_at = 0.0

    def _env_int(self, key: str, default: int, min_value: int, max_value: int) -> int:
        raw = os.getenv(key)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def _compile_cmd(self, cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        process_limit = int(self.compile_process_limit)
        if process_limit <= 0:
            process_limit = None
        try:
            result = self.sandbox.run(
                ExecSpec(
                    command=cmd,
                    cwd=cwd,
                    timeout_sec=self.compile_timeout_sec,
                    memory_mb=self.compile_memory_mb,
                    process_limit=process_limit,
                    output_kb=self.compile_output_kb,
                )
            )
        except FileNotFoundError as exc:
            missing = str(cmd[0]) if cmd else "tool"
            return 127, "", f"{missing} not found: {exc}"
        status = str(result.status or "").strip().lower()
        if result.timed_out or status == "tle":
            return -1, result.stdout, (result.stderr or "") + "\ncompile timed out"
        if status == "sandbox_error":
            return 1, result.stdout, result.stderr
        if result.returncode is None:
            return 1, result.stdout, result.stderr
        return int(result.returncode), result.stdout, result.stderr

    def _cache_lock_path(self, cache_bin: Path) -> Path:
        return cache_bin.with_suffix(".lock")

    def _acquire_file_lock(self, lock_path: Path, nonblocking: bool):
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(lock_file.fileno(), mode)
        except Exception:
            lock_file.close()
            raise
        return lock_file

    def _try_remove_cache_file(self, cache_bin: Path) -> int:
        try:
            size = cache_bin.stat().st_size
        except OSError:
            return 0
        lock_path = self._cache_lock_path(cache_bin)
        try:
            with self._acquire_file_lock(lock_path, nonblocking=True):
                try:
                    cache_bin.unlink()
                except FileNotFoundError:
                    return 0
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return size
        except (BlockingIOError, OSError):
            return 0

    def _touch_cache_file(self, cache_bin: Path) -> None:
        try:
            os.utime(cache_bin, None)
        except OSError:
            return

    def _trim_compile_cache(self, now_ts: float) -> None:
        if not self.cache_root.exists():
            return
        entries: list[tuple[float, int, Path]] = []
        for dirpath, _, filenames in os.walk(self.cache_root, topdown=True, followlinks=False):
            root = Path(dirpath)
            for name in filenames:
                if not name.endswith(".bin"):
                    continue
                p = root / name
                try:
                    if not p.is_file() or p.is_symlink():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                entries.append((float(st.st_mtime), int(st.st_size), p))

        if not entries:
            return

        removed: set[Path] = set()
        if self.cache_ttl_sec > 0:
            cutoff = now_ts - float(self.cache_ttl_sec)
            for mtime, _, path in entries:
                if mtime >= cutoff:
                    continue
                removed_size = self._try_remove_cache_file(path)
                if removed_size > 0:
                    removed.add(path)

        remaining: list[tuple[float, int, Path]] = []
        total_size = 0
        for mtime, size, path in entries:
            if path in removed or not path.exists():
                continue
            remaining.append((mtime, size, path))
            total_size += size

        if self.cache_max_bytes > 0 and total_size > self.cache_max_bytes:
            for _, size, path in sorted(remaining, key=lambda item: item[0]):
                if total_size <= self.cache_max_bytes:
                    break
                removed_size = self._try_remove_cache_file(path)
                if removed_size <= 0:
                    continue
                total_size -= removed_size

        self._prune_empty_cache_dirs()

    def _prune_empty_cache_dirs(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.cache_root, topdown=False, followlinks=False):
            if dirnames or filenames:
                continue
            p = Path(dirpath)
            if p == self.cache_root:
                continue
            try:
                p.rmdir()
            except OSError:
                continue

    def cleanup_cache(self, force: bool = False) -> bool:
        if self.cache_ttl_sec <= 0 and self.cache_max_bytes <= 0:
            return False
        now_ts = time.time()
        with self._cleanup_state_lock:
            if not force and self.cache_cleanup_interval_sec > 0:
                if (now_ts - self._last_cleanup_at) < float(self.cache_cleanup_interval_sec):
                    return False
            self._last_cleanup_at = now_ts

        lock_path = self.cache_root / TOOLCHAIN_CACHE_CLEANUP_LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._acquire_file_lock(lock_path, nonblocking=True):
                self._trim_compile_cache(now_ts=now_ts)
                return True
        except (BlockingIOError, OSError):
            return False

    def digest(self, cxx: str, cxxflags: list[str]) -> str:
        payload = "\n".join([cxx, *cxxflags]).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def _is_within_roots(self, resolved_path: Path, roots: list[Path]) -> bool:
        for root in roots:
            if root == resolved_path or root in resolved_path.parents:
                return True
        return False

    def _dependency_files(
        self,
        source: Path,
        include_dirs: list[Path],
        allowed_roots: list[Path] | None = None,
    ) -> tuple[list[Path], bool]:
        include_roots: list[Path] = []
        for d in include_dirs:
            try:
                include_roots.append(d.resolve())
            except OSError:
                continue
        normalized_allowed: list[Path] = []
        for root in allowed_roots or []:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved not in normalized_allowed:
                normalized_allowed.append(resolved)
        seen: set[Path] = set()
        has_external_dependency = False
        try:
            source_resolved = source.resolve()
        except OSError:
            return [], False
        stack: list[Path] = [source_resolved]

        while stack:
            current = stack.pop()
            if current in seen or not current.exists() or not current.is_file():
                continue
            if normalized_allowed and not self._is_within_roots(current, normalized_allowed):
                has_external_dependency = True
                continue
            seen.add(current)

            try:
                text = current.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for match in TOOLCHAIN_INCLUDE_RE.finditer(text):
                inc = match.group(1)
                candidate = (current.parent / inc).resolve()
                if candidate.exists() and candidate.is_file():
                    if normalized_allowed and not self._is_within_roots(candidate, normalized_allowed):
                        has_external_dependency = True
                        continue
                    stack.append(candidate)
                    continue
                for root in include_roots:
                    p = (root / inc).resolve()
                    if p.exists() and p.is_file():
                        if normalized_allowed and not self._is_within_roots(p, normalized_allowed):
                            has_external_dependency = True
                            break
                        stack.append(p)
                        break

        return sorted(seen, key=lambda p: str(p)), has_external_dependency

    def _canonical_dep_id(self, dep: Path, roots: list[Path]) -> str:
        dep_resolved = dep.resolve()
        best: str | None = None
        for idx, root in enumerate(roots):
            try:
                rel = dep_resolved.relative_to(root)
            except ValueError:
                continue
            candidate = f"r{idx}:{rel.as_posix()}"
            if best is None or len(candidate) < len(best):
                best = candidate
        if best is not None:
            return best
        suffix = "/".join(dep_resolved.parts[-4:])
        return f"tail:{suffix}"

    def compile_cpp(
        self,
        source: Path,
        output: Path,
        include_dirs: list[Path],
        path_roots: list[Path] | None = None,
        cxx: str = "g++",
        cxxflags: list[str] | None = None,
    ) -> tuple[bool, str, str, str]:
        source = source.resolve()
        output = output.resolve()
        normalized_include_dirs = [inc.resolve() for inc in include_dirs]
        cxxflags = cxxflags or list(TOOLCHAIN_CPP_CXXFLAGS)
        self.cleanup_cache(force=False)
        normalized_roots: list[Path] = []
        for root in [*(path_roots or []), source.parent, *normalized_include_dirs]:
            resolved = root.resolve()
            if resolved not in normalized_roots:
                normalized_roots.append(resolved)
        toolchain_digest = self.digest(cxx, cxxflags)
        dep_files, has_external_dependency = self._dependency_files(
            source,
            normalized_include_dirs,
            allowed_roots=normalized_roots,
        )
        key_parts = []
        for p in dep_files:
            dep_id = self._canonical_dep_id(p, normalized_roots)
            key_parts.append(f"{dep_id}:{sha256_file(p)}")
        key_parts.sort()
        source_hash = hashlib.sha256("\n".join(key_parts).encode("utf-8")).hexdigest()
        cache_bin = self.cache_root / toolchain_digest / f"{source_hash}.bin"
        cache_bin.parent.mkdir(parents=True, exist_ok=True)
        cache_lock = self._cache_lock_path(cache_bin)
        output.parent.mkdir(parents=True, exist_ok=True)
        compile_cwd = output.parent
        if has_external_dependency:
            cmd = [cxx, *cxxflags]
            for inc in normalized_include_dirs:
                cmd += ["-I", str(inc)]
            # Keep output path relative to cwd so root-switched sandboxes can keep
            # output writes constrained to the declared working directory mount.
            cmd += [str(source), "-o", output.name]
            rc, out, err = self._compile_cmd(cmd, cwd=compile_cwd)
            return rc == 0, out, err, toolchain_digest

        with cache_lock.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if cache_bin.exists():
                shutil.copy2(cache_bin, output)
                output.chmod(0o755)
                self._touch_cache_file(cache_bin)
                return True, "", "", toolchain_digest

            cmd = [cxx, *cxxflags]
            for inc in normalized_include_dirs:
                cmd += ["-I", str(inc)]
            # Keep output path relative to cwd so root-switched sandboxes can keep
            # output writes constrained to the declared working directory mount.
            cmd += [str(source), "-o", output.name]
            rc, out, err = self._compile_cmd(cmd, cwd=compile_cwd)
            if rc == 0 and output.exists():
                tmp_cache = cache_bin.parent / f".{cache_bin.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
                try:
                    shutil.copy2(output, tmp_cache)
                    tmp_cache.chmod(0o755)
                    os.replace(tmp_cache, cache_bin)
                finally:
                    if tmp_cache.exists():
                        tmp_cache.unlink(missing_ok=True)
            return rc == 0, out, err, toolchain_digest

    def _compiler_digest(self, tool: str) -> str:
        payload = f"{tool}:{self.compile_timeout_sec}:{self.compile_memory_mb}:{self.compile_output_kb}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def _detect_language(self, source: Path) -> str:
        suffix = source.suffix.lower()
        if suffix in {".cpp", ".cc", ".cxx", ".c++"}:
            return "cpp"
        if suffix == ".py":
            return "python"
        if suffix == ".java":
            return "java"
        raise RuntimeError(f"unsupported source language: {suffix or '(no extension)'}")

    def _write_launcher(self, output: Path, command: list[str]) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        quoted = " ".join(shlex.quote(str(item)) for item in command)
        script = "#!/bin/sh\n" + f"exec {quoted} \"$@\"\n"
        output.write_text(script, encoding="utf-8")
        output.chmod(0o755)

    def _write_java_launcher(self, output: Path, class_root: Path, main_class: str) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        java_flags = " ".join(shlex.quote(str(item)) for item in TOOLCHAIN_JAVA_RUNTIME_FLAGS)
        classpath = shlex.quote(str(class_root))
        main = shlex.quote(str(main_class))
        default_heap = int(TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB)
        min_heap = int(TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB)
        initial_heap = int(TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB)
        script = (
            "#!/bin/sh\n"
            "set -eu\n"
            "JAVA_AS_KB=\"$(ulimit -v 2>/dev/null || true)\"\n"
            "JAVA_HEAP_MB=\"${POLYGONLIKE_JAVA_MAX_HEAP_MB:-}\"\n"
            "case \"$JAVA_HEAP_MB\" in\n"
            "  ''|*[!0-9]*) JAVA_HEAP_MB=\"\" ;;\n"
            "esac\n"
            "if [ -z \"$JAVA_HEAP_MB\" ]; then\n"
            "  case \"$JAVA_AS_KB\" in\n"
            f"    ''|unlimited) JAVA_HEAP_MB={default_heap} ;;\n"
            f"    *) JAVA_HEAP_MB=$(( (JAVA_AS_KB / 1024) * 3 / 8 )) ;;\n"
            "  esac\n"
            "fi\n"
            f"if [ \"$JAVA_HEAP_MB\" -lt {min_heap} ]; then\n"
            f"  JAVA_HEAP_MB={min_heap}\n"
            "fi\n"
            f"exec java {java_flags} -Xms{initial_heap}m -Xmx${{JAVA_HEAP_MB}}m -cp {classpath} {main} \"$@\"\n"
        )
        output.write_text(script, encoding="utf-8")
        output.chmod(0o755)

    def _extract_java_main_class(self, source: Path) -> str:
        fallback = source.stem or "Main"
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return fallback
        match = TOOLCHAIN_JAVA_MAIN_CLASS_RE.search(text)
        if match:
            return match.group(1)
        return fallback

    def compile_python(self, source: Path, output: Path) -> tuple[bool, str, str, str]:
        source = source.resolve()
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        pyc_name = f".{output.name}.compile-check.pyc"
        pyc_path = output.parent / pyc_name
        pyc_path.unlink(missing_ok=True)
        rc, out, err = self._compile_cmd(
            [
                "python3",
                "-c",
                "import py_compile,sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
                str(source),
                pyc_name,
            ],
            cwd=output.parent,
        )
        try:
            pyc_path.unlink(missing_ok=True)
        except OSError:
            pass
        digest = self._compiler_digest("python3")
        if rc != 0:
            return False, out, err, digest
        self._write_launcher(output, ["python3", str(source)])
        return True, out, err, digest

    def compile_java(self, source: Path, output: Path) -> tuple[bool, str, str, str]:
        source = source.resolve()
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        class_root = output.parent / f"{output.name}.classes"
        if class_root.exists():
            shutil.rmtree(class_root, ignore_errors=True)
        class_root.mkdir(parents=True, exist_ok=True)
        rc, out, err = self._compile_cmd(
            [
                "javac",
                *[f"-J{flag}" for flag in TOOLCHAIN_JAVA_JAVAC_FLAGS],
                "-encoding",
                "UTF-8",
                "-d",
                class_root.name,
                str(source),
            ],
            cwd=output.parent,
        )
        digest = self._compiler_digest("javac")
        if rc != 0:
            return False, out, err, digest
        main_class = self._extract_java_main_class(source)
        self._write_java_launcher(output, class_root, main_class)
        return True, out, err, digest

    def compile_program(
        self,
        source: Path,
        output: Path,
        include_dirs: list[Path],
        path_roots: list[Path] | None = None,
        cxx: str = "g++",
        cxxflags: list[str] | None = None,
    ) -> tuple[bool, str, str, str]:
        language = self._detect_language(source)
        if language == "cpp":
            return self.compile_cpp(
                source,
                output,
                include_dirs,
                path_roots=path_roots,
                cxx=cxx,
                cxxflags=cxxflags,
            )
        if language == "python":
            return self.compile_python(source, output)
        if language == "java":
            return self.compile_java(source, output)
        raise RuntimeError(f"unsupported source language: {source.suffix.lower()}")
