from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.contest.access import (
    contest_access_grant,
    contest_access_page,
    contest_access_revoke,
    contest_problem_access_save,
)
from app.impl.contest.overview import (
    contest_build_all_packages,
    contest_overview_page,
)
from app.impl.contest.package import contest_packages_download
from app.impl.contest.problem import (
    contest_problems_add,
    contest_problems_page,
    contest_problems_remove,
    contest_problems_remove_selected,
    contest_problems_save,
)
from app.impl.contest.property import (
    contest_properties_page,
    contest_properties_save,
    contest_property_add,
    contest_property_delete,
    contest_property_insert_preset,
    contest_property_language_add,
)
from app.impl.contest.statement_review import (
    contest_statement_pdf_page,
    contest_statement_review_build,
    contest_statement_review_page,
    contest_statement_review_resource,
)
from app.impl.contest.statement_source import (
    contest_statement_language_remove,
    contest_statement_source_delete,
    contest_statement_source_file,
    contest_statement_source_save,
    contest_statement_source_upload,
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
    "/contests/{contest}/properties/add",
    contest_property_add,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties/language/add",
    contest_property_language_add,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties/delete",
    contest_property_delete,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties/insert-preset",
    contest_property_insert_preset,
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
    "/contests/{contest}/access/problems/save",
    contest_problem_access_save,
    methods=["POST"],
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
    "/contests/{contest}/packages/download",
    contest_packages_download,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties/statement/files",
    contest_statement_source_file,
    methods=["GET"],
)

router.add_api_route(
    "/contests/{contest}/properties/statement/save",
    contest_statement_source_save,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties/statement/upload",
    contest_statement_source_upload,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties/statement/delete",
    contest_statement_source_delete,
    methods=["POST"],
)

router.add_api_route(
    "/contests/{contest}/properties/statement/language/remove",
    contest_statement_language_remove,
    methods=["POST"],
)
