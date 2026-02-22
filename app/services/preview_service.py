from __future__ import annotations

import json
import re
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
            "SELECT id FROM previews WHERE problem_id=? AND source_commit=? AND status='ok' AND id<>? ORDER BY created_at DESC LIMIT 20",
            [problem_id, source_commit, current_preview_id],
        )

        for row in rows:
            root = self._preview_artifact_root(problem, str(row["id"]))
            if root is None:
                continue
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

    def _preview_artifact_root(self, problem: str, preview_id: str) -> Path | None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", preview_id):
            return None
        base = (self.workspace_service.settings.artifacts_root / problem).resolve()
        root = (base / preview_id).resolve()
        try:
            rel = root.relative_to(base)
        except ValueError:
            return None
        if len(rel.parts) != 1 or rel.parts[0] != preview_id:
            return None
        return root

    def compile_preview(self, problem: str, username: str, commit: str | None = None) -> str:
        build_id = f"p-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username)
        workspace = Path(ctx["workspace"]["path"])
        source_commit = "" if commit else (ctx["workspace"].get("head_commit") or "").strip()
        source_ref = commit or (ctx["workspace"].get("branch") or "main")
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

        log = artifacts.logs / "latex.log"
        status = "ok"
        summary: dict[str, object] = {}
        snapshot: Path | None = None
        try:
            if commit:
                source_commit = self.workspace_service.resolve_commit(workspace, commit)
                source_ref = commit
                self.db.execute("UPDATE previews SET source_commit=?, source_ref=? WHERE id=?", [source_commit, source_ref, build_id])

                reused_from = self._try_reuse_preview(problem, ctx["problem"]["id"], source_commit, build_id, artifacts)
                if reused_from is not None:
                    summary = {"pdf": "statement_preview/statement.pdf", "reused_from": reused_from}
                    return build_id
                snapshot = self.workspace_service.create_snapshot(workspace, source_commit)
            else:
                # Clean workspace HEAD preview is immutable until the next workspace mutation.
                with self.workspace_service.workspace_lock(workspace):
                    head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
                    branch = run_cmd(["git", "-C", str(workspace), "branch", "--show-current"]).stdout.strip() or source_ref
                    dirty = self.workspace_service.workspace_is_dirty(workspace)
                    if head:
                        source_commit = head
                        source_ref = branch
                        self.db.execute(
                            "UPDATE previews SET source_commit=?, source_ref=? WHERE id=?",
                            [source_commit, source_ref, build_id],
                        )
                    if head and not dirty:
                        reused_from = self._try_reuse_preview(problem, ctx["problem"]["id"], head, build_id, artifacts)
                        if reused_from is not None:
                            summary = {"pdf": "statement_preview/statement.pdf", "reused_from": reused_from}
                            return build_id
                    snapshot = self.workspace_service.create_snapshot(workspace, None)

            tex = snapshot / "statement/main.tex"
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
                    shutil.copy2(generated, target)
                if proc.returncode != 0 or not target.exists():
                    status = "failed"
                    summary = {"error": "latex compile failed", "returncode": proc.returncode}
                else:
                    summary = {"pdf": "statement_preview/statement.pdf"}
            except FileNotFoundError as exc:
                status = "failed"
                summary = {"error": str(exc)}
                log.write_text(str(exc) + "\n", encoding="utf-8")
        except Exception as exc:
            status = "failed"
            summary = {"error": str(exc)}
            if not log.exists():
                log.write_text(str(exc) + "\n", encoding="utf-8")
        finally:
            self.db.execute(
                "UPDATE previews SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [status, json.dumps(summary), now_iso(), build_id],
            )
            if snapshot is not None:
                shutil.rmtree(snapshot.parent, ignore_errors=True)
        return build_id
