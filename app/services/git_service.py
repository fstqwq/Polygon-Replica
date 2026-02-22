from __future__ import annotations

import shutil
from pathlib import Path

from app.services.util import run_cmd


class GitService:
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

    def list_files(self, workspace: Path, rel: str = ".") -> list[str]:
        base = (workspace / rel).resolve()
        if workspace.resolve() not in base.parents and base != workspace.resolve():
            raise ValueError("invalid path")
        paths: list[str] = []
        for p in sorted(base.rglob("*")):
            if ".git" in p.parts:
                continue
            paths.append(str(p.relative_to(workspace)))
        return paths

    def read_file(self, workspace: Path, rel_path: str) -> str:
        p = (workspace / rel_path).resolve()
        if workspace.resolve() not in p.parents:
            raise ValueError("invalid path")
        return p.read_text(encoding="utf-8")

    def write_file(self, workspace: Path, rel_path: str, content: str) -> None:
        p = (workspace / rel_path).resolve()
        if workspace.resolve() not in p.parents:
            raise ValueError("invalid path")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def delete_path(self, workspace: Path, rel_path: str) -> None:
        p = (workspace / rel_path).resolve()
        if workspace.resolve() not in p.parents:
            raise ValueError("invalid path")
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def rename_path(self, workspace: Path, old_rel: str, new_rel: str) -> None:
        src = (workspace / old_rel).resolve()
        dst = (workspace / new_rel).resolve()
        if workspace.resolve() not in src.parents or workspace.resolve() not in dst.parents:
            raise ValueError("invalid path")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
