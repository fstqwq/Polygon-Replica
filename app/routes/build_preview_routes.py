from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl import build_preview as handlers

router = APIRouter()

router.add_api_route(
    "/problems/{problem:path}/{user}/tests",
    handlers.build_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/add-manual",
    handlers.tests_spec_add_manual,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/add-manual-upload",
    handlers.tests_spec_add_manual_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/add-gen",
    handlers.tests_spec_add_gen,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/edit",
    handlers.tests_spec_edit,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/delete",
    handlers.tests_spec_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/reindex",
    handlers.tests_spec_reindex,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/gen-script",
    handlers.tests_spec_gen_script_save,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/payload/download",
    handlers.tests_spec_payload_download,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/payload/upload",
    handlers.tests_spec_payload_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/preview",
    handlers.preview_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/preview/run",
    handlers.preview_run,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/preview/status",
    handlers.preview_status,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/preview/save",
    handlers.preview_save,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/statement/attachments/delete",
    handlers.statement_attachment_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/verification/start",
    handlers.verification_start,
    methods=["POST"],
)

