"""Contest orchestration for disposable statement previews."""

from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import TypedDict

from app.service.access.query import AccessQuery
from app.service.contest.service import ContestProblem, ContestService
from app.service.contest.snapshot import ContestSourceSnapshotService
from app.service.contest.statement import ContestStatementService
from app.service.statement.preview_state import (
    StatementPreviewRepository,
    StatementPreviewRow,
    StatementPreviewSource,
)
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.hashing import sha256_hex_json
from app.service.problem_package.service import ProblemPackageService
from app.service.repository.workspace import WorkspaceService
from app.service.statement.context import statement_languages
from app.service.statement.transient_preview import (
    PreparedStatementRender,
    StatementPreviewService,
)


class ContestStatementPreviewLinkGroup(TypedDict):
    source: StatementPreviewSource
    label: str
    languages: list[str]


class ContestStatementPreviewItem(TypedDict):
    idx: str
    problem_id: int
    problem_slug: str
    preview_id: str
    status: str
    error: str


class ContestStatementPreviewService:
    """Build and project one ordered Contest review from per-Problem previews."""

    def __init__(
        self,
        *,
        contest_service: ContestService,
        access_query: AccessQuery,
        workspace_service: WorkspaceService,
        package_service: ProblemPackageService,
        problem_preview_service: StatementPreviewService,
        storage_layout: StorageLayout,
        preview_store: StatementPreviewRepository,
        statement_service: ContestStatementService,
        snapshot_service: ContestSourceSnapshotService,
    ) -> None:
        self._contests = contest_service
        self._access = access_query
        self._workspaces = workspace_service
        self._packages = package_service
        self._problem_previews = problem_preview_service
        self._storage = storage_layout
        self._store = preview_store
        self._statements = statement_service
        self._snapshots = snapshot_service

    def link_groups(
        self,
        contest_id: int,
        *,
        user_id: int,
        username: str,
    ) -> list[ContestStatementPreviewLinkGroup]:
        rows = self._contests.contest_problems(contest_id)
        if not rows:
            return []
        problem_ids = [row["problem_id"] for row in rows]
        access = self._access.problem_contexts(problem_ids, user_id)
        workspace_languages = self._workspace_language_sets(
            rows,
            access=access,
            user_id=user_id,
            username=username,
        )
        package_languages = self._package_language_sets(rows, access=access)
        groups: list[ContestStatementPreviewLinkGroup] = []
        workspace_intersection = self._intersection(workspace_languages)
        if workspace_intersection:
            groups.append(
                {
                    "source": "workspace",
                    "label": "Workspace",
                    "languages": workspace_intersection,
                }
            )
        package_intersection = self._intersection(package_languages)
        if package_intersection:
            groups.append(
                {
                    "source": "native_package",
                    "label": "Packages",
                    "languages": package_intersection,
                }
            )
        return groups

    def build_html(
        self,
        contest_id: int,
        *,
        user_id: int,
        username: str,
        source_kind: StatementPreviewSource,
        language: str,
    ) -> StatementPreviewRow:
        groups = self.link_groups(
            contest_id,
            user_id=user_id,
            username=username,
        )
        available = {
            item["source"]: set(item["languages"])
            for item in groups
        }
        if language not in available.get(source_kind, set()):
            raise ValueError(
                f"{language} is not available for every accessible Contest problem"
            )
        rows = self._contests.contest_problems(contest_id)
        access = self._access.problem_contexts(
            [row["problem_id"] for row in rows],
            user_id,
        )

        def build(row: ContestProblem) -> ContestStatementPreviewItem:
            problem_access = access[row["problem_id"]]
            allowed = (
                problem_access["can_write"]
                if source_kind == "workspace"
                else problem_access["can_read"]
            )
            if not allowed:
                reason = (
                    problem_access["write_block_reason"]
                    if source_kind == "workspace"
                    else problem_access["read_block_reason"]
                )
                return self._item(row, error=str(reason or "problem access required"))
            try:
                preview = self._problem_previews.build_problem(
                    row["problem_slug"],
                    username,
                    source_kind=source_kind,
                    output_kind="html",
                    language=language,
                )
            except Exception as exc:  # one Problem must not hide the remaining review
                return self._item(row, error=str(exc) or "statement preview failed")
            return self._item(
                row,
                preview_id=preview["id"],
                status=preview["status"],
                error=str(preview["summary"].get("error") or ""),
            )

        worker_count = min(4, max(1, len(rows)))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="contest-statement-preview",
        ) as pool:
            items = list(pool.map(build, rows))
        identity = sha256_hex_json(
            {
                "subject": "contest",
                "contest_id": contest_id,
                "source": source_kind,
                "output": "html",
                "language": language,
                "items": [
                    {
                        "idx": item["idx"],
                        "problem_id": item["problem_id"],
                        "preview_id": item["preview_id"],
                        "status": item["status"],
                    }
                    for item in items
                ],
            },
            ensure_ascii=True,
        )
        preview_id = f"sp-{uuid.uuid4().hex[:16]}"
        self._store.insert(
            preview_id=preview_id,
            actor_user_id=user_id,
            subject_kind="contest",
            problem_id=None,
            contest_id=contest_id,
            source_kind=source_kind,
            output_kind="html",
            language=language,
            input_identity=identity,
            options={},
        )
        successful = sum(item["status"] == "ok" for item in items)
        status = "ok" if successful else "failed"
        self._store.finish(
            preview_id,
            status=status,
            summary={
                "items": list(items),
                "successful": successful,
                "failed": len(items) - successful,
            },
        )
        result = self._store.row(preview_id)
        if result is None:
            raise RuntimeError("Contest statement preview result disappeared")
        return result

    def latest_html(
        self,
        contest_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        language: str,
    ) -> StatementPreviewRow | None:
        return self._store.latest_contest(
            contest_id,
            actor_user_id=actor_user_id,
            source_kind=source_kind,
            output_kind="html",
            language=language,
        )

    def build_pdf(
        self,
        contest_id: int,
        *,
        contest_slug: str,
        user_id: int,
        username: str,
        source_kind: StatementPreviewSource,
        language: str,
    ) -> StatementPreviewRow:
        groups = self.link_groups(
            contest_id,
            user_id=user_id,
            username=username,
        )
        available = {
            item["source"]: set(item["languages"])
            for item in groups
        }
        if language not in available.get(source_kind, set()):
            raise ValueError(
                f"{language} is not available for every accessible Contest problem"
            )
        rows = self._contests.contest_problems(contest_id)
        access = self._access.problem_contexts(
            [row["problem_id"] for row in rows],
            user_id,
        )

        options: dict[str, object] = {}
        contest = self._contests.contest_context(contest_slug)
        if contest is None or contest["id"] != contest_id:
            raise ValueError("contest not found")
        for row in rows:
            problem_access = access[row["problem_id"]]
            allowed = (
                problem_access["can_write"]
                if source_kind == "workspace"
                else problem_access["can_read"]
            )
            if not allowed:
                reason = (
                    problem_access["write_block_reason"]
                    if source_kind == "workspace"
                    else problem_access["read_block_reason"]
                )
                raise PermissionError(str(reason or "problem access required"))
        with ExitStack() as stack:
            prepared: list[PreparedStatementRender] = [
                stack.enter_context(
                    self._problem_previews.prepare_render_tree(
                        row["problem_slug"],
                        username,
                        source_kind=source_kind,
                        language=language,
                    )
                )
                for row in rows
            ]
            identity = sha256_hex_json(
                {
                    "subject": "contest",
                    "contest_id": contest_id,
                    "source_generation": contest["source_generation"],
                    "source": source_kind,
                    "output": "pdf",
                    "language": language,
                    "problems": [
                        {
                            "idx": row["idx"],
                            "problem_id": row["problem_id"],
                            "source_identity": item.source_identity,
                        }
                        for row, item in zip(rows, prepared, strict=True)
                    ],
                },
                ensure_ascii=True,
            )
            cached = self._store.cached_contest(
                contest_id,
                actor_user_id=user_id,
                source_kind=source_kind,
                output_kind="pdf",
                language=language,
                input_identity=identity,
                options=options,
            )
            if (
                cached is not None
                and self._problem_previews.pdf(
                    cached["id"], actor_user_id=user_id
                )
                is not None
            ):
                return cached
            preview_id = f"sp-{uuid.uuid4().hex[:16]}"
            self._store.insert(
                preview_id=preview_id,
                actor_user_id=user_id,
                subject_kind="contest",
                problem_id=None,
                contest_id=contest_id,
                source_kind=source_kind,
                output_kind="pdf",
                language=language,
                input_identity=identity,
                options=options,
            )
            preview_root = self._storage.resolve_preview_root(preview_id)
            try:
                default_template = self._statements.default_statements_template()
                source_snapshot = self._snapshots.copy_to(
                    contest_slug=contest["slug"],
                    target=preview_root / "contest-sources",
                    language=language,
                    default_statements_template=default_template,
                )
                summary = self._statements.build_preview_pdf(
                    contest_slug=contest["slug"],
                    language=language,
                    source_snapshot=source_snapshot,
                    problem_entries=rows,
                    render_roots={item.problem_id: item.root for item in prepared},
                    output_root=preview_root,
                )
                status = "failed" if summary.get("error") else "ok"
                self._store.finish(preview_id, status=status, summary=summary)
            except Exception as exc:
                self._store.finish(
                    preview_id,
                    status="failed",
                    summary={"error": str(exc)},
                )
            finally:
                # The compiled document and log are the cache result.  The copied
                # Contest source and TeX working tree are intentionally ephemeral.
                shutil.rmtree(preview_root / "contest-sources", ignore_errors=True)
                shutil.rmtree(preview_root / "contest-pdf-src", ignore_errors=True)
            result = self._store.row(preview_id)
            if result is None:
                raise RuntimeError("Contest statement PDF result disappeared")
            return result

    def latest_pdf(
        self,
        contest_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        language: str,
    ) -> StatementPreviewRow | None:
        return self._store.latest_contest(
            contest_id,
            actor_user_id=actor_user_id,
            source_kind=source_kind,
            output_kind="pdf",
            language=language,
        )

    @staticmethod
    def items(row: StatementPreviewRow | None) -> list[ContestStatementPreviewItem]:
        if row is None:
            return []
        raw_items = row["summary"].get("items")
        if not isinstance(raw_items, list):
            return []
        items: list[ContestStatementPreviewItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            items.append(
                {
                    "idx": str(raw.get("idx") or ""),
                    "problem_id": int(raw.get("problem_id") or 0),
                    "problem_slug": str(raw.get("problem_slug") or ""),
                    "preview_id": str(raw.get("preview_id") or ""),
                    "status": str(raw.get("status") or "failed"),
                    "error": str(raw.get("error") or ""),
                }
            )
        return items

    def _workspace_language_sets(
        self,
        rows: list[ContestProblem],
        *,
        access: dict,
        user_id: int,
        username: str,
    ) -> list[set[str]]:
        eligible = [row for row in rows if access[row["problem_id"]]["can_write"]]
        states = self._workspaces.workspace_rows(
            [row["problem_id"] for row in eligible],
            user_id,
        )
        result: list[set[str]] = []
        for row in eligible:
            state = states.get(row["problem_id"])
            if state is None:
                result.append(set())
                continue
            try:
                workspace = Path(str(state["path"])).resolve()
                expected = self._storage.workspace(username, row["problem_slug"])
            except (OSError, ValueError):
                result.append(set())
                continue
            if workspace != expected or not workspace.is_dir() or not (workspace / ".git").is_dir():
                result.append(set())
                continue
            result.append(set(statement_languages(workspace)))
        return result

    def _package_language_sets(
        self,
        rows: list[ContestProblem],
        *,
        access: dict,
    ) -> list[set[str]]:
        eligible = [row for row in rows if access[row["problem_id"]]["can_read"]]
        readiness = self._packages.published_readiness_many(
            [row["problem_id"] for row in eligible]
        )
        result: list[set[str]] = []
        for row in eligible:
            package_id = readiness[row["problem_id"]]["native_package_id"]
            result.append(
                set(self._packages.statement_languages(package_id))
                if readiness[row["problem_id"]]["status"] == "ready" and package_id
                else set()
            )
        return result

    @staticmethod
    def _intersection(values: list[set[str]]) -> list[str]:
        if not values:
            return []
        return sorted(set.intersection(*values))

    @staticmethod
    def _item(
        row: ContestProblem,
        *,
        preview_id: str = "",
        status: str = "failed",
        error: str = "",
    ) -> ContestStatementPreviewItem:
        return {
            "idx": row["idx"],
            "problem_id": row["problem_id"],
            "problem_slug": row["problem_slug"],
            "preview_id": preview_id,
            "status": status,
            "error": error,
        }
