from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

from app.service.platform.error_text import bounded_display_text, normalize_display_text
from app.service.problem_package.service import (
    ProblemPackageService,
    PublishedPackageReadiness,
)
from app.service.repository.revision import parse_verification_source
from app.service.verification.failure_display import verification_task_failure_hint
from app.service.verification.service import VerificationService
from app.service.verification.types import Kind, WorkspaceVerificationRow
from app.service.verification.workspace_fingerprint import (
    cached_verification_for_fingerprint,
    remember_verification_fingerprint,
    verification_sources_fingerprint,
    verification_sources_signature,
)


ReadinessTone = Literal["normal", "warning", "danger"]
WorkspaceReadinessState = Literal["current", "behind"]
VerificationResult = Literal["none", "running", "ok", "failed"]
PackageReadinessState = Literal["ready", "stale", "none"]

_SANITY_STATUS_TOKENS = {
    "ok",
    "passed",
    "pending",
    "running",
    "warning",
    "failed",
    "skipped",
}
_GENERIC_VERIFICATION_ERRORS = {
    "verification failed",
    "solution run did not complete",
    "verification mismatch",
}
_REASON_LIMIT_BYTES = 180


class WorkspaceReadinessSubject(TypedDict):
    problem_id: int
    workspace_id: int
    workspace_path: Path
    head_commit: str
    dirty: bool
    local_revision: int | None
    upstream_revision: int | None
    needs_update: bool


class WorkspaceReadiness(TypedDict):
    state: WorkspaceReadinessState
    local_revision: int | None
    upstream_revision: int | None
    dirty: bool
    needs_update: bool
    tone: ReadinessTone


class VerificationReadiness(TypedDict):
    result: VerificationResult
    display: str
    stale: bool
    sanity_status: str
    tone: ReadinessTone
    verification_id: str
    reason_short: str
    created_at: str


class PackageReadiness(TypedDict):
    state: PackageReadinessState
    revision_number: int | None
    tone: ReadinessTone
    reason: str


class ProblemReadiness(TypedDict):
    workspace: WorkspaceReadiness
    verification: VerificationReadiness
    package: PackageReadiness


def _empty_verification() -> VerificationReadiness:
    return {
        "result": "none",
        "display": "none",
        "stale": False,
        "sanity_status": "unknown",
        "tone": "danger",
        "verification_id": "",
        "reason_short": "",
        "created_at": "",
    }


def _compact_reason(parts: list[str]) -> str:
    normalized: list[str] = []
    for part in parts:
        text = normalize_display_text(part)
        compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if compact and compact not in normalized:
            normalized.append(compact)
    return bounded_display_text(
        "; ".join(normalized),
        limit_bytes=_REASON_LIMIT_BYTES,
    )


def _error_prefers_task_hint(error_text: str) -> bool:
    return bool(
        not error_text
        or error_text in _GENERIC_VERIFICATION_ERRORS
        or (
            error_text.startswith("required=[")
            and ", allowed=[" in error_text
            and ", got=[" in error_text
        )
    )


class ProblemReadinessService:
    """Read-only review projection for already-authorized workspaces.

    This service may inspect Git and workspace sources. It never creates or
    refreshes workspaces, validates package archives, or writes durable state.
    """

    def __init__(
        self,
        verification_service: VerificationService,
        problem_package_service: ProblemPackageService,
    ) -> None:
        self.verification_service = verification_service
        self.problem_package_service = problem_package_service

    @classmethod
    def unavailable(
        cls,
        subject: WorkspaceReadinessSubject,
    ) -> ProblemReadiness:
        return {
            "workspace": cls._workspace(subject),
            "verification": _empty_verification(),
            "package": {
                "state": "none",
                "revision_number": None,
                "tone": "danger",
                "reason": "readiness unavailable",
            },
        }

    @staticmethod
    def _workspace(subject: WorkspaceReadinessSubject) -> WorkspaceReadiness:
        needs_update = bool(subject["needs_update"])
        return {
            "state": "behind" if needs_update else "current",
            "local_revision": subject["local_revision"],
            "upstream_revision": subject["upstream_revision"],
            "dirty": bool(subject["dirty"]),
            "needs_update": needs_update,
            "tone": "danger" if needs_update else "normal",
        }

    @staticmethod
    def _package(readiness: PublishedPackageReadiness) -> PackageReadiness:
        status = readiness["status"]
        if status == "ready":
            return {
                "state": "ready",
                "revision_number": readiness["materialized_revision_number"],
                "tone": "normal",
                "reason": "",
            }
        if status == "stale":
            return {
                "state": "stale",
                "revision_number": readiness["materialized_revision_number"],
                "tone": "warning",
                "reason": readiness["missing_reason"],
            }
        return {
            "state": "none",
            "revision_number": None,
            "tone": "danger",
            "reason": readiness["missing_reason"],
        }

    @staticmethod
    def _current_verification_row(
        subject: WorkspaceReadinessSubject,
        rows: list[WorkspaceVerificationRow],
    ) -> tuple[WorkspaceVerificationRow, bool]:
        head_commit = subject["head_commit"]
        if not subject["dirty"] and head_commit:
            commit_row = next(
                (
                    row
                    for row in rows
                    if parse_verification_source(row["source_commit"]).base_commit
                    == head_commit
                ),
                None,
            )
            if commit_row is not None:
                return (commit_row, False)

        fingerprint = ""
        signature = ""
        try:
            fingerprint = verification_sources_fingerprint(subject["workspace_path"])
        except (OSError, RuntimeError, ValueError):
            fingerprint = ""
        row, signature = cached_verification_for_fingerprint(
            subject["problem_id"],
            subject["workspace_id"],
            fingerprint,
            rows,
        )
        fingerprint_match = row is not None
        if row is None:
            try:
                signature = verification_sources_signature(subject["workspace_path"])
            except (OSError, RuntimeError, ValueError):
                signature = ""
        if row is None and signature:
            row = next((item for item in rows if item["signature"] == signature), None)
        if row is None:
            row = rows[0]
        if fingerprint:
            remember_verification_fingerprint(
                subject["problem_id"],
                subject["workspace_id"],
                fingerprint,
                row["id"],
                signature,
            )
        if fingerprint_match and not signature:
            stale = False
        elif signature and row["signature"]:
            stale = row["signature"] != signature
        else:
            stale = bool(
                subject["dirty"]
                or (
                    head_commit
                    and row["source_commit"]
                    and row["source_commit"] != head_commit
                )
            )
        return (row, stale)

    def _verification(
        self,
        subject: WorkspaceReadinessSubject,
        rows: list[WorkspaceVerificationRow],
        *,
        explain: bool,
    ) -> VerificationReadiness:
        if not rows:
            return _empty_verification()
        row, stale = self._current_verification_row(subject, rows)
        status = row["status"]
        if status == "ok":
            result: VerificationResult = "ok"
        elif status in {"queued", "pending", "running"}:
            result = "running"
        else:
            result = "failed"
        sanity_status = row["sanity_status"].lower()
        if sanity_status not in _SANITY_STATUS_TOKENS:
            sanity_status = "unknown"
        sanity_attention = result == "ok" and sanity_status in {"warning", "failed"}
        display = f"{result} (stale)" if stale else result
        if not stale and result == "ok" and sanity_status == "warning":
            display = "ok (has warning)"
        elif not stale and result == "ok" and sanity_status == "failed":
            display = "ok (sanity failed)"
        if stale or sanity_attention:
            tone: ReadinessTone = "warning"
        elif result == "failed":
            tone = "danger"
        else:
            tone = "normal"

        reason = ""
        if explain and (result == "failed" or stale or sanity_attention):
            parts: list[str] = []
            if stale:
                parts.append("Inputs changed since this verification")
            error_text = row["fail_reason"] or row["error"]
            if result == "failed":
                task_hint = ""
                if _error_prefers_task_hint(error_text):
                    task_hint = verification_task_failure_hint(
                        self.verification_service.task_store,
                        row["id"],
                    )
                parts.append(task_hint or error_text or "verification failed")
            elif sanity_attention:
                if not error_text:
                    detail = self.verification_service.verification_detail(row["id"])
                    error_text = str(detail.get("error") or "")
                parts.append(error_text or f"sanity check {sanity_status}")
            reason = _compact_reason(parts)
        return {
            "result": result,
            "display": display,
            "stale": stale,
            "sanity_status": sanity_status,
            "tone": tone,
            "verification_id": row["id"],
            "reason_short": reason,
            "created_at": row["created_at"],
        }

    def _result(
        self,
        subject: WorkspaceReadinessSubject,
        rows: list[WorkspaceVerificationRow],
        package: PublishedPackageReadiness,
        *,
        explain_verification: bool,
    ) -> ProblemReadiness:
        return {
            "workspace": self._workspace(subject),
            "verification": self._verification(
                subject,
                rows,
                explain=explain_verification,
            ),
            "package": self._package(package),
        }

    def readiness(
        self,
        subject: WorkspaceReadinessSubject,
        *,
        explain_verification: bool = True,
    ) -> ProblemReadiness:
        rows = self.verification_service.workspace_verification_rows(
            subject["problem_id"],
            subject["workspace_id"],
            limit=40,
            kinds=(Kind.ALL.value,),
        )
        package = self.problem_package_service.published_readiness(
            subject["problem_id"]
        )
        return self._result(
            subject,
            rows,
            package,
            explain_verification=explain_verification,
        )

    def readiness_many(
        self,
        subjects: list[WorkspaceReadinessSubject],
        *,
        explain_verification: bool = False,
    ) -> dict[int, ProblemReadiness]:
        if not subjects:
            return {}
        verification_rows = self.verification_service.workspace_verification_rows_many(
            [
                (subject["problem_id"], subject["workspace_id"])
                for subject in subjects
            ],
            limit=40,
            kinds=(Kind.ALL.value,),
        )
        package_rows = self.problem_package_service.published_readiness_many(
            [subject["problem_id"] for subject in subjects]
        )
        return {
            subject["problem_id"]: self._result(
                subject,
                verification_rows.get(
                    (subject["problem_id"], subject["workspace_id"]),
                    [],
                ),
                package_rows[subject["problem_id"]],
                explain_verification=explain_verification,
            )
            for subject in subjects
        }
