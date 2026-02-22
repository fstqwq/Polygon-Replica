from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.artifact_service import ArtifactService
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService


class PreviewService:
    def __init__(self, db: DB, workspace_service: WorkspaceService, artifacts: ArtifactService):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts

    def _try_reuse_preview(
        self,
        problem: str,
        problem_id: int,
        source_commit: str,
        current_preview_id: str,
        artifacts,
    ) -> str | None:
        rows = self.db.fetch_all(
            "SELECT id,artifact_path FROM previews WHERE problem_id=? AND source_commit=? AND status='ok' AND id<>? ORDER BY created_at DESC LIMIT 20",
            [problem_id, source_commit, current_preview_id],
        )

        for row in rows:
            root = Path(row["artifact_path"]) if row["artifact_path"] else self.workspace_service.settings.artifacts_root / problem / row["id"]
            src_pdf = root / "statement_preview" / "statement.pdf"
            src_log = root / "logs" / "latex.log"
            if not src_pdf.exists() or not src_log.exists():
                continue

            artifacts.statement_preview.mkdir(parents=True, exist_ok=True)
            artifacts.logs.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_pdf, artifacts.statement_preview / "statement.pdf")
            shutil.copy2(src_log, artifacts.logs / "latex.log")
            return str(row["id"])
        return None

    def compile_preview(self, problem: str, username: str, commit: str | None = None) -> str:
        build_id = f"p-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username)
        workspace = Path(ctx["workspace"]["path"])
        source_commit = commit or (ctx["workspace"].get("head_commit") or "")
        source_ref = ctx["workspace"].get("branch") or "main"
        ws_row = self.db.fetch_one(
            "SELECT id FROM workspaces WHERE problem_id=? AND user_id=?",
            [ctx["problem"]["id"], ctx["user"]["id"]],
        )
        self.db.execute(
            "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [build_id, ctx["problem"]["id"], ws_row["id"], source_commit, source_ref, "running", "", now_iso()],
        )
        artifacts = self.artifacts.prepare(problem, build_id)
        self.db.execute("UPDATE previews SET artifact_path=? WHERE id=?", [str(artifacts.root), build_id])

        # Commit-based previews are immutable; reuse existing compiled artifacts when available.
        if commit:
            reused_from = self._try_reuse_preview(problem, ctx["problem"]["id"], source_commit, build_id, artifacts)
            if reused_from is not None:
                summary = {"pdf": "statement_preview/statement.pdf", "reused_from": reused_from}
                self.db.execute(
                    "UPDATE previews SET status=?, summary_json=?, finished_at=? WHERE id=?",
                    ["ok", json.dumps(summary), now_iso(), build_id],
                )
                return build_id

        with self.workspace_service.workspace_lock(workspace):
            snapshot = self.workspace_service.create_snapshot(workspace, commit)

        tex = snapshot / "statement/main.tex"
        log = artifacts.logs / "latex.log"
        status = "ok"
        summary: dict[str, object] = {}
        try:
            if not tex.exists():
                status = "failed"
                summary = {"error": "statement/main.tex not found"}
                log.write_text("statement/main.tex not found\n", encoding="utf-8")
                return build_id

            cmd = [
                "pdflatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-output-directory={artifacts.statement_preview}",
                str(tex),
            ]
            try:
                proc = run_cmd(cmd, cwd=snapshot, timeout=120)
                log.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
                generated = artifacts.statement_preview / f"{tex.stem}.pdf"
                target = artifacts.statement_preview / "statement.pdf"
                if generated.exists() and generated != target:
                    target.write_bytes(generated.read_bytes())
                if proc.returncode != 0 or not target.exists():
                    status = "failed"
                    summary = {"error": "latex compile failed", "returncode": proc.returncode}
                else:
                    summary = {"pdf": "statement_preview/statement.pdf"}
            except FileNotFoundError as exc:
                status = "failed"
                summary = {"error": str(exc)}
                log.write_text(str(exc) + "\n", encoding="utf-8")
        finally:
            self.db.execute(
                "UPDATE previews SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [status, json.dumps(summary), now_iso(), build_id],
            )
            shutil.rmtree(snapshot.parent, ignore_errors=True)
        return build_id
