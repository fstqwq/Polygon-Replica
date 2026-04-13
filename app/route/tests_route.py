from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.tests_spec.routes import (
    add_generator_test,
    add_manual_test,
    delete_spec_test,
    download_test_payload,
    edit_spec_test,
    reindex_spec_test,
    render_tests_page,
    save_gen_script,
    upload_manual_test,
    upload_test_payload,
)
from app.impl.tests_spec.verification import verification_start

router = APIRouter()

router.add_api_route(
    "/problems/{problem:path}/tests",
    render_tests_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/add-manual",
    add_manual_test,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/add-manual-upload",
    upload_manual_test,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/add-gen",
    add_generator_test,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/edit",
    edit_spec_test,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/delete",
    delete_spec_test,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/reindex",
    reindex_spec_test,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/gen-script",
    save_gen_script,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/payload/download",
    download_test_payload,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/tests/spec/payload/upload",
    upload_test_payload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/verification/start",
    verification_start,
    methods=["POST"],
)
