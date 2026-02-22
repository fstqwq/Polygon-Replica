from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path

from app.services.util import run_cmd, sha256_file


class ToolchainService:
    INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)

    def __init__(self, cache_root: Path):
        self.cache_root = cache_root / "compile"
        self.cache_root.mkdir(parents=True, exist_ok=True)

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

            for match in self.INCLUDE_RE.finditer(text):
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
        cxxflags = cxxflags or ["-O2", "-std=c++20", "-pipe", "-static"]
        normalized_roots: list[Path] = []
        for root in [*(path_roots or []), source.parent, *include_dirs]:
            resolved = root.resolve()
            if resolved not in normalized_roots:
                normalized_roots.append(resolved)
        toolchain_digest = self.digest(cxx, cxxflags)
        dep_files, has_external_dependency = self._dependency_files(
            source,
            include_dirs,
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
        cache_lock = cache_bin.with_suffix(".lock")
        output.parent.mkdir(parents=True, exist_ok=True)
        if has_external_dependency:
            cmd = [cxx, *cxxflags]
            for inc in include_dirs:
                cmd += ["-I", str(inc)]
            cmd += [str(source), "-o", str(output)]
            proc = run_cmd(cmd)
            return proc.returncode == 0, proc.stdout, proc.stderr, toolchain_digest

        with cache_lock.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if cache_bin.exists():
                shutil.copy2(cache_bin, output)
                output.chmod(0o755)
                return True, "", "", toolchain_digest

            cmd = [cxx, *cxxflags]
            for inc in include_dirs:
                cmd += ["-I", str(inc)]
            cmd += [str(source), "-o", str(output)]
            proc = run_cmd(cmd)
            if proc.returncode == 0 and output.exists():
                tmp_cache = cache_bin.parent / f".{cache_bin.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
                try:
                    shutil.copy2(output, tmp_cache)
                    tmp_cache.chmod(0o755)
                    os.replace(tmp_cache, cache_bin)
                finally:
                    if tmp_cache.exists():
                        tmp_cache.unlink(missing_ok=True)
            return proc.returncode == 0, proc.stdout, proc.stderr, toolchain_digest
