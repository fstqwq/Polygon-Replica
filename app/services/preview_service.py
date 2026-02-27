from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.artifact_service import ArtifactService
from app.services.sandbox import ExecSpec, SandboxBackend, create_sandbox_backend
from app.services.statement_template import render_statement_main, statement_sources_signature
from app.services.util import is_canonical_artifact_id, run_cmd
from app.services.workspace_service import WorkspaceService


class PreviewService:
    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        artifacts: ArtifactService,
        sandbox_backend: SandboxBackend | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.sandbox = sandbox_backend or create_sandbox_backend()
        self.tex_timeout_sec = self._env_int("POLYGONLIKE_TEX_TIMEOUT_SEC", default=120, min_value=5, max_value=1800)
        self.tex_memory_mb = self._env_int("POLYGONLIKE_TEX_MEMORY_MB", default=1024, min_value=16, max_value=262144)
        self.tex_process_limit = self._env_int("POLYGONLIKE_TEX_PROCESS_LIMIT", default=64, min_value=1, max_value=4096)
        self.tex_output_kb = self._env_int("POLYGONLIKE_TEX_OUTPUT_KB", default=131072, min_value=64, max_value=1048576)

    def _env_int(self, key: str, default: int, min_value: int, max_value: int) -> int:
        raw = os.getenv(key)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def _latex_compile_error_detail(self, output_text: str, returncode: int | None) -> str:
        text = str(output_text or "")
        low = text.lower()
        if ("can't find the format file" in low) and ("pdflatex.fmt" in low):
            return "missing LaTeX format pdflatex.fmt; run `fmtutil -user --byfmt pdflatex` (or `sudo fmtutil-sys --byfmt pdflatex`)"
        if int(returncode or 0) != 0:
            return "latex compile failed"
        return ""

    def _is_safe_regular_file(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        if path.is_symlink() or not path.exists() or not path.is_file():
            return False
        try:
            resolved_root = root_resolved if root_resolved is not None else root.resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents or resolved_root == resolved

    def _find_cached_preview_id(
        self,
        problem: str,
        problem_id: int,
        workspace_id: int,
        source_commit: str | None = None,
        statement_signature: str | None = None,
    ) -> str | None:
        source = str(source_commit or "").strip()
        signature = str(statement_signature or "").strip()
        try:
            artifact_base = (self.workspace_service.settings.artifacts_root / problem).resolve()
        except OSError:
            return None
        sql = (
            "SELECT id,summary_json FROM previews "
            "WHERE problem_id=? AND workspace_id=? AND status='ok'"
        )
        params: list[object] = [problem_id, workspace_id]
        if source_commit is not None:
            sql += " AND source_commit=?"
            params.append(source)
        sql += " ORDER BY created_at DESC,id DESC LIMIT 100"
        rows = self.db.fetch_all(
            sql,
            params,
        )
        for row in rows:
            preview_id = str(row["id"] or "").strip()
            if signature:
                cached_signature = self._summary_statement_signature(row["summary_json"])
                if cached_signature != signature:
                    continue
            root = self._preview_artifact_root(problem, preview_id, artifact_base=artifact_base)
            if root is None:
                continue
            cached_pdf = root / "statement_preview" / "statement.pdf"
            cached_log = root / "logs" / "latex.log"
            if not self._is_safe_regular_file(root, cached_pdf, root_resolved=root):
                continue
            if not self._is_safe_regular_file(root, cached_log, root_resolved=root):
                continue
            return preview_id
        return None

    def _summary_statement_signature(self, raw: object) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("statement_signature") or "").strip()

    def _preview_artifact_root(
        self,
        problem: str,
        preview_id: str,
        artifact_base: Path | None = None,
    ) -> Path | None:
        if not is_canonical_artifact_id(preview_id):
            return None
        try:
            base = artifact_base if artifact_base is not None else (self.workspace_service.settings.artifacts_root / problem).resolve()
        except OSError:
            return None
        root = (base / preview_id).resolve()
        try:
            rel = root.relative_to(base)
        except ValueError:
            return None
        if len(rel.parts) != 1 or rel.parts[0] != preview_id:
            return None
        return root

    def _prune_workspace_preview_history(
        self,
        problem: str,
        problem_id: int,
        workspace_id: int,
        keep_preview_id: str,
    ) -> None:
        keep_id = str(keep_preview_id or "").strip()
        if not keep_id:
            return
        rows = self.db.fetch_all(
            "SELECT id FROM previews WHERE problem_id=? AND workspace_id=? AND id<>?",
            [problem_id, workspace_id, keep_id],
        )
        if not rows:
            return
        for row in rows:
            stale_id = str(row["id"] or "").strip()
            root = self._preview_artifact_root(problem, stale_id)
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
        self.db.execute(
            "DELETE FROM previews WHERE problem_id=? AND workspace_id=? AND id<>?",
            [problem_id, workspace_id, keep_id],
        )

    def find_cached_preview_id(
        self,
        problem: str,
        problem_id: int,
        workspace_id: int,
        source_commit: str | None = None,
        statement_signature: str | None = None,
    ) -> str | None:
        return self._find_cached_preview_id(
            problem,
            problem_id,
            workspace_id,
            source_commit=source_commit,
            statement_signature=statement_signature,
        )

    def prune_workspace_preview_history(
        self,
        problem: str,
        problem_id: int,
        workspace_id: int,
        keep_preview_id: str,
    ) -> None:
        self._prune_workspace_preview_history(problem, problem_id, workspace_id, keep_preview_id)

    def compile_preview(self, problem: str, username: str) -> str:
        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(ctx["workspace"]["path"])
        problem_title = str(ctx["problem"].get("name") or "").strip()
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        source_commit = ""
        source_ref = (ctx["workspace"].get("branch") or "main")
        statement_signature = ""
        snapshot: Path | None = None

        with self.workspace_service.workspace_lock(workspace):
            ws_status = self.workspace_service.read_workspace_status(workspace)
            head = str(ws_status.get("head_commit") or "").strip()
            branch = str(ws_status.get("branch") or "").strip() or source_ref
            dirty = bool(ws_status.get("dirty"))
            statement_signature = statement_sources_signature(workspace, problem_title=problem_title)
            if not head:
                head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
            if head:
                source_commit = "" if dirty else head
                source_ref = branch
            if head and not dirty:
                cached_id = self._find_cached_preview_id(
                    problem,
                    problem_id,
                    workspace_id,
                    source_commit=head,
                    statement_signature=statement_signature,
                )
                if cached_id is not None:
                    self._prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                    return cached_id
            if dirty:
                cached_id = self._find_cached_preview_id(
                    problem,
                    problem_id,
                    workspace_id,
                    source_commit=None,
                    statement_signature=statement_signature,
                )
                if cached_id is not None:
                    self._prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                    return cached_id
            # Legacy rows may miss `statement_signature`; only allow
            # signature-agnostic cache fallback when signature is unavailable.
            if not statement_signature:
                if head and not dirty:
                    cached_id = self._find_cached_preview_id(
                        problem,
                        problem_id,
                        workspace_id,
                        source_commit=head,
                        statement_signature=None,
                    )
                    if cached_id is not None:
                        self._prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                        return cached_id
                if dirty:
                    cached_id = self._find_cached_preview_id(
                        problem,
                        problem_id,
                        workspace_id,
                        source_commit=None,
                        statement_signature=None,
                    )
                    if cached_id is not None:
                        self._prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                        return cached_id
            snapshot = self.workspace_service.create_snapshot(
                workspace,
                None,
                workspace_head=head,
                workspace_dirty=dirty,
            )

        build_id = f"p-{uuid.uuid4().hex[:12]}"
        artifacts = self.artifacts.prepare(problem, build_id)
        self.db.execute(
            "INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [build_id, problem_id, workspace_id, source_commit, source_ref, "running", str(artifacts.root), now_iso()],
        )

        log = artifacts.logs / "latex.log"
        status = "ok"
        summary: dict[str, object] = {"statement_signature": statement_signature}
        try:
            tex = render_statement_main(snapshot / "statement", problem_title=problem_title)

            cmd = [
                "pdflatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                str(tex.name),
            ]
            try:
                proc = self.sandbox.run(
                    ExecSpec(
                        command=cmd,
                        cwd=tex.parent,
                        timeout_sec=self.tex_timeout_sec,
                        memory_mb=self.tex_memory_mb,
                        process_limit=self.tex_process_limit,
                        output_kb=self.tex_output_kb,
                    )
                )
                output_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
                log_text = output_text
                generated_log = tex.parent / f"{tex.stem}.log"
                if generated_log.exists():
                    try:
                        log_text = generated_log.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        log_text = output_text
                log.write_text(log_text, encoding="utf-8")
                generated = tex.parent / f"{tex.stem}.pdf"
                target = artifacts.statement_preview / "statement.pdf"
                if generated.exists() and generated != target:
                    shutil.copy2(generated, target)
                if proc.timed_out:
                    status = "failed"
                    summary = {"error": "latex compile timeout", "statement_signature": statement_signature}
                elif int(proc.returncode or 0) != 0 or not target.exists():
                    error_detail = self._latex_compile_error_detail(log_text, proc.returncode)
                    if not error_detail:
                        error_detail = "latex compile failed"
                    status = "failed"
                    summary = {
                        "error": error_detail,
                        "returncode": proc.returncode,
                        "statement_signature": statement_signature,
                    }
                else:
                    summary = {"pdf": "statement_preview/statement.pdf", "statement_signature": statement_signature}
            except FileNotFoundError as exc:
                status = "failed"
                summary = {"error": str(exc), "statement_signature": statement_signature}
                log.write_text(str(exc) + "\n", encoding="utf-8")
        except Exception as exc:
            status = "failed"
            summary = {"error": str(exc), "statement_signature": statement_signature}
            if not log.exists():
                log.write_text(str(exc) + "\n", encoding="utf-8")
        finally:
            self.db.execute(
                "UPDATE previews SET source_commit=?, source_ref=?, status=?, summary_json=?, finished_at=? WHERE id=?",
                [source_commit, source_ref, status, json.dumps(summary), now_iso(), build_id],
            )
            self._prune_workspace_preview_history(problem, problem_id, workspace_id, build_id)
            if snapshot is not None:
                shutil.rmtree(snapshot.parent, ignore_errors=True)
        return build_id
