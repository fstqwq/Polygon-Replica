from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from time import monotonic

from app.services.util import run_cmd


class GitService:
    BRANCH_CACHE_TTL_SEC = 5.0
    BRANCH_CACHE_MAX_ENTRIES = 256
    BRANCH_CAPPED_CACHE_MAX_ENTRIES = 512
    STATUS_MAX_LINES = 512
    DIFF_MAX_CHARS = 131072

    def __init__(self) -> None:
        self._branch_cache: dict[str, tuple[float, list[str]]] = {}
        self._branch_capped_cache: dict[str, tuple[float, list[str], bool]] = {}
        self._branch_cache_lock = threading.Lock()

    def _workspace_key(self, workspace: Path) -> str:
        return str(workspace.resolve())

    def _invalidate_branch_cache(self, workspace: Path) -> None:
        key = self._workspace_key(workspace)
        with self._branch_cache_lock:
            self._branch_cache.pop(key, None)
            capped_prefix = f"{key}::"
            stale_keys = [k for k in self._branch_capped_cache if k.startswith(capped_prefix)]
            for stale in stale_keys:
                self._branch_capped_cache.pop(stale, None)

    def _branch_cache_get(self, key: str, now: float, force_refresh: bool = False) -> list[str] | None:
        with self._branch_cache_lock:
            cached = self._branch_cache.get(key)
            if force_refresh or cached is None:
                return None
            ts, branches = cached
            if now - ts > self.BRANCH_CACHE_TTL_SEC:
                self._branch_cache.pop(key, None)
                return None
            # Promote on access to keep hot workspaces resident.
            self._branch_cache.pop(key, None)
            self._branch_cache[key] = (ts, branches)
            return list(branches)

    def _branch_cache_put(self, key: str, timestamp: float, branches: list[str]) -> None:
        with self._branch_cache_lock:
            self._branch_cache.pop(key, None)
            self._branch_cache[key] = (timestamp, list(branches))
            while len(self._branch_cache) > self.BRANCH_CACHE_MAX_ENTRIES:
                oldest_key = next(iter(self._branch_cache))
                self._branch_cache.pop(oldest_key, None)

    def _capped_branch_cache_key(self, workspace: Path, limit: int) -> str:
        return f"{self._workspace_key(workspace)}::{max(1, int(limit))}"

    def _branch_capped_cache_get(
        self, key: str, now: float, force_refresh: bool = False
    ) -> tuple[list[str], bool] | None:
        with self._branch_cache_lock:
            cached = self._branch_capped_cache.get(key)
            if force_refresh or cached is None:
                return None
            ts, branches, truncated = cached
            if now - ts > self.BRANCH_CACHE_TTL_SEC:
                self._branch_capped_cache.pop(key, None)
                return None
            # Promote on access to keep hot workspaces resident.
            self._branch_capped_cache.pop(key, None)
            self._branch_capped_cache[key] = (ts, branches, truncated)
            return list(branches), bool(truncated)

    def _branch_capped_cache_put(self, key: str, timestamp: float, branches: list[str], truncated: bool) -> None:
        with self._branch_cache_lock:
            self._branch_capped_cache.pop(key, None)
            self._branch_capped_cache[key] = (timestamp, list(branches), bool(truncated))
            while len(self._branch_capped_cache) > self.BRANCH_CAPPED_CACHE_MAX_ENTRIES:
                oldest_key = next(iter(self._branch_capped_cache))
                self._branch_capped_cache.pop(oldest_key, None)

    def _contains_symlink_component(self, root: Path, candidate: Path) -> bool:
        try:
            if root.is_symlink():
                return True
        except OSError:
            return True
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            return True
        cur = root
        for part in rel.parts:
            cur = cur / part
            try:
                if cur.is_symlink():
                    return True
            except OSError:
                return True
            if not cur.exists():
                break
        return False

    def _resolve_user_path(self, workspace: Path, rel_path: str, allow_workspace_root: bool = False) -> Path:
        ws_root = workspace.resolve()
        candidate = workspace / rel_path
        p = candidate.resolve()
        if ws_root not in p.parents and p != ws_root:
            raise ValueError("invalid path")
        if self._contains_symlink_component(ws_root, candidate):
            raise ValueError("invalid path")
        if not allow_workspace_root and p == ws_root:
            raise ValueError("invalid path")
        rel = p.relative_to(ws_root)
        if ".git" in rel.parts or ".polygonlike.lock" in rel.parts:
            raise ValueError("reserved path")
        return p

    def _is_reserved_status_path(self, path: str) -> bool:
        normalized = str(path or "").strip().strip('"')
        return normalized == ".polygonlike.lock" or normalized.endswith("/.polygonlike.lock")

    def _is_reserved_status_line(self, line: str) -> bool:
        raw = line.rstrip("\n")
        if not raw or raw.startswith("## "):
            return False
        payload = raw[3:].strip() if len(raw) >= 4 else raw.strip()
        if " -> " in payload:
            for part in payload.split(" -> "):
                if self._is_reserved_status_path(part):
                    return True
            return False
        return self._is_reserved_status_path(payload)

    def _is_reserved_diff_header(self, line: str) -> bool:
        prefix = "diff --git a/"
        raw = line.rstrip("\n")
        if not raw.startswith(prefix):
            return False
        rest = raw[len(prefix) :]
        if " b/" not in rest:
            return False
        lhs, rhs = rest.split(" b/", 1)
        return self._is_reserved_status_path(lhs) or self._is_reserved_status_path(rhs)

    def _filter_reserved_diff(self, diff_text: str) -> str:
        if not diff_text:
            return ""
        lines = diff_text.splitlines(keepends=True)
        out: list[str] = []
        chunk: list[str] = []
        dropping = False
        in_chunk = False
        for line in lines:
            if line.startswith("diff --git "):
                if in_chunk and not dropping:
                    out.extend(chunk)
                in_chunk = True
                chunk = [line]
                dropping = self._is_reserved_diff_header(line)
                continue
            if in_chunk:
                chunk.append(line)
            else:
                out.append(line)
        if in_chunk and not dropping:
            out.extend(chunk)
        return "".join(out)

    def _append_truncation_marker(self, text: str, max_chars: int) -> str:
        clipped = text
        if "\n" in clipped:
            clipped = clipped.rsplit("\n", 1)[0] + "\n"
        return clipped + f"... [truncated; showing first {max_chars} characters]\n"

    def _truncate_text(self, text: str, max_chars: int) -> tuple[str, bool]:
        if max_chars <= 0:
            return text, False
        if len(text) <= max_chars:
            return text, False
        return self._append_truncation_marker(text[:max_chars], max_chars), True

    def _read_text_prefix(self, path: Path, max_chars: int) -> tuple[str, bool]:
        if max_chars <= 0:
            return path.read_text(encoding="utf-8", errors="replace"), False
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(max_chars + 1)
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars], True

    def status(self, workspace: Path) -> dict:
        proc = run_cmd(["git", "-C", str(workspace), "status", "--short", "--branch"])
        filtered_lines: list[str] = []
        status_truncated = False
        need_diff = False
        status_limit = max(1, int(self.STATUS_MAX_LINES))
        for raw in proc.stdout.splitlines():
            line = raw.rstrip("\n")
            if not line:
                continue
            keep_line = True
            if line.startswith("## "):
                keep_line = True
            elif self._is_reserved_status_line(line):
                keep_line = False
            if keep_line:
                if len(filtered_lines) < status_limit:
                    filtered_lines.append(line)
                else:
                    status_truncated = True
            else:
                continue
            # "??" entries are untracked-only and do not appear in `git diff`.
            if line.startswith("??"):
                continue
            # Porcelain format: XY<space>PATH. Y != ' ' means unstaged worktree change.
            if len(line) >= 2 and line[1] != " ":
                need_diff = True
        if status_truncated:
            filtered_lines.append(f"... [truncated; showing first {status_limit} lines]")
        status_text = "\n".join(filtered_lines) + ("\n" if filtered_lines else "")
        diff_truncated = False
        diff_limit = max(1, int(self.DIFF_MAX_CHARS))
        if need_diff:
            tmp_path: Path | None = None
            try:
                fd, tmp_name = tempfile.mkstemp(prefix="git-diff-", suffix=".patch")
                os.close(fd)
                tmp_path = Path(tmp_name)
                diff_proc = run_cmd(
                    [
                        "git",
                        "-C",
                        str(workspace),
                        "diff",
                        "--",
                        ".",
                        ":(exclude).polygonlike.lock",
                        ":(exclude)**/.polygonlike.lock",
                    ],
                    stdout_path=tmp_path,
                )
                if diff_proc.returncode == 0:
                    diff_text, diff_truncated = self._read_text_prefix(tmp_path, diff_limit)
                    if diff_truncated:
                        diff_text = self._append_truncation_marker(diff_text, diff_limit)
                else:
                    raw_diff = run_cmd(["git", "-C", str(workspace), "diff", "--", "."]).stdout
                    filtered_diff = self._filter_reserved_diff(raw_diff)
                    diff_text, diff_truncated = self._truncate_text(filtered_diff, diff_limit)
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
        else:
            diff_text = ""
        return {
            "status": status_text,
            "diff": diff_text,
            "status_truncated": status_truncated,
            "status_line_limit": status_limit,
            "diff_truncated": diff_truncated,
            "diff_char_limit": diff_limit,
        }

    def commit(self, workspace: Path, message: str, name: str, email: str) -> str:
        run_cmd(["git", "-C", str(workspace), "config", "user.name", name])
        run_cmd(["git", "-C", str(workspace), "config", "user.email", email])
        run_cmd(["git", "-C", str(workspace), "add", "."])
        # Never allow internal workspace lock files to enter repository history.
        run_cmd(
            [
                "git",
                "-C",
                str(workspace),
                "reset",
                "--quiet",
                "--",
                ".polygonlike.lock",
                ":(glob)**/.polygonlike.lock",
            ]
        )
        proc = run_cmd(["git", "-C", str(workspace), "commit", "-m", message])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"])
        return head.stdout.strip()

    def push(self, workspace: Path, branch: str) -> str:
        proc = run_cmd(["git", "-C", str(workspace), "push", "origin", branch])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        return proc.stdout + proc.stderr

    def pull(self, workspace: Path, branch: str) -> str:
        proc = run_cmd(["git", "-C", str(workspace), "pull", "origin", branch])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        return proc.stdout + proc.stderr

    def switch_branch(self, workspace: Path, branch: str, create: bool = False) -> str:
        cmd = ["git", "-C", str(workspace), "switch"]
        if create:
            cmd += ["-c", branch]
        else:
            cmd += [branch]
        proc = run_cmd(cmd)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        self._invalidate_branch_cache(workspace)
        return proc.stdout + proc.stderr

    def merge(self, workspace: Path, source_branch: str, target_branch: str = "main") -> str:
        self.switch_branch(workspace, target_branch)
        proc = run_cmd(["git", "-C", str(workspace), "merge", "--no-ff", source_branch])
        if proc.returncode != 0:
            out = proc.stdout + proc.stderr
            run_cmd(["git", "-C", str(workspace), "merge", "--abort"])
            raise RuntimeError(out)
        self._invalidate_branch_cache(workspace)
        return proc.stdout + proc.stderr

    def list_branches(self, workspace: Path, force_refresh: bool = False) -> list[str]:
        key = self._workspace_key(workspace)
        now = monotonic()
        cached = self._branch_cache_get(key, now, force_refresh=force_refresh)
        if cached is not None:
            return cached

        proc = run_cmd(["git", "-C", str(workspace), "branch", "--format", "%(refname:short)"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        branches = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self._branch_cache_put(key, now, branches)
        return list(branches)

    def list_branches_capped(
        self,
        workspace: Path,
        current_branch: str,
        limit: int,
        force_refresh: bool = False,
    ) -> tuple[list[str], bool]:
        cap = max(1, int(limit))
        key = self._capped_branch_cache_key(workspace, cap)
        now = monotonic()
        cached = self._branch_capped_cache_get(key, now, force_refresh=force_refresh)

        def _with_current(values: list[str], truncated: bool) -> tuple[list[str], bool]:
            selected = list(values)
            current = str(current_branch or "").strip()
            if current and current not in selected:
                if selected:
                    selected[-1] = current
                else:
                    selected = [current]
                    return selected, False
            return selected, truncated

        if cached is not None:
            cached_branches, cached_truncated = cached
            return _with_current(cached_branches, cached_truncated)

        proc = run_cmd(
            [
                "git",
                "-C",
                str(workspace),
                "for-each-ref",
                "--format",
                "%(refname:short)",
                "--count",
                str(cap + 1),
                "refs/heads",
            ]
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        branches = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        truncated = len(branches) > cap
        if truncated:
            branches = branches[:cap]
        self._branch_capped_cache_put(key, now, branches, truncated)
        return _with_current(branches, truncated)

    def list_files(self, workspace: Path, rel: str = ".") -> list[str]:
        files, _ = self.list_files_capped(workspace, rel=rel, limit=None)
        return files

    def list_files_capped(self, workspace: Path, rel: str = ".", limit: int | None = None) -> tuple[list[str], bool]:
        workspace_root = workspace.resolve()
        base = self._resolve_user_path(workspace, rel, allow_workspace_root=True)
        paths: list[str] = []
        truncated = False
        capped = max(1, int(limit)) if limit is not None else None
        stop_scan = False
        for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            next_dirs: list[str] = []
            for name in sorted(dirnames):
                d = dir_root / name
                if ".git" in d.parts or ".polygonlike.lock" in d.parts or d.is_symlink():
                    continue
                try:
                    resolved = d.resolve()
                except OSError:
                    continue
                if workspace_root not in resolved.parents and workspace_root != resolved:
                    continue
                next_dirs.append(name)
                if capped is not None and len(paths) >= capped:
                    truncated = True
                    stop_scan = True
                    break
                paths.append(str(d.relative_to(workspace)))
            if stop_scan:
                dirnames[:] = []
                break
            dirnames[:] = next_dirs

            for name in sorted(filenames):
                p = dir_root / name
                if ".git" in p.parts or ".polygonlike.lock" in p.parts or p.is_symlink():
                    continue
                try:
                    resolved = p.resolve()
                except OSError:
                    continue
                if workspace_root not in resolved.parents and workspace_root != resolved:
                    continue
                if capped is not None and len(paths) >= capped:
                    truncated = True
                    stop_scan = True
                    break
                paths.append(str(p.relative_to(workspace)))
            if stop_scan:
                break
        if capped is None:
            paths.sort()
        return paths, truncated

    def read_file(self, workspace: Path, rel_path: str) -> str:
        p = self._resolve_user_path(workspace, rel_path)
        return p.read_text(encoding="utf-8")

    def read_file_limited(self, workspace: Path, rel_path: str, max_chars: int) -> tuple[str, bool]:
        p = self._resolve_user_path(workspace, rel_path)
        cap = max(1, int(max_chars))
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(cap + 1)
        if len(text) <= cap:
            return text, False
        return self._append_truncation_marker(text[:cap], cap), True

    def write_file(self, workspace: Path, rel_path: str, content: str) -> None:
        p = self._resolve_user_path(workspace, rel_path)
        if p.exists() and p.is_dir():
            raise ValueError("path is a directory")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def delete_path(self, workspace: Path, rel_path: str) -> None:
        p = self._resolve_user_path(workspace, rel_path)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def rename_path(self, workspace: Path, old_rel: str, new_rel: str) -> None:
        src = self._resolve_user_path(workspace, old_rel)
        dst = self._resolve_user_path(workspace, new_rel)
        if not src.exists():
            raise ValueError("path not found")
        if dst.exists() and dst.is_dir():
            raise ValueError("destination is a directory")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
