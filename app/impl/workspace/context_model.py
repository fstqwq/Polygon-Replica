"""Typed, shared projections for the Problem workbench UI.

The builder in :mod:`context_ui` owns I/O.  Functions in this module only
combine already-loaded facts; they never query runtime services or inspect the
workspace.  A status therefore has one canonical UI representation regardless
of whether the consumer is the Problem navigation, Workspace review, or a
page-specific template.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from app.service.access.model import ProblemAccessContext, WorkspaceAccessContext
from app.service.problem.content_review import ProblemContentReview
from app.service.problem.context import (
    ContextTone,
    ProblemMetadataContext,
    ProblemTestsContext,
    StatusContext,
    status_context,
)
from app.service.problem.readiness import ProblemReadiness
from app.service.problem.query import SolutionSourceRow
from app.service.repository.git import StatusChangeSummary
from app.service.repository.workspace import WorkspaceContext

if TYPE_CHECKING:
    from app.impl.contest.workspace_scope import ContestWorkspaceContext


class PackageDownloadContext(TypedDict):
    export_id: str
    filename: str


RevisionTone = Literal["normal", "warning", "danger"]
RevisionPairStatus = Literal["current", "stale", "queued", "none"]


class RevisionPairView(TypedDict):
    left_label: str
    left_display: str
    left_tone: RevisionTone
    right_label: str
    right_display: str
    status: RevisionPairStatus
    aria_label: str


class WorkspaceRevisionNotice(TypedDict):
    display: str
    meta: Literal["local changes", "sync required", ""]
    tone: RevisionTone


class NavigationStatusContext(StatusContext):
    download: PackageDownloadContext | None


class ProblemNavigationContext(TypedDict):
    general: NavigationStatusContext
    statements: NavigationStatusContext
    checker: NavigationStatusContext
    interactor: NavigationStatusContext
    validator: NavigationStatusContext
    generators: NavigationStatusContext
    solutions: NavigationStatusContext
    tests: NavigationStatusContext
    verification: NavigationStatusContext
    packages: NavigationStatusContext
    files: NavigationStatusContext
    access: NavigationStatusContext
    workspace: NavigationStatusContext


class SourceComponentContext(TypedDict):
    mode: Literal["repository", "missing", "not-applicable"]
    display: str
    repo_source: str
    repo_source_exists: bool


class CheckerComponentContext(SourceComponentContext):
    standard_checker: str
    standard_expected_checker: str
    standard_warning: str
    standard_valid: bool


class GeneratorSourceContext(TypedDict):
    path: str
    exists: bool
    configured: bool
    reference_count: int


class GeneratorComponentContext(TypedDict):
    mode: Literal["repository", "missing", "empty"]
    display: str
    repo_source: str
    repo_source_exists: bool
    source_rows: list[GeneratorSourceContext]
    configured_sources: list[str]
    source_rows_truncated: bool


class SolutionsComponentContext(TypedDict):
    mode: Literal["ready", "missing-main", "missing"]
    display: str
    accepted_source: str
    accepted_exists: bool
    count: int
    count_display: str
    truncated: bool
    entries: list[SolutionSourceRow]


class ProblemComponentsContext(TypedDict):
    checker: CheckerComponentContext
    interactor: SourceComponentContext
    validator: SourceComponentContext
    generators: GeneratorComponentContext
    solutions: SolutionsComponentContext
    tests: ProblemTestsContext
    statements: StatusContext


class SystemLimitRow(TypedDict):
    label: str
    value: str


class SystemLimitInfo(TypedDict):
    title: str
    description: str
    rows: list[SystemLimitRow]


class ProblemShellContext(TypedDict):
    metadata: ProblemMetadataContext
    components: ProblemComponentsContext
    readiness: ProblemReadiness
    content_review: ProblemContentReview
    navigation: ProblemNavigationContext
    workspace_changes: StatusChangeSummary
    workspace_revision_pair: RevisionPairView


class ProblemPageContext(WorkspaceContext):
    access: ProblemAccessContext
    workspace_access: WorkspaceAccessContext
    branches: list[str]
    branches_truncated: bool
    branch_limit: int
    workspace_auto_update_message: str
    workspace_merge_result: dict[str, object]
    workspace_has_merge_undo: bool
    system_limit_info: SystemLimitInfo
    shell: ProblemShellContext
    contest_workspace: ContestWorkspaceContext | None
    page_wide_content: bool
    topbar_max_1400: NotRequired[bool]
    page_title: NotRequired[str]
    merge_ui: NotRequired[bool]


def navigation_status(
    status: StatusContext,
    *,
    download: PackageDownloadContext | None = None,
) -> NavigationStatusContext:
    return {
        **status,
        "download": download,
    }


def _revision_display(revision: int | None) -> str:
    return f"v{revision}" if revision is not None else "none"


def _revision_aria(revision: int | None) -> str:
    return str(revision) if revision is not None else "none"


def package_published_revision_pair(
    readiness: ProblemReadiness,
) -> RevisionPairView:
    package = readiness["package"]
    package_state = package["state"]
    published_display = _revision_display(package["published_revision_number"])
    published_aria = _revision_aria(package["published_revision_number"])
    package_display = _revision_display(package["revision_number"])
    package_aria = _revision_aria(package["revision_number"])
    if package_state == "ready":
        status: RevisionPairStatus = "current"
        package_tone: RevisionTone = "normal"
    elif package_state == "stale":
        status = "stale"
        package_tone = "warning"
    elif package_state == "queued":
        status = "queued"
        package_display = "queued"
        package_aria = "queued"
        package_tone = "normal"
    else:
        status = "none"
        package_display = "none"
        package_tone = "danger"
    return {
        "left_label": "Package",
        "left_display": package_display,
        "left_tone": package_tone,
        "right_label": "Published",
        "right_display": published_display,
        "status": status,
        "aria_label": (
            f"Package revision {package_aria}; published revision "
            f"{published_aria}; package is {status}"
        ),
    }


def workspace_published_revision_pair(
    local_revision: int | None,
    published_revision: int | None,
    *,
    dirty: bool = False,
    needs_update: bool = False,
) -> RevisionPairView:
    local_display = _revision_display(local_revision)
    published_display = _revision_display(published_revision)
    if local_revision is None or published_revision is None:
        status: RevisionPairStatus = "none"
        tone: RevisionTone = "danger"
    elif needs_update or local_revision != published_revision:
        status = "stale"
        tone = (
            "danger"
            if needs_update or local_revision < published_revision
            else "warning"
        )
    else:
        status = "current"
        tone = "normal"
    dirty_aria = "; workspace has local changes" if dirty else ""
    return {
        "left_label": "Workspace",
        "left_display": local_display,
        "left_tone": tone,
        "right_label": "Published",
        "right_display": published_display,
        "status": status,
        "aria_label": (
            f"Workspace revision {_revision_aria(local_revision)}; "
            f"published revision {_revision_aria(published_revision)}; "
            f"workspace is {status}{dirty_aria}"
        ),
    }


def workspace_revision_notice(
    readiness: ProblemReadiness,
) -> WorkspaceRevisionNotice | None:
    workspace = readiness["workspace"]
    local_revision = workspace["local_revision"]
    published_revision = workspace["upstream_revision"]
    dirty = workspace["dirty"]
    needs_update = workspace["needs_update"]
    if (
        not dirty
        and not needs_update
        and local_revision is not None
        and local_revision == published_revision
    ):
        return None
    if (
        needs_update
        or local_revision is None
        or published_revision is None
        or local_revision < published_revision
    ):
        meta: Literal["local changes", "sync required", ""] = "sync required"
        tone: RevisionTone = "danger"
    elif dirty or local_revision != published_revision:
        meta = "local changes"
        tone = "warning"
    else:
        meta = ""
        tone = "normal"
    return {
        "display": _revision_display(local_revision),
        "meta": meta,
        "tone": tone,
    }


def _component_status(
    component: SourceComponentContext,
    *,
    not_applicable_text: str = "not used",
) -> StatusContext:
    state = component["mode"]
    text = component["display"]
    if state == "not-applicable":
        text = not_applicable_text
    tone: ContextTone = (
        "danger"
        if state in {"missing", "invalid", "none", "missing-main"}
        else "normal"
    )
    return status_context(state=state, text=text, tone=tone)


def _generator_status(component: GeneratorComponentContext) -> StatusContext:
    rows = component["source_rows"]
    if rows:
        used_count = sum(
            1
            for row in rows
            if row["reference_count"] > 0
        )
        count = len(rows)
        noun = "file" if count == 1 else "files"
        return status_context(
            state=str(component["mode"]),
            text=f"{count} {noun}, {used_count} used",
        )
    return status_context(
        state=component["mode"],
        text=component["display"],
        tone="danger" if component["mode"] == "missing" else "normal",
    )


def _solutions_status(component: SolutionsComponentContext) -> StatusContext:
    state = component["mode"]
    if state == "missing-main":
        count_display = str(component["count_display"])
        text = f"{count_display} (no main correct)" if count_display else "no main correct"
    else:
        text = str(component["count_display"] or component["display"])
    return status_context(
        state=state,
        text=text,
        tone="normal" if state == "ready" else "danger",
    )


def _tests_status(component: ProblemTestsContext) -> StatusContext:
    state = component["mode"]
    total = component["total"]
    samples = component["sample"]
    if total > 0:
        sample_label = "sample" if samples == 1 else "samples"
        text = f"{total} ({samples} {sample_label})"
    else:
        text = str(component["display"])
    return status_context(
        state=state,
        text=text,
        tone=(
            "danger"
            if state in {"empty", "invalid", "missing", "none"} or (total > 0 and samples == 0)
            else "normal"
        ),
    )


def navigation_context(
    *,
    metadata: ProblemMetadataContext,
    components: ProblemComponentsContext,
    readiness: ProblemReadiness,
    workspace_changes: StatusChangeSummary,
    access_role: str,
    package_download: PackageDownloadContext | None,
) -> ProblemNavigationContext:
    general_parts = [
        metadata["time_limit_display"],
        metadata["memory_limit_display"],
    ]
    if metadata["pass_limit"] > 1:
        general_parts.append(f'{metadata["pass_limit"]} passes')
    general_parts.append(metadata["mode"])
    general = status_context(state="ready", text=", ".join(general_parts))

    checker = _component_status(
        components["checker"],
        not_applicable_text="uses interactor",
    )
    standard_checker = str(components["checker"]["standard_checker"])
    if metadata["mode"] != "interactive" and standard_checker:
        checker["text"] = standard_checker

    verification = readiness["verification"]
    verification_status = status_context(
        state=verification["result"],
        text=verification["display"],
        tone=verification["tone"],
        hint=verification["reason_short"],
    )

    package = readiness["package"]
    if package["state"] == "ready" and package["revision_number"] is not None:
        package_text = f'v{package["revision_number"]}'
    elif package["state"] == "queued":
        package_text = "queued"
    elif package["state"] == "stale" and package["revision_number"] is not None:
        package_text = f'v{package["revision_number"]} (stale)'
    else:
        package_text = "none"
    package_status = status_context(
        state=package["state"],
        text=package_text,
        tone=(
            "danger"
            if package["state"] == "none"
            else "warning" if package["state"] == "stale" else "normal"
        ),
    )

    changes_total = int(workspace_changes["total"])
    files = status_context(
        state="dirty" if changes_total else "clean",
        text=f"{changes_total} changed" if changes_total else "clean",
    )
    access = status_context(state=access_role, text=access_role)
    return {
        "general": navigation_status(general),
        "statements": navigation_status(components["statements"]),
        "checker": navigation_status(checker),
        "interactor": navigation_status(_component_status(components["interactor"])),
        "validator": navigation_status(_component_status(components["validator"])),
        "generators": navigation_status(_generator_status(components["generators"])),
        "solutions": navigation_status(_solutions_status(components["solutions"])),
        "tests": navigation_status(_tests_status(components["tests"])),
        "verification": navigation_status(verification_status),
        "packages": navigation_status(package_status, download=package_download),
        "files": navigation_status(files),
        "access": navigation_status(access),
        "workspace": navigation_status(access),
    }
