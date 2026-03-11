from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.contest.access import contest_access_grant, contest_access_page, contest_access_revoke
from app.impl.contest.overview import contest_overview_page
from app.impl.contest.package import contest_packages_artifact_download, contest_packages_build_start, contest_packages_job_status, contest_packages_page, contest_packages_preview_start
from app.impl.contest.problem import contest_problems_add, contest_problems_change_general, contest_problems_page, contest_problems_remove, contest_problems_remove_selected, contest_problems_renumber, contest_problems_reorder
from app.impl.contest.property import contest_properties_page, contest_properties_save

router = APIRouter()

router.add_api_route(
    "/contests/{contest}/{user}/overview",
    contest_overview_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/problems",
    contest_problems_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/add",
    contest_problems_add,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/remove",
    contest_problems_remove,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/remove-selected",
    contest_problems_remove_selected,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/reorder",
    contest_problems_reorder,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/renumber",
    contest_problems_renumber,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/problems/change-general",
    contest_problems_change_general,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/properties",
    contest_properties_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/properties/save",
    contest_properties_save,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/access",
    contest_access_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/access/grant",
    contest_access_grant,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/access/revoke",
    contest_access_revoke,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages",
    contest_packages_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/preview/start",
    contest_packages_preview_start,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/build/start",
    contest_packages_build_start,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/jobs/status",
    contest_packages_job_status,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/{user}/packages/artifacts/{artifact_id}",
    contest_packages_artifact_download,
    methods=["GET"],
)
