from __future__ import annotations
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from app.db import DB
from app.main_util import problem_slug_leaf
from app.service.disk.verification_store import VerificationStore
from app.service.disk.preview_store import PreviewArtifactRow, PreviewRow, PreviewStore
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.hashing import sha256_hex_json
from app.service.statement.tex_compile import TexCompileService
from app.service.statement.render import (
    render_statement_main,
    statement_title_for_language,
)
from app.service.statement.signature import statement_sources_signature
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    dumps_tests_spec,
    load_tests_spec,
    payload_rel_path_for_test,
    read_statement_sample_text,
)
from app.service.problem.runtime_config import load_problem_config, problem_config_limits
from app.service.platform.git_process import run_git
from app.service.platform.process import is_canonical_artifact_id
from app.service.repository.workspace import WorkspaceService
from app.service.statement.context import normalize_statement_language

if TYPE_CHECKING:
    from app.service.verification.service import VerificationService
    from app.service.verification.workflow import VerificationWorkflow


class PreviewStateRow(TypedDict):
    id: str
    row_status: str
    display_status: str
    source_commit: str
    source_ref: str
    summary: dict[str, object]
    created_at: str
    finished_at: str
    pdf_available: bool
    log_available: bool


class PreviewService:
    @dataclass(frozen=True)
    class _SampleVerificationRow:
        index: int
        test_id: str
        kind: str
        needs_input_copy: bool
        needs_output_copy: bool
        validate_custom_output: bool

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        pdf_compiler: TexCompileService,
        storage_layout: StorageLayout,
        verification_service: VerificationService | None = None,
        verification_workflow: VerificationWorkflow | None = None,
    ):
        self.db = db
        self._store = PreviewStore(db)
        self._verification_store = VerificationStore(db)
        self.workspace_service = workspace_service
        self.verification_service = verification_service
        self.verification_workflow = verification_workflow
        self.storage_layout = storage_layout
        self.pdf_compiler = pdf_compiler

    def list_workspace_previews(self, problem_id: int, workspace_id: int) -> list[PreviewRow]:
        return self._store.list_workspace_previews(problem_id, workspace_id)

    def get_workspace_preview(self, problem_id: int, workspace_id: int, preview_id: str) -> PreviewRow | None:
        return self._store.get_workspace_preview(problem_id, workspace_id, preview_id)

    def get_workspace_preview_artifact(
        self,
        problem_id: int,
        workspace_id: int,
        preview_id: str,
    ) -> PreviewArtifactRow | None:
        return self._store.get_workspace_preview_artifact(problem_id, workspace_id, preview_id)

    def latest_workspace_preview(self, problem_id: int, workspace_id: int) -> dict[str, str] | None:
        row = self._store.get_latest_workspace_preview(problem_id, workspace_id)
        if row is None:
            return None
        return row

    def _problem_mode(self, workspace: Path) -> str:
        return load_problem_config(
            workspace,
            limits=problem_config_limits(self.db.config_values),
        )["mode"]

    def _sample_verification_rows_from_spec(
        self,
        workspace: Path,
        *,
        document_max_bytes: int,
        sample_max_bytes: int,
    ) -> list[_SampleVerificationRow]:
        if self._problem_mode(workspace) == "interactive":
            return []
        spec_path = workspace / TESTS_SPEC_REL
        try:
            entries = load_tests_spec(
                spec_path,
                document_max_bytes=document_max_bytes,
                sample_max_bytes=sample_max_bytes,
            )
        except Exception as exc:
            raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
        rows: list[PreviewService._SampleVerificationRow] = []
        for index, entry in enumerate(entries, start=1):
            if not entry["sample"]:
                continue
            test_id = entry["id"]
            kind = entry["kind"]
            sample_input = entry.get("sample_input", "")
            sample_output = entry.get("sample_output", "")
            sample_output_validate = entry.get("sample_output_validate", True)
            needs_input_copy = False
            needs_output_copy = False
            if not sample_input:
                if kind == "gen":
                    needs_input_copy = True
                else:
                    try:
                        input_rel = Path(payload_rel_path_for_test(test_id, kind))
                        input_path = workspace / input_rel
                        if input_path.is_symlink() or (not input_path.exists()) or (not input_path.is_file()):
                            needs_input_copy = True
                    except Exception:
                        needs_input_copy = True
            if not sample_output:
                needs_output_copy = True
            validate_custom_output = bool(sample_output) and sample_output_validate
            if needs_input_copy or needs_output_copy or validate_custom_output:
                rows.append(
                    PreviewService._SampleVerificationRow(
                        index=index,
                        test_id=test_id,
                        kind=kind,
                        needs_input_copy=needs_input_copy,
                        needs_output_copy=needs_output_copy,
                        validate_custom_output=validate_custom_output,
                    )
                )
        return rows

    def _copy_sample_payloads_from_verification(self, problem: str, username: str, snapshot: Path) -> dict[str, object]:
        config_snapshot = self.db.config_values.snapshot()
        tests_spec_max_bytes = int(config_snapshot["TEXTAREA_MAX_BYTES"])
        statement_sample_max_bytes = int(
            config_snapshot["STATEMENT_SAMPLE_MAX_BYTES"]
        )
        if self._problem_mode(snapshot) == "interactive":
            return {"sample_count": 0, "copied": 0, "verification_id": "", "skipped": "interactive"}
        rows = self._sample_verification_rows_from_spec(
            snapshot,
            document_max_bytes=tests_spec_max_bytes,
            sample_max_bytes=statement_sample_max_bytes,
        )
        if not rows:
            return {"sample_count": 0, "copied": 0, "verification_id": ""}
        if self.verification_service is None or self.verification_workflow is None:
            raise RuntimeError("preview sample sync requires verification service")
        verification_id = self.verification_workflow.run_workspace(
            problem,
            username,
            sample_only=True,
        )
        verification_row = self._verification_store.record_row(verification_id)
        if verification_row is None:
            raise RuntimeError(f"sample verification missing: {verification_id}")
        verification_status = str(verification_row["status"]).strip().lower()
        if verification_status != "ok":
            detail_payload = self.verification_service.verification_detail(verification_id)
            error_text = str(verification_row["fail_reason"] or detail_payload.get("error") or "").strip()
            if error_text:
                raise RuntimeError(f"sample verification failed ({verification_id}): {error_text}")
            raise RuntimeError(f"sample verification failed ({verification_id})")
        copied = 0
        snapshot_root = snapshot.resolve()
        spec_entries = load_tests_spec(
            snapshot / TESTS_SPEC_REL,
            document_max_bytes=tests_spec_max_bytes,
            sample_max_bytes=statement_sample_max_bytes,
        )
        spec_changed = False
        for row in rows:
            index = int(row.index)
            test_id = str(row.test_id)
            kind = str(row.kind)
            test_name = f"{int(index):03d}.in"
            input_ref = self.verification_service.verification_artifact_ref(verification_id, test_name, "input_ref")
            answer_ref = self.verification_service.verification_artifact_ref(verification_id, test_name, "answer_ref")
            input_rel = Path(payload_rel_path_for_test(test_id, kind))
            input_target = (snapshot / input_rel).resolve()
            if snapshot_root not in input_target.parents:
                raise RuntimeError(f"invalid sample target path for test id {test_id}")
            copied_row = False
            if row.needs_input_copy:
                input_file = self.verification_service.artifact_descriptor(input_ref) if input_ref else None
                if input_file is None:
                    raise RuntimeError(f"sample input missing from verification for test id {test_id} (row {index})")
                input_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(input_file.path, input_target)
                copied_row = True
            if row.needs_output_copy:
                answer_file = self.verification_service.artifact_descriptor(answer_ref) if answer_ref else None
                if answer_file is None:
                    raise RuntimeError(f"sample answer missing from verification for test id {test_id} (row {index})")
                if index < 1 or index > len(spec_entries):
                    raise RuntimeError(f"invalid tests/spec.json row for sample id {test_id}")
                sample_input_text = str(
                    spec_entries[index - 1].get("sample_input") or ""
                )
                if not sample_input_text:
                    sample_input_text = read_statement_sample_text(
                        input_target,
                        max_bytes=statement_sample_max_bytes,
                    )
                spec_entries[index - 1]["sample_output"] = (
                    read_statement_sample_text(
                        answer_file.path,
                        max_bytes=(
                            statement_sample_max_bytes
                            - len(sample_input_text.encode("utf-8"))
                        ),
                    )
                )
                spec_changed = True
                copied_row = True
            if copied_row:
                copied += 1
        if spec_changed:
            (snapshot / TESTS_SPEC_REL).write_text(
                dumps_tests_spec(
                    spec_entries,
                    document_max_bytes=tests_spec_max_bytes,
                    sample_max_bytes=statement_sample_max_bytes,
                ),
                encoding="utf-8",
            )
        return {"sample_count": len(rows), "copied": copied, "verification_id": verification_id}

    def sync_sample_payloads_for_snapshot(self, problem: str, username: str, snapshot: Path) -> dict[str, object]:
        return self._copy_sample_payloads_from_verification(problem, username, snapshot)

    def _latex_compile_error_detail(self, output_text: str, returncode: int | None) -> str:
        text = str(output_text or "")
        low = text.lower()
        if "can't find the format file" in low:
            return "missing LaTeX format file; run `fmtutil-sys --all` to regenerate"
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
        language: str = "",
    ) -> str:
        payload = {
            "schema": "preview-ref",
            "problem_id": int(problem_id),
            "workspace_id": int(workspace_id),
            "source_commit": str(source_commit or "").strip() or "__dirty__",
            "source_ref": str(source_ref or "").strip(),
            "statement_signature": str(statement_signature or "").strip(),
            "dynamic_samples": bool(dynamic_samples),
            "language": str(language or "").strip(),
        }
        return sha256_hex_json(payload, ensure_ascii=True)

    def _summary_language(self, summary: dict[str, object]) -> str:
        raw = summary.get("language")
        safe_language = normalize_statement_language(raw)
        return safe_language or "english"

    def find_cached_preview_id(
        self,
        problem: str,
        problem_id: int,
        workspace_id: int,
        language: str,
        source_commit: str | None = None,
        statement_signature: str | None = None,
    ) -> str | None:
        safe_language = normalize_statement_language(language)
        if not safe_language:
            raise RuntimeError("preview language is required")
        signature = str(statement_signature or "").strip()
        rows = self._store.list_cached_ok_previews(
            int(problem_id),
            int(workspace_id),
            source_commit=source_commit,
            limit=100,
        )
        for row in rows:
            preview_id = str(row["id"] or "").strip()
            if self._summary_language(dict(row["summary"])) != safe_language:
                continue
            if signature:
                cached_signature = self._summary_statement_signature(row["summary"])
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
            return preview_id
        return None

    def _summary_statement_signature(self, summary: dict[str, object]) -> str:
        signature_obj = summary.get("statement_signature")
        return str(signature_obj).strip() if signature_obj is not None else ""

    def _preview_file_availability(self, root: Path | None) -> tuple[bool, bool]:
        if root is None:
            return (False, False)
        pdf_path = root / "statement_preview" / "statement.pdf"
        log_path = root / "logs" / "latex.log"
        pdf_available = self._is_safe_regular_file(root, pdf_path, root_resolved=root)
        log_available = self._is_safe_regular_file(root, log_path, root_resolved=root)
        return (pdf_available, log_available)

    def get_workspace_preview_state(
        self,
        problem_id: int,
        workspace_id: int,
        preview_id: str,
        *,
        statement_signature: str = "",
        workspace_head: str = "",
        language: str = "",
    ) -> PreviewStateRow | None:
        row = self._store.get_workspace_preview(int(problem_id), int(workspace_id), str(preview_id or "").strip())
        if row is None:
            return None
        root = self._preview_artifact_root(int(problem_id), int(workspace_id), str(preview_id or "").strip())
        pdf_available, log_available = self._preview_file_availability(root)
        summary = dict(row["summary"])
        safe_language = normalize_statement_language(language)
        if safe_language and self._summary_language(summary) != safe_language:
            return None
        row_status = str(row["status"] or "").strip().lower() or "missing"
        display_status = row_status
        if row_status == "ok":
            if not pdf_available:
                display_status = "missing"
            else:
                preview_signature = self._summary_statement_signature(summary)
                preview_source_commit = str(row["source_commit"] or "").strip()
                safe_statement_signature = str(statement_signature or "").strip()
                safe_workspace_head = str(workspace_head or "").strip()
                stale_by_signature = bool(
                    preview_signature and safe_statement_signature and (preview_signature != safe_statement_signature)
                )
                stale_by_head = bool(
                    (not preview_signature or not safe_statement_signature)
                    and preview_source_commit
                    and safe_workspace_head
                    and (preview_source_commit != safe_workspace_head)
                )
                display_status = "stale" if (stale_by_signature or stale_by_head) else "ok"
        return {
            "id": str(row["id"]),
            "row_status": row_status,
            "display_status": display_status,
            "source_commit": str(row["source_commit"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "summary": summary,
            "created_at": str(row["created_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
            "pdf_available": bool(pdf_available),
            "log_available": bool(log_available),
        }

    def latest_workspace_preview_state(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        statement_signature: str = "",
        workspace_head: str = "",
        language: str = "",
    ) -> PreviewStateRow | None:
        safe_language = normalize_statement_language(language)
        if not safe_language:
            latest = self._store.get_latest_workspace_preview(int(problem_id), int(workspace_id))
            if latest is None:
                return None
            return self.get_workspace_preview_state(
                int(problem_id),
                int(workspace_id),
                str(latest["id"] or ""),
                statement_signature=statement_signature,
                workspace_head=workspace_head,
            )
        rows = self._store.list_workspace_previews(int(problem_id), int(workspace_id))
        for row in rows:
            state = self.get_workspace_preview_state(
                int(problem_id),
                int(workspace_id),
                str(row["id"] or ""),
                statement_signature=statement_signature,
                workspace_head=workspace_head,
                language=safe_language,
            )
            if state is not None:
                return state
        return None

    def _preview_artifact_root(
        self,
        problem_id: int,
        workspace_id: int,
        preview_id: str,
    ) -> Path | None:
        if not is_canonical_artifact_id(preview_id):
            return None
        row = self._store.get_workspace_preview_artifact(int(problem_id), int(workspace_id), str(preview_id or "").strip())
        if row is None:
            return None
        try:
            root = self.storage_layout.resolve_preview_root(str(preview_id or "").strip())
            base = self.storage_layout.preview_root.resolve()
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
        rows = self._store.list_other_workspace_previews(problem_id, workspace_id, keep_id)
        if not rows:
            return
        terminal_ids: list[str] = []
        for row in rows:
            stale_id = row["id"].strip()
            status = row["status"].strip().lower()
            if status not in {"ok", "failed", "cancelled"}:
                continue
            terminal_ids.append(stale_id)
            root = self._preview_artifact_root(
                problem_id=int(problem_id),
                workspace_id=int(workspace_id),
                preview_id=stale_id,
            )
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
        if not terminal_ids:
            return
        self._store.delete_previews(problem_id, workspace_id, terminal_ids)

    def compile_preview(self, problem: str, username: str, language: str) -> str:
        config_snapshot = self.db.config_values.snapshot()
        tests_spec_max_bytes = int(config_snapshot["TEXTAREA_MAX_BYTES"])
        statement_sample_max_bytes = int(
            config_snapshot["STATEMENT_SAMPLE_MAX_BYTES"]
        )
        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(ctx["workspace"]["path"])
        safe_language = normalize_statement_language(language)
        if not safe_language:
            raise RuntimeError("preview language is required")
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        source_commit = ""
        source_ref_obj = ctx["workspace"].get("branch")
        source_ref = str(source_ref_obj).strip() if source_ref_obj is not None else "main"
        statement_signature = ""
        snapshot: Path | None = None
        dynamic_samples = False
        preview_ref = ""
        preview_id = ""
        sample_verification_id: str | None = None
        preview_layout = None
        with self.workspace_service.workspace_lock(workspace):
            problem_title = statement_title_for_language(
                workspace,
                safe_language,
                fallback_title=problem_slug_leaf(problem),
            )
            ws_status = self.workspace_service.read_workspace_status(workspace)
            head_obj = ws_status.get("head_commit")
            head = str(head_obj).strip() if head_obj is not None else ""
            branch_obj = ws_status.get("branch")
            branch = str(branch_obj).strip() if branch_obj is not None else ""
            if not branch:
                branch = source_ref
            dirty = bool(ws_status.get("dirty"))
            statement_signature = statement_sources_signature(
                workspace,
                problem_title=problem_title,
                tests_spec_max_bytes=tests_spec_max_bytes,
                statement_sample_max_bytes=statement_sample_max_bytes,
            )
            dynamic_samples = bool(
                self._sample_verification_rows_from_spec(
                    workspace,
                    document_max_bytes=tests_spec_max_bytes,
                    sample_max_bytes=statement_sample_max_bytes,
                )
            )
            if not head:
                head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
            if head:
                source_commit = "" if dirty else head
                source_ref = branch
            if (not dynamic_samples) and head and (not dirty):
                cached_id = self.find_cached_preview_id(
                    problem,
                    problem_id,
                    workspace_id,
                    language=safe_language,
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
                    language=safe_language,
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
                language=safe_language,
            )
            preview_id = f"p-{uuid.uuid4().hex[:12]}"
            preview_layout = self.storage_layout.prepare_preview_layout(preview_id)
            self._store.insert_running_preview(
                preview_id=preview_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                source_commit=source_commit,
                source_ref=source_ref,
            )
        if snapshot is None or preview_layout is None or (not str(preview_id or "").strip()):
            raise RuntimeError("preview compile setup failed")

        log = preview_layout.logs / "latex.log"
        status = "ok"
        summary: dict[str, object] = {"statement_signature": statement_signature, "preview_ref": preview_ref, "language": safe_language}
        sample_sync: dict[str, object] | None = None

        def _summary_with_sample(payload: dict[str, object]) -> dict[str, object]:
            out = dict(payload or {})
            if "preview_ref" not in out:
                out["preview_ref"] = preview_ref
            if "language" not in out:
                out["language"] = safe_language
            if sample_sync is not None and "sample_sync" not in out:
                out["sample_sync"] = sample_sync
            return out

        try:
            if dynamic_samples:
                sample_sync = self._copy_sample_payloads_from_verification(problem, username, snapshot)
                sample_verification_id_obj = sample_sync.get("verification_id")
                if sample_verification_id_obj is not None:
                    sample_verification_id_text = str(sample_verification_id_obj).strip()
                    sample_verification_id = sample_verification_id_text if sample_verification_id_text else None
                summary["sample_sync"] = sample_sync
            tex = render_statement_main(
                snapshot / "statement",
                problem_title=problem_title,
                language=safe_language,
                tests_spec_max_bytes=tests_spec_max_bytes,
                statement_sample_max_bytes=statement_sample_max_bytes,
                problem_limits=problem_config_limits(self.db.config_values),
            )

            try:
                compile_result = self.pdf_compiler.compile_pdf(tex)
                final_proc = compile_result.proc
                final_log_text = compile_result.log_text
                log.write_text(final_log_text, encoding="utf-8")
                generated = compile_result.pdf_path
                target = preview_layout.statement_preview / "statement.pdf"
                if final_proc.timed_out:
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
                        error_obj = summary.get("error")
                        fallback_error = str(error_obj).strip() if error_obj is not None else ""
                    if not fallback_error:
                        fallback_error = "latex compile failed"
                    try:
                        log.parent.mkdir(parents=True, exist_ok=True)
                        log.write_text(fallback_error + "\n", encoding="utf-8")
                    except OSError:
                        pass
            self._store.update_preview_result(
                preview_id=preview_id,
                verification_id=sample_verification_id,
                source_commit=source_commit,
                source_ref=source_ref,
                status=status,
                summary=summary,
            )
            self.prune_workspace_preview_history(problem, problem_id, workspace_id, preview_id)
            shutil.rmtree(snapshot.parent, ignore_errors=True)
        return preview_id
