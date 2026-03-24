from __future__ import annotations
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.db import DB
from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.disk.verification_store import VerificationStore
from app.service.platform.artifact import ArtifactService
from app.service.disk.preview_store import PreviewArtifactRow, PreviewRow, PreviewStore
from app.service.platform.fs.layout import FsManager
from app.service.platform.hashing import sha256_hex_json
from app.service.sandbox.base import ExecSpec, SandboxBackend
from app.service.sandbox.tex_backend import TexSandboxBackend
from app.service.statement.render import render_statement_main
from app.service.statement.signature import statement_sources_signature
from app.service.problem.test_spec import TESTS_SPEC_REL, load_tests_spec, payload_rel_path_for_test
from app.service.platform.git_process import run_git
from app.service.platform.process import is_canonical_artifact_id
from app.service.repository.workspace import WorkspaceService

if TYPE_CHECKING:
    from app.service.platform.async_task_cache import AsyncTaskCacheService
    from app.service.verification.service import VerificationService


class PreviewService:
    PREVIEW_CACHE_NAMESPACE = "preview.compile"

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
        artifacts: ArtifactService,
        verification_service: VerificationService | None = None,
        sandbox_backend: SandboxBackend | None = None,
        constants: RuntimeValues | None = None,
        async_task_cache_service: AsyncTaskCacheService | None = None,
    ):
        self.db = db
        self._store = PreviewStore(db)
        self._verification_store = VerificationStore(db)
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.verification_service = verification_service
        self._async_task_cache_service = async_task_cache_service
        self.fs_manager = FsManager(
            self.workspace_service.settings.cache_root,
            self.workspace_service.settings.artifacts_root,
        )
        self.sandbox = sandbox_backend or TexSandboxBackend()
        self.tex_timeout_sec = 120
        self.tex_memory_mb = 1024
        self.tex_process_limit = 64
        self.tex_output_kb = 131072
        self.tex_passes = 2
        self.apply_runtime_values(constants or build_runtime_values())

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

    def _sample_verification_rows_from_spec(self, workspace: Path) -> list[_SampleVerificationRow]:
        spec_path = workspace / TESTS_SPEC_REL
        try:
            entries = load_tests_spec(spec_path)
        except Exception as exc:
            raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
        rows: list[PreviewService._SampleVerificationRow] = []
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            if not bool(entry.get("sample")):
                continue
            test_id_obj = entry.get("id")
            test_id = str(test_id_obj).strip() if test_id_obj is not None else ""
            kind_obj = entry.get("kind")
            kind = str(kind_obj).strip().lower() if kind_obj is not None else ""
            if kind not in {"manual", "gen"}:
                raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}: {kind or '(empty)'}")
            if not test_id:
                continue
            sample_input_obj = entry.get("sample_input")
            sample_input = str(sample_input_obj) if sample_input_obj is not None else ""
            sample_output_obj = entry.get("sample_output")
            sample_output = str(sample_output_obj) if sample_output_obj is not None else ""
            sample_output_validate = bool(entry.get("sample_output_validate", True))
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
                if sample_input:
                    needs_output_copy = True
                else:
                    answer_path = workspace / "tests" / "answers" / f"{test_id}.ans"
                    try:
                        if answer_path.is_symlink() or (not answer_path.exists()) or (not answer_path.is_file()):
                            needs_output_copy = True
                    except Exception:
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
        rows = self._sample_verification_rows_from_spec(snapshot)
        if not rows:
            return {"sample_count": 0, "copied": 0, "verification_id": ""}
        if self.verification_service is None:
            raise RuntimeError("preview sample sync requires verification service")
        verification_id = self.verification_service.run_verification(
            problem,
            username,
            sample_only=True,
        )
        verification_row = self._verification_store.record_row(verification_id)
        if verification_row is None:
            raise RuntimeError(f"sample verification missing: {verification_id}")
        verification_status = str(verification_row["status"]).strip().lower()
        if verification_status != "ok":
            metadata_payload = self._verification_store.metadata(verification_id)
            error_text = str(verification_row["fail_reason"] or metadata_payload.get("error") or "").strip()
            if error_text:
                raise RuntimeError(f"sample verification failed ({verification_id}): {error_text}")
            raise RuntimeError(f"sample verification failed ({verification_id})")
        runtime_layout = self.fs_manager.verification_runtime_layout(verification_id)
        tests_dir = runtime_layout.tests
        ans_dir = runtime_layout.answers
        if not tests_dir.exists() or not tests_dir.is_dir() or tests_dir.is_symlink():
            raise RuntimeError(f"sample verification missing tests directory: {verification_id}")
        if not ans_dir.exists() or not ans_dir.is_dir() or ans_dir.is_symlink():
            raise RuntimeError(f"sample verification missing ans directory: {verification_id}")
        copied = 0
        snapshot_root = snapshot.resolve()
        for row in rows:
            index = int(row.index)
            test_id = str(row.test_id)
            kind = str(row.kind)
            source_in = tests_dir / f"{int(index):03d}.in"
            source_ans = ans_dir / f"{int(index):03d}.ans"
            input_rel = Path(payload_rel_path_for_test(test_id, kind))
            answer_rel = Path("tests") / "answers" / f"{test_id}.ans"
            input_target = (snapshot / input_rel).resolve()
            answer_target = (snapshot / answer_rel).resolve()
            if snapshot_root not in input_target.parents or snapshot_root not in answer_target.parents:
                raise RuntimeError(f"invalid sample target path for test id {test_id}")
            copied_row = False
            if row.needs_input_copy:
                if source_in.is_symlink() or (not source_in.exists()) or (not source_in.is_file()):
                    raise RuntimeError(f"sample input missing from verification for test id {test_id} (row {index})")
                input_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_in, input_target)
                copied_row = True
            if row.needs_output_copy:
                if source_ans.is_symlink() or (not source_ans.exists()) or (not source_ans.is_file()):
                    raise RuntimeError(f"sample answer missing from verification for test id {test_id} (row {index})")
                answer_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_ans, answer_target)
                copied_row = True
            if copied_row:
                copied += 1
        return {"sample_count": len(rows), "copied": copied, "verification_id": verification_id}

    def sync_sample_payloads_for_snapshot(self, problem: str, username: str, snapshot: Path) -> dict[str, object]:
        return self._copy_sample_payloads_from_verification(problem, username, snapshot)

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
            row = self._store.get_workspace_preview(int(problem_id), int(workspace_id), preview_id)
            if row is None:
                return False
            if row["status"].strip().lower() != "ok":
                return False
            if signature:
                cached_signature = self._summary_statement_signature(row["summary"])
                if cached_signature != signature:
                    return False
            return True

        cache_service = self._async_task_cache_service
        if cache_service is not None:
            cached_entry = cache_service.get(self.PREVIEW_CACHE_NAMESPACE, cache_key)
            if isinstance(cached_entry, dict):
                cached_obj = cached_value if isinstance(cached_value := cached_entry.get("value"), dict) else {}
                cached_preview_id_obj = cached_obj.get("preview_id")
                cached_preview_id = str(cached_preview_id_obj).strip() if cached_preview_id_obj is not None else ""
                if cached_preview_id and _cached_preview_still_valid(cached_preview_id):
                    return cached_preview_id
                if cached_preview_id and allow_cache_mutation:
                    cache_service.delete(self.PREVIEW_CACHE_NAMESPACE, cache_key)
        rows = self._store.list_cached_ok_previews(
            int(problem_id),
            int(workspace_id),
            source_commit=source_commit,
            limit=100,
        )
        for row in rows:
            preview_id = str(row["id"] or "").strip()
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

    def _summary_statement_signature(self, summary: dict[str, object]) -> str:
        signature_obj = summary.get("statement_signature")
        return str(signature_obj).strip() if signature_obj is not None else ""

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
            root = self.fs_manager.resolve_preview_root(str(preview_id or "").strip())
            base = self.fs_manager.preview_root.resolve()
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

    def compile_preview(self, problem: str, username: str) -> str:
        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(ctx["workspace"]["path"])
        problem_title = str(ctx["problem"]["name"]).strip()
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
            ws_status = self.workspace_service.read_workspace_status(workspace)
            head_obj = ws_status.get("head_commit")
            head = str(head_obj).strip() if head_obj is not None else ""
            branch_obj = ws_status.get("branch")
            branch = str(branch_obj).strip() if branch_obj is not None else ""
            if not branch:
                branch = source_ref
            dirty = bool(ws_status.get("dirty"))
            statement_signature = statement_sources_signature(workspace, problem_title=problem_title)
            dynamic_samples = bool(self._sample_verification_rows_from_spec(workspace))
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
            preview_layout = self.fs_manager.prepare_preview_layout(preview_id)
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
                sample_verification_id_obj = sample_sync.get("verification_id")
                if sample_verification_id_obj is not None:
                    sample_verification_id_text = str(sample_verification_id_obj).strip()
                    sample_verification_id = sample_verification_id_text if sample_verification_id_text else None
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
                target = preview_layout.statement_preview / "statement.pdf"
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
