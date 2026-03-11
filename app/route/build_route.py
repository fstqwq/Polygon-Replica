from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.build.test_spec import (
    build_page,
    tests_spec_add_gen,
    tests_spec_add_manual,
    tests_spec_add_manual_upload,
    tests_spec_delete,
    tests_spec_edit,
    tests_spec_gen_script_save,
    tests_spec_payload_download,
    tests_spec_payload_upload,
    tests_spec_reindex,
)
from app.impl.build.verification import verification_start

router = APIRouter()

router.add_api_route(
    "/problems/{problem:path}/{user}/tests",
    build_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/add-manual",
    tests_spec_add_manual,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/add-manual-upload",
    tests_spec_add_manual_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/add-gen",
    tests_spec_add_gen,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/edit",
    tests_spec_edit,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/delete",
    tests_spec_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/reindex",
    tests_spec_reindex,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/gen-script",
    tests_spec_gen_script_save,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/payload/download",
    tests_spec_payload_download,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/tests/spec/payload/upload",
    tests_spec_payload_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/verification/start",
    verification_start,
    methods=["POST"],
)
