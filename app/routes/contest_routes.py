from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl import contests as handlers

router = APIRouter()

router.add_api_route(
    "/contests/{contest}/{user}/overview",
    handlers.contest_overview_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/problems",
    handlers.contest_problems_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/add",
    handlers.contest_problems_add,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/remove",
    handlers.contest_problems_remove,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/remove-selected",
    handlers.contest_problems_remove_selected,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/reorder",
    handlers.contest_problems_reorder,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/renumber",
    handlers.contest_problems_renumber,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/change-general",
    handlers.contest_problems_change_general,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/properties",
    handlers.contest_properties_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/properties/save",
    handlers.contest_properties_save,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/access",
    handlers.contest_access_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/access/grant",
    handlers.contest_access_grant,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/access/revoke",
    handlers.contest_access_revoke,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages",
    handlers.contest_packages_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/preview/start",
    handlers.contest_packages_preview_start,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/build/start",
    handlers.contest_packages_build_start,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/jobs/status",
    handlers.contest_packages_job_status,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/artifacts/{artifact_id}",
    handlers.contest_packages_artifact_download,
    methods=["GET"],
)
