from __future__ import annotations

import fcntl
import re
import shlex
import shutil
import threading
import time
import uuid
from pathlib import Path
import os

from app.runtime_values import RuntimeValues, build_runtime_values
from app.services.hashing import compile_command_digest, sha256_file, sha256_hex_of_hashes, sha256_hex_text
from app.services.sandbox import ExecSpec, SandboxBackend, NativeSandboxBackend

TOOLCHAIN_CACHE_CLEANUP_LOCK: str = ".cleanup.lock"
TOOLCHAIN_CPP_COMPILER: str = "g++"
TOOLCHAIN_PYTHON_EXECUTABLE: str = "python3"
TOOLCHAIN_JAVA_COMPILER: str = "javac"
TOOLCHAIN_CPP_CXXFLAGS: tuple[str, ...] = ("-O2", "-std=gnu++20", "-pipe", "-static", "-DDOMJUDGE")
TOOLCHAIN_INCLUDE_RE = None
TOOLCHAIN_JAVA_MAIN_CLASS_RE = None
TOOLCHAIN_JAVA_JAVAC_FLAGS: tuple[str, ...] = (
    "-Xms16m",
    "-Xmx256m",
    "-XX:MaxMetaspaceSize=64m",
    "-XX:CompressedClassSpaceSize=32m",
)
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


def apply_runtime_values(values: RuntimeValues) -> None:
    global TOOLCHAIN_CACHE_CLEANUP_LOCK
    global TOOLCHAIN_CPP_COMPILER
    global TOOLCHAIN_PYTHON_EXECUTABLE
    global TOOLCHAIN_JAVA_COMPILER
    global TOOLCHAIN_CPP_CXXFLAGS
    global TOOLCHAIN_INCLUDE_RE
    global TOOLCHAIN_JAVA_MAIN_CLASS_RE
    global TOOLCHAIN_JAVA_JAVAC_FLAGS
    global TOOLCHAIN_JAVA_RUNTIME_FLAGS
    global TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB
    global TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB
    global TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB
    TOOLCHAIN_CACHE_CLEANUP_LOCK = str(values.TOOLCHAIN_CACHE_CLEANUP_LOCK)
    TOOLCHAIN_CPP_COMPILER = str(values.TOOLCHAIN_CPP_COMPILER)
    TOOLCHAIN_PYTHON_EXECUTABLE = str(values.TOOLCHAIN_PYTHON_EXECUTABLE)
    TOOLCHAIN_JAVA_COMPILER = str(values.TOOLCHAIN_JAVA_COMPILER)
    TOOLCHAIN_CPP_CXXFLAGS = tuple(str(x) for x in values.TOOLCHAIN_CPP_CXXFLAGS)
    TOOLCHAIN_INCLUDE_RE = values.TOOLCHAIN_INCLUDE_RE
    TOOLCHAIN_JAVA_MAIN_CLASS_RE = values.TOOLCHAIN_JAVA_MAIN_CLASS_RE
    TOOLCHAIN_JAVA_JAVAC_FLAGS = tuple(str(x) for x in values.TOOLCHAIN_JAVA_JAVAC_FLAGS)
    TOOLCHAIN_JAVA_RUNTIME_FLAGS = tuple(str(x) for x in values.TOOLCHAIN_JAVA_RUNTIME_FLAGS)
    TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB = int(values.TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB)
    TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB = int(values.TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB)
    TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB = int(values.TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB)

apply_runtime_values(build_runtime_values())


def _with_domjudge_define(flags: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in flags:
        token = str(raw or "").strip()
        if token:
            normalized.append(token)
    has_domjudge = False
    for idx, token in enumerate(normalized):
        if token == "-DDOMJUDGE" or token.startswith("-DDOMJUDGE="):
            has_domjudge = True
            break
        if token == "-D" and idx + 1 < len(normalized) and normalized[idx + 1] == "DOMJUDGE":
            has_domjudge = True
            break
    if not has_domjudge:
        normalized.append("-DDOMJUDGE")
    return normalized


class ToolchainService:
    def __init__(
        self,
        cache_root: Path,
        sandbox_backend: SandboxBackend | None = None,
        constants: RuntimeValues | None = None,
    ):
        self.cache_root = cache_root / "compile"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.sandbox = sandbox_backend or NativeSandboxBackend()
        self.compile_timeout_sec = 120
        self.compile_memory_mb = 2048
        self.compile_process_limit = 0
        self.compile_output_kb = 262144
        self.cache_cleanup_interval_sec = 600
        self.cache_max_bytes = 2147483648
        self.cache_max_entries = 0
        self._cleanup_state_lock = threading.Lock()
        self._last_cleanup_at = 0.0
        self.apply_runtime_values(constants or build_runtime_values())

    def _coerce_int(self, raw: object, default: int, min_value: int, max_value: int) -> int:
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.compile_timeout_sec = self._coerce_int(
            values.get("TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120),
            default=120,
            min_value=5,
            max_value=1800,
        )
        self.compile_memory_mb = self._coerce_int(
            values.get("TOOLCHAIN_COMPILE_MEMORY_MB", 2048),
            default=2048,
            min_value=64,
            max_value=262144,
        )
        # RLIMIT_NPROC is UID-scoped and can cause compiler fork failures on busy hosts.
        # Keep compile process limits opt-in; runtime execution limits are enforced separately.
        self.compile_process_limit = self._coerce_int(
            values.get("TOOLCHAIN_COMPILE_PROCESS_LIMIT", 0),
            default=0,
            min_value=0,
            max_value=4096,
        )
        self.compile_output_kb = self._coerce_int(
            values.get("TOOLCHAIN_COMPILE_OUTPUT_KB", 262144),
            default=262144,
            min_value=64,
            max_value=1048576,
        )
        self.cache_cleanup_interval_sec = self._coerce_int(
            values.get("TOOLCHAIN_CACHE_CLEANUP_INTERVAL_SEC", 600),
            default=600,
            min_value=0,
            max_value=86400,
        )
        self.cache_max_bytes = self._coerce_int(
            values.get("TOOLCHAIN_CACHE_MAX_BYTES", 2147483648),
            default=2147483648,
            min_value=0,
            max_value=1125899906842624,
        )
        self.cache_max_entries = self._coerce_int(
            values.get("TOOLCHAIN_CACHE_MAX_ENTRIES", 0),
            default=0,
            min_value=0,
            max_value=10000000,
        )

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

    @staticmethod
    def _cache_entry_dir(toolchain_digest: str, source_hash: str, root: Path) -> Path:
        return (root / str(toolchain_digest or "").strip().lower() / str(source_hash or "").strip().lower()).resolve()

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = (path.parent / f".{path.name}.{os.getpid()}.tmp").resolve()
        try:
            tmp.write_bytes(bytes(payload))
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _set_hash_from_hashes(hashes: list[str]) -> str:
        return sha256_hex_of_hashes(hashes)

    @staticmethod
    def _clear_integrity_markers(entry_dir: Path) -> None:
        if (not entry_dir.exists()) or (not entry_dir.is_dir()) or entry_dir.is_symlink():
            return
        for child in list(entry_dir.iterdir()):
            if (not child.is_file()) or child.is_symlink():
                continue
            if re.fullmatch(r"[0-9a-f]{64}", str(child.name or "").strip().lower()) is None:
                continue
            try:
                child.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_cache_integrity_marker(self, cache_bin: Path) -> str:
        entry_dir = cache_bin.parent
        if (not entry_dir.exists()) or (not entry_dir.is_dir()) or entry_dir.is_symlink():
            return ""
        found: list[str] = []
        for child in sorted(entry_dir.iterdir(), key=lambda item: item.name):
            if (not child.is_file()) or child.is_symlink():
                continue
            token = str(child.name or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", token):
                found.append(token)
        if len(found) != 1:
            return ""
        return found[0]

    def _write_cache_integrity_marker(self, cache_bin: Path) -> None:
        if (not cache_bin.exists()) or (not cache_bin.is_file()) or cache_bin.is_symlink():
            return
        file_hash = sha256_file(cache_bin)
        marker_hash = self._set_hash_from_hashes([file_hash])
        entry_dir = cache_bin.parent
        entry_dir.mkdir(parents=True, exist_ok=True)
        self._clear_integrity_markers(entry_dir)
        marker = (entry_dir / marker_hash).resolve()
        if marker.parent != entry_dir:
            return
        self._atomic_write_bytes(marker, b"")

    def _cache_integrity_ok(self, cache_bin: Path) -> bool:
        if (not cache_bin.exists()) or (not cache_bin.is_file()) or cache_bin.is_symlink():
            return False
        marker = self._read_cache_integrity_marker(cache_bin)
        if not marker:
            return False
        file_hash = sha256_file(cache_bin)
        expected = self._set_hash_from_hashes([file_hash])
        return marker == expected

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
        entry_dir = cache_bin.parent
        try:
            with self._acquire_file_lock(lock_path, nonblocking=True):
                try:
                    cache_bin.unlink()
                except FileNotFoundError:
                    return 0
                try:
                    self._clear_integrity_markers(entry_dir)
                except OSError:
                    pass
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    if entry_dir.exists() and entry_dir.is_dir() and (not entry_dir.is_symlink()):
                        for child in list(entry_dir.iterdir()):
                            if child.is_file() and (not child.is_symlink()):
                                # orphaned sidecar files for this entry are safe to remove on eviction
                                child.unlink(missing_ok=True)
                        entry_dir.rmdir()
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
        entries: list[tuple[int, float, int, Path]] = []
        for dirpath, _, filenames in os.walk(self.cache_root, topdown=True, followlinks=False):
            root = Path(dirpath)
            for name in filenames:
                candidate = root / name
                if name.endswith(".lock"):
                    # Only delete stale lock files when they are not actively held.
                    base = name[: -len(".lock")]
                    has_bin = (root / f"{base}.bin").exists() or (root / "binary.bin").exists()
                    if not has_bin:
                        try:
                            with self._acquire_file_lock(candidate, nonblocking=True):
                                try:
                                    candidate.unlink(missing_ok=True)
                                except OSError:
                                    pass
                        except (BlockingIOError, OSError):
                            # Active compilation may hold the lock while binary.bin is not yet materialized.
                            pass
                    continue
                token = str(name or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", token):
                    if not (root / "binary.bin").exists():
                        try:
                            candidate.unlink(missing_ok=True)
                        except OSError:
                            pass
                    continue
                if not name.endswith(".bin"):
                    continue
                p = candidate
                try:
                    if not p.is_file() or p.is_symlink():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                if not self._cache_integrity_ok(p):
                    self._try_remove_cache_file(p)
                    continue
                last_hit_ts = float(st.st_mtime)
                hit_count = 0
                # Lower heat first (smaller hit_count, then older last_hit).
                entries.append((hit_count, float(last_hit_ts), int(st.st_size), p))

        if not entries:
            self._prune_empty_cache_dirs()
            return

        removed: set[Path] = set()
        remaining: list[tuple[int, float, int, Path]] = []
        total_size = 0
        for hit_count, last_hit_ts, size, path in entries:
            if path in removed or not path.exists():
                continue
            remaining.append((hit_count, last_hit_ts, size, path))
            total_size += size

        if self.cache_max_entries > 0 and len(remaining) > self.cache_max_entries:
            for _, _, size, path in sorted(remaining, key=lambda item: (item[0], item[1], str(item[3]))):
                if len(remaining) <= self.cache_max_entries:
                    break
                removed_size = self._try_remove_cache_file(path)
                if removed_size <= 0:
                    continue
                remaining = [row for row in remaining if row[3] != path]
                total_size = max(0, total_size - removed_size)

        if self.cache_max_bytes > 0 and total_size > self.cache_max_bytes:
            for _, _, size, path in sorted(remaining, key=lambda item: (item[0], item[1], str(item[3]))):
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
        return compile_command_digest(cxx, cxxflags)[:16]

    def current_cpp_command_digest(self) -> str:
        return compile_command_digest(TOOLCHAIN_CPP_COMPILER, list(TOOLCHAIN_CPP_CXXFLAGS))

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
    ) -> list[Path]:
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
        try:
            source_resolved = source.resolve()
        except OSError:
            return []
        stack: list[Path] = [source_resolved]

        while stack:
            current = stack.pop()
            if current in seen or not current.exists() or not current.is_file():
                continue
            if normalized_allowed and not self._is_within_roots(current, normalized_allowed):
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
                        continue
                    stack.append(candidate)
                    continue
                for root in include_roots:
                    p = (root / inc).resolve()
                    if p.exists() and p.is_file():
                        if normalized_allowed and not self._is_within_roots(p, normalized_allowed):
                            break
                        stack.append(p)
                        break

        return sorted(seen, key=lambda p: str(p))

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
        cxx: str | None = None,
        cxxflags: list[str] | None = None,
    ) -> tuple[bool, str, str, str]:
        source = source.resolve()
        output = output.resolve()
        normalized_include_dirs = [inc.resolve() for inc in include_dirs]
        cxxflags = _with_domjudge_define(cxxflags or list(TOOLCHAIN_CPP_CXXFLAGS))
        cxx_cmd = str(cxx or TOOLCHAIN_CPP_COMPILER).strip() or "g++"
        self.cleanup_cache(force=False)
        normalized_roots: list[Path] = []
        for root in [*(path_roots or []), source.parent, *normalized_include_dirs]:
            resolved = root.resolve()
            if resolved not in normalized_roots:
                normalized_roots.append(resolved)
        toolchain_digest = self.digest(cxx_cmd, cxxflags)
        dep_files = self._dependency_files(
            source,
            normalized_include_dirs,
            allowed_roots=normalized_roots,
        )
        key_parts = []
        for p in dep_files:
            dep_id = self._canonical_dep_id(p, normalized_roots)
            key_parts.append(f"{dep_id}:{sha256_file(p)}")
        key_parts.sort()
        source_hash = sha256_hex_text("\n".join(key_parts))
        cache_entry_dir = self._cache_entry_dir(toolchain_digest, source_hash, self.cache_root)
        cache_entry_dir.mkdir(parents=True, exist_ok=True)
        cache_bin = cache_entry_dir / "binary.bin"
        cache_lock = self._cache_lock_path(cache_bin)
        output.parent.mkdir(parents=True, exist_ok=True)
        compile_cwd = output.parent
        with cache_lock.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if cache_bin.exists() and self._cache_integrity_ok(cache_bin):
                shutil.copy2(cache_bin, output)
                output.chmod(0o755)
                self._touch_cache_file(cache_bin)
                return True, "", "", toolchain_digest
            if cache_bin.exists():
                self._try_remove_cache_file(cache_bin)

            cmd = [cxx_cmd, *cxxflags]
            for inc in normalized_include_dirs:
                cmd += ["-I", str(inc)]
            # Keep output path relative to cwd so root-switched sandboxes can keep
            # output writes constrained to the declared working directory mount.
            cmd += [str(source), "-o", output.name]
            rc, out, err = self._compile_cmd(cmd, cwd=compile_cwd)
            if rc == 0 and output.exists():
                cache_bin.parent.mkdir(parents=True, exist_ok=True)
                tmp_cache = cache_bin.parent / f".{cache_bin.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
                try:
                    shutil.copy2(output, tmp_cache)
                    tmp_cache.chmod(0o755)
                    os.replace(tmp_cache, cache_bin)
                    self._write_cache_integrity_marker(cache_bin)
                finally:
                    if tmp_cache.exists():
                        tmp_cache.unlink(missing_ok=True)
            return rc == 0, out, err, toolchain_digest

    def _compiler_digest(self, tool: str) -> str:
        payload = f"{tool}:{self.compile_timeout_sec}:{self.compile_memory_mb}:{self.compile_output_kb}"
        return sha256_hex_text(payload)[:16]

    def _detect_language(self, source: Path) -> str:
        suffix = source.suffix.lower()
        if suffix in {".cpp", ".cc", ".cxx", ".c++"}:
            return "cpp"
        if suffix == ".py":
            return "python"
        if suffix == ".java":
            return "java"
        raise RuntimeError(f"unsupported source language: {suffix or '(no extension)'}")

    def _write_python_launcher(self, output: Path, script_name: str) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        py_exec = shlex.quote(str(TOOLCHAIN_PYTHON_EXECUTABLE))
        script_rel = shlex.quote(str(script_name))
        script = (
            "#!/bin/sh\n"
            "set -eu\n"
            "HERE=\"$(CDPATH= cd -- \"$(dirname \"$0\")\" && pwd)\"\n"
            f"exec {py_exec} \"$HERE\"/{script_rel} \"$@\"\n"
        )
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
        if (not fallback) or (not self._is_valid_java_identifier(fallback)):
            fallback = "Main"
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return fallback
        match = TOOLCHAIN_JAVA_MAIN_CLASS_RE.search(text)
        if match:
            candidate = str(match.group(1) or "").strip()
            if self._is_valid_java_identifier(candidate):
                return candidate
        return fallback

    @staticmethod
    def _is_valid_java_identifier(token: str) -> bool:
        value = str(token or "").strip()
        if not value:
            return False
        head = value[0]
        if (not head.isalpha()) and (head != "_"):
            return False
        for ch in value[1:]:
            if (not ch.isalnum()) and (ch != "_"):
                return False
        return True

    def compile_python(self, source: Path, output: Path) -> tuple[bool, str, str, str]:
        source = source.resolve()
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        runtime_source = output.parent / f"{output.name}.py"
        shutil.copy2(source, runtime_source)
        pyc_name = f".{output.name}.compile-check.pyc"
        pyc_path = output.parent / pyc_name
        pyc_path.unlink(missing_ok=True)
        rc, out, err = self._compile_cmd(
            [
                TOOLCHAIN_PYTHON_EXECUTABLE,
                "-c",
                "import py_compile,sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
                str(runtime_source),
                pyc_name,
            ],
            cwd=output.parent,
        )
        try:
            pyc_path.unlink(missing_ok=True)
        except OSError:
            pass
        digest = self._compiler_digest(TOOLCHAIN_PYTHON_EXECUTABLE)
        if rc != 0:
            return False, out, err, digest
        self._write_python_launcher(output, runtime_source.name)
        return True, out, err, digest

    def compile_java(self, source: Path, output: Path) -> tuple[bool, str, str, str]:
        source = source.resolve()
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        class_root = output.parent / f"{output.name}.classes"
        if class_root.exists():
            shutil.rmtree(class_root, ignore_errors=True)
        class_root.mkdir(parents=True, exist_ok=True)
        main_class = self._extract_java_main_class(source)
        source_root = output.parent / f"{output.name}.java-src"
        if source_root.exists():
            shutil.rmtree(source_root, ignore_errors=True)
        source_root.mkdir(parents=True, exist_ok=True)
        build_source = source_root / f"{main_class}.java"
        shutil.copy2(source, build_source)
        rc, out, err = self._compile_cmd(
            [
                TOOLCHAIN_JAVA_COMPILER,
                *[f"-J{flag}" for flag in TOOLCHAIN_JAVA_JAVAC_FLAGS],
                "-encoding",
                "UTF-8",
                "-d",
                class_root.name,
                str(build_source),
            ],
            cwd=output.parent,
        )
        digest = self._compiler_digest(TOOLCHAIN_JAVA_COMPILER)
        if rc != 0:
            return False, out, err, digest
        self._write_java_launcher(output, class_root, main_class)
        return True, out, err, digest

    def compile_program(
        self,
        source: Path,
        output: Path,
        include_dirs: list[Path],
        path_roots: list[Path] | None = None,
        cxx: str | None = None,
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
