from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.contest.access import (
    contest_access_grant,
    contest_access_page,
    contest_access_revoke,
)
from app.impl.contest.overview import (
    contest_build_all_packages,
    contest_overview_page,
)
from app.impl.contest.package import (
    contest_packages_artifact_download,
    contest_packages_build_start,
    contest_packages_job_status,
    contest_packages_page,
    contest_statement_source_delete,
    contest_statement_source_file,
    contest_statement_source_save,
    contest_statement_source_upload,
)
from app.impl.contest.problem import (
    contest_problems_add,
    contest_problems_change_general_retry,
    contest_problems_page,
    contest_problems_remove,
    contest_problems_remove_selected,
    contest_problems_save,
)
from app.impl.contest.property import contest_properties_page, contest_properties_save
from app.impl.contest.statement_review import (
    contest_statement_pdf_build,
    contest_statement_pdf_file,
    contest_statement_pdf_page,
    contest_statement_review_build,
    contest_statement_review_page,
    contest_statement_review_resource,
)

router = APIRouter()

router.add_api_route(
    "/contests/{contest}/overview",
    contest_overview_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="contest_overview",
)

router.add_api_route(
    "/contests/{contest}/packages/build-all",
    contest_build_all_packages,
    methods=["POST"],
    name="contest_build_all_packages",
)

router.add_api_route(
    "/contests/{contest}/problems",
    contest_problems_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/problems/add",
    contest_problems_add,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/problems/remove",
    contest_problems_remove,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/problems/remove-selected",
    contest_problems_remove_selected,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/problems/save",
    contest_problems_save,
    methods=["POST"],
    name="contest_problems_save",
)

router.add_api_route(
    "/contests/{contest}/problems/change-general/retry",
    contest_problems_change_general_retry,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties",
    contest_properties_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/properties/save",
    contest_properties_save,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/access",
    contest_access_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/access/grant",
    contest_access_grant,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/access/revoke",
    contest_access_revoke,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/packages",
    contest_packages_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/statements/review",
    contest_statement_review_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/{contest}/statements/review",
    contest_statement_review_build,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/statements/review/resources/{preview_id}/{name}",
    contest_statement_review_resource,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/statements/pdf",
    contest_statement_pdf_page,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/statements/pdf",
    contest_statement_pdf_build,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/statements/pdf/file/{preview_id}",
    contest_statement_pdf_file,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/packages/build/start",
    contest_packages_build_start,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/packages/jobs/status",
    contest_packages_job_status,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/packages/artifacts/{artifact_id}",
    contest_packages_artifact_download,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/packages/statement/files",
    contest_statement_source_file,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/packages/statement/save",
    contest_statement_source_save,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/packages/statement/upload",
    contest_statement_source_upload,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/packages/statement/delete",
    contest_statement_source_delete,
    methods=["POST"],
)
