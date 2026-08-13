from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse



router = APIRouter()


@router.get("/maintenance", include_in_schema=False)
def maintenance_page(request: Request):
    snapshot = request.app.state.runtime.maintenance_service.snapshot()
    status = str(snapshot.get("status") or "idle")
    operation = str(snapshot.get("operation") or "artifact_cleanup")
    operation_label = {
        "source_backup": "source backup",
        "restart": "application restart",
    }.get(operation, "artifact cleanup")
    if status == "succeeded":
        query = "backup=success" if operation == "source_backup" else "cleanup=success"
        return RedirectResponse(
            f"/admin?{query}",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
    if status == "idle":
        return PlainTextResponse(
            "No site-wide maintenance operation is running.\n",
            headers={"Cache-Control": "no-store"},
        )
    if status == "failed":
        result = snapshot.get("result") or {}
        completed_stage = (
            str(result.get("completed_stage") or "none")
            if isinstance(result, dict)
            else "none"
        )
        body = (
            f"Site-wide {operation_label} failed.\n"
            f"operation_id: {snapshot.get('operation_id') or 'unknown'}\n"
            f"stage: {snapshot.get('stage') or 'unknown'}\n"
            f"completed_stage: {completed_stage}\n"
            f"error: {snapshot.get('error') or 'unknown error'}\n"
            f"An administrator may retry the {operation_label} from Admin.\n"
        )
        return PlainTextResponse(body, headers={"Cache-Control": "no-store"})
    body = (
        "The site is in maintenance mode.\n"
        f"operation: {operation_label}\n"
        f"status: {status}\n"
        f"operation_id: {snapshot.get('operation_id') or 'unknown'}\n"
        f"stage: {snapshot.get('stage') or 'starting'}\n"
        f"started_at: {snapshot.get('started_at') or 'unknown'}\n"
        "Please retry after this page refreshes.\n"
    )
    return PlainTextResponse(
        body,
        headers={"Cache-Control": "no-store", "Refresh": "2"},
    )
