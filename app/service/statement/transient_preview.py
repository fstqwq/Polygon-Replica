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
from app.service.disk.statement_preview_store import (
    StatementPreviewOutput,
    StatementPreviewRow,
    StatementPreviewSource,
    StatementPreviewStore,
)
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.hashing import sha256_hex_json
from app.service.problem.runtime_config import problem_config_limits
from app.service.problem_package.service import ProblemPackageService
from app.service.repository.workspace import WorkspaceService
from app.service.statement.context import normalize_statement_language
from app.service.statement.examples import (
    StatementExamplesProducer,
    statement_examples_require_verification,
)
from app.service.statement.html_render import StatementHtmlRenderer
from app.service.statement.render import (
    render_statement_offline_tree,
    statement_title_for_language,
)
from app.service.statement.signature import statement_sources_signature
from app.service.statement.tex_compile import TexCompileService
from app.service.verification.workflow import VerificationWorkflow


@dataclass(frozen=True)
class PreparedStatementRender:
    """One disposable, fully rendered Problem statement source tree."""

    problem_id: int
    root: Path
    source_identity: str
    sample_count: int | None


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
    ) -> None:
        self._db = db
        self._store = StatementPreviewStore(db)
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
    ) -> StatementPreviewRow:
        safe_language = self._language(language)
        actor_user_id = int(self._workspaces.user_row(username)["id"])
        with self.prepare_render_tree(
            problem,
            username,
            source_kind=source_kind,
            language=safe_language,
        ) as prepared:
            identity = sha256_hex_json(
                {
                    "subject": "problem",
                    "problem_id": prepared.problem_id,
                    "source": source_kind,
                    "output": output_kind,
                    "language": safe_language,
                    "source_identity": prepared.source_identity,
                },
                ensure_ascii=True,
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
                    raise RuntimeError("statement preview result disappeared") from exc
                return row

    @contextmanager
    def prepare_render_tree(
        self,
        problem: str,
        username: str,
        *,
        source_kind: StatementPreviewSource,
        language: str,
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
            status = self._workspaces.read_workspace_status(workspace)
            snapshot = self._workspaces.create_snapshot(
                workspace,
                None,
                workspace_head=str(status.get("head_commit") or ""),
                workspace_dirty=bool(status.get("dirty")),
            )
        try:
            verification_id = (
                self._verification.run_workspace(problem, username, sample_only=True)
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
                    "examples": bundle,
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
    ) -> Iterator[PreparedStatementRender]:
        problem_id = int(self._workspaces.problem_row(problem)["id"])
        readiness = self._packages.published_readiness(problem_id)
        native_package_id = readiness["native_package_id"]
        if readiness["status"] != "ready" or not native_package_id:
            raise ValueError("current Native Package is unavailable")
        source_identity = sha256_hex_json(
            {
                "subject": "problem",
                "problem_id": problem_id,
                "source": "native_package",
                "native_package_id": native_package_id,
                "language": language,
            },
            ensure_ascii=True,
        )
        staging_parent = Path(
            tempfile.mkdtemp(prefix="statement-native-", dir=self._snapshot_parent())
        )
        try:
            render_root = staging_parent / "rendered-statement"
            with self._packages.open_reader(native_package_id) as reader:
                source = reader.root / "statement-build" / language
                if not source.is_dir() or source.is_symlink():
                    raise ValueError(f"Native Package has no {language} statement")
                shutil.copytree(source, render_root)
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
            html_result = self._html.render(
                render_root,
                html_root,
                subject_token=subject_token,
            )
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
            if (
                pdf_result.proc.timed_out
                or pdf_result.proc.returncode != 0
                or not pdf_result.pdf_path.is_file()
            ):
                raise RuntimeError("statement PDF compile failed")
            target = preview_root / "pdf" / "statement.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf_result.pdf_path, target)
            logs = preview_root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "latex.log").write_text(
                pdf_result.log_text,
                encoding="utf-8",
            )
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
