from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.preview.preview import (
    preview_page,
    preview_run,
    preview_save,
    preview_status,
    statement_compile_asset_upload,
    statement_attachment_upload,
    statement_language_add,
    statement_attachment_delete,
    statement_compile_asset_delete,
)

router = APIRouter()

router.add_api_route(
    "/problems/{problem:path}/preview",
    preview_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/preview/run",
    preview_run,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/preview/status",
    preview_status,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/preview/save",
    preview_save,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/statement/assets/upload",
    statement_compile_asset_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/statement/assets/delete",
    statement_compile_asset_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/statement/attachments/upload",
    statement_attachment_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/statement/attachments/delete",
    statement_attachment_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/statement/languages/add",
    statement_language_add,
    methods=["POST"],
)
