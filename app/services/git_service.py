from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.services.util import run_cmd


class GitService:
    def _resolve_user_path(self, workspace: Path, rel_path: str, allow_workspace_root: bool = False) -> Path:
        ws_root = workspace.resolve()
        p = (workspace / rel_path).resolve()
        if ws_root not in p.parents and p != ws_root:
            raise ValueError("invalid path")
        if not allow_workspace_root and p == ws_root:
            raise ValueError("invalid path")
        rel = p.relative_to(ws_root)
        if ".git" in rel.parts or ".polygonlike.lock" in rel.parts:
            raise ValueError("reserved path")
        return p

    def status(self, workspace: Path) -> dict:
        proc = run_cmd(["git", "-C", str(workspace), "status", "--short", "--branch"])
        diff = run_cmd(["git", "-C", str(workspace), "diff", "--", "."])
        return {"status": proc.stdout, "diff": diff.stdout}

    def commit(self, workspace: Path, message: str, name: str, email: str) -> str:
        run_cmd(["git", "-C", str(workspace), "config", "user.name", name])
        run_cmd(["git", "-C", str(workspace), "config", "user.email", email])
        run_cmd(["git", "-C", str(workspace), "add", "."])
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
        return proc.stdout + proc.stderr

    def merge(self, workspace: Path, source_branch: str, target_branch: str = "main") -> str:
        self.switch_branch(workspace, target_branch)
        proc = run_cmd(["git", "-C", str(workspace), "merge", "--no-ff", source_branch])
        if proc.returncode != 0:
            out = proc.stdout + proc.stderr
            run_cmd(["git", "-C", str(workspace), "merge", "--abort"])
            raise RuntimeError(out)
        return proc.stdout + proc.stderr

    def list_branches(self, workspace: Path) -> list[str]:
        proc = run_cmd(["git", "-C", str(workspace), "branch", "--format", "%(refname:short)"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def list_files(self, workspace: Path, rel: str = ".") -> list[str]:
        workspace_root = workspace.resolve()
        base = self._resolve_user_path(workspace, rel, allow_workspace_root=True)
        paths: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            next_dirs: list[str] = []
            for name in sorted(dirnames):
                d = dir_root / name
                if ".git" in d.parts or d.is_symlink():
                    continue
                try:
                    resolved = d.resolve()
                except OSError:
                    continue
                if workspace_root not in resolved.parents and workspace_root != resolved:
                    continue
                next_dirs.append(name)
                paths.append(str(d.relative_to(workspace)))
            dirnames[:] = next_dirs

            for name in sorted(filenames):
                p = dir_root / name
                if ".git" in p.parts or p.is_symlink():
                    continue
                try:
                    resolved = p.resolve()
                except OSError:
                    continue
                if workspace_root not in resolved.parents and workspace_root != resolved:
                    continue
                paths.append(str(p.relative_to(workspace)))
        paths.sort()
        return paths

    def read_file(self, workspace: Path, rel_path: str) -> str:
        p = self._resolve_user_path(workspace, rel_path)
        return p.read_text(encoding="utf-8")

    def write_file(self, workspace: Path, rel_path: str, content: str) -> None:
        p = self._resolve_user_path(workspace, rel_path)
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
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
