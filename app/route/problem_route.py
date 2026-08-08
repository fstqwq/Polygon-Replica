from __future__ import annotations
from app.impl.auth.session import require_session_user
from typing import Annotated

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse

from app.impl.workspace.context_ui import render_workspace_page
from app.impl.problem.access import workspace_access_grant, workspace_access_revoke
from app.impl.problem.checker import (
    checker_page,
    checker_rename_source,
    checker_save_source,
    checker_set_standard,
    checker_view_standard,
)
from app.impl.problem.file import (
    files_create_template,
    files_delete,
    files_download,
    files_new,
    files_page,
    files_rename,
    files_restore_default,
    files_save,
    files_upload,
)
from app.impl.problem.general import general_save
from app.impl.problem.generator import (
    generator_rename_source,
    generator_save_source,
    generators_page,
)
from app.impl.problem.git_op import git_discard_path, revision_commit
from app.impl.problem.history import history_page
from app.impl.problem.merge_op import (
    merge_apply,
    merge_compare,
    merge_file,
    merge_page,
    merge_start,
    merge_undo,
)
from app.impl.problem.interactor import (
    interactor_page,
    interactor_rename_source,
    interactor_save_source,
)
from app.impl.problem.setting import (
    settings_page,
    settings_password_update,
)
from app.impl.problem.solution import solutions_delete, solutions_editor_page, solutions_page, solutions_rename, solutions_save_source, solutions_set_tag
from app.impl.problem.validator import (
    validator_page,
    validator_rename_source,
    validator_save_source,
)
from app.impl.problem.workspace_op import problem_delete, switch_workspace, workspace_delete
from app.impl.preview.preview import preview_page
from app.route.problem_scoped_router import ProblemScopedRouter

router = ProblemScopedRouter()

def access_admin_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    return render_workspace_page(request, problem, user, show_access_admin=True)

router.add_api_route(
    "/problems/{problem:path}/statement",
    preview_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_statement",
)
router.add_api_route(
    "/problems/{problem:path}/statement/save",
    general_save,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/generators",
    generators_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_generators",
)
router.add_api_route(
    "/problems/{problem:path}/generators/save-source",
    generator_save_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/generators/rename-source",
    generator_rename_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/checker",
    checker_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_checker",
)
router.add_api_route(
    "/problems/{problem:path}/checker/view-standard",
    checker_view_standard,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/checker/set-standard",
    checker_set_standard,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/checker/save-source",
    checker_save_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/checker/rename-source",
    checker_rename_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/validator",
    validator_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_validator",
)
router.add_api_route(
    "/problems/{problem:path}/validator/save-source",
    validator_save_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/validator/rename-source",
    validator_rename_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/interactor",
    interactor_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_interactor",
)
router.add_api_route(
    "/problems/{problem:path}/interactor/save-source",
    interactor_save_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/interactor/rename-source",
    interactor_rename_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/solutions",
    solutions_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_solutions",
)
router.add_api_route(
    "/problems/{problem:path}/solutions/editor",
    solutions_editor_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/solutions/save-source",
    solutions_save_source,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/solutions/set-tag",
    solutions_set_tag,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/solutions/rename",
    solutions_rename,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/solutions/delete",
    solutions_delete,
    methods=["POST"],
)
router.add_api_route(
    "/settings",
    settings_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/settings/password",
    settings_password_update,
    methods=["POST"],
)
router.add_api_route(
    "/switch-workspace",
    switch_workspace,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/workspace/delete",
    workspace_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/problem/delete",
    problem_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files",
    files_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_files",
)
router.add_api_route(
    "/problems/{problem:path}/files/save",
    files_save,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files/new",
    files_new,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files/create-template",
    files_create_template,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files/restore-default",
    files_restore_default,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files/upload",
    files_upload,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files/rename",
    files_rename,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files/delete",
    files_delete,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/files/download",
    files_download,
    methods=["GET"],
)
router.add_api_route(
    "/problems/{problem:path}/workspace",
    render_workspace_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_workspace",
)
router.add_api_route(
    "/problems/{problem:path}/access",
    access_admin_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_access",
)
router.add_api_route(
    "/problems/{problem:path}/access/grant",
    workspace_access_grant,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/access/revoke",
    workspace_access_revoke,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/history",
    history_page,
    methods=["GET"],
    response_class=HTMLResponse,
    name="problem_history",
)
router.add_api_route(
    "/problems/{problem:path}/revision/commit",
    revision_commit,
    methods=["POST"],
)
router.add_api_route(
    "/problems/{problem:path}/git/discard-path",
    git_discard_path,
    methods=["POST"],
)
router.add_api_route("/problems/{problem:path}/merge/start", merge_start, methods=["POST"])
router.add_api_route(
    "/problems/{problem:path}/merge/{preview_id}",
    merge_page,
    methods=["GET"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/problems/{problem:path}/merge/{preview_id}/apply", merge_apply, methods=["POST"]
)
router.add_api_route(
    "/problems/{problem:path}/merge/{preview_id}/file/{entry_id}", merge_file, methods=["GET"]
)
router.add_api_route(
    "/problems/{problem:path}/merge/{preview_id}/compare/{entry_id}",
    merge_compare,
    methods=["GET"],
)
router.add_api_route("/problems/{problem:path}/merge/undo", merge_undo, methods=["POST"])
