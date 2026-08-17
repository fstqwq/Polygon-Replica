from fastapi.responses import HTMLResponse

from app.impl.run_export.artifact import (
    artifact_file,
    export_file,
    native_package_file,
)
from app.impl.run_export.export import export_create, export_page
from app.impl.run_export.run import (
    run_cancel,
    run_details_page,
    run_details_sample_json,
    run_details_test_fragment,
    run_execute,
    run_new_page,
    run_page,
    run_rejudge,
)
from app.route.problem_scoped_router import ProblemScopedRouter

router = ProblemScopedRouter()

router.add_api_route(
    "/problems/{problem:path}/run",
    run_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_run",
)
router.add_api_route(
    "/problems/{problem:path}/run/new",
    run_new_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/run/details",
    run_details_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/run/details/test-fragment",
    run_details_test_fragment,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/run/details/sample-json",
    run_details_sample_json,
    methods=["GET"],
    name="run_details_sample_json",
)
router.add_api_route(
    "/problems/{problem:path}/run/execute",
    run_execute,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/run/rejudge",
    run_rejudge,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/run/cancel",
    run_cancel,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/export",
    export_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_export",
)
router.add_api_route(
    "/problems/{problem:path}/export/create",
    export_create,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/artifacts/{verification_id}/{rel_path:path}",
    artifact_file,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/exports/{export_id}/{filename}",
    export_file,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/native-packages/{native_package_id}/download",
    native_package_file,
    methods=["GET"],
    name="native_package_file",
)
