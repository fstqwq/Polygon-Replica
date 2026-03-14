from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from app.db import DB, now_iso
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.artifact import ArtifactService
from app.service.platform.fs.layout import FsManager
from app.service.platform.hashing import sha256_hex_json
from app.service.sandbox.base import ExecSpec, SandboxBackend
from app.service.sandbox.tex_backend import TexSandboxBackend
from app.service.statement.render import render_statement_main
from app.service.statement.signature import statement_sources_signature
from app.service.problem.test_spec import TESTS_SPEC_REL, load_tests_spec, payload_rel_path_for_test
from app.service.platform.process import is_canonical_artifact_id, run_cmd
from app.service.repository.workspace import WorkspaceService

if TYPE_CHECKING:
    from app.service.platform.async_task_cache import AsyncTaskCacheService
    from app.service.verification.service import VerificationService


class PreviewService:
    PREVIEW_CACHE_NAMESPACE = "preview.compile"

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        artifacts: ArtifactService,
        verification_service: VerificationService | None = None,
        sandbox_backend: SandboxBackend | None = None,
        constants: RuntimeValues | None = None,
        async_task_cache_service: AsyncTaskCacheService | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.verification_service = verification_service
        self._async_task_cache_service = async_task_cache_service
        self.fs_manager = FsManager(
            self.workspace_service.settings.artifacts_root,
            self.workspace_service.settings.run_root,
        )
        self.sandbox = sandbox_backend or TexSandboxBackend()
        self.tex_timeout_sec = 120
        self.tex_memory_mb = 1024
        self.tex_process_limit = 64
        self.tex_output_kb = 131072
        self.tex_passes = 2
        self.apply_runtime_values(constants or build_runtime_values())

    def _sample_rows_from_spec(self, workspace: Path) -> list[tuple[int, str, str]]:
        spec_path = workspace / TESTS_SPEC_REL
        try:
            entries = load_tests_spec(spec_path)
        except Exception as exc:
            raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
        rows: list[tuple[int, str, str]] = []
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            if not bool(entry.get("sample")):
                continue
            test_id = str(entry.get("id") or "").strip()
            kind = str(entry.get("kind") or "").strip().lower()
            if kind not in {"manual", "gen"}:
                raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}: {kind or '(empty)'}")
            if not test_id:
                continue
            sample_input = str(entry.get("sample_input") or "")
            sample_output = str(entry.get("sample_output") or "")
            needs_sync = False
            if not sample_input:
                if kind == "gen":
                    needs_sync = True
                else:
                    try:
                        input_rel = Path(payload_rel_path_for_test(test_id, kind))
                        input_path = workspace / input_rel
                        if input_path.is_symlink() or (not input_path.exists()) or (not input_path.is_file()):
                            needs_sync = True
                    except Exception:
                        needs_sync = True
            if (not sample_output) and (not needs_sync):
                answer_path = workspace / "tests" / "answers" / f"{test_id}.ans"
                try:
                    if answer_path.is_symlink() or (not answer_path.exists()) or (not answer_path.is_file()):
                        needs_sync = True
                except Exception:
                    needs_sync = True
            if needs_sync:
                rows.append((index, test_id, kind))
        return rows

    def _copy_sample_payloads_from_verification(self, problem: str, username: str, snapshot: Path) -> dict[str, object]:
        rows = self._sample_rows_from_spec(snapshot)
        if not rows:
            return {"sample_count": 0, "copied": 0, "verification_id": ""}
        if self.verification_service is None:
            raise RuntimeError("preview sample sync requires verification service")
        verification_id = self.verification_service.run_verification(
            problem,
            username,
            sample_only=True,
        )
        verification_row = self.db.fetch_one(
            "SELECT status,summary_json,artifact_path FROM verifications WHERE id=?",
            [verification_id],
        )
        if verification_row is None:
            raise RuntimeError(f"sample verification missing: {verification_id}")
        verification_status = str(verification_row["status"] or "").strip().lower()
        if verification_status != "ok":
            error_text = ""
            try:
                payload = json.loads(str(verification_row["summary_json"] or "{}"))
                if isinstance(payload, dict):
                    error_text = str(payload.get("error") or "").strip()
            except Exception:
                error_text = ""
            if error_text:
                raise RuntimeError(f"sample verification failed ({verification_id}): {error_text}")
            raise RuntimeError(f"sample verification failed ({verification_id})")
        artifact_path = str(verification_row["artifact_path"] or "").strip()
        if not artifact_path:
            raise RuntimeError(f"sample verification has no artifact path: {verification_id}")
        artifact_root = Path(artifact_path)
        tests_dir = artifact_root / "tests"
        ans_dir = artifact_root / "ans"
        if not tests_dir.exists() or not tests_dir.is_dir() or tests_dir.is_symlink():
            raise RuntimeError(f"sample verification missing tests directory: {verification_id}")
        if not ans_dir.exists() or not ans_dir.is_dir() or ans_dir.is_symlink():
            raise RuntimeError(f"sample verification missing ans directory: {verification_id}")
        copied = 0
        snapshot_root = snapshot.resolve()
        for index, test_id, kind in rows:
            source_in = tests_dir / f"{int(index):03d}.in"
            source_ans = ans_dir / f"{int(index):03d}.ans"
            if source_in.is_symlink() or (not source_in.exists()) or (not source_in.is_file()):
                raise RuntimeError(f"sample input missing from verification for test id {test_id} (row {index})")
            if source_ans.is_symlink() or (not source_ans.exists()) or (not source_ans.is_file()):
                raise RuntimeError(f"sample answer missing from verification for test id {test_id} (row {index})")
            input_rel = Path(payload_rel_path_for_test(test_id, kind))
            answer_rel = Path("tests") / "answers" / f"{test_id}.ans"
            input_target = (snapshot / input_rel).resolve()
            answer_target = (snapshot / answer_rel).resolve()
            if snapshot_root not in input_target.parents or snapshot_root not in answer_target.parents:
                raise RuntimeError(f"invalid sample target path for test id {test_id}")
            input_target.parent.mkdir(parents=True, exist_ok=True)
            answer_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_in, input_target)
            shutil.copy2(source_ans, answer_target)
            copied += 1
        return {"sample_count": len(rows), "copied": copied, "verification_id": verification_id}

    def _coerce_int(self, raw: object, default: int, min_value: int, max_value: int) -> int:
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.tex_timeout_sec = self._coerce_int(
            values.get("PREVIEW_TEX_TIMEOUT_SEC", 120),
            default=120,
            min_value=5,
            max_value=1800,
        )
        self.tex_memory_mb = self._coerce_int(
            values.get("PREVIEW_TEX_MEMORY_MB", 1024),
            default=1024,
            min_value=16,
            max_value=262144,
        )
        self.tex_process_limit = self._coerce_int(
            values.get("PREVIEW_TEX_PROCESS_LIMIT", 64),
            default=64,
            min_value=1,
            max_value=4096,
        )
        self.tex_output_kb = self._coerce_int(
            values.get("PREVIEW_TEX_OUTPUT_KB", 131072),
            default=131072,
            min_value=64,
            max_value=1048576,
        )
        self.tex_passes = self._coerce_int(
            values.get("PREVIEW_TEX_PASSES", 2),
            default=2,
            min_value=1,
            max_value=4,
        )

    def _latex_compile_error_detail(self, output_text: str, returncode: int | None) -> str:
        text = str(output_text or "")
        low = text.lower()
        if ("can't find the format file" in low) and ("pdflatex.fmt" in low):
            return "missing LaTeX format pdflatex.fmt; run `fmtutil -user --byfmt pdflatex` (or `sudo fmtutil-sys --byfmt pdflatex`)"
        missing_pkg = re.search("File `([^`]+\\.sty)' not found", text)
        if missing_pkg is not None:
            pkg_name = str(missing_pkg.group(1) or "").strip()
            if pkg_name:
                return f"missing LaTeX package {pkg_name}"
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

    def _preview_ref(
        self,
        *,
        problem_id: int,
        workspace_id: int,
        source_commit: str,
        source_ref: str,
        statement_signature: str,
        dynamic_samples: bool,
    ) -> str:
        payload = {
            "schema": "preview-ref.v1",
            "problem_id": int(problem_id),
            "workspace_id": int(workspace_id),
            "source_commit": str(source_commit or "").strip() or "__dirty__",
            "source_ref": str(source_ref or "").strip(),
            "statement_signature": str(statement_signature or "").strip(),
            "dynamic_samples": bool(dynamic_samples),
        }
        return sha256_hex_json(payload, ensure_ascii=True)

    def find_cached_preview_id(
        self,
        problem: str,
        problem_id: int,
        workspace_id: int,
        source_commit: str | None = None,
        statement_signature: str | None = None,
        allow_cache_mutation: bool = True,
    ) -> str | None:
        source = str(source_commit or "").strip()
        signature = str(statement_signature or "").strip()
        cache_key = {
            "problem_id": int(problem_id),
            "workspace_id": int(workspace_id),
            "source_commit": source if source_commit is not None else "__dirty__",
            "statement_signature": signature,
            "schema": "v2",
        }

        def _cached_preview_still_valid(preview_id: str) -> bool:
            root = self._preview_artifact_root(
                problem_id=int(problem_id),
                workspace_id=int(workspace_id),
                preview_id=preview_id,
            )
            if root is None:
                return False
            cached_pdf = root / "statement_preview" / "statement.pdf"
            cached_log = root / "logs" / "latex.log"
            if not self._is_safe_regular_file(root, cached_pdf, root_resolved=root):
                return False
            if not self._is_safe_regular_file(root, cached_log, root_resolved=root):
                return False
            row = self.db.fetch_one(
                "SELECT status,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?",
                [preview_id, int(problem_id), int(workspace_id)],
            )
            if row is None:
                return False
            if str(row["status"] or "").strip().lower() != "ok":
                return False
            if signature:
                cached_signature = self._summary_statement_signature(row["summary_json"])
                if cached_signature != signature:
                    return False
            return True

        cache_service = self._async_task_cache_service
        if cache_service is not None:
            cached_entry = cache_service.get(self.PREVIEW_CACHE_NAMESPACE, cache_key)
            if isinstance(cached_entry, dict):
                cached_value = cached_entry.get("value")
                cached_obj = cached_value if isinstance(cached_value, dict) else {}
                cached_preview_id = str(cached_obj.get("preview_id") or "").strip()
                if cached_preview_id and _cached_preview_still_valid(cached_preview_id):
                    return cached_preview_id
                if cached_preview_id and allow_cache_mutation:
                    cache_service.delete(self.PREVIEW_CACHE_NAMESPACE, cache_key)
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
            root = self._preview_artifact_root(
                problem_id=int(problem_id),
                workspace_id=int(workspace_id),
                preview_id=preview_id,
            )
            if root is None:
                continue
            cached_pdf = root / "statement_preview" / "statement.pdf"
            cached_log = root / "logs" / "latex.log"
            if not self._is_safe_regular_file(root, cached_pdf, root_resolved=root):
                continue
            if not self._is_safe_regular_file(root, cached_log, root_resolved=root):
                continue
            if cache_service is not None and allow_cache_mutation:
                cache_service.put(
                    self.PREVIEW_CACHE_NAMESPACE,
                    cache_key,
                    {"preview_id": preview_id},
                    tags={
                        "problem_id": str(problem_id),
                        "workspace_id": str(workspace_id),
                        "source_commit": source if source_commit is not None else "__dirty__",
                    },
                )
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
        problem_id: int,
        workspace_id: int,
        preview_id: str,
    ) -> Path | None:
        if not is_canonical_artifact_id(preview_id):
            return None
        row = self.db.fetch_one(
            "SELECT artifact_path FROM previews WHERE id=? AND problem_id=? AND workspace_id=?",
            [str(preview_id or "").strip(), int(problem_id), int(workspace_id)],
        )
        if row is None:
            return None
        artifact_path = str(row["artifact_path"] or "").strip()
        if not artifact_path:
            return None
        try:
            root = Path(artifact_path).resolve()
            base = self.workspace_service.settings.artifacts_root.resolve()
        except OSError:
            return None
        if root != base and base not in root.parents:
            return None
        return root

    def prune_workspace_preview_history(
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
            "SELECT id,status,artifact_path FROM previews WHERE problem_id=? AND workspace_id=? AND id<>?",
            [problem_id, workspace_id, keep_id],
        )
        if not rows:
            return
        terminal_ids: list[str] = []
        for row in rows:
            stale_id = str(row["id"] or "").strip()
            status = str(row["status"] or "").strip().lower()
            if status not in {"ok", "failed", "cancelled"}:
                continue
            terminal_ids.append(stale_id)
            artifact_path = str(row["artifact_path"] or "").strip()
            has_other_ref = False
            if artifact_path:
                shared = self.db.fetch_one(
                    "SELECT 1 FROM previews WHERE artifact_path=? AND id<>? LIMIT 1",
                    [artifact_path, stale_id],
                )
                has_other_ref = shared is not None
            if has_other_ref:
                continue
            root = self._preview_artifact_root(
                problem_id=int(problem_id),
                workspace_id=int(workspace_id),
                preview_id=stale_id,
            )
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
        if not terminal_ids:
            return
        placeholders = ",".join(["?"] * len(terminal_ids))
        self.db.execute(
            f"DELETE FROM previews WHERE problem_id=? AND workspace_id=? AND id IN ({placeholders})",
            [problem_id, workspace_id, *terminal_ids],
        )

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
        dynamic_samples = False
        preview_ref = ""
        preview_id = ""
        sample_verification_id = ""
        artifacts = None
        with self.workspace_service.workspace_lock(workspace):
            ws_status = self.workspace_service.read_workspace_status(workspace)
            head = str(ws_status.get("head_commit") or "").strip()
            branch = str(ws_status.get("branch") or "").strip() or source_ref
            dirty = bool(ws_status.get("dirty"))
            statement_signature = statement_sources_signature(workspace, problem_title=problem_title)
            dynamic_samples = bool(self._sample_rows_from_spec(workspace))
            if not head:
                head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
            if head:
                source_commit = "" if dirty else head
                source_ref = branch
            if (not dynamic_samples) and head and (not dirty):
                cached_id = self.find_cached_preview_id(
                    problem,
                    problem_id,
                    workspace_id,
                    source_commit=head,
                    statement_signature=statement_signature,
                )
                if cached_id is not None:
                    self.prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                    return cached_id
            if (not dynamic_samples) and dirty:
                cached_id = self.find_cached_preview_id(
                    problem,
                    problem_id,
                    workspace_id,
                    source_commit=None,
                    statement_signature=statement_signature,
                )
                if cached_id is not None:
                    self.prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                    return cached_id
            snapshot = self.workspace_service.create_snapshot(
                workspace,
                None,
                workspace_head=head,
                workspace_dirty=dirty,
            )

            preview_ref = self._preview_ref(
                problem_id=int(problem_id),
                workspace_id=int(workspace_id),
                source_commit=str(source_commit or ""),
                source_ref=str(source_ref or ""),
                statement_signature=str(statement_signature or ""),
                dynamic_samples=bool(dynamic_samples),
            )
            preview_id = f"p-{uuid.uuid4().hex[:12]}"
            artifacts = self.fs_manager.ensure_artifact_layout(preview_ref)
            self.db.execute(
                "INSERT INTO previews(id,problem_id,workspace_id,verification_id,source_commit,source_ref,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [preview_id, problem_id, workspace_id, "", source_commit, source_ref, "running", str(artifacts.root), now_iso()],
            )
        if snapshot is None or artifacts is None or (not str(preview_id or "").strip()):
            raise RuntimeError("preview compile setup failed")

        log = artifacts.logs / "latex.log"
        status = "ok"
        summary: dict[str, object] = {"statement_signature": statement_signature, "preview_ref": preview_ref}
        sample_sync: dict[str, object] | None = None

        def _summary_with_sample(payload: dict[str, object]) -> dict[str, object]:
            out = dict(payload or {})
            if "preview_ref" not in out:
                out["preview_ref"] = preview_ref
            if sample_sync is not None and "sample_sync" not in out:
                out["sample_sync"] = sample_sync
            return out

        try:
            if dynamic_samples:
                sample_sync = self._copy_sample_payloads_from_verification(problem, username, snapshot)
                sample_verification_id = str(sample_sync.get("verification_id") or "").strip()
                summary["sample_sync"] = sample_sync
            tex = render_statement_main(snapshot / "statement", problem_title=problem_title)

            try:
                final_proc = None
                final_log_text = ""
                for _ in range(max(1, int(self.tex_passes))):
                    proc = self.sandbox.run(
                        ExecSpec(
                            command=[
                                "pdflatex",
                                "-interaction=nonstopmode",
                                "-halt-on-error",
                                str(tex.name),
                            ],
                            cwd=tex.parent,
                            timeout_sec=self.tex_timeout_sec,
                            output_kb=self.tex_output_kb,
                            memory_mb=self.tex_memory_mb,
                            process_limit=self.tex_process_limit,
                        )
                    )
                    final_proc = proc
                    output_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
                    log_text = output_text
                    generated_log = tex.parent / f"{tex.stem}.log"
                    if generated_log.exists():
                        try:
                            log_text = generated_log.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            log_text = output_text
                    final_log_text = log_text
                    if proc.timed_out or int(proc.returncode or 0) != 0:
                        break
                log.write_text(final_log_text, encoding="utf-8")
                generated = tex.parent / f"{tex.stem}.pdf"
                target = artifacts.statement_preview / "statement.pdf"
                if final_proc is None:
                    status = "failed"
                    summary = _summary_with_sample(
                        {
                            "error": "latex compile failed",
                            "statement_signature": statement_signature,
                            "failed_stage": "latex_compile",
                        }
                    )
                elif final_proc.timed_out:
                    status = "failed"
                    summary = _summary_with_sample(
                        {
                            "error": "latex compile timeout",
                            "statement_signature": statement_signature,
                            "failed_stage": "latex_compile",
                        }
                    )
                elif int(final_proc.returncode or 0) != 0:
                    error_detail = self._latex_compile_error_detail(final_log_text, final_proc.returncode)
                    if not error_detail:
                        error_detail = "latex compile failed"
                    status = "failed"
                    summary = _summary_with_sample(
                        {
                            "error": error_detail,
                            "returncode": final_proc.returncode,
                            "statement_signature": statement_signature,
                            "failed_stage": "latex_compile",
                        }
                    )
                elif not generated.exists():
                    status = "failed"
                    summary = _summary_with_sample(
                        {
                            "error": "latex compile failed",
                            "statement_signature": statement_signature,
                            "failed_stage": "latex_compile",
                        }
                    )
                else:
                    if generated != target:
                        shutil.copy2(generated, target)
                    summary = _summary_with_sample({"pdf": "statement_preview/statement.pdf", "statement_signature": statement_signature})
            except FileNotFoundError as exc:
                status = "failed"
                summary = _summary_with_sample(
                    {
                        "error": str(exc),
                        "statement_signature": statement_signature,
                        "failed_stage": "latex_compile",
                    }
                )
                log.write_text(str(exc) + "\n", encoding="utf-8")
        except Exception as exc:
            status = "failed"
            failed_stage = "sample_sync" if str(exc).startswith("sample verification failed") else "latex_compile"
            summary = _summary_with_sample(
                {
                    "error": str(exc),
                    "statement_signature": statement_signature,
                    "failed_stage": failed_stage,
                }
            )
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(str(exc) + "\n", encoding="utf-8")
        finally:
            if status != "ok":
                try:
                    log_missing_or_empty = (not log.exists()) or log.stat().st_size <= 0
                except OSError:
                    log_missing_or_empty = True
                if log_missing_or_empty:
                    fallback_error = ""
                    if isinstance(summary, dict):
                        fallback_error = str(summary.get("error") or "").strip()
                    if not fallback_error:
                        fallback_error = "latex compile failed"
                    try:
                        log.parent.mkdir(parents=True, exist_ok=True)
                        log.write_text(fallback_error + "\n", encoding="utf-8")
                    except OSError:
                        pass
            self.db.execute(
                "UPDATE previews SET verification_id=?, source_commit=?, source_ref=?, status=?, summary_json=?, finished_at=? WHERE id=?",
                [sample_verification_id, source_commit, source_ref, status, json.dumps(summary), now_iso(), preview_id],
            )
            if status == "ok" and self._async_task_cache_service is not None:
                self._async_task_cache_service.put(
                    self.PREVIEW_CACHE_NAMESPACE,
                    {
                        "problem_id": int(problem_id),
                        "workspace_id": int(workspace_id),
                        "source_commit": source_commit if source_commit else "__dirty__",
                        "statement_signature": str(statement_signature or "").strip(),
                        "schema": "v2",
                    },
                    {"preview_id": preview_id},
                    tags={
                        "problem_id": str(problem_id),
                        "workspace_id": str(workspace_id),
                        "source_commit": source_commit if source_commit else "__dirty__",
                    },
                )
            self.prune_workspace_preview_history(problem, problem_id, workspace_id, preview_id)
            shutil.rmtree(snapshot.parent, ignore_errors=True)
        return preview_id
