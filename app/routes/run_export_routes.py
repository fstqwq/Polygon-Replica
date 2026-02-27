from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl import run_export as handlers

router = APIRouter()

router.add_api_route(
    "/problems/{problem}/{user}/run",
    handlers.run_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem}/{user}/run/new",
    handlers.run_new_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem}/{user}/run/details",
    handlers.run_details_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem}/{user}/run/execute",
    handlers.run_execute,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/export",
    handlers.export_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem}/{user}/export/create",
    handlers.export_create,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem}/{user}/artifacts/{build_id}/{rel_path:path}",
    handlers.artifact_file,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem}/{user}/runs/{run_id}/artifacts/{rel_path:path}",
    handlers.run_artifact_file,
    methods=["GET"],
)
