from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl import build_preview as handlers

router = APIRouter()

router.add_api_route(
    "/problems/{problem}/{user}/tests",
    handlers.build_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/add-manual",
    handlers.tests_spec_add_manual,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/add-manual-batch",
    handlers.tests_spec_add_manual_batch,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/add-gen",
    handlers.tests_spec_add_gen,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/add-gen-batch",
    handlers.tests_spec_add_gen_batch,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/update",
    handlers.tests_spec_update,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/set-id",
    handlers.tests_spec_set_id,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/delete",
    handlers.tests_spec_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/move",
    handlers.tests_spec_move,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/payload/download",
    handlers.tests_spec_payload_download,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem}/{user}/tests/spec/payload/upload",
    handlers.tests_spec_payload_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/preview",
    handlers.preview_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem}/{user}/preview/run",
    handlers.preview_run,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/preview/save",
    handlers.preview_save,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/verification/start",
    handlers.verification_start,
    methods=["POST"],
)
