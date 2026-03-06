from __future__ import annotations

from fastapi import APIRouter

from app.impl import judgehost_api as handlers

router = APIRouter()

router.add_api_route(
    "/api/v4/config",
    handlers.domjudge_config,
    methods=["GET"],
)
router.add_api_route(
    "/api/v4/languages",
    handlers.domjudge_languages,
    methods=["GET"],
)
router.add_api_route(
    "/api/v4/judgehosts",
    handlers.domjudge_judgehosts_get,
    methods=["GET"],
)
router.add_api_route(
    "/api/v4/judgehosts",
    handlers.domjudge_judgehosts_post,
    methods=["POST"],
)
router.add_api_route(
    "/api/v4/judgehosts/fetch-work",
    handlers.domjudge_fetch_work,
    methods=["POST"],
)
router.add_api_route(
    "/api/v4/judgehosts/get_files/source/{item_id}",
    handlers.domjudge_get_files_source_submit,
    methods=["GET"],
)
router.add_api_route(
    "/api/v4/judgehosts/get_files/source/{contest_id}/{item_id}",
    handlers.domjudge_get_files_source,
    methods=["GET"],
)
router.add_api_route(
    "/api/v4/judgehosts/get_files/{file_type}/{item_id}",
    handlers.domjudge_get_files_by_type,
    methods=["GET"],
)
router.add_api_route(
    "/api/v4/judgehosts/get_version_commands/{judgetask_id}",
    handlers.domjudge_get_version_commands,
    methods=["GET"],
)
router.add_api_route(
    "/api/v4/judgehosts/check_versions/{judgetask_id}",
    handlers.domjudge_check_versions,
    methods=["PUT"],
)
router.add_api_route(
    "/api/v4/judgehosts/update-judging/{hostname}/{judgetask_id}",
    handlers.domjudge_update_judging,
    methods=["PUT"],
)
router.add_api_route(
    "/api/v4/judgehosts/add-judging-run/{hostname}/{judgetask_id}",
    handlers.domjudge_add_judging_run,
    methods=["POST"],
)
router.add_api_route(
    "/api/v4/judgehosts/add-debug-info/{hostname}/{judgetask_id}",
    handlers.domjudge_add_debug_info,
    methods=["POST"],
)
router.add_api_route(
    "/api/v4/judgehosts/internal-error",
    handlers.domjudge_internal_error,
    methods=["POST"],
)
