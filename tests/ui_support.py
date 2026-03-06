from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from tests.common import (
    SmokeBase,
    _wait_for_export_workers,
    _wait_for_run_execute_workers,
    _wait_for_verification_workers,
)
from app.impl import build_preview as build_preview_impl
from app.impl import contests as contests_impl
from app.impl import problem_editor as problem_editor_impl
from app.impl import root_auth as root_auth_impl
from app.impl import run_export as run_export_impl
from app.impl import auth as auth_impl
from app.impl import workspace as workspace_impl
from app.impl.config import config
from app.services.statement_template import statement_sources_signature
from app.services.util import run_cmd

AUTH_COOKIE_NAME = config.constants.AUTH_COOKIE_NAME
FLASH_COOKIE_NAME = config.constants.FLASH_COOKIE_NAME
ADMIN_CONFIG_DEFAULTS = config.constants.ADMIN_CONFIG_DEFAULTS
_issue_password_form_csrf_token = auth_impl._issue_password_form_csrf_token
_session_user = auth_impl._session_user
_workspace_revision_info = workspace_impl._workspace_revision_info
auth_password_meta = root_auth_impl.auth_password_meta
auth_middleware = auth_impl.auth_middleware
artifact_file = run_export_impl.artifact_file
access_page = problem_editor_impl.access_page
build_service = config.build_service
build_page = build_preview_impl.build_page
tests_spec_add_gen = build_preview_impl.tests_spec_add_gen
tests_spec_edit = build_preview_impl.tests_spec_edit
tests_spec_add_manual = build_preview_impl.tests_spec_add_manual
tests_spec_add_manual_upload = build_preview_impl.tests_spec_add_manual_upload
tests_spec_delete = build_preview_impl.tests_spec_delete
tests_spec_gen_script_save = build_preview_impl.tests_spec_gen_script_save
tests_spec_payload_download = build_preview_impl.tests_spec_payload_download
tests_spec_payload_upload = build_preview_impl.tests_spec_payload_upload
tests_spec_reindex = build_preview_impl.tests_spec_reindex
checker_page = problem_editor_impl.checker_page
checker_save_source = problem_editor_impl.checker_save_source
checker_set_standard = problem_editor_impl.checker_set_standard
checker_view_standard = problem_editor_impl.checker_view_standard
db = config.db
export_create = run_export_impl.export_create
export_import = run_export_impl.export_import
export_import_slug_hint = run_export_impl.export_import_slug_hint
export_page = run_export_impl.export_page
export_service = config.export_service
files_create_template = problem_editor_impl.files_create_template
files_page = problem_editor_impl.files_page
files_save = problem_editor_impl.files_save
generator_create_template = problem_editor_impl.generator_create_template
generator_save_source = problem_editor_impl.generator_save_source
generators_page = problem_editor_impl.generators_page
general_page = build_preview_impl.preview_page
general_save = problem_editor_impl.general_save
git_commit = problem_editor_impl.git_commit
git_rebase_abort = problem_editor_impl.git_rebase_abort
git_restore_revision = problem_editor_impl.git_restore_revision
git_service = config.git_service
history_page = problem_editor_impl.history_page
login_submit = root_auth_impl.login_submit
login_page = root_auth_impl.login_page
sudo_page = root_auth_impl.sudo_page
sudo_submit = root_auth_impl.sudo_submit
setup_page = root_auth_impl.setup_page
setup_submit = root_auth_impl.setup_submit
problems_root_page = root_auth_impl.problems_root_page
problems_root_import = root_auth_impl.problems_root_import
problems_root_import_slug_hint = root_auth_impl.problems_root_import_slug_hint
preview_page = build_preview_impl.preview_page
preview_run = build_preview_impl.preview_run
preview_status = build_preview_impl.preview_status
preview_save = build_preview_impl.preview_save
register_submit = root_auth_impl.register_submit
register_page = root_auth_impl.register_page
run_page = run_export_impl.run_page
run_new_page = run_export_impl.run_new_page
run_details_page = run_export_impl.run_details_page
run_details_test_fragment = run_export_impl.run_details_test_fragment
run_service = config.run_service
run_execute = run_export_impl.run_execute
verification_start = build_preview_impl.verification_start
contests_root_create = root_auth_impl.contests_root_create
contests_root_import = root_auth_impl.contests_root_import
contests_root_import_confirm = root_auth_impl.contests_root_import_confirm
contests_root_import_review = root_auth_impl.contests_root_import_review
contests_root_page = root_auth_impl.contests_root_page
contest_access_grant = contests_impl.contest_access_grant
contest_access_page = contests_impl.contest_access_page
contest_access_revoke = contests_impl.contest_access_revoke
contest_overview_page = contests_impl.contest_overview_page
contest_packages_page = contests_impl.contest_packages_page
contest_packages_artifact_download = contests_impl.contest_packages_artifact_download
contest_packages_build_start = contests_impl.contest_packages_build_start
contest_packages_job_status = contests_impl.contest_packages_job_status
contest_packages_preview_start = contests_impl.contest_packages_preview_start
contest_problems_add = contests_impl.contest_problems_add
contest_problems_change_general = contests_impl.contest_problems_change_general
contest_problems_page = contests_impl.contest_problems_page
contest_problems_remove_selected = contests_impl.contest_problems_remove_selected
contest_problems_renumber = contests_impl.contest_problems_renumber
contest_problems_reorder = contests_impl.contest_problems_reorder
contest_properties_page = contests_impl.contest_properties_page
contest_properties_save = contests_impl.contest_properties_save
solutions_create_template = problem_editor_impl.solutions_create_template
solutions_editor_page = problem_editor_impl.solutions_editor_page
solutions_page = problem_editor_impl.solutions_page
solutions_save_source = problem_editor_impl.solutions_save_source
solutions_rename = problem_editor_impl.solutions_rename
solutions_delete = problem_editor_impl.solutions_delete
solutions_set_tag = problem_editor_impl.solutions_set_tag
settings_password_update = problem_editor_impl.settings_password_update
settings_page = problem_editor_impl.settings_page
settings_judgehost_snapshot = problem_editor_impl.settings_judgehost_snapshot
settings_config_category_page = problem_editor_impl.settings_config_category_page
settings_config_category_update = problem_editor_impl.settings_config_category_update
settings_system_config_reset = problem_editor_impl.settings_system_config_reset
settings_worker_queue_snapshot = problem_editor_impl.settings_worker_queue_snapshot
switch_workspace = problem_editor_impl.switch_workspace
workspace_delete = problem_editor_impl.workspace_delete
problem_delete = problem_editor_impl.problem_delete
interactor_create_template = problem_editor_impl.interactor_create_template
interactor_page = problem_editor_impl.interactor_page
interactor_save_source = problem_editor_impl.interactor_save_source
validator_create_template = problem_editor_impl.validator_create_template
validator_page = problem_editor_impl.validator_page
validator_save_source = problem_editor_impl.validator_save_source
workspace_page = workspace_impl._render_workspace_page
workspace_access_grant = problem_editor_impl.workspace_access_grant
workspace_access_revoke = problem_editor_impl.workspace_access_revoke
workspace_service = config.workspace_service


def _request(
    path: str,
    query: str = "",
    *,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    scheme: str = "http",
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "headers": headers or [],
            "client": ("127.0.0.1", 0),
            "server": ("testserver", 80),
            "scheme": scheme,
            "root_path": "",
        }
    )


def _request_with_cookie(
    path: str,
    cookie_header: str,
    query: str = "",
    *,
    method: str = "GET",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    scheme: str = "http",
) -> Request:
    headers = [(b"cookie", cookie_header.encode("utf-8"))]
    if extra_headers:
        headers.extend(extra_headers)
    return _request(path, query, method=method, headers=headers, scheme=scheme)


def _response_set_cookie_headers(response) -> list[str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    values: list[str] = []
    try:
        values = [str(item or "") for item in headers.getlist("set-cookie")]
    except Exception:
        values = []
    if not values:
        raw_headers = list(getattr(response, "raw_headers", []) or [])
        for key, value in raw_headers:
            if bytes(key).lower() == b"set-cookie":
                values.append(bytes(value).decode("latin-1", errors="ignore"))
    if values:
        return values
    single = str(headers.get("set-cookie", "") or "")
    return [single] if single else []


def _extract_cookie_value(set_cookie: str | list[str], cookie_name: str) -> str:
    prefix = f"{cookie_name}="
    headers = [str(item or "") for item in set_cookie] if isinstance(set_cookie, list) else [str(set_cookie or "")]
    for header in headers:
        first = str(header).split(";", 1)[0].strip()
        if first.startswith(prefix):
            return first[len(prefix) :]
    return ""


def _extract_hidden_input_value(html: str, name: str) -> str:
    # Attribute order is not stable in rendered HTML; parse per-input tag instead of
    # assuming `name` appears before `value`.
    for match in re.finditer(r"<input\b[^>]*>", html, flags=re.IGNORECASE):
        tag = str(match.group(0) or "")
        name_match = re.search(r'\bname\s*=\s*([\"\'])(.*?)\1', tag, flags=re.IGNORECASE)
        if not name_match:
            continue
        if str(name_match.group(2) or "") != str(name):
            continue
        value_match = re.search(r'\bvalue\s*=\s*([\"\'])(.*?)\1', tag, flags=re.IGNORECASE)
        if value_match:
            return str(value_match.group(2) or "")
        return ""
    return ""


def _flash_messages_from_set_cookie(set_cookie: str | list[str]) -> list[str]:
    token = _extract_cookie_value(set_cookie, FLASH_COOKIE_NAME)
    if not token:
        return []
    try:
        padded = token + ("=" * ((4 - (len(token) % 4)) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    messages: list[str] = []
    for item in payload:
        text = str(item or "").strip()
        if text:
            messages.append(text)
    return messages


def _flash_messages_from_response(response) -> list[str]:
    return _flash_messages_from_set_cookie(_response_set_cookie_headers(response))


def _cookie_value_from_response(response, cookie_name: str) -> str:
    return _extract_cookie_value(_response_set_cookie_headers(response), cookie_name)


def _response_set_cookie_blob(response) -> str:
    return "\n".join(_response_set_cookie_headers(response))


def _flash_cookie_header(*messages: str) -> str:
    safe = [str(msg or "").strip() for msg in messages if str(msg or "").strip()]
    payload = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{FLASH_COOKIE_NAME}={token}"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _password_verifier_hex(password: str, salt_hex: str, iters: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)).hex()


def _post_request(path: str, *, origin: str = "http://testserver") -> Request:
    return _request(path, method="POST", headers=[(b"origin", origin.encode("utf-8"))])


def _post_form_request(path: str, form_data: dict[str, object], *, origin: str = "http://testserver") -> Request:
    parts: list[tuple[str, str]] = []
    for key, raw_value in dict(form_data or {}).items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        if isinstance(raw_value, list):
            for item in raw_value:
                parts.append((key_text, str(item if item is not None else "")))
        else:
            parts.append((key_text, str(raw_value if raw_value is not None else "")))
    body = urlencode(parts, doseq=True).encode("utf-8")
    headers = [
        (b"origin", origin.encode("utf-8")),
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 0),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        },
        receive,
    )

def _register_with_password_proof(username: str, password: str, *, next_path: str = "/"):
    page = register_page(_request("/register"))
    html = page.body.decode("utf-8", errors="replace")
    csrf = _extract_hidden_input_value(html, "csrf_token")
    salt = _extract_hidden_input_value(html, "password_salt")
    iters = int(_extract_hidden_input_value(html, "password_iters") or "0")
    verifier = _password_verifier_hex(password, salt, iters)
    proof = _sha256_hex(csrf + verifier)
    password_hash = _sha256_hex(csrf + password)
    return register_submit(
        request=_post_request("/register"),
        username=username,
        password=password_hash,
        password_confirm=password_hash,
        password_verifier=verifier,
        password_proof=proof,
        csrf_token=csrf,
        password_salt=salt,
        password_iters=str(iters),
        next=next_path,
    )

def _login_with_password_proof(username: str, password: str, *, next_path: str = "/"):
    page = login_page(_request("/login"))
    html = page.body.decode("utf-8", errors="replace")
    csrf = _extract_hidden_input_value(html, "csrf_token")
    meta = auth_password_meta(username=username, csrf_token=csrf)
    salt = str(meta.get("salt") or "").strip().lower()
    iters = int(meta.get("iters") or 0)
    verifier = _password_verifier_hex(password, salt, iters)
    proof = _sha256_hex(csrf + verifier)
    password_hash = _sha256_hex(csrf + password)
    return login_submit(
        request=_post_request("/login"),
        username=username,
        password=password_hash,
        password_proof=proof,
        csrf_token=csrf,
        next=next_path,
    )

def _setup_with_password_proof(username: str, password: str, *, confirm_config: str = "1", next_path: str = "/"):
    page = setup_page(_request("/setup"))
    html = page.body.decode("utf-8", errors="replace")
    csrf = _extract_hidden_input_value(html, "csrf_token")
    salt = _extract_hidden_input_value(html, "password_salt")
    iters = int(_extract_hidden_input_value(html, "password_iters") or "0")
    verifier = _password_verifier_hex(password, salt, iters)
    proof = _sha256_hex(csrf + verifier)
    password_hash = _sha256_hex(csrf + password)
    return setup_submit(
        request=_post_request("/setup"),
        username=username,
        password=password_hash,
        password_confirm=password_hash,
        password_verifier=verifier,
        password_proof=proof,
        csrf_token=csrf,
        password_salt=salt,
        password_iters=str(iters),
        confirm_config=confirm_config,
        next=next_path,
    )

def _sudo_with_password_proof(cookie_header: str, password: str, *, next_path: str = "/"):
    query = f"next={quote_plus(next_path)}" if next_path else ""
    page = sudo_page(_request_with_cookie("/sudo", cookie_header, query=query))
    html = page.body.decode("utf-8", errors="replace")
    csrf = _extract_hidden_input_value(html, "csrf_token")
    salt = _extract_hidden_input_value(html, "password_salt")
    iters = int(_extract_hidden_input_value(html, "password_iters") or "0")
    verifier = _password_verifier_hex(password, salt, iters)
    proof = _sha256_hex(csrf + verifier)
    password_hash = _sha256_hex(csrf + password)
    return sudo_submit(
        request=_request_with_cookie(
            "/sudo",
            cookie_header,
            method="POST",
            extra_headers=[(b"origin", b"http://testserver")],
        ),
        password=password_hash,
        password_proof=proof,
        csrf_token=csrf,
        next=next_path,
    )

def _settings_password_update_with_proof(user: str, current_password: str, new_password: str):
    csrf = _issue_password_form_csrf_token("settings-password")
    auth_row = db.fetch_one("SELECT password_salt,password_iters FROM users WHERE username=?", [user])
    if auth_row is None:
        return settings_password_update(user=user)
    current_salt = str(auth_row["password_salt"] or "").strip().lower()
    current_iters = int(auth_row["password_iters"] or 0)
    new_salt = uuid.uuid4().hex
    new_iters = current_iters
    current_verifier = _password_verifier_hex(current_password, current_salt, current_iters)
    current_proof = _sha256_hex(csrf + current_verifier)
    new_verifier = _password_verifier_hex(new_password, new_salt, new_iters)
    new_proof = _sha256_hex(csrf + new_verifier)
    current_password_hash = _sha256_hex(csrf + current_password)
    new_password_hash = _sha256_hex(csrf + new_password)
    return settings_password_update(
        user=user,
        current_password=current_password_hash,
        new_password=new_password_hash,
        new_password_confirm=new_password_hash,
        current_password_proof=current_proof,
        new_password_verifier=new_verifier,
        new_password_proof=new_proof,
        csrf_token=csrf,
        new_password_salt=new_salt,
        new_password_iters=str(new_iters),
    )


def _wait_for_row(sql: str, params: list[object], timeout_sec: float = 8.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        row = db.fetch_one(sql, params)
        if row is not None:
            return row
        time.sleep(0.05)
    return db.fetch_one(sql, params)

class UIBaseSuite(SmokeBase):
    def setUp(self) -> None:
        _wait_for_run_execute_workers()
        super().setUp()
        self.problem = "alice/sample"
        self.user = "alice"

    def _prepare_verification_workspace(self, problem: str, user: str = "alice") -> Path:
        safe_problem = str(problem or "").strip()
        safe_user = str(user or "alice").strip() or "alice"
        if "/" not in safe_problem:
            safe_problem = f"{safe_user}/{safe_problem}"
        workspace_service.ensure_problem(safe_problem, f"{safe_problem} title")
        ws = Path(workspace_service.ensure_workspace(safe_problem, safe_user))
        workspace_service.grant_repo_access(safe_problem, safe_user, "owner")
        for rel in ["generators", "validators", "checkers", "solutions", "tests/manual", "third_party/testlib", "config"]:
            (ws / rel).mkdir(parents=True, exist_ok=True)
        (ws / "generators" / "generator.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;

int main() {
    cout << "7\\n";
    return 0;
}
""",
            encoding="utf-8",
        )
        (ws / "validators" / "validator.cpp").write_text(
            """#include "testlib.h"

int main(int argc, char* argv[]) {
    registerValidation(argc, argv);
    inf.readInt(1, 1000);
    inf.readEoln();
    inf.readEof();
}
""",
            encoding="utf-8",
        )
        (ws / "checkers" / "checker.cpp").write_text(
            """#include "testlib.h"

int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);
    long long jury = ans.readLong();
    long long team = ouf.readLong();
    if (jury == team) {
        quitf(_ok, "ok");
    }
    quitf(_wa, "expected %lld got %lld", jury, team);
}
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    long long x = 0;
    if (!(cin >> x)) {
        return 0;
    }
    cout << x << "\\n";
    return 0;
}
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "generator_sources": ["generators/generator.cpp"],
                    "validator_source": "validators/validator.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "accepted_solution_source": "solutions/accepted.cpp",
                    "generator_runs": 1,
                    "compile_jobs": 1,
                    "validate_jobs": 1,
                    "solve_jobs": 1,
                    "run_jobs": 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return ws

__all__ = [
    "ADMIN_CONFIG_DEFAULTS",
    "AUTH_COOKIE_NAME",
    "HTTPException",
    "Path",
    "PlainTextResponse",
    "Request",
    "UIBaseSuite",
    "_cookie_value_from_response",
    "_extract_hidden_input_value",
    "_flash_cookie_header",
    "_flash_messages_from_response",
    "_issue_password_form_csrf_token",
    "_password_verifier_hex",
    "_register_with_password_proof",
    "_login_with_password_proof",
    "_setup_with_password_proof",
    "_settings_password_update_with_proof",
    "_sudo_with_password_proof",
    "_post_request",
    "_post_form_request",
    "_request",
    "_request_with_cookie",
    "_response_set_cookie_blob",
    "_session_user",
    "_sha256_hex",
    "_wait_for_export_workers",
    "_wait_for_row",
    "_wait_for_run_execute_workers",
    "_wait_for_verification_workers",
    "_workspace_revision_info",
    "access_page",
    "artifact_file",
    "asyncio",
    "auth_middleware",
    "auth_password_meta",
    "build_page",
    "build_service",
    "checker_page",
    "checker_save_source",
    "checker_set_standard",
    "checker_view_standard",
    "config",
    "contests_root_create",
    "contests_root_import",
    "contests_root_import_confirm",
    "contests_root_import_review",
    "contests_root_page",
    "contest_access_grant",
    "contest_access_page",
    "contest_access_revoke",
    "contest_overview_page",
    "contest_packages_artifact_download",
    "contest_packages_build_start",
    "contest_packages_job_status",
    "contest_packages_page",
    "contest_packages_preview_start",
    "contest_problems_add",
    "contest_problems_change_general",
    "contest_problems_page",
    "contest_problems_remove_selected",
    "contest_problems_renumber",
    "contest_problems_reorder",
    "contest_properties_page",
    "contest_properties_save",
    "db",
    "export_create",
    "export_import",
    "export_import_slug_hint",
    "export_page",
    "general_page",
    "general_save",
    "generator_create_template",
    "generator_save_source",
    "generators_page",
    "git_commit",
    "git_rebase_abort",
    "git_restore_revision",
    "git_service",
    "history_page",
    "interactor_create_template",
    "interactor_page",
    "interactor_save_source",
    "io",
    "json",
    "login_page",
    "login_submit",
    "os",
    "parse_qs",
    "patch",
    "preview_page",
    "preview_run",
    "preview_status",
    "preview_save",
    "problems_root_page",
    "problems_root_import",
    "problems_root_import_slug_hint",
    "quote_plus",
    "re",
    "register_page",
    "register_submit",
    "run_cmd",
    "run_details_page",
    "run_details_test_fragment",
    "run_execute",
    "run_export_impl",
    "run_new_page",
    "run_page",
    "run_service",
    "settings_page",
    "settings_judgehost_snapshot",
    "settings_config_category_page",
    "settings_config_category_update",
    "settings_password_update",
    "settings_system_config_reset",
    "settings_worker_queue_snapshot",
    "setup_page",
    "setup_submit",
    "sudo_page",
    "sudo_submit",
    "solutions_create_template",
    "solutions_delete",
    "solutions_editor_page",
    "solutions_page",
    "solutions_rename",
    "solutions_save_source",
    "solutions_set_tag",
    "statement_sources_signature",
    "switch_workspace",
    "workspace_delete",
    "problem_delete",
    "tests_spec_add_gen",
    "tests_spec_edit",
    "tests_spec_add_manual",
    "tests_spec_add_manual_upload",
    "tests_spec_delete",
    "tests_spec_gen_script_save",
    "tests_spec_payload_download",
    "tests_spec_payload_upload",
    "tests_spec_reindex",
    "threading",
    "time",
    "urlparse",
    "uuid",
    "validator_create_template",
    "validator_page",
    "validator_save_source",
    "verification_start",
    "workspace_access_grant",
    "workspace_access_revoke",
    "workspace_impl",
    "workspace_page",
    "workspace_service",
]
