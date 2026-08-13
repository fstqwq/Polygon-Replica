from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.admin.panel import (
    admin_artifacts_cleanup,
    admin_application_restart,
    admin_config_category_page,
    admin_config_category_update,
    admin_config_index,
    admin_judgehost_host_action,
    admin_judgehost_runtime_update,
    admin_judgehost_snapshot,
    admin_judgehosts_page,
    admin_mail_page,
    admin_maintenance_admission,
    admin_overview_page,
    admin_source_backup,
    admin_source_backup_download,
    admin_smtp_test,
    admin_smtp_update,
    admin_system_config_reset,
    admin_user_ban_update,
    admin_user_password_update,
    admin_user_system_admin_update,
    admin_users_page,
    admin_worker_queue_snapshot,
)


router = APIRouter()

router.add_api_route("/admin", admin_overview_page, methods=["GET"], response_class=HTMLResponse)
router.add_api_route(
    "/admin/judgehosts",
    admin_judgehosts_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/admin/maintenance/admission",
    admin_maintenance_admission,
    methods=["POST"],
)
router.add_api_route(
    "/admin/maintenance/restart",
    admin_application_restart,
    methods=["POST"],
)
router.add_api_route("/admin/users", admin_users_page, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/admin/mail", admin_mail_page, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/admin/config", admin_config_index, methods=["GET"])
router.add_api_route(
    "/admin/config/{category}",
    admin_config_category_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/admin/config/{category}",
    admin_config_category_update,
    methods=["POST"],
)

router.add_api_route(
    "/admin/maintenance/artifacts/cleanup",
    admin_artifacts_cleanup,
    methods=["POST"],
)
router.add_api_route(
    "/admin/maintenance/source-backup",
    admin_source_backup,
    methods=["POST"],
)
router.add_api_route(
    "/admin/maintenance/source-backup/latest",
    admin_source_backup_download,
    methods=["GET"],
)
router.add_api_route("/admin/judgehosts/runtime", admin_judgehost_runtime_update, methods=["POST"])
router.add_api_route("/admin/judgehosts/host-action", admin_judgehost_host_action, methods=["POST"])
router.add_api_route("/admin/judgehosts/snapshot", admin_judgehost_snapshot, methods=["GET"])
router.add_api_route("/admin/worker-queue/snapshot", admin_worker_queue_snapshot, methods=["GET"])
router.add_api_route("/admin/users/system-admin", admin_user_system_admin_update, methods=["POST"])
router.add_api_route("/admin/users/ban", admin_user_ban_update, methods=["POST"])
router.add_api_route("/admin/users/password", admin_user_password_update, methods=["POST"])
router.add_api_route("/admin/mail", admin_smtp_update, methods=["POST"])
router.add_api_route("/admin/mail/test", admin_smtp_test, methods=["POST"])
router.add_api_route("/admin/config/reset", admin_system_config_reset, methods=["POST"])
