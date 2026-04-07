from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.run_export.artifact import artifact_file, export_file
from app.impl.run_export.export import export_create, export_page
from app.impl.run_export.import_source import export_import, export_import_slug_hint
from app.impl.run_export.run import run_cancel, run_details_page, run_details_test_fragment, run_execute, run_new_page, run_page

router = APIRouter()

router.add_api_route(
    "/problems/{problem:path}/{user}/run",
    run_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/new",
    run_new_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/details",
    run_details_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/details/test-fragment",
    run_details_test_fragment,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/execute",
    run_execute,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/cancel",
    run_cancel,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export",
    export_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export/create",
    export_create,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export/import",
    export_import,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export/import/slug-hint",
    export_import_slug_hint,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/artifacts/{verification_id}/{rel_path:path}",
    artifact_file,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/exports/{export_id}/{filename}",
    export_file,
    methods=["GET"],
)
