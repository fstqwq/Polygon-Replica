from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.agent.api import (
    agent_auth_status,
    agent_commit,
    agent_commit_status,
    agent_export_download,
    agent_export_start,
    agent_export_status,
    agent_poll_access,
    agent_register,
    agent_request_access,
    agent_verification_detail,
    agent_verification_detail_text,
    agent_verification_start,
    agent_verification_status,
    agent_workspace_delete,
    agent_workspace_file,
    agent_workspace_files,
    agent_workspace_status,
    agent_workspace_upload,
)
from app.impl.agent.pages import (
    agent_approve_page,
    agent_approve_submit,
    agent_connect,
    agent_disconnect_session,
    agent_revoke_token,
    agent_sessions_page,
)

router = APIRouter()

router.add_api_route("/agent/sessions", agent_sessions_page, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/agent/connect", agent_connect, methods=["POST"])
router.add_api_route("/agent/approve/{request_id}", agent_approve_page, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/agent/approve/{request_id}", agent_approve_submit, methods=["POST"])
router.add_api_route("/agent/revoke/{token_id}", agent_revoke_token, methods=["POST"])
router.add_api_route("/agent/disconnect/{session_id}", agent_disconnect_session, methods=["POST"])

router.add_api_route("/agent/v1/register/{code}", agent_register, methods=["POST"])
router.add_api_route("/agent/v1/auth/status", agent_auth_status, methods=["GET"])
router.add_api_route("/agent/v1/auth/request-access", agent_request_access, methods=["POST"])
router.add_api_route("/agent/v1/auth/poll/{request_id}", agent_poll_access, methods=["GET"])
router.add_api_route("/agent/v1/verification/start", agent_verification_start, methods=["POST"])
router.add_api_route("/agent/v1/verification/{verification_id}/status", agent_verification_status, methods=["GET"])
router.add_api_route("/agent/v1/verification/{verification_id}/detail", agent_verification_detail, methods=["GET"])
router.add_api_route("/agent/v1/verification/{verification_id}/detail/text", agent_verification_detail_text, methods=["GET"])
router.add_api_route("/agent/v1/export/start", agent_export_start, methods=["POST"])
router.add_api_route("/agent/v1/export/{export_id}/status", agent_export_status, methods=["GET"])
router.add_api_route("/agent/v1/export/{export_id}/download", agent_export_download, methods=["GET"])
router.add_api_route("/agent/v1/workspace/files", agent_workspace_files, methods=["GET"])
router.add_api_route("/agent/v1/workspace/status", agent_workspace_status, methods=["GET"])
router.add_api_route("/agent/v1/workspace/file", agent_workspace_file, methods=["GET"])
router.add_api_route("/agent/v1/workspace/upload", agent_workspace_upload, methods=["POST"])
router.add_api_route("/agent/v1/workspace/files/{path:path}", agent_workspace_delete, methods=["DELETE"])
router.add_api_route("/agent/v1/commit", agent_commit, methods=["POST"])
router.add_api_route("/agent/v1/commit/{ref}/status", agent_commit_status, methods=["GET"])
