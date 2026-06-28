from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from app.service.platform.git_process import run_git
from app.service.platform.workspace_path import contains_symlink_component, is_hidden_workspace_path


class GitService:
    STATUS_MAX_LINES = 512
    DIFF_MAX_CHARS = 131072
    HISTORY_MAX_ITEMS = 300

    def _resolve_user_path(self, workspace: Path, rel_path: str, allow_workspace_root: bool = False) -> Path:
        ws_root = workspace.resolve()
        candidate = workspace / rel_path
        p = candidate.resolve()
        if ws_root not in p.parents and p != ws_root:
            raise ValueError("invalid path")
        if contains_symlink_component(ws_root, candidate):
            raise ValueError("invalid path")
        if not allow_workspace_root and p == ws_root:
            raise ValueError("invalid path")
        rel = p.relative_to(ws_root)
        if is_hidden_workspace_path(rel.parts):
            raise ValueError("hidden path is not allowed")
        return p

    def _is_hidden_status_path(self, path: str) -> bool:
        normalized = str(path or "").strip().strip('"')
        if not normalized:
            return False
        parts = tuple(part for part in PurePosixPath(normalized.replace("\\", "/")).parts if part not in {"", "."})
        return is_hidden_workspace_path(parts)

    def _is_hidden_status_line(self, line: str) -> bool:
        raw = line.rstrip("\n")
        if not raw or raw.startswith("## "):
            return False
        payload = raw[3:].strip() if len(raw) >= 4 else raw.strip()
        if " -> " in payload:
            for part in payload.split(" -> "):
                if self._is_hidden_status_path(part):
                    return True
            return False
        return self._is_hidden_status_path(payload)

    def _is_hidden_diff_header(self, line: str) -> bool:
        prefix = "diff --git a/"
        raw = line.rstrip("\n")
        if not raw.startswith(prefix):
            return False
        rest = raw[len(prefix) :]
        if " b/" not in rest:
            return False
        lhs, rhs = rest.split(" b/", 1)
        return self._is_hidden_status_path(lhs) or self._is_hidden_status_path(rhs)

    def _filter_hidden_diff(self, diff_text: str) -> str:
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
                dropping = self._is_hidden_diff_header(line)
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
        proc = run_git(["git", "-C", str(workspace), "status", "--short", "--branch"])
        filtered_lines: list[str] = []
        status_truncated = False
        status_limit = max(1, int(self.STATUS_MAX_LINES))
        for raw in proc.stdout.splitlines():
            line = raw.rstrip("\n")
            if not line:
                continue
            keep_line = True
            if line.startswith("## "):
                keep_line = True
            elif self._is_hidden_status_line(line):
                keep_line = False
            if keep_line:
                if len(filtered_lines) < status_limit:
                    filtered_lines.append(line)
                else:
                    status_truncated = True
            else:
                continue
        if status_truncated:
            filtered_lines.append(f"... [truncated; showing first {status_limit} lines]")
        status_text = "\n".join(filtered_lines) + ("\n" if filtered_lines else "")
        diff_limit = max(1, int(self.DIFF_MAX_CHARS))
        diff_truncated = False
        diff_text = ""
        rebase_active = self._rebase_active(workspace)
        conflicted_files = self._conflicted_files(workspace) if rebase_active else []
        return {
            "status": status_text,
            "diff": diff_text,
            "status_truncated": status_truncated,
            "status_line_limit": status_limit,
            "diff_truncated": diff_truncated,
            "diff_char_limit": diff_limit,
            "rebase_active": rebase_active,
            "conflicted_files": conflicted_files,
        }

    def _normalize_status_path(self, raw: str) -> str:
        path = str(raw or "").strip()
        if path.startswith('"') and path.endswith('"') and len(path) >= 2:
            path = path[1:-1]
        return path

    def _status_kind(self, code: str, path: str) -> str:
        status = str(code or "").strip()
        if status == "??":
            return "added"
        if status == "!!":
            return "ignored"
        if "U" in status or status in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            return "conflicted"
        if "R" in status or "C" in status or " -> " in str(path or ""):
            return "renamed"
        if "A" in status:
            return "added"
        if "D" in status:
            return "deleted"
        if "T" in status:
            return "typechange"
        if "M" in status:
            return "modified"
        return "other"

    def status_change_summary(self, workspace: Path, limit: int | None = None) -> dict:
        cap: int | None = None
        if limit is not None:
            parsed = int(limit)
            if parsed > 0:
                cap = parsed
        proc = run_git(["git", "-C", str(workspace), "status", "--short", "--untracked-files=all"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)

        counts = {
            "added": 0,
            "modified": 0,
            "deleted": 0,
            "renamed": 0,
            "untracked": 0,
            "conflicted": 0,
            "typechange": 0,
            "other": 0,
        }
        rows: list[dict] = []
        total = 0
        for raw in proc.stdout.splitlines():
            line = raw.rstrip("\n")
            if not line or self._is_hidden_status_line(line):
                continue
            if len(line) < 3:
                continue
            code = line[:2]
            path_part = line[3:].strip()
            display_path = self._normalize_status_path(path_part)
            link_path = display_path
            if " -> " in display_path:
                before, after = display_path.split(" -> ", 1)
                display_path = f"{before} -> {after}"
                link_path = after
            kind = self._status_kind(code, display_path)
            if kind in counts:
                counts[kind] += 1
            else:
                counts["other"] += 1
            display_code = code
            if str(code).strip() == "??" and kind == "added":
                display_code = "A "
            total += 1
            if cap is not None and len(rows) >= cap:
                continue
            rows.append(
                {
                    "code": display_code,
                    "path": display_path,
                    "link_path": self._normalize_status_path(link_path),
                    "kind": kind if kind in counts else "other",
                }
            )
        return {
            "counts": counts,
            "rows": rows,
            "total": total,
            "truncated": bool(cap is not None and total > cap),
            "limit": cap,
        }

    def history(self, workspace: Path, limit: int = 80) -> list[dict]:
        cap = max(1, min(self.HISTORY_MAX_ITEMS, int(limit)))
        fmt = "%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1e"
        proc = run_git(
            [
                "git",
                "-C",
                str(workspace),
                "log",
                f"-n{cap}",
                "--date=iso-strict",
                f"--pretty=format:{fmt}",
                "--first-parent",
                "HEAD",
            ]
        )
        if proc.returncode != 0:
            detail = proc.stderr or proc.stdout or ""
            detail_lower = str(detail).strip().lower()
            # Empty repositories stay at v0 (unborn main): treat history as empty.
            if (
                "does not have any commits yet" in detail_lower
                or "bad revision 'head'" in detail_lower
                or (
                    "ambiguous argument 'head'" in detail_lower
                    and "unknown revision or path not in the working tree" in detail_lower
                )
            ):
                return []
            raise RuntimeError(proc.stderr or proc.stdout)
        rows: list[dict] = []
        for block in proc.stdout.split("\x1e"):
            row = block.strip()
            if not row:
                continue
            parts = row.split("\x1f")
            if len(parts) < 5:
                continue
            rows.append(
                {
                    "commit": parts[0],
                    "short": parts[1],
                    "author": parts[2],
                    "date": parts[3],
                    "subject": parts[4],
                }
            )
        return rows

    def commit(self, workspace: Path, message: str, name: str, email: str) -> str:
        if self._rebase_active(workspace):
            raise RuntimeError("rebase in progress; resolve conflicts and continue/abort rebase first")
        self._assert_on_main(workspace)
        run_git(["git", "-C", str(workspace), "config", "user.name", name])
        run_git(["git", "-C", str(workspace), "config", "user.email", email])
        run_git(["git", "-C", str(workspace), "add", "."])
        # Never allow internal workspace lock files to enter repository history.
        run_git(
            [
                "git",
                "-C",
                str(workspace),
                "reset",
                "--quiet",
                "--",
                ":(glob).*",
                ":(glob)**/.*",
            ]
        )
        proc = run_git(["git", "-C", str(workspace), "commit", "-m", message])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"])
        if head.returncode != 0:
            raise RuntimeError(head.stderr or head.stdout or "unable to resolve committed head")
        resolved_head = head.stdout.strip()
        if not resolved_head:
            raise RuntimeError("unable to resolve committed head")
        return resolved_head

    def _sync_local_origin_head(self, workspace: Path, branch: str) -> None:
        branch_name = str(branch or "").strip() or "main"
        remote_url_proc = run_git(["git", "-C", str(workspace), "remote", "get-url", "origin"])
        if remote_url_proc.returncode != 0:
            return
        remote_url = remote_url_proc.stdout.strip()
        if not remote_url:
            return
        remote_path = Path(remote_url)
        if not remote_path.is_absolute() or (not remote_path.exists()) or (not remote_path.is_dir()):
            return
        run_git(["git", "--git-dir", str(remote_path), "symbolic-ref", "HEAD", f"refs/heads/{branch_name}"])

    def rollback_last_commit(self, workspace: Path, expected_head: str = "") -> str:
        if self._rebase_active(workspace):
            raise RuntimeError("rebase in progress; resolve conflicts and continue/abort rebase first")
        self._assert_on_main(workspace)
        expected = str(expected_head or "").strip()
        current_head_proc = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"])
        if current_head_proc.returncode != 0:
            raise RuntimeError(current_head_proc.stderr or current_head_proc.stdout)
        current_head = current_head_proc.stdout.strip()
        if expected and current_head != expected:
            raise RuntimeError("head changed; cannot rollback commit safely")
        parent_proc = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD^"])
        if parent_proc.returncode != 0:
            raise RuntimeError(parent_proc.stderr or parent_proc.stdout)
        proc = run_git(["git", "-C", str(workspace), "reset", "--mixed", "HEAD^"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        new_head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"])
        if new_head.returncode != 0:
            raise RuntimeError(new_head.stderr or new_head.stdout)
        return new_head.stdout.strip()

    def push(self, workspace: Path, branch: str) -> str:
        if str(branch or "main") != "main":
            raise RuntimeError("only main is supported")
        self._assert_on_main(workspace)
        proc = run_git(["git", "-C", str(workspace), "push", "origin", "HEAD:main"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        self._sync_local_origin_head(workspace, branch)
        return proc.stdout + proc.stderr

    def _status_entries(self, workspace: Path) -> list[dict[str, str]]:
        proc = run_git(["git", "-C", str(workspace), "status", "--short", "--untracked-files=all"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        entries: list[dict[str, str]] = []
        for raw in proc.stdout.splitlines():
            line = raw.rstrip("\n")
            if not line or self._is_hidden_status_line(line):
                continue
            if len(line) < 3:
                continue
            code = line[:2]
            path_part = self._normalize_status_path(line[3:].strip())
            source_path = ""
            link_path = path_part
            if " -> " in path_part:
                before, after = path_part.split(" -> ", 1)
                source_path = before
                link_path = after
            entries.append(
                {
                    "code": code,
                    "path": path_part,
                    "link_path": link_path,
                    "source_path": source_path,
                    "kind": self._status_kind(code, path_part),
                }
            )
        return entries

    def _status_entry_for_path(self, workspace: Path, rel_path: str) -> dict[str, str] | None:
        normalized = self._normalize_status_path(rel_path)
        if not normalized:
            raise ValueError("path is required")
        for entry in self._status_entries(workspace):
            if entry["link_path"] == normalized:
                return entry
        return None

    def _upstream_blob_exists(self, workspace: Path, upstream_ref: str, rel_path: str) -> bool:
        proc = run_git(["git", "-C", str(workspace), "cat-file", "-e", f"{upstream_ref}:{rel_path}"])
        return proc.returncode == 0

    def _read_upstream_blob_bytes(self, workspace: Path, upstream_ref: str, rel_path: str) -> bytes:
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="git-upstream-blob-", suffix=".bin")
            os.close(fd)
            tmp_path = Path(tmp_name)
            proc = run_git(
                ["git", "-C", str(workspace), "show", f"{upstream_ref}:{rel_path}"],
                stdout_path=tmp_path,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr or proc.stdout or "failed to read upstream blob")
            return tmp_path.read_bytes()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _reconcile_safe_untracked_pull_conflicts(self, workspace: Path, upstream_ref: str) -> list[str]:
        skipped_paths: list[str] = []
        for entry in self._status_entries(workspace):
            if entry["code"] != "??":
                continue
            rel_path = entry["link_path"]
            if not rel_path or (not self._upstream_blob_exists(workspace, upstream_ref, rel_path)):
                continue
            target = self._resolve_user_path(workspace, rel_path)
            if (not target.exists()) or (not target.is_file()):
                continue
            local_bytes = target.read_bytes()
            if local_bytes:
                upstream_bytes = self._read_upstream_blob_bytes(workspace, upstream_ref, rel_path)
                if local_bytes != upstream_bytes:
                    continue
            target.unlink()
            skipped_paths.append(rel_path)
        return skipped_paths

    def _blocking_untracked_pull_conflicts(self, workspace: Path, upstream_ref: str) -> list[str]:
        blocked: list[str] = []
        for entry in self._status_entries(workspace):
            if entry["code"] != "??":
                continue
            rel_path = entry["link_path"]
            if not rel_path or (not self._upstream_blob_exists(workspace, upstream_ref, rel_path)):
                continue
            target = self._resolve_user_path(workspace, rel_path)
            if target.exists():
                blocked.append(rel_path)
        return blocked

    def pull(self, workspace: Path, branch: str) -> str:
        if str(branch or "main") != "main":
            raise RuntimeError("only main is supported")
        self._assert_on_main(workspace)
        fetch = run_git(["git", "-C", str(workspace), "fetch", "origin", "main"])
        if fetch.returncode != 0:
            raise RuntimeError(fetch.stderr or fetch.stdout)
        skipped_paths = self._reconcile_safe_untracked_pull_conflicts(workspace, "origin/main")
        proc = run_git(["git", "-C", str(workspace), "pull", "--rebase", "--autostash", "origin", "main"])
        if proc.returncode != 0:
            blocked_paths = self._blocking_untracked_pull_conflicts(workspace, "origin/main")
            if blocked_paths:
                detail = ", ".join(blocked_paths[:5])
                if len(blocked_paths) > 5:
                    detail = f"{detail}, ... (+{len(blocked_paths) - 5} more)"
                if skipped_paths:
                    raise RuntimeError(
                        f"pull blocked by untracked files that differ from upstream after skipping {len(skipped_paths)} safe path(s): {detail}"
                    )
                raise RuntimeError(f"pull blocked by untracked files that differ from upstream: {detail}")
            raise RuntimeError(proc.stderr or proc.stdout)
        if skipped_paths:
            detail = ", ".join(skipped_paths[:5])
            if len(skipped_paths) > 5:
                detail = f"{detail}, ... (+{len(skipped_paths) - 5} more)"
            return f"pull ok; skipped {len(skipped_paths)} safe untracked path(s): {detail}"
        return "pull ok"

    def _discard_local_changes(self, workspace: Path) -> None:
        reset = run_git(["git", "-C", str(workspace), "reset", "--hard", "HEAD"])
        if reset.returncode != 0:
            raise RuntimeError(reset.stderr or reset.stdout or "failed to discard local changes")
        clean = run_git(["git", "-C", str(workspace), "clean", "-fd"])
        if clean.returncode != 0:
            raise RuntimeError(clean.stderr or clean.stdout or "failed to clean untracked files")

    def restore_revision_to_working_copy(self, workspace: Path, revision: str) -> str:
        target = str(revision or "").strip()
        if not target:
            raise RuntimeError("revision is required")
        if self._rebase_active(workspace):
            raise RuntimeError("rebase in progress; resolve conflicts and continue/abort rebase first")
        self._assert_on_main(workspace)
        self._discard_local_changes(workspace)
        self.pull(workspace, "main")

        resolved = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", f"{target}^{{commit}}"])
        if resolved.returncode != 0:
            raise RuntimeError(resolved.stderr or resolved.stdout or "invalid revision")
        commit = resolved.stdout.strip()
        if not commit:
            raise RuntimeError("invalid revision")

        restore = run_git(["git", "-C", str(workspace), "restore", "--source", commit, "--staged", "--worktree", ":/"])
        if restore.returncode != 0:
            raise RuntimeError(restore.stderr or restore.stdout or "failed to restore revision")

        run_git(
            [
                "git",
                "-C",
                str(workspace),
                "reset",
                "--quiet",
                "--",
                ":(glob).*",
                ":(glob)**/.*",
            ]
        )
        return commit

    def _current_branch(self, workspace: Path) -> str:
        proc = run_git(["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"])
        branch = proc.stdout.strip()
        if proc.returncode == 0 and branch and branch != "HEAD":
            return branch

        # For unborn branches (v0), rev-parse fails; resolve symbolic HEAD instead.
        symbolic = run_git(["git", "-C", str(workspace), "symbolic-ref", "--quiet", "--short", "HEAD"])
        symbolic_branch = symbolic.stdout.strip()
        if symbolic.returncode == 0 and symbolic_branch:
            return symbolic_branch

        detail = proc.stderr or proc.stdout or symbolic.stderr or symbolic.stdout or "unable to resolve branch"
        raise RuntimeError(detail)

    def _assert_on_main(self, workspace: Path) -> None:
        branch = self._current_branch(workspace)
        if branch != "main":
            raise RuntimeError("only main is supported; update workspace to main")

    def _rebase_active(self, workspace: Path) -> bool:
        git_dir = workspace / ".git"
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def _conflicted_files(self, workspace: Path) -> list[str]:
        proc = run_git(["git", "-C", str(workspace), "diff", "--name-only", "--diff-filter=U"])
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def rebase_continue(self, workspace: Path) -> str:
        if not self._rebase_active(workspace):
            raise RuntimeError("no rebase in progress")
        proc = run_git(["git", "-C", str(workspace), "rebase", "--continue"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        return proc.stdout + proc.stderr

    def rebase_abort(self, workspace: Path) -> str:
        if not self._rebase_active(workspace):
            raise RuntimeError("no rebase in progress")
        proc = run_git(["git", "-C", str(workspace), "rebase", "--abort"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        return proc.stdout + proc.stderr

    def list_files_capped(self, workspace: Path, rel: str = ".", limit: int | None = None) -> tuple[list[str], bool]:
        workspace_root = workspace.resolve()
        base = self._resolve_user_path(workspace, rel, allow_workspace_root=True)
        paths: list[str] = []
        truncated = False
        capped = max(1, int(limit)) if limit is not None else None
        stop_scan = False
        for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if workspace_root not in dir_root_resolved.parents and workspace_root != dir_root_resolved:
                dirnames[:] = []
                continue
            try:
                rel_root = dir_root.relative_to(workspace)
            except ValueError:
                dirnames[:] = []
                continue
            rel_prefix = "" if rel_root == Path(".") else rel_root.as_posix()
            candidate_dirs: list[str] = []
            for name in dirnames:
                d = dir_root / name
                if name.startswith(".") or d.is_symlink():
                    continue
                candidate_dirs.append(name)

            next_dirs: list[str] = []
            for name in sorted(candidate_dirs):
                next_dirs.append(name)
                if capped is not None and len(paths) >= capped:
                    truncated = True
                    stop_scan = True
                    break
                if rel_prefix:
                    paths.append(f"{rel_prefix}/{name}")
                else:
                    paths.append(name)
            if stop_scan:
                dirnames[:] = []
                break
            dirnames[:] = next_dirs

            safe_files: list[str] = []
            for name in filenames:
                p = dir_root / name
                if name.startswith(".") or p.is_symlink():
                    continue
                if not p.is_file():
                    continue
                safe_files.append(name)

            for name in sorted(safe_files):
                if capped is not None and len(paths) >= capped:
                    truncated = True
                    stop_scan = True
                    break
                if rel_prefix:
                    paths.append(f"{rel_prefix}/{name}")
                else:
                    paths.append(name)
            if stop_scan:
                break
        if capped is None:
            paths.sort()
        return paths, truncated

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
        p.write_text(content, encoding="utf-8", newline="\n")

    def delete_path(self, workspace: Path, rel_path: str) -> None:
        p = self._resolve_user_path(workspace, rel_path)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def discard_path(self, workspace: Path, rel_path: str) -> None:
        if self._rebase_active(workspace):
            raise RuntimeError("rebase in progress; resolve conflicts and continue/abort rebase first")
        normalized = self._normalize_status_path(rel_path)
        if not normalized:
            raise ValueError("path is required")
        entry = self._status_entry_for_path(workspace, normalized)
        if entry is None:
            raise ValueError("path not found in workspace changes")
        if entry["code"] == "??":
            self.delete_path(workspace, normalized)
            return
        restore_targets = [normalized]
        source_path = entry["source_path"]
        if entry["kind"] == "renamed" and source_path:
            restore_targets = [source_path, normalized]
        restore = run_git(
            [
                "git",
                "-C",
                str(workspace),
                "restore",
                "--source",
                "HEAD",
                "--staged",
                "--worktree",
                "--",
                *restore_targets,
            ]
        )
        if restore.returncode != 0:
            raise RuntimeError(restore.stderr or restore.stdout or "failed to discard local changes")

    def rename_path(self, workspace: Path, old_rel: str, new_rel: str) -> None:
        src = self._resolve_user_path(workspace, old_rel)
        dst = self._resolve_user_path(workspace, new_rel)
        if not src.exists():
            raise ValueError("path not found")
        if dst.exists() and dst.is_dir():
            raise ValueError("destination is a directory")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    def _synthetic_added_diff(self, rel_path: str, content: str, *, truncated: bool) -> str:
        lines = str(content or "").splitlines()
        out: list[str] = []
        out.append(f"diff --git a/{rel_path} b/{rel_path}\n")
        out.append("new file mode 100644\n")
        out.append("--- /dev/null\n")
        out.append(f"+++ b/{rel_path}\n")
        out.append(f"@@ -0,0 +1,{len(lines)} @@\n")
        for line in lines:
            out.append(f"+{line}\n")
        if truncated:
            out.append("+... [truncated]\n")
        return "".join(out)

    def diff_for_path(self, workspace: Path, rel_path: str, max_chars: int | None = None) -> tuple[str, bool]:
        target = self._resolve_user_path(workspace, rel_path)
        if target.exists() and target.is_dir():
            raise ValueError("path is a directory")
        normalized = str(rel_path or "").strip()
        if not normalized:
            raise ValueError("path is required")

        pieces: list[str] = []

        unstaged = run_git(["git", "-C", str(workspace), "diff", "--", normalized])
        if unstaged.returncode != 0:
            raise RuntimeError(unstaged.stderr or unstaged.stdout)
        if unstaged.stdout:
            pieces.append(unstaged.stdout)

        staged = run_git(["git", "-C", str(workspace), "diff", "--cached", "--", normalized])
        if staged.returncode != 0:
            raise RuntimeError(staged.stderr or staged.stdout)
        if staged.stdout:
            pieces.append(staged.stdout)

        status = run_git(["git", "-C", str(workspace), "status", "--short", "--untracked-files=all", "--", normalized])
        if status.returncode != 0:
            raise RuntimeError(status.stderr or status.stdout)

        has_untracked = any((line.startswith("??") for line in status.stdout.splitlines()))
        if has_untracked and target.exists() and target.is_file():
            cap = self.DIFF_MAX_CHARS if max_chars is None else max(1, int(max_chars))
            # Reserve a small budget for synthetic headers so text truncation remains predictable.
            body_cap = max(1, cap - 256)
            text, truncated = self._read_text_prefix(target, body_cap)
            pieces.append(self._synthetic_added_diff(normalized, text, truncated=truncated))

        combined = self._filter_hidden_diff("".join(pieces))
        cap = self.DIFF_MAX_CHARS if max_chars is None else max(1, int(max_chars))
        return self._truncate_text(combined, cap)

    def diff_for_revision(self, workspace: Path, revision: str, max_chars: int | None = None) -> tuple[str, bool]:
        target = str(revision or "").strip()
        if not target:
            raise ValueError("revision is required")
        resolved = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", f"{target}^{{commit}}"])
        if resolved.returncode != 0:
            raise RuntimeError(resolved.stderr or resolved.stdout or "invalid revision")
        commit = resolved.stdout.strip()
        if not commit:
            raise RuntimeError("invalid revision")
        show = run_git(
            [
                "git",
                "-C",
                str(workspace),
                "show",
                "--pretty=format:",
                "--no-color",
                commit,
                "--",
                ".",
                ":(exclude).*",
                ":(exclude)**/.*",
            ]
        )
        if show.returncode != 0:
            raise RuntimeError(show.stderr or show.stdout or "failed to read revision diff")
        filtered = self._filter_hidden_diff(show.stdout or "")
        cap = self.DIFF_MAX_CHARS if max_chars is None else max(1, int(max_chars))
        return self._truncate_text(filtered, cap)
