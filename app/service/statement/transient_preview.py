"""Disposable statement previews shared by problem and contest surfaces."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.db import DB
from app.main_util import problem_slug_leaf
from app.service.platform.error_text import sanitize_log_text_for_ui
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.hashing import sha256_hex_json
from app.service.problem.runtime_config import problem_config_limits
from app.service.problem_package.service import (
    NativePackage,
    NativePackageOperationBusy,
    ProblemPackageService,
)
from app.service.repository.workspace import WorkspaceService
from app.service.statement.context import normalize_statement_language
from app.service.statement.examples import (
    StatementExamplesProducer,
    statement_examples_require_verification,
)
from app.service.statement.html_render import (
    StatementHtmlRenderError,
    StatementHtmlRenderer,
)
from app.service.statement.latex_error import (
    latex_error_excerpt,
    latex_log_for_display,
)
from app.service.statement.preview_state import (
    StatementPreviewOutput,
    StatementPreviewRepository,
    StatementPreviewRow,
    StatementPreviewSource,
)
from app.service.statement.render import (
    render_statement_offline_tree,
    statement_title_for_language,
)
from app.service.statement.signature import statement_sources_signature
from app.service.statement.tex_compile import TexCompileService
from app.service.verification.workspace_fingerprint import (
    verification_sources_signature,
)
from app.service.verification.workflow import VerificationWorkflow


@dataclass(frozen=True)
class PreparedStatementRender:
    """One disposable, fully rendered Problem statement source tree."""

    problem_id: int
    root: Path
    source_identity: str
    sample_count: int | None


@dataclass(frozen=True)
class StatementPreviewInput:
    """A cheap, source-derived cache identity for one Problem statement."""

    problem_id: int
    source_identity: str
    identity: str


class StatementPreviewService:
    """Build cache-only problem statement outputs from a canonical source."""

    def __init__(
        self,
        db: DB,
        storage_layout: StorageLayout,
        workspace_service: WorkspaceService,
        package_service: ProblemPackageService,
        examples_producer: StatementExamplesProducer,
        verification_workflow: VerificationWorkflow,
        html_renderer: StatementHtmlRenderer,
        pdf_compiler: TexCompileService,
        preview_repository: StatementPreviewRepository,
    ) -> None:
        self._db = db
        self._store = preview_repository
        self._storage = storage_layout
        self._workspaces = workspace_service
        self._packages = package_service
        self._examples = examples_producer
        self._verification = verification_workflow
        self._html = html_renderer
        self._pdf = pdf_compiler

    def latest_problem(
        self,
        problem_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
    ) -> StatementPreviewRow | None:
        return self._store.latest_problem(
            problem_id,
            actor_user_id=actor_user_id,
            source_kind=source_kind,
            output_kind=output_kind,
            language=self._language(language),
        )

    def row(
        self,
        preview_id: str,
        *,
        actor_user_id: int,
    ) -> StatementPreviewRow | None:
        return self._store.row(preview_id, actor_user_id=actor_user_id)

    def build_problem(
        self,
        problem: str,
        username: str,
        *,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        native_package_id: str = "",
    ) -> StatementPreviewRow:
        safe_language = self._language(language)
        actor_user_id = int(self._workspaces.user_row(username)["id"])
        preview_input = self.problem_input(
            problem,
            username,
            source_kind=source_kind,
            output_kind=output_kind,
            language=safe_language,
            native_package_id=native_package_id,
        )
        cached = self._cached_problem(
            preview_input.problem_id,
            actor_user_id=actor_user_id,
            source_kind=source_kind,
            output_kind=output_kind,
            language=safe_language,
            identity=preview_input.identity,
        )
        if cached is not None:
            return cached
        prepared_ready = False
        try:
            with self.prepare_render_tree(
                problem,
                username,
                source_kind=source_kind,
                language=safe_language,
                native_package_id=native_package_id,
            ) as prepared:
                prepared_ready = True
                identity = self._preview_identity(
                    prepared.problem_id,
                    source_kind=source_kind,
                    output_kind=output_kind,
                    language=safe_language,
                    source_identity=prepared.source_identity,
                )
                cached = self._cached_problem(
                    prepared.problem_id,
                    actor_user_id=actor_user_id,
                    source_kind=source_kind,
                    output_kind=output_kind,
                    language=safe_language,
                    identity=identity,
                )
                if cached is not None:
                    return cached
                preview_id = self._insert_problem(
                    prepared.problem_id,
                    actor_user_id=actor_user_id,
                    source_kind=source_kind,
                    output_kind=output_kind,
                    language=safe_language,
                    identity=identity,
                )
                try:
                    return self._render_result(
                        preview_id,
                        prepared.root,
                        output_kind=output_kind,
                        subject_token=f"problem-{prepared.problem_id}",
                        sample_count=prepared.sample_count,
                    )
                except Exception as exc:
                    self._store.finish(
                        preview_id,
                        status="failed",
                        summary={"error": str(exc)},
                    )
                    row = self._store.row(preview_id)
                    if row is None:
                        raise RuntimeError(
                            "statement preview result disappeared"
                        ) from exc
                    return row
        except NativePackageOperationBusy:
            raise
        except RuntimeError as exc:
            if prepared_ready:
                raise
            return self._failed_problem_preparation(
                preview_input,
                actor_user_id=actor_user_id,
                source_kind=source_kind,
                output_kind=output_kind,
                language=safe_language,
                error=str(exc),
            )

    def problem_input(
        self,
        problem: str,
        username: str,
        *,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        native_package_id: str = "",
    ) -> StatementPreviewInput:
        """Return the cache identity without materializing a render tree."""

        safe_language = self._language(language)
        if source_kind == "workspace":
            ctx = self._workspaces.workspace_context(
                problem,
                username,
                include_recent=False,
            )
            workspace = Path(ctx["workspace"]["path"])
            problem_id = int(ctx["problem"]["id"])
            with self._workspaces.workspace_lock(workspace):
                source_identity = self._workspace_source_identity(
                    workspace,
                    problem=problem,
                    problem_id=problem_id,
                    language=safe_language,
                )
        elif source_kind == "native_package":
            problem_id = int(self._workspaces.problem_row(problem)["id"])
            native_package = self._native_package_for_problem(
                problem_id,
                native_package_id=native_package_id,
            )
            source_identity = self._native_source_identity(
                problem_id,
                language=safe_language,
                native_package=native_package,
            )
        else:
            raise ValueError("unsupported statement preview source")
        return StatementPreviewInput(
            problem_id=problem_id,
            source_identity=source_identity,
            identity=self._preview_identity(
                problem_id,
                source_kind=source_kind,
                output_kind=output_kind,
                language=safe_language,
                source_identity=source_identity,
            ),
        )

    @contextmanager
    def prepare_render_tree(
        self,
        problem: str,
        username: str,
        *,
        source_kind: StatementPreviewSource,
        language: str,
        native_package_id: str = "",
    ) -> Iterator[PreparedStatementRender]:
        """Prepare the canonical render tree without creating a Preview row."""

        safe_language = self._language(language)
        if source_kind == "workspace":
            with self._prepare_workspace_render_tree(
                problem,
                username,
                language=safe_language,
            ) as prepared:
                yield prepared
            return
        if source_kind == "native_package":
            with self._prepare_native_render_tree(
                problem,
                language=safe_language,
                native_package_id=native_package_id,
            ) as prepared:
                yield prepared
            return
        raise ValueError("unsupported statement preview source")

    def html_fragment(
        self,
        preview_id: str,
        *,
        actor_user_id: int,
        problem_id: int | None = None,
    ) -> str | None:
        row = self._store.row(preview_id, actor_user_id=actor_user_id)
        if row is None or row["status"] != "ok" or row["output_kind"] != "html":
            return None
        if problem_id is not None and row["problem_id"] != int(problem_id):
            return None
        path = self._preview_root(preview_id) / "html" / "content.html"
        if not self._safe_file(self._preview_root(preview_id), path):
            return None
        return path.read_text(encoding="utf-8")

    def resource(
        self,
        preview_id: str,
        name: str,
        *,
        actor_user_id: int,
    ) -> Path | None:
        row = self._store.row(preview_id, actor_user_id=actor_user_id)
        if row is None or row["status"] != "ok" or row["output_kind"] != "html":
            return None
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            return None
        root = self._preview_root(preview_id)
        path = root / "html" / "resources" / name
        return path if self._safe_file(root, path) else None

    def pdf(self, preview_id: str, *, actor_user_id: int) -> Path | None:
        row = self._store.row(preview_id, actor_user_id=actor_user_id)
        if row is None or row["status"] != "ok" or row["output_kind"] != "pdf":
            return None
        root = self._preview_root(preview_id)
        path = root / "pdf" / "statement.pdf"
        return path if self._safe_file(root, path) else None

    def latex_log(self, preview_id: str, *, actor_user_id: int) -> str:
        row = self._store.row(preview_id, actor_user_id=actor_user_id)
        if row is None or row["output_kind"] != "pdf":
            return ""
        root = self._preview_root(preview_id)
        path = root / "logs" / "latex.log"
        if not self._safe_file(root, path):
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def pandoc_log(self, preview_id: str, *, actor_user_id: int) -> str:
        row = self._store.row(preview_id, actor_user_id=actor_user_id)
        if row is None or row["output_kind"] != "html":
            return ""
        root = self._preview_root(preview_id)
        path = root / "logs" / "pandoc.log"
        if not self._safe_file(root, path):
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    @contextmanager
    def _prepare_workspace_render_tree(
        self,
        problem: str,
        username: str,
        *,
        language: str,
    ) -> Iterator[PreparedStatementRender]:
        ctx = self._workspaces.workspace_context(problem, username, include_recent=False)
        workspace = Path(ctx["workspace"]["path"])
        problem_id = int(ctx["problem"]["id"])
        tests_spec_limit = self._db.config_values.integer("TEXTAREA_MAX_BYTES")
        sample_limit = self._db.config_values.integer("STATEMENT_SAMPLE_MAX_BYTES")
        limits = problem_config_limits(self._db.config_values)
        snapshot: Path | None = None
        with self._workspaces.workspace_lock(workspace):
            title = statement_title_for_language(
                workspace,
                language,
                fallback_title=problem_slug_leaf(problem),
            )
            signature = statement_sources_signature(
                workspace,
                problem_title=title,
                tests_spec_max_bytes=tests_spec_limit,
                statement_sample_max_bytes=sample_limit,
            )
            dynamic = statement_examples_require_verification(
                workspace,
                tests_spec_max_bytes=tests_spec_limit,
                statement_sample_max_bytes=sample_limit,
                problem_limits=limits,
            )
            verification_signature = (
                verification_sources_signature(workspace) if dynamic else ""
            )
            status = self._workspaces.read_workspace_status(workspace)
            snapshot = self._workspaces.create_snapshot(
                workspace,
                None,
                workspace_head=str(status.get("head_commit") or ""),
                workspace_dirty=bool(status.get("dirty")),
            )
        try:
            verification_id = (
                self._verification.run_workspace(
                    problem,
                    username,
                    sample_only=True,
                    service_class="foreground",
                )
                if dynamic
                else ""
            )
            bundle = self._examples.produce(
                snapshot,
                verification_id=verification_id,
                tests_spec_max_bytes=tests_spec_limit,
                statement_sample_max_bytes=sample_limit,
                problem_limits=limits,
            )
            source_identity = sha256_hex_json(
                {
                    "subject": "problem",
                    "problem_id": problem_id,
                    "source": "workspace",
                    "language": language,
                    "statement": signature,
                    "verification_sources": verification_signature,
                },
                ensure_ascii=True,
            )
            render_root = snapshot.parent / "rendered-statement"
            render_statement_offline_tree(
                snapshot,
                language,
                render_root,
                problem_title=title,
                examples_bundle=bundle,
                tests_spec_max_bytes=tests_spec_limit,
                statement_sample_max_bytes=sample_limit,
                problem_limits=limits,
            )
            yield PreparedStatementRender(
                problem_id=problem_id,
                root=render_root,
                source_identity=source_identity,
                sample_count=len(bundle["context"]["samples"]),
            )
        finally:
            shutil.rmtree(snapshot.parent, ignore_errors=True)

    @contextmanager
    def _prepare_native_render_tree(
        self,
        problem: str,
        *,
        language: str,
        native_package_id: str = "",
    ) -> Iterator[PreparedStatementRender]:
        problem_id = int(self._workspaces.problem_row(problem)["id"])
        native_package = self._native_package_for_problem(
            problem_id,
            native_package_id=native_package_id,
        )
        staging_parent = Path(
            tempfile.mkdtemp(prefix="statement-native-", dir=self._snapshot_parent())
        )
        try:
            render_root = staging_parent / "rendered-statement"
            render_root.mkdir()
            extracted_package = self._packages.extract_statement_render_tree(
                native_package["id"],
                language,
                render_root,
            )
            source_identity = self._native_source_identity(
                problem_id,
                language=language,
                native_package=extracted_package,
            )
            yield PreparedStatementRender(
                problem_id=problem_id,
                root=render_root,
                source_identity=source_identity,
                sample_count=None,
            )
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    def _render_result(
        self,
        preview_id: str,
        render_root: Path,
        *,
        output_kind: StatementPreviewOutput,
        subject_token: str,
        sample_count: int | None,
    ) -> StatementPreviewRow:
        preview_root = self._preview_root(preview_id)
        preview_root.mkdir(parents=True, exist_ok=True)
        summary: dict[str, object] = {}
        if sample_count is not None:
            summary["sample_count"] = sample_count
        if output_kind == "html":
            html_root = preview_root / "html"
            try:
                html_result = self._html.render(
                    render_root,
                    html_root,
                    subject_token=subject_token,
                )
            except StatementHtmlRenderError as exc:
                if exc.log_text:
                    logs = preview_root / "logs"
                    logs.mkdir(parents=True, exist_ok=True)
                    (logs / "pandoc.log").write_text(
                        sanitize_log_text_for_ui(
                            exc.log_text,
                            path_prefixes=[(str(render_root), ".")],
                            normalize_path_separators=False,
                        ),
                        encoding="utf-8",
                    )
                raise
            summary.update(
                {
                    "content": "html/content.html",
                    "warnings": list(html_result.warnings),
                    "resources": list(html_result.resources),
                }
            )
            if html_result.log_text:
                logs = preview_root / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / "pandoc.log").write_text(
                    html_result.log_text,
                    encoding="utf-8",
                )
        else:
            pdf_result = self._pdf.compile_pdf(render_root / "statements.tex")
            logs = preview_root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            display_log = latex_log_for_display(
                pdf_result.log_text,
                path_prefixes=[(str(render_root), ".")],
            )
            (logs / "latex.log").write_text(
                display_log,
                encoding="utf-8",
            )
            if (
                pdf_result.proc.timed_out
                or pdf_result.proc.returncode != 0
                or not pdf_result.pdf_path.is_file()
            ):
                error = latex_error_excerpt(
                    pdf_result.log_text,
                    max_bytes=self._db.config_values.integer(
                        "AUX_DISPLAY_TEXT_LIMIT_BYTES"
                    ),
                    path_prefixes=[(str(render_root), ".")],
                )
                if pdf_result.proc.timed_out:
                    fallback = "LaTeX compilation timed out."
                elif (
                    not pdf_result.pdf_path.is_file()
                    and pdf_result.proc.returncode == 0
                ):
                    fallback = "LaTeX completed without producing statement.pdf."
                else:
                    fallback = "LaTeX compilation failed."
                summary["error"] = error or fallback
                summary["returncode"] = pdf_result.proc.returncode
                self._store.finish(preview_id, status="failed", summary=summary)
                row = self._store.row(preview_id)
                if row is None:
                    raise RuntimeError("statement preview result disappeared")
                return row
            target = preview_root / "pdf" / "statement.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf_result.pdf_path, target)
            summary["pdf"] = "pdf/statement.pdf"
        self._store.finish(preview_id, status="ok", summary=summary)
        row = self._store.row(preview_id)
        if row is None:
            raise RuntimeError("statement preview result disappeared")
        return row

    def _cached_problem(
        self,
        problem_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        identity: str,
    ) -> StatementPreviewRow | None:
        row = self._store.cached_problem(
            problem_id,
            actor_user_id=actor_user_id,
            source_kind=source_kind,
            output_kind=output_kind,
            language=language,
            input_identity=identity,
        )
        if row is None:
            return None
        if output_kind == "html":
            return (
                row
                if self.html_fragment(
                    row["id"],
                    actor_user_id=actor_user_id,
                    problem_id=problem_id,
                )
                is not None
                else None
            )
        return row if self.pdf(row["id"], actor_user_id=actor_user_id) is not None else None

    def _workspace_source_identity(
        self,
        workspace: Path,
        *,
        problem: str,
        problem_id: int,
        language: str,
    ) -> str:
        tests_spec_limit = self._db.config_values.integer("TEXTAREA_MAX_BYTES")
        sample_limit = self._db.config_values.integer("STATEMENT_SAMPLE_MAX_BYTES")
        limits = problem_config_limits(self._db.config_values)
        title = statement_title_for_language(
            workspace,
            language,
            fallback_title=problem_slug_leaf(problem),
        )
        statement_signature = statement_sources_signature(
            workspace,
            problem_title=title,
            tests_spec_max_bytes=tests_spec_limit,
            statement_sample_max_bytes=sample_limit,
        )
        dynamic = statement_examples_require_verification(
            workspace,
            tests_spec_max_bytes=tests_spec_limit,
            statement_sample_max_bytes=sample_limit,
            problem_limits=limits,
        )
        verification_signature = (
            verification_sources_signature(workspace) if dynamic else ""
        )
        return sha256_hex_json(
            {
                "subject": "problem",
                "problem_id": problem_id,
                "source": "workspace",
                "language": language,
                "statement": statement_signature,
                "verification_sources": verification_signature,
            },
            ensure_ascii=True,
        )

    def _native_package_for_problem(
        self,
        problem_id: int,
        *,
        native_package_id: str,
    ) -> NativePackage:
        requested_id = str(native_package_id or "").strip()
        if requested_id:
            native_package = self._packages.native_package(requested_id)
        else:
            readiness = self._packages.published_readiness(problem_id)
            if readiness["status"] != "ready" or not readiness["native_package_id"]:
                raise ValueError("current Native Package is unavailable")
            native_package = self._packages.native_package(
                readiness["native_package_id"]
            )
        if (
            native_package is None
            or int(native_package["problem_id"]) != int(problem_id)
            or native_package["status"] != "available"
        ):
            raise ValueError("Native Package is unavailable")
        return native_package

    @staticmethod
    def _native_source_identity(
        problem_id: int,
        *,
        language: str,
        native_package: NativePackage,
    ) -> str:
        return sha256_hex_json(
            {
                "subject": "problem",
                "problem_id": problem_id,
                "source": "native_package",
                "native_package_id": native_package["id"],
                "archive_sha256": native_package["archive_sha256"],
                "language": language,
            },
            ensure_ascii=True,
        )

    @staticmethod
    def _preview_identity(
        problem_id: int,
        *,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        source_identity: str,
    ) -> str:
        return sha256_hex_json(
            {
                "subject": "problem",
                "problem_id": problem_id,
                "source": source_kind,
                "output": output_kind,
                "language": language,
                "source_identity": source_identity,
            },
            ensure_ascii=True,
        )

    def _insert_problem(
        self,
        problem_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        identity: str,
    ) -> str:
        preview_id = f"sp-{uuid.uuid4().hex[:16]}"
        self._store.insert(
            preview_id=preview_id,
            actor_user_id=actor_user_id,
            subject_kind="problem",
            problem_id=problem_id,
            contest_id=None,
            source_kind=source_kind,
            output_kind=output_kind,
            language=language,
            input_identity=identity,
            options={},
        )
        return preview_id

    def _failed_problem_preparation(
        self,
        preview_input: StatementPreviewInput,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        error: str,
    ) -> StatementPreviewRow:
        preview_id = self._insert_problem(
            preview_input.problem_id,
            actor_user_id=actor_user_id,
            source_kind=source_kind,
            output_kind=output_kind,
            language=language,
            identity=preview_input.identity,
        )
        self._store.finish(
            preview_id,
            status="failed",
            summary={"error": error},
        )
        row = self._store.row(preview_id)
        if row is None:
            raise RuntimeError("statement preview result disappeared")
        return row

    def _preview_root(self, preview_id: str) -> Path:
        return self._storage.resolve_preview_root(preview_id)

    def _snapshot_parent(self) -> str:
        root = self._storage.snapshot_root
        root.mkdir(parents=True, exist_ok=True)
        return str(root)

    @staticmethod
    def _language(language: str) -> str:
        safe = normalize_statement_language(language)
        if not safe:
            raise ValueError("statement language is required")
        return safe

    @staticmethod
    def _safe_file(root: Path, path: Path) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            resolved_root = root.resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents
