from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.impl.root.auth_pages import (
    auth_login_pubkey,
    auth_password_meta,
    home,
    login_page,
    login_submit,
    logout,
    register_page,
    register_submit,
    register_verify,
    register_verify_page,
    setup_page,
    setup_submit,
    sudo_page,
    sudo_submit,
)
from app.impl.root.problems import (
    problems_root_import,
    problems_root_import_slug_hint,
    problems_root_page,
)
from app.impl.root.contests import (
    contests_root_create,
    contests_root_import,
    contests_root_import_confirm,
    contests_root_import_review,
    contests_root_page,
)

router = APIRouter()

router.add_api_route(
    "/login",
    login_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/auth/password-meta",
    auth_password_meta,
    methods=["GET"],
)

router.add_api_route(
    "/auth/login-pubkey",
    auth_login_pubkey,
    methods=["GET"],
)

router.add_api_route(
    "/login",
    login_submit,
    methods=["POST"],
)

router.add_api_route(
    "/register",
    register_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/register",
    register_submit,
    methods=["POST"],
)

router.add_api_route(
    "/register/verify",
    register_verify_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/register/verify",
    register_verify,
    methods=["POST"],
)

router.add_api_route(
    "/setup",
    setup_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/setup",
    setup_submit,
    methods=["POST"],
)

router.add_api_route(
    "/sudo",
    sudo_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/sudo",
    sudo_submit,
    methods=["POST"],
)

router.add_api_route(
    "/logout",
    logout,
    methods=["POST"],
)

router.add_api_route(
    "/",
    home,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/problems",
    problems_root_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/import/slug-hint",
    problems_root_import_slug_hint,
    methods=["GET"],
)
router.add_api_route(
    "/problems/import",
    problems_root_import,
    methods=["POST"],
)

router.add_api_route(
    "/contests",
    contests_root_page,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/create",
    contests_root_create,
    methods=["POST"],
)

router.add_api_route(
    "/contests/import",
    contests_root_import,
    methods=["POST"],
)

router.add_api_route(
    "/contests/import/review",
    contests_root_import_review,
    methods=["GET"],
    response_class=HTMLResponse,
)

router.add_api_route(
    "/contests/import/confirm",
    contests_root_import_confirm,
    methods=["POST"],
)
