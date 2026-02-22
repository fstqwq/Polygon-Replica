from __future__ import annotations

import uuid
from pathlib import Path

from app.db import DB
from app.services.artifact_service import ArtifactService
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService


class PreviewService:
    def __init__(self, db: DB, workspace_service: WorkspaceService, artifacts: ArtifactService):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts

    def compile_preview(self, problem: str, username: str, commit: str | None = None) -> str:
        build_id = f"p-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username)
        workspace = Path(ctx["workspace"]["path"])
        with self.workspace_service.workspace_lock(workspace):
            snapshot = self.workspace_service.create_snapshot(workspace, commit)
        artifacts = self.artifacts.prepare(problem, build_id)

        tex = snapshot / "statement/main.tex"
        log = artifacts.logs / "latex.log"
        if not tex.exists():
            log.write_text("statement/main.tex not found\n", encoding="utf-8")
            return build_id

        cmd = [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={artifacts.statement_preview}",
            str(tex),
        ]
        proc = run_cmd(cmd, cwd=snapshot, timeout=120)
        log.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
        generated = artifacts.statement_preview / f"{tex.stem}.pdf"
        target = artifacts.statement_preview / "statement.pdf"
        if generated.exists() and generated != target:
            target.write_bytes(generated.read_bytes())
        return build_id
