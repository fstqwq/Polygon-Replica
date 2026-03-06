from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl import root_auth as handlers

router = APIRouter()

router.add_api_route(
    "/login",
    handlers.login_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/auth/password-meta",
    handlers.auth_password_meta,
    methods=["GET"],
)

router.add_api_route(
    "/login",
    handlers.login_submit,
    methods=["POST"],
)

router.add_api_route(
    "/register",
    handlers.register_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/register",
    handlers.register_submit,
    methods=["POST"],
)

router.add_api_route(
    "/setup",
    handlers.setup_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/setup",
    handlers.setup_submit,
    methods=["POST"],
)

router.add_api_route(
    "/sudo",
    handlers.sudo_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/sudo",
    handlers.sudo_submit,
    methods=["POST"],
)

router.add_api_route(
    "/logout",
    handlers.logout,
    methods=["POST"],
)

router.add_api_route(
    "/",
    handlers.home,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/problems",
    handlers.problems_root_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/import/slug-hint",
    handlers.problems_root_import_slug_hint,
    methods=["GET"],
)
router.add_api_route(
    "/problems/import",
    handlers.problems_root_import,
    methods=["POST"],
)

router.add_api_route(
    "/contests",
    handlers.contests_root_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/create",
    handlers.contests_root_create,
    methods=["POST"],
)

router.add_api_route(
    "/contests/import",
    handlers.contests_root_import,
    methods=["POST"],
)

router.add_api_route(
    "/contests/import/review",
    handlers.contests_root_import_review,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/import/confirm",
    handlers.contests_root_import_confirm,
    methods=["POST"],
)
