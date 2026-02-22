from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from app.services.util import run_cmd, sha256_file


class ToolchainService:
    def __init__(self, cache_root: Path):
        self.cache_root = cache_root / "compile"
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def digest(self, cxx: str, cxxflags: list[str]) -> str:
        payload = "\n".join([cxx, *cxxflags]).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def compile_cpp(
        self,
        source: Path,
        output: Path,
        include_dirs: list[Path],
        cxx: str = "g++",
        cxxflags: list[str] | None = None,
    ) -> tuple[bool, str, str, str]:
        cxxflags = cxxflags or ["-O2", "-std=c++20", "-pipe", "-static"]
        toolchain_digest = self.digest(cxx, cxxflags)
        key_parts = [sha256_file(source)]
        for include_dir in include_dirs:
            header = include_dir / "testlib.h"
            if header.exists():
                key_parts.append(sha256_file(header))
        source_hash = hashlib.sha256("\n".join(key_parts).encode("utf-8")).hexdigest()
        cache_bin = self.cache_root / toolchain_digest / f"{source_hash}.bin"
        cache_bin.parent.mkdir(parents=True, exist_ok=True)
        if cache_bin.exists():
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache_bin, output)
            output.chmod(0o755)
            return True, "", "", toolchain_digest

        cmd = [cxx, *cxxflags]
        for inc in include_dirs:
            cmd += ["-I", str(inc)]
        cmd += [str(source), "-o", str(output)]
        proc = run_cmd(cmd)
        if proc.returncode == 0 and output.exists():
            shutil.copy2(output, cache_bin)
            cache_bin.chmod(0o755)
        return proc.returncode == 0, proc.stdout, proc.stderr, toolchain_digest
