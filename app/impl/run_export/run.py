from collections.abc import Mapping
import json
import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import File, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import Response

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.workspace_scope import (
    contest_workspace_context_from_request,
    problem_template_navigation,
)
from app.impl.runtime.dependency import runtime
from app.impl.run_export.artifact import verification_artifact_file
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_job import start_verification_job
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_model import ProblemPageContext
from app.impl.workspace.context_job_helper import allocate_verification_id
from app.impl.workspace.context_operation import (
    RunSolutionOption,
    dedupe_preserve_order,
    run_solution_options_context,
    run_test_options_context,
)
from app.impl.workspace.context_run_detail import (
    normalize_run_test_name_token,
    parse_run_test_names,
    parse_verification_detail_id,
)
from app.impl.workspace.context_verification import (
    normalize_program_id_token,
    normalize_run_id_token,
)
from app.impl.workspace.run_view_detail import build_run_detail_context
from app.impl.workspace.run_view_list import run_list_rows
from app.main_util import normalize_optional_component_source_path, normalize_optional_component_source_path_safe, read_fileobj_bytes_limited
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.problem.sample_json import SampleJsonEvent, normalize_sample_json
from app.service.problem.test_spec import read_statement_sample_text
from app.service.statement.sample_transcript import statement_sample_events_from_transcript
from app.impl.run_export.query import (
    _rerun_solution_paths_from_verification,
    _run_detail_use_compact_layout,
)
from app.service.verification.types import ACTIVE

logger = logging.getLogger(__name__)


def _context_section(
    context: Mapping[str, object],
    key: str,
) -> dict[str, object]:
    value = context.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"problem page context {key} is missing")
    return value


def _context_int(section: dict[str, object], key: str) -> int:
    value = section.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"problem page context {key} must be an integer")
    return value


def _upload_filename_token(raw: str) -> str:
    token = Path(str(raw or "").strip()).name
    if token:
        return token
    return "upload.cpp"


def _uploaded_target_path(program_id: str, upload_filename: str) -> str:
    return f"uploads/{program_id}/{_upload_filename_token(upload_filename)}"


def _truthy_form_token(value: str) -> bool:
    return value.lower() in {'1', 'true', 'yes', 'on'}


def run_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=True,
        include_workspace_changes=True,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    execute_mode = ctx['shell']['metadata']['mode']
    workspace_id = int(ctx['workspace']['id'])
    requested_verification_id = parse_verification_detail_id(request)
    if requested_verification_id:
        detail_ctx = build_run_detail_context(
            ctx,
            execute_mode,
            requested_verification_id=requested_verification_id,
        )
        cancel_verification_id = requested_verification_id or detail_ctx["verification_id"]
        detail_ctx["cancel_verification_id"] = cancel_verification_id
        detail_ctx["cancel_available"] = bool(
            detail_ctx["can_cancel"]
            and cancel_verification_id
            and detail_ctx["detail_running"]
        )
        detail_table_compact = _run_detail_use_compact_layout(detail_ctx)
        detail_ctx["detail_table_compact"] = detail_table_compact
        detail_page_ctx = dict(ctx)
        detail_page_ctx['page_wide_content'] = detail_table_compact
        detail_page_ctx['topbar_max_1400'] = detail_table_compact
        return template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})
    runs = run_list_rows(int(ctx['problem']['id']), workspace_id, workspace, limit=10, actor_user_id=int(ctx['user']['id']))
    return template_response(request, 'run.html', {'ctx': ctx, 'runs': runs})

def run_new_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=True,
        include_workspace_changes=True,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    try:
        solution_options, default_submission_path, solution_options_truncated = (
            run_solution_options_context(workspace)
        )
    except ValueError:
        solution_options = []
        default_submission_path = ''
        solution_options_truncated = False
    test_options, test_options_truncated = run_test_options_context(workspace)
    selected_solution_paths: list[str] = []
    for raw in request.query_params.getlist('solution_paths'):
        normalized = normalize_optional_component_source_path_safe(raw, 'solutions', 'solution path')
        if normalized:
            selected_solution_paths.append(normalized)
    selected_solution_paths = dedupe_preserve_order(selected_solution_paths)
    if not selected_solution_paths and default_submission_path:
        selected_solution_paths = [default_submission_path]
    selected_test_names_raw = request.query_params.getlist('test_names')
    selected_test_names = parse_run_test_names(selected_test_names_raw)
    allowed_test_names = {row["name"] for row in test_options}
    selected_test_names = [name for name in selected_test_names if name in allowed_test_names]
    if not selected_test_names_raw and test_options and (not test_options_truncated):
        selected_test_names = [row["name"] for row in test_options]
    return template_response(
        request,
        'run_execute.html',
        {
            'ctx': ctx,
            'solution_options': solution_options,
            'solution_options_truncated': solution_options_truncated,
            'selected_solution_paths': selected_solution_paths,
            'test_options': test_options,
            'test_options_truncated': test_options_truncated,
            'selected_test_names': selected_test_names,
        },
    )

def run_details_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=True,
        include_workspace_changes=True,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    execute_mode = ctx['shell']['metadata']['mode']
    requested_verification_id = parse_verification_detail_id(request)
    detail_ctx = build_run_detail_context(
        ctx,
        execute_mode,
        requested_verification_id=requested_verification_id,
    )
    cancel_verification_id = requested_verification_id or detail_ctx["verification_id"]
    detail_ctx["cancel_verification_id"] = cancel_verification_id
    detail_ctx["cancel_available"] = bool(
        detail_ctx["can_cancel"]
        and cancel_verification_id
        and detail_ctx["detail_running"]
    )
    detail_table_compact = _run_detail_use_compact_layout(detail_ctx)
    detail_ctx["detail_table_compact"] = detail_table_compact
    detail_page_ctx = dict(ctx)
    detail_page_ctx['page_wide_content'] = detail_table_compact
    detail_page_ctx['topbar_max_1400'] = detail_table_compact
    return template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})

def _selected_run_test_detail(
    request: Request,
    problem: str,
    user: str,
) -> tuple[ProblemPageContext, str, list[dict[str, object]], dict[str, object]]:
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=True,
        include_workspace_changes=True,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    execute_mode = ctx['shell']['metadata']['mode']

    requested_verification_id = parse_verification_detail_id(request)

    test_name = normalize_run_test_name_token(request.query_params.get('test'))
    if not test_name:
        raise HTTPException(status_code=400, detail='test is required')
    program_id_param = request.query_params.get('program_id')
    program_id = normalize_program_id_token(program_id_param)
    if program_id_param is not None and not program_id:
        raise HTTPException(status_code=400, detail='program_id is invalid')

    detail_ctx = build_run_detail_context(
        ctx,
        execute_mode,
        requested_verification_id=requested_verification_id,
        include_row_details=True,
        detail_test_name=test_name,
        detail_program_id=program_id,
    )
    detail_columns = detail_ctx['detail_columns']
    if program_id and (
        len(detail_columns) != 1
        or str(detail_columns[0].get('id') or '') != program_id
    ):
        raise HTTPException(status_code=404, detail='run detail not found')
    detail_rows = detail_ctx['detail_rows']
    if not detail_rows:
        raise HTTPException(status_code=404, detail='test detail not found')
    return ctx, str(detail_ctx['verification_id']), detail_columns, detail_rows[0]


def run_details_test_fragment(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx, verification_id, detail_columns, row = _selected_run_test_detail(
        request,
        problem,
        user,
    )
    fragment_context: dict[str, object] = {
        'ctx': ctx,
        'row': row,
        'detail_columns': detail_columns,
        'verification_id': verification_id,
    }
    fragment_context.update(problem_template_navigation(request, problem))
    response = runtime().templates.TemplateResponse(
        request,
        '_run_test_detail_fragment.html',
        fragment_context,
    )
    return response


def _detail_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=409, detail=f"{label} is unavailable")
    return value


def _sample_artifact_text(
    preview_value: object,
    *,
    label: str,
    max_bytes: int,
) -> str:
    preview = _detail_mapping(preview_value, label=label)
    verification_id = preview.get("download_verification_id")
    rel_path = preview.get("download_rel_path")
    if not isinstance(verification_id, str) or not isinstance(rel_path, str):
        raise HTTPException(status_code=409, detail=f"{label} was not captured")
    artifact = verification_artifact_file(verification_id, rel_path)
    if artifact is None:
        raise HTTPException(status_code=409, detail=f"{label} is unavailable")
    payload, _filename = artifact
    try:
        return read_statement_sample_text(payload.path, max_bytes=max_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=f"{label}: {exc}") from exc


def _sample_transcript_events(
    transcript_value: object,
    *,
    label: str,
    max_bytes: int,
) -> list[SampleJsonEvent]:
    transcript_projection = _detail_mapping(transcript_value, label=label)
    verification_id = transcript_projection.get("download_verification_id")
    rel_path = transcript_projection.get("download_rel_path")
    if not isinstance(verification_id, str) or not isinstance(rel_path, str):
        raise HTTPException(status_code=409, detail=f"{label} was not captured")
    artifact = verification_artifact_file(verification_id, rel_path)
    if artifact is None:
        raise HTTPException(status_code=409, detail=f"{label} is unavailable")
    payload, _filename = artifact
    try:
        return statement_sample_events_from_transcript(
            payload.path,
            raw_size_bytes=payload.size,
            max_bytes=max_bytes,
            label=label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def run_details_sample_json(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
) -> Response:
    """Download one solution's complete test evidence as authored sample JSON."""

    _ctx, _verification_id, detail_columns, row = _selected_run_test_detail(
        request,
        problem,
        user,
    )
    if len(detail_columns) != 1:
        raise HTTPException(status_code=400, detail="program_id is required")
    cells = row.get("cells")
    if not isinstance(cells, list) or len(cells) != 1:
        raise HTTPException(status_code=404, detail="run detail not found")
    cell = _detail_mapping(cells[0], label="solution detail")
    detail = _detail_mapping(cell.get("detail"), label="solution detail")
    if bool(detail.get("mode_malformed")):
        raise HTTPException(status_code=409, detail="verification mode is unavailable")
    pass_values = detail.get("pass_rows")
    if not isinstance(pass_values, list) or not pass_values:
        raise HTTPException(status_code=409, detail="pass evidence is unavailable")

    max_bytes = runtime().config_values.integer("STATEMENT_SAMPLE_MAX_BYTES")
    is_interactive = bool(detail.get("is_interactive"))
    passes: list[dict[str, object]] = []
    for index, pass_value in enumerate(pass_values, start=1):
        pass_row = _detail_mapping(pass_value, label=f"pass {index}")
        pass_number = pass_row.get("pass_number")
        if isinstance(pass_number, bool) or not isinstance(pass_number, int):
            raise HTTPException(status_code=409, detail=f"pass {index} number is invalid")
        if is_interactive:
            passes.append(
                {
                    "number": pass_number,
                    "events": _sample_transcript_events(
                        pass_row.get("interactive_transcript"),
                        label=f"pass {pass_number} transcript",
                        max_bytes=max_bytes,
                    ),
                }
            )
        else:
            passes.append(
                {
                    "number": pass_number,
                    "input": _sample_artifact_text(
                        pass_row.get("input_preview"),
                        label=f"pass {pass_number} input",
                        max_bytes=max_bytes,
                    ),
                    "output": _sample_artifact_text(
                        pass_row.get("output_preview"),
                        label=f"pass {pass_number} output",
                        max_bytes=max_bytes,
                    ),
                }
            )
    try:
        value = normalize_sample_json(
            {
                "presentation": "interaction" if is_interactive else "pair",
                "passes": passes,
            },
            max_bytes=max_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if value is None:
        raise HTTPException(status_code=409, detail="sample evidence is unavailable")
    return Response(
        content=json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=sample.json",
            "X-Content-Type-Options": "nosniff",
        },
    )

def run_cancel(problem: str, user: Annotated[str, Depends(require_session_user)], verification_id: Annotated[str, Form()] = ""):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return redirect_response(
            f"/problems/{problem}/run",
            status_code=303,
            message="verification id is required",
        )
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    details_url = f"/problems/{problem}/run/details?verification_id={quote_plus(safe_verification_id)}"
    record = runtime().verification_service.verification_record(safe_verification_id)
    if record is None:
        return redirect_response(details_url, status_code=303, message="verification not found")
    access = runtime().access_query.verification_context(
        actor_user_id=int(ctx["user"]["id"]),
        actor_workspace_id=workspace_id,
        expected_problem_id=problem_id,
        verification=record,
    )
    if not access["can_view"]:
        return redirect_response(details_url, status_code=303, message="verification not found")
    if not access["can_cancel"]:
        return redirect_response(
            details_url,
            status_code=303,
            message=f"cancel unavailable: {access['cancel_block_reason']}",
        )
    reason = "verification cancelled by user"
    try:
        cancellation_result = (
            runtime().verification_execution_service.cancel_verification(
                safe_verification_id,
                reason=reason,
            )
        )
    except Exception as exc:
        logger.exception(
            "failed to cancel verification execution %s",
            safe_verification_id,
        )
        raise HTTPException(
            status_code=500,
            detail="failed to cancel verification execution",
        ) from exc
    transition = cancellation_result.transition
    if transition.outcome == "missing":
        return redirect_response(details_url, status_code=303, message="verification not found")
    if transition.outcome == "closed":
        return redirect_response(
            details_url,
            status_code=303,
            message="verification already finished",
        )
    cancellation = cancellation_result.drain
    awaiting_receipts = int(cancellation["awaiting_receipts"])
    if awaiting_receipts > 0:
        msg = f"verification cancelled ({awaiting_receipts} running cases awaiting receipt)"
    else:
        msg = "verification cancelled"
    return redirect_response(details_url, status_code=303, message=msg)


def _build_dag_targets(
    *,
    solution_options: list[RunSolutionOption],
    accepted_solution_path: str,
    selected_solution_paths: list[str],
    uploaded: bool,
    upload_filename: str,
    upload_content: bytes,
) -> tuple[list[str], list[dict[str, object]]]:
    if not accepted_solution_path:
        raise ValueError("main correct solution is required")
    solution_expected_map = {
        str(row["path"]): str(row["expected_behavior"])
        for row in solution_options
    }
    solution_program_ids: list[str] = []
    dag_targets: list[dict[str, object]] = []
    target_paths = [
        accepted_solution_path,
        *(
            path
            for path in selected_solution_paths
            if path != accepted_solution_path
        ),
    ]
    solution_index = 0
    for target_path in target_paths:
        if target_path == accepted_solution_path:
            program_id = "accepted"
        else:
            program_id = f"solution-{solution_index}"
            solution_index += 1
        solution_program_ids.append(program_id)
        expected_behavior = solution_expected_map.get(target_path, "unknown")
        if target_path == accepted_solution_path:
            expected_behavior = "accepted"
        dag_targets.append(
            {
                "path": target_path,
                "expected_behavior": normalize_expected_behavior(expected_behavior),
                "program_id": program_id,
            }
        )
    if uploaded:
        uploaded_program_id = f"solution-{solution_index}"
        solution_program_ids.append(uploaded_program_id)
        dag_targets.append(
            {
                "path": _uploaded_target_path(uploaded_program_id, upload_filename),
                "expected_behavior": "unknown",
                "program_id": uploaded_program_id,
                "upload_filename": _upload_filename_token(upload_filename),
                "upload_content": upload_content,
            }
        )
    return (solution_program_ids, dag_targets)


def _start_run_verification(
    *,
    problem: str,
    user: str,
    ctx: ProblemPageContext,
    workspace: Path,
    selected_solution_paths: list[str],
    selected_test_names: list[str],
    uploaded: bool = False,
    upload_filename: str = "",
    upload_content: bytes = b"",
    bypass_case_result_cache_flag: bool = False,
):
    if (not selected_solution_paths) and (not uploaded):
        msg = 'select at least one solution or upload source file'
        return redirect_response(f'/problems/{problem}/run/new', status_code=303, message=msg)
    solution_options, accepted_solution_path, _ = run_solution_options_context(workspace)
    verification_id = allocate_verification_id()
    (
        solution_program_ids,
        dag_targets,
    ) = _build_dag_targets(
        solution_options=solution_options,
        accepted_solution_path=accepted_solution_path,
        selected_solution_paths=selected_solution_paths,
        uploaded=uploaded,
        upload_filename=upload_filename,
        upload_content=upload_content,
    )
    workspace_context = _context_section(ctx, "workspace")
    user_context = _context_section(ctx, "user")
    problem_context = _context_section(ctx, "problem")
    workspace_head = str(workspace_context.get("head_commit") or "")
    workspace_dirty = bool(workspace_context.get("dirty"))
    try:
        started = start_verification_job(
            runtime(),
            problem,
            user,
            actor_user_id=_context_int(user_context, "id"),
            problem_id=_context_int(problem_context, "id"),
            workspace_id=_context_int(workspace_context, "id"),
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
            targets=dag_targets,
            verification_id=verification_id,
            workspace_path=workspace,
            selected_test_names=selected_test_names,
            bypass_case_result_cache=bypass_case_result_cache_flag,
        )
    except Exception as exc:
        return redirect_response(f"/problems/{problem}/run", status_code=303, message=str(exc))
    if not started:
        return redirect_response(f"/problems/{problem}/run", status_code=303, message="verification already running")
    message_parts: list[str] = []
    if selected_test_names:
        message_parts.append(f'tests selected ({len(selected_test_names)})')
    message_parts.append(
        f'verification running ({len(solution_program_ids)} solutions)'
    )
    message_text = '; '.join(message_parts)
    return redirect_response(
        f'/problems/{problem}/run/details?verification_id={quote_plus(verification_id)}',
        status_code=303,
        message=message_text,
    )


def run_rejudge(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    verification_id: Annotated[str, Form()] = "",
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return redirect_response(f"/problems/{problem}/run", status_code=303, message="verification id is required")
    details_url = f"/problems/{problem}/run/details?verification_id={quote_plus(safe_verification_id)}"
    record = runtime().verification_service.verification_record(safe_verification_id)
    if record is None:
        return redirect_response(details_url, status_code=303, message="verification not found")
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    access = runtime().access_query.verification_context(
        actor_user_id=int(ctx["user"]["id"]),
        actor_workspace_id=workspace_id,
        expected_problem_id=problem_id,
        verification=record,
    )
    if not access["can_view"]:
        return redirect_response(details_url, status_code=303, message="verification not found")
    if not access["can_rejudge"]:
        return redirect_response(
            details_url,
            status_code=303,
            message=f"rejudge unavailable: {access['rejudge_block_reason']}",
        )
    if str(record.get("status") or "") in ACTIVE:
        return redirect_response(details_url, status_code=303, message="rejudge unavailable: verification still running")
    workspace = Path(ctx['workspace']['path'])
    selected_solution_paths = _rerun_solution_paths_from_verification(
        problem_id=problem_id,
        workspace_id=workspace_id,
        actor_user_id=int(ctx["user"]["id"]),
        workspace=workspace,
        verification_id=safe_verification_id,
    )
    if not selected_solution_paths:
        return redirect_response(details_url, status_code=303, message="rejudge unavailable: no reusable solution sources")
    return _start_run_verification(
        problem=problem,
        user=user,
        ctx=ctx,
        workspace=workspace,
        selected_solution_paths=selected_solution_paths,
        selected_test_names=[],
        bypass_case_result_cache_flag=True,
    )


def run_execute(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    artifact_verification_id: Annotated[str, Form()] = "",
    solution_paths: Annotated[list[str], Form()] = [],
    test_names: Annotated[list[str], Form()] = [],
    submission_upload: Annotated[UploadFile | None, File()] = None,
    bypass_case_result_cache: Annotated[str, Form()] = "",
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    upload_content = None
    upload_filename = ''
    uploaded = False
    bypass_case_result_cache_flag = _truthy_form_token(bypass_case_result_cache)
    try:
        if submission_upload is not None:
            normalized_name = (submission_upload.filename or '').strip()
            if normalized_name:
                upload_filename = normalized_name
                upload_content = read_fileobj_bytes_limited(
                    submission_upload.file,
                    label='submission upload',
                    max_bytes=runtime().config_values.integer("UPLOAD_MAX_BYTES"),
                )
                uploaded = True
        selected_solution_paths = dedupe_preserve_order(
            [
                normalize_optional_component_source_path(path, 'solutions', 'solution path')
                for path in solution_paths
                if path
            ]
        )
        selected_test_names = parse_run_test_names(test_names)
        execution_targets: list[tuple[str | None, bool]] = []
        execution_targets.extend(((path, False) for path in selected_solution_paths))
        if uploaded:
            execution_targets.append((None, True))
        if not execution_targets:
            msg = 'select at least one solution or upload source file'
            return redirect_response(f'/problems/{problem}/run/new', status_code=303, message=msg)
        deduped_targets: list[tuple[str | None, bool]] = []
        seen_targets: set[tuple[str, bool]] = set()
        for target_submission_path, target_is_upload in execution_targets:
            key = (target_submission_path or '', target_is_upload)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            deduped_targets.append((target_submission_path, target_is_upload))
        selected_solution_paths = [
            target_submission_path
            for target_submission_path, target_is_upload in deduped_targets
            if (not target_is_upload) and target_submission_path is not None
        ]
        return _start_run_verification(
            problem=problem,
            user=user,
            ctx=ctx,
            workspace=workspace,
            selected_solution_paths=selected_solution_paths,
            selected_test_names=selected_test_names,
            uploaded=uploaded,
            upload_filename=upload_filename,
            upload_content=bytes(upload_content or b""),
            bypass_case_result_cache_flag=bypass_case_result_cache_flag,
        )
    finally:
        if submission_upload is not None:
            submission_upload.file.close()
