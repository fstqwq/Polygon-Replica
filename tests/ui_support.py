from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote_plus, urlencode

from starlette.requests import Request

from .common import (
    SmokeBase,
    _wait_for_run_execute_workers,
)
import app.impl.auth.api as auth_api_module
import app.impl.contest.access as contest_access_module
import app.impl.contest.overview as contest_overview_module
import app.impl.contest.package as contest_package_module
import app.impl.contest.problem as contest_problem_module
import app.impl.contest.property as contest_property_module
import app.impl.preview.preview as preview_module
import app.impl.problem.access as problem_access_module
import app.impl.problem.checker as problem_checker_module
import app.impl.problem.file as problem_file_module
import app.impl.problem.general as problem_general_module
import app.impl.problem.generator as problem_generator_module
import app.impl.problem.git_op as problem_git_module
import app.impl.problem.history as problem_history_module
import app.impl.problem.interactor as problem_interactor_module
import app.impl.problem.setting as problem_setting_module
import app.impl.problem.solution as problem_solution_module
import app.impl.problem.validator as problem_validator_module
import app.impl.problem.workspace_op as problem_workspace_op_module
import app.impl.root.api as root_api_module
import app.impl.run_export.artifact as run_export_artifact_module
import app.impl.run_export.export as run_export_export_module
import app.impl.run_export.import_source as run_export_import_module
import app.impl.run_export.query as run_export_query_module
import app.impl.run_export.run as run_export_run_module
import app.impl.tests_spec.routes as tests_spec_module
import app.impl.tests_spec.verification as tests_spec_verification_module
import app.impl.workspace.context_job as workspace_job_module
import app.impl.workspace.context_ui as workspace_ui_module
import app.impl.workspace.context_verification as workspace_verification_module
import app.impl.workspace.run_view_detail as workspace_run_view_detail_module
import app.impl.workspace.run_view_list as workspace_run_view_list_module
from app.impl.runtime.config import config
from app.service.verification.store import load_verification_summary, verification_run_ids as verification_summary_run_ids
_API_MODULES = (
    auth_api_module,
    tests_spec_module,
    tests_spec_verification_module,
    preview_module,
    contest_access_module,
    contest_overview_module,
    contest_package_module,
    contest_problem_module,
    contest_property_module,
    problem_access_module,
    problem_checker_module,
    problem_file_module,
    problem_general_module,
    problem_generator_module,
    problem_git_module,
    problem_history_module,
    problem_interactor_module,
    problem_setting_module,
    problem_solution_module,
    problem_validator_module,
    problem_workspace_op_module,
    run_export_artifact_module,
    run_export_export_module,
    run_export_import_module,
    run_export_run_module,
    workspace_job_module,
    workspace_ui_module,
    root_api_module,
)

def _verification_record_run_ids(problem_id: int, workspace_id: int, verification_id: str) -> list[str]:
    summary = load_verification_summary(config.db, verification_id)
    if not isinstance(summary, dict):
        return []
    return verification_summary_run_ids(summary)


run_export_impl = SimpleNamespace(
    _finalize_cancelled_verifications=run_export_run_module._finalize_cancelled_verifications,
    _run_detail_use_compact_layout=run_export_query_module._run_detail_use_compact_layout,
    run_artifact_file=run_export_artifact_module.run_artifact_file,
    run_cancel=run_export_run_module.run_cancel,
)
workspace_impl = SimpleNamespace(
    _run_cell_kind=workspace_run_view_list_module._run_cell_kind,
    _verification_solution_match=workspace_verification_module._verification_solution_match,
    _verification_sources_signature=workspace_verification_module._verification_sources_signature,
    _verification_sources_signature_details=workspace_verification_module._verification_sources_signature_details,
    build_run_detail_context=workspace_run_view_detail_module.build_run_detail_context,
    record_async_run_failure=workspace_job_module.record_async_run_failure,
    run_list_rows=workspace_run_view_list_module.run_list_rows,
    verification_detail_context=workspace_run_view_detail_module.build_run_detail_context,
    verification_run_ids=_verification_record_run_ids,
)

def _api_attr(name: str):
    for module in _API_MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"api symbol not found: {name}")

AUTH_COOKIE_NAME = config.constants.AUTH_COOKIE_NAME
FLASH_COOKIE_NAME = config.constants.FLASH_COOKIE_NAME
ADMIN_CONFIG_DEFAULTS = config.constants.ADMIN_CONFIG_DEFAULTS
issue_password_form_csrf_token = _api_attr("issue_password_form_csrf_token")
session_user = _api_attr("session_user")
workspace_revision_info = _api_attr("workspace_revision_info")
auth_password_meta = _api_attr("auth_password_meta")
auth_middleware = _api_attr("auth_middleware")
artifact_file = _api_attr("artifact_file")
tests_page = tests_spec_module.render_tests_page
tests_spec_add_gen = tests_spec_module.add_generator_test
tests_spec_edit = tests_spec_module.edit_spec_test
tests_spec_add_manual = tests_spec_module.add_manual_test
tests_spec_add_manual_upload = tests_spec_module.upload_manual_test
tests_spec_delete = tests_spec_module.delete_spec_test
tests_spec_gen_script_save = tests_spec_module.save_gen_script
tests_spec_payload_download = tests_spec_module.download_test_payload
tests_spec_payload_upload = tests_spec_module.upload_test_payload
tests_spec_reindex = tests_spec_module.reindex_spec_test
checker_page = _api_attr("checker_page")
checker_save_source = _api_attr("checker_save_source")
checker_set_standard = _api_attr("checker_set_standard")
checker_view_standard = _api_attr("checker_view_standard")
db = config.db
export_create = _api_attr("export_create")
export_import = _api_attr("export_import")
export_import_slug_hint = _api_attr("export_import_slug_hint")
export_page = _api_attr("export_page")
export_service = config.export_service
files_create_template = _api_attr("files_create_template")
files_page = _api_attr("files_page")
files_save = _api_attr("files_save")
generator_create_template = _api_attr("generator_create_template")
generator_save_source = _api_attr("generator_save_source")
generators_page = _api_attr("generators_page")
general_page = _api_attr("preview_page")
general_save = _api_attr("general_save")
git_commit = _api_attr("git_commit")
git_rebase_abort = _api_attr("git_rebase_abort")
git_restore_revision = _api_attr("git_restore_revision")
git_service = config.git_service
history_page = _api_attr("history_page")
login_submit = _api_attr("login_submit")
login_page = _api_attr("login_page")
sudo_page = _api_attr("sudo_page")
sudo_submit = _api_attr("sudo_submit")
setup_page = _api_attr("setup_page")
setup_submit = _api_attr("setup_submit")
problems_root_page = _api_attr("problems_root_page")
problems_root_import = _api_attr("problems_root_import")
problems_root_import_slug_hint = _api_attr("problems_root_import_slug_hint")
preview_page = _api_attr("preview_page")
preview_run = _api_attr("preview_run")
preview_status = _api_attr("preview_status")
preview_save = _api_attr("preview_save")
register_submit = _api_attr("register_submit")
register_page = _api_attr("register_page")
run_page = _api_attr("run_page")
run_new_page = _api_attr("run_new_page")
run_details_page = _api_attr("run_details_page")
run_details_test_fragment = _api_attr("run_details_test_fragment")
run_execute = _api_attr("run_execute")
verification_start = _api_attr("verification_start")
contests_root_create = _api_attr("contests_root_create")
contests_root_import = _api_attr("contests_root_import")
contests_root_import_confirm = _api_attr("contests_root_import_confirm")
contests_root_import_review = _api_attr("contests_root_import_review")
contests_root_page = _api_attr("contests_root_page")
contest_access_grant = _api_attr("contest_access_grant")
contest_access_page = _api_attr("contest_access_page")
contest_access_revoke = _api_attr("contest_access_revoke")
contest_overview_page = _api_attr("contest_overview_page")
contest_packages_page = _api_attr("contest_packages_page")
contest_packages_artifact_download = _api_attr("contest_packages_artifact_download")
contest_packages_build_start = _api_attr("contest_packages_build_start")
contest_packages_job_status = _api_attr("contest_packages_job_status")
contest_packages_preview_start = _api_attr("contest_packages_preview_start")
contest_problems_add = _api_attr("contest_problems_add")
contest_problems_change_general = _api_attr("contest_problems_change_general")
contest_problems_page = _api_attr("contest_problems_page")
contest_problems_remove_selected = _api_attr("contest_problems_remove_selected")
contest_problems_renumber = _api_attr("contest_problems_renumber")
contest_problems_reorder = _api_attr("contest_problems_reorder")
contest_properties_page = _api_attr("contest_properties_page")
contest_properties_save = _api_attr("contest_properties_save")
solutions_create_template = _api_attr("solutions_create_template")
solutions_editor_page = _api_attr("solutions_editor_page")
solutions_page = _api_attr("solutions_page")
solutions_save_source = _api_attr("solutions_save_source")
solutions_rename = _api_attr("solutions_rename")
solutions_delete = _api_attr("solutions_delete")
solutions_set_tag = _api_attr("solutions_set_tag")
settings_password_update = _api_attr("settings_password_update")
settings_page = _api_attr("settings_page")
settings_judgehost_snapshot = _api_attr("settings_judgehost_snapshot")
settings_config_category_page = _api_attr("settings_config_category_page")
settings_config_category_update = _api_attr("settings_config_category_update")
settings_system_config_reset = _api_attr("settings_system_config_reset")
settings_worker_queue_snapshot = _api_attr("settings_worker_queue_snapshot")
switch_workspace = _api_attr("switch_workspace")
workspace_delete = _api_attr("workspace_delete")
problem_delete = _api_attr("problem_delete")
interactor_create_template = _api_attr("interactor_create_template")
interactor_page = _api_attr("interactor_page")
interactor_save_source = _api_attr("interactor_save_source")
validator_create_template = _api_attr("validator_create_template")
validator_page = _api_attr("validator_page")
validator_save_source = _api_attr("validator_save_source")
workspace_page = _api_attr("render_workspace_page")
access_page = lambda request, problem, user: workspace_page(request, problem, user, show_access_admin=True)
workspace_access_grant = _api_attr("workspace_access_grant")
workspace_access_revoke = _api_attr("workspace_access_revoke")
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
    csrf = issue_password_form_csrf_token("settings-password")
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


