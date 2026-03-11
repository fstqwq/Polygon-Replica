from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Callable, Mapping

from app.service.platform.process import run_cmd


def normalized_path_prefixes(paths: list[Path] | tuple[Path, ...] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_path in paths or []:
        text = str(raw_path or "").strip()
        if not text:
            continue
        p = Path(text)
        try:
            normalized = str(p.resolve())
        except OSError:
            normalized = str(p.absolute())
        normalized = normalized.rstrip("/") or "/"
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def ensure_path_guard_library(*, source: Path, cache_root: Path, lock) -> Path | None:
    if not source.exists() or not source.is_file():
        return None
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / "path_guard.so"
    with lock:
        try:
            source_mtime = source.stat().st_mtime
        except OSError:
            return None
        rebuild = True
        if target.exists():
            try:
                rebuild = target.stat().st_mtime < source_mtime
            except OSError:
                rebuild = True
        if rebuild:
            tmp = cache_root / f".path_guard-{uuid.uuid4().hex[:8]}.tmp.so"
            cmd = ["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-o", str(tmp), str(source), "-ldl", "-pthread"]
            proc = run_cmd(cmd, timeout=30)
            if proc.returncode != 0:
                tmp.unlink(missing_ok=True)
                return None
            os.replace(tmp, target)
            try:
                target.chmod(0o755)
            except OSError:
                pass
        if not target.exists() or not target.is_file():
            return None
        return target


def build_path_guard_environment(
    *,
    base_env: dict[str, str] | None,
    deny_paths: list[Path] | tuple[Path, ...] | None,
    allow_paths: list[Path] | tuple[Path, ...] | None,
    ensure_library: Callable[[], Path | None],
    environ: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    deny_prefixes = normalized_path_prefixes(deny_paths)
    if not deny_prefixes:
        return base_env
    path_guard_so = ensure_library()
    if path_guard_so is None:
        raise RuntimeError("submission path guard is unavailable")
    env = dict(os.environ if environ is None else environ)
    if base_env:
        env.update(base_env)
    env["POLYGONLIKE_PATH_GUARD_DENY_PREFIXES"] = "\n".join(deny_prefixes)
    allow_prefixes = normalized_path_prefixes(allow_paths)
    if allow_prefixes:
        env["POLYGONLIKE_PATH_GUARD_ALLOW_PREFIXES"] = "\n".join(allow_prefixes)
    else:
        env.pop("POLYGONLIKE_PATH_GUARD_ALLOW_PREFIXES", None)
    existing_ld_preload = str(env.get("LD_PRELOAD") or "").strip()
    if existing_ld_preload:
        env["LD_PRELOAD"] = f"{str(path_guard_so)}:{existing_ld_preload}"
    else:
        env["LD_PRELOAD"] = str(path_guard_so)
    return env


