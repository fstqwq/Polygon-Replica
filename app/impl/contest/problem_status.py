from typing import Literal, TypedDict

from app.impl.workspace.context_model import workspace_revision_notice
from app.service.problem.readiness import ProblemReadiness, ReadinessTone


StatusTone = Literal["normal", "warning", "danger"]


class ContestProblemStatusItem(TypedDict):
    label: str
    display: str
    tone: StatusTone
    note: str
    note_tone: StatusTone


class ContestProblemStatusView(TypedDict):
    items: list[ContestProblemStatusItem]
    aria_label: str


def _revision_display(revision: int | None) -> str:
    return f"v{revision}" if revision is not None else "none"


def _item(
    label: str,
    display: str,
    *,
    tone: StatusTone = "normal",
    note: str = "",
    note_tone: StatusTone = "normal",
) -> ContestProblemStatusItem:
    return {
        "label": label,
        "display": display,
        "tone": tone,
        "note": note,
        "note_tone": note_tone,
    }


def _package_item(readiness: ProblemReadiness) -> ContestProblemStatusItem:
    package = readiness["package"]
    state = package["state"]
    if state == "ready":
        return _item("Package", "ready")
    if state == "stale":
        return _item(
            "Package",
            _revision_display(package["revision_number"]),
            note="stale",
            note_tone="warning",
        )
    if state == "queued":
        return _item("Package", "queued")
    return _item("Package", "none", tone="danger")


def contest_problem_status(
    readiness: ProblemReadiness,
) -> ContestProblemStatusView:
    package = readiness["package"]
    published_revision = package["published_revision_number"]
    items = [
        _item(
            "Published",
            _revision_display(published_revision),
            tone="danger" if published_revision is None else "normal",
        )
    ]

    workspace = workspace_revision_notice(readiness)
    if workspace is not None:
        note_tone: ReadinessTone = workspace["tone"]
        items.append(
            _item(
                "Workspace",
                workspace["display"],
                note=workspace["meta"],
                note_tone=note_tone,
            )
        )

    verification = readiness["verification"]
    items.append(
        _item(
            "Verification",
            verification["display"],
            tone=verification["tone"],
        )
    )
    items.append(_package_item(readiness))

    aria_parts = []
    for item in items:
        description = f'{item["label"]}: {item["display"]}'
        if item["note"]:
            description += f' ({item["note"]})'
        aria_parts.append(description)
    return {
        "items": items,
        "aria_label": "; ".join(aria_parts),
    }
