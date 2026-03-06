from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl import run_export as handlers

router = APIRouter()

router.add_api_route(
    "/problems/{problem:path}/{user}/run",
    handlers.run_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/new",
    handlers.run_new_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/details",
    handlers.run_details_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/details/test-fragment",
    handlers.run_details_test_fragment,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/execute",
    handlers.run_execute,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/run/cancel",
    handlers.run_cancel,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export",
    handlers.export_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export/create",
    handlers.export_create,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export/import",
    handlers.export_import,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/export/import/slug-hint",
    handlers.export_import_slug_hint,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/artifacts/{build_id}/{rel_path:path}",
    handlers.artifact_file,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/{user}/runs/{run_id}/artifacts/{rel_path:path}",
    handlers.run_artifact_file,
    methods=["GET"],
)

