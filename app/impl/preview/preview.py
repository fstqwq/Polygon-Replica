import app.main_constant as _K
from app.impl.auth.session import require_session_user

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, TypedDict
from urllib.parse import quote_plus, urlencode

from fastapi import File, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import JSONResponse, PlainTextResponse

from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.runtime.dependency import runtime
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_model import ProblemPageContext
from app.impl.workspace.context_operation import (
    read_text_safe_limited,
    read_workspace_source_with_default,
)
from app.main_util import (
    enforce_textarea_max_bytes,
    problem_slug_leaf,
    sanitize_log_text_for_ui,
    write_upload_file_limited,
)
from app.service.platform.workspace_path import (
    normalize_workspace_rel_path,
    safe_workspace_path,
)
from app.service.statement.constant import (
    DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
    STATEMENT_ASSETS_DIR,
    STATEMENT_DEFAULT_FILES,
    STATEMENT_DIR,
    STATEMENT_EXAMPLES_REL,
    STATEMENT_PROBLEM_REL,
    STATEMENT_SECTIONS_DIR,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    is_canonical_statement_section_entry,
)
from app.service.statement.context import (
    normalize_statement_language,
    pick_statement_language,
    statement_languages,
)
from app.service.statement.render import (
    ensure_statement_language_sources,
    render_statement_problem_assets_for_language,
    statement_title_for_language,
)
from app.service.statement.signature import statement_sources_signature
from app.service.problem.runtime_config import problem_config_limits

_CONTESTANT_ATTACHMENTS_ROOT = "attachments"


class PreviewLogRef(TypedDict):
    file: str
    line: int
    context: str


class PreviewCompileDetails(TypedDict):
    status: str
    preview_id: str
    preview_status: str
    failed_stage: str
    workspace_head: str
    workspace_dirty: bool
    source: str
    source_commit: str
    source_ref: str
    error: str


class StatementExamplesTemplateEditor(TypedDict):
    enabled: bool
    path: str
    content: str
    truncated: bool
    error: str


def statement_compile_asset_rows(workspace: Path) -> list[dict[str, str]]:
    try:
        assets_dir = safe_workspace_path(workspace, STATEMENT_ASSETS_DIR.as_posix())
    except HTTPException:
        return []
    if not assets_dir.exists() or (not assets_dir.is_dir()) or assets_dir.is_symlink():
        return []
    workspace_root = workspace.resolve()
    rows: list[dict[str, str]] = []
    try:
        for item in sorted(assets_dir.rglob("*")):
            if not item.is_file() or item.is_symlink():
                continue
            try:
                rel = item.resolve().relative_to(workspace_root).as_posix()
            except (ValueError, OSError):
                continue
            display_path = item.relative_to(assets_dir).as_posix()
            rows.append({"path": rel, "path_q": quote_plus(rel), "display_path": display_path})
    except OSError:
        return rows
    return rows


def contestant_attachment_rows(workspace: Path) -> list[dict[str, str]]:
    try:
        attachment_dir = safe_workspace_path(workspace, _CONTESTANT_ATTACHMENTS_ROOT)
    except HTTPException:
        return []
    if not attachment_dir.exists() or (not attachment_dir.is_dir()) or attachment_dir.is_symlink():
        return []
    workspace_root = workspace.resolve()
    rows: list[dict[str, str]] = []
    try:
        for item in sorted(attachment_dir.rglob("*")):
            if not item.is_file() or item.is_symlink():
                continue
            try:
                rel = item.resolve().relative_to(workspace_root).as_posix()
            except (ValueError, OSError):
                continue
            display_path = item.relative_to(attachment_dir).as_posix()
            rows.append({"path": rel, "path_q": quote_plus(rel), "display_path": display_path})
    except OSError:
        return rows
    return rows


def normalize_contestant_attachment_target(path: str, *, upload_filename: str = "") -> str:
    safe_path = normalize_workspace_rel_path(path)
    if safe_path:
        if safe_path == _CONTESTANT_ATTACHMENTS_ROOT:
            raise ValueError("attachment target must be a file path")
        if safe_path.startswith(_CONTESTANT_ATTACHMENTS_ROOT + "/"):
            return safe_path
        return f"{_CONTESTANT_ATTACHMENTS_ROOT}/{safe_path}"
    safe_name = Path(str(upload_filename or "").strip().replace("\\", "/")).name
    if not safe_name:
        raise ValueError("attachment path is required")
    return f"{_CONTESTANT_ATTACHMENTS_ROOT}/{safe_name}"


def normalize_statement_compile_asset_target(path: str, *, upload_filename: str = "") -> str:
    safe_path = normalize_workspace_rel_path(path)
    if safe_path:
        if safe_path == STATEMENT_ASSETS_DIR.as_posix():
            raise ValueError("statement asset target must be a file path")
        if safe_path.startswith(STATEMENT_ASSETS_DIR.as_posix() + "/"):
            target_rel = safe_path
        else:
            target_rel = f"{STATEMENT_ASSETS_DIR.as_posix()}/{safe_path}"
    else:
        safe_name = Path(upload_filename.replace("\\", "/")).name
        if not safe_name:
            raise ValueError("statement asset path is required")
        target_rel = f"{STATEMENT_ASSETS_DIR.as_posix()}/{safe_name}"
    rel_in_assets = Path(target_rel).relative_to(STATEMENT_ASSETS_DIR)
    if is_canonical_statement_section_entry(rel_in_assets):
        raise ValueError("canonical statement section sources must be edited in the statement editor")
    return target_rel


def statement_mode_from_ctx(ctx: ProblemPageContext) -> str:
    return ctx["shell"]["metadata"]["mode"]


def statement_editor_section_paths(language: str) -> dict[str, Path]:
    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise ValueError("statement language is required")
    section_root = Path("statement-sections") / safe_language
    return {
        "name": section_root / "name.tex",
        "legend": section_root / "legend.tex",
        "input": section_root / "input.tex",
        "output": section_root / "output.tex",
        "interaction": section_root / "interaction.tex",
        "notes": section_root / "notes.tex",
    }


def normalize_statement_target_page(page: str) -> str:
    return page if page in {"statement", "preview"} else "preview"


def resolve_statement_page_language(workspace: Path, requested_language: object) -> str:
    available_languages = statement_languages(workspace)
    safe_requested = normalize_statement_language(requested_language)
    if safe_requested and safe_requested in available_languages:
        return safe_requested
    if available_languages:
        return pick_statement_language(workspace)
    return ""


def selected_statement_language(workspace: Path, requested_language: object) -> str:
    safe_requested = normalize_statement_language(requested_language)
    available_languages = statement_languages(workspace)
    if not available_languages:
        raise ValueError("statement language is missing")
    if not safe_requested:
        return available_languages[0]
    if safe_requested in available_languages:
        return safe_requested
    raise ValueError(f"unknown statement language: {safe_requested}")


def statement_redirect_url(
    problem: str,
    user: str,
    page: str,
    *,
    language: str = "",
    preview_id: str = "",
) -> str:
    base = f"/problems/{problem}/{normalize_statement_target_page(page)}"
    query: dict[str, str] = {}
    safe_language = normalize_statement_language(language)
    safe_preview_id = str(preview_id or "").strip()
    if safe_language:
        query["language"] = safe_language
    if safe_preview_id:
        query["preview_id"] = safe_preview_id
    if not query:
        return base
    return f"{base}?{urlencode(query)}"


def statement_editor_sections(
    workspace: Path,
    mode: str,
    language: str,
) -> list[dict[str, object]]:
    section_paths = statement_editor_section_paths(language)
    interaction_enabled = mode != "pass-fail"
    specs: tuple[tuple[str, str, str, str], ...] = (
        ("name", "name_tex", "Title", ""),
        ("legend", "legend_tex", "Legend", ""),
        ("input", "input_tex", "Input", ""),
        ("output", "output_tex", "Output", ""),
        ("interaction", "interaction_tex", "Interaction Protocol", ""),
        ("notes", "notes_tex", "Notes", ""),
    )
    rows: list[dict[str, object]] = []
    for key, field_name, label, fallback in specs:
        rel = section_paths[key]
        content_text, content_truncated = read_workspace_source_with_default(workspace, rel, fallback)
        enabled = key != "interaction" or interaction_enabled
        rows.append(
            {
                "key": key,
                "label": label,
                "field_name": field_name,
                "path": rel.as_posix(),
                "content": content_text,
                "truncated": bool(content_truncated),
                "enabled": bool(enabled),
            }
        )
    return rows


def _statement_template_target(workspace: Path, rel: Path) -> Path:
    statement_root = workspace / STATEMENT_DIR
    if statement_root.is_symlink():
        raise ValueError(f"{STATEMENT_DIR.as_posix()} must be a regular directory")
    if statement_root.exists() and not statement_root.is_dir():
        raise ValueError(f"{STATEMENT_DIR.as_posix()} must be a regular directory")
    return workspace / rel


def _statement_examples_template_editor(
    workspace: Path,
) -> StatementExamplesTemplateEditor:
    result: StatementExamplesTemplateEditor = {
        "enabled": False,
        "path": STATEMENT_EXAMPLES_REL.as_posix(),
        "content": "",
        "truncated": False,
        "error": "",
    }
    try:
        target = _statement_template_target(workspace, STATEMENT_EXAMPLES_REL)
        if target.is_symlink():
            raise ValueError(
                f"{STATEMENT_EXAMPLES_REL.as_posix()} must be a regular file"
            )
        if not target.exists():
            return result
        if not target.is_file():
            raise ValueError(
                f"{STATEMENT_EXAMPLES_REL.as_posix()} must be a regular file"
            )
        result["enabled"] = True
        editor_limit = runtime().config_values.integer("TEXTAREA_MAX_BYTES")
        if target.stat().st_size <= editor_limit:
            content = target.read_text(encoding="utf-8")
            truncated = False
        else:
            content, truncated = read_text_safe_limited(
                target,
                editor_limit,
            )
        result["content"] = content
        result["truncated"] = bool(truncated)
    except UnicodeDecodeError:
        result["error"] = (
            f"{STATEMENT_EXAMPLES_REL.as_posix()} must be valid UTF-8"
        )
    except (ValueError, OSError) as exc:
        result["error"] = str(exc)
    return result


def extract_latex_failure_summary(log_text: str, summary_obj: dict[str, object] | None = None) -> str:
    lines = log_text.splitlines()
    if lines:
        file_hint = ""
        star_file_re = re.compile(r"^\*\*(?P<file>[^\s]+\.tex)\s*$")
        open_file_re = re.compile(r"\((?:\./)?(?P<file>[^()\s]+\.tex)\b")
        line_re = re.compile(r"^l\.(?P<line>\d+)\s*(?P<context>.*)$")
        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            m_star = star_file_re.match(stripped)
            if m_star:
                file_hint = m_star.group("file") or ""
                break
            m_open = open_file_re.search(stripped)
            if m_open:
                file_hint = m_open.group("file") or ""
                break
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped.startswith("!"):
                continue
            error_msg = stripped[1:].strip()
            if not error_msg:
                continue
            line_no = ""
            for j in range(idx + 1, min(len(lines), idx + 8)):
                probe = lines[j].strip()
                m_line = line_re.match(probe)
                if not m_line:
                    continue
                line_no = m_line.group("line") or ""
                break
            if file_hint and line_no:
                return f"{file_hint}:{line_no} {error_msg}"
            if line_no:
                return f"line {line_no}: {error_msg}"
            return error_msg
        noise_prefixes = (
            "this is pdftex",
            "entering extended mode",
            "restricted /write18 enabled",
            "%&-line parsing enabled",
            "**",
        )
        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if any((lowered.startswith(prefix) for prefix in noise_prefixes)):
                continue
            return stripped
    if summary_obj is not None:
        summary_error = summary_obj.get("error")
        return summary_error if isinstance(summary_error, str) else ""
    return ""


def preview_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace_id = ctx['workspace']['id']
    problem_id = int(ctx['problem']['id'])
    workspace = Path(ctx['workspace']['path'])
    available_languages = statement_languages(workspace)
    current_language = resolve_statement_page_language(workspace, request.query_params.get("language", ""))
    problem_title = statement_title_for_language(
        workspace,
        current_language or pick_statement_language(workspace),
        fallback_title=problem_slug_leaf(problem),
    )
    try:
        current_statement_signature = statement_sources_signature(
            workspace,
            problem_title=problem_title,
            tests_spec_max_bytes=runtime().config_values.integer(
                "TEXTAREA_MAX_BYTES"
            ),
            statement_sample_max_bytes=runtime().config_values.integer(
                "STATEMENT_SAMPLE_MAX_BYTES"
            ),
        )
    except RuntimeError:
        current_statement_signature = ""
    workspace_head = str(ctx["workspace"].get("head_commit") or "")
    requested_preview_id = request.query_params.get("preview_id", "")
    has_statement_language = bool(current_language)
    preview_id = requested_preview_id
    message = ''
    previews = runtime().preview_service.list_workspace_previews(problem_id, workspace_id)
    if has_statement_language and current_statement_signature and (not preview_id):
        dirty = bool(ctx['workspace'].get('dirty'))
        if workspace_head and (not dirty):
            cached_id = runtime().preview_service.find_cached_preview_id(
                problem,
                problem_id,
                workspace_id,
                language=current_language,
                source_commit=workspace_head,
                statement_signature=current_statement_signature,
            )
            if cached_id:
                preview_id = cached_id
        elif dirty:
            cached_id = runtime().preview_service.find_cached_preview_id(
                problem,
                problem_id,
                workspace_id,
                language=current_language,
                source_commit=None,
                statement_signature=current_statement_signature,
            )
            if cached_id:
                preview_id = cached_id
    safe_mode = statement_mode_from_ctx(ctx)
    if has_statement_language:
        statement_sections = statement_editor_sections(
            workspace,
            safe_mode,
            current_language,
        )
    else:
        statement_sections = []
    ctx["page_title"] = "Statements"
    log = ''
    log_truncated = False
    pdf_exists = False
    preview_artifacts_missing = False
    preview_display_status = 'none'
    preview_is_stale = False
    log_refs: list[PreviewLogRef] = []
    log_refs_total = 0
    log_refs_truncated = False
    selected_preview_summary: dict[str, object] | None = None
    selected_preview_row_status = 'none'
    preview_compile_failed = False
    preview_failed_stage = ''
    preview_failure_title = 'Compile failed.'
    preview_failure_detail = ''
    latex_log_available = False

    if has_statement_language and current_statement_signature and preview_id:
        lp = None
        preview_state = runtime().preview_service.get_workspace_preview_state(
            problem_id,
            workspace_id,
            preview_id,
            statement_signature=current_statement_signature,
            workspace_head=workspace_head,
            language=current_language,
        )
        if preview_state is None:
            preview_id = ''
        else:
            preview_display_status = preview_state['display_status']
            preview_is_stale = preview_display_status == 'stale'
            selected_preview_row_status = preview_state['row_status']
            selected_preview_summary = dict(preview_state['summary'])
            if (not requested_preview_id) and preview_display_status in {'stale', 'missing'}:
                preview_id = ''
                preview_state = None
    else:
        preview_id = ''
    if preview_id:
        if preview_state is not None:
            pdf_exists = bool(preview_state['pdf_available'])
            preview_artifacts_missing = preview_display_status == 'missing'
            if bool(preview_state['log_available']):
                lp = runtime().storage_layout.resolve_preview_root(preview_id) / 'logs' / 'latex.log'
            preview_compile_failed = selected_preview_row_status in {'failed', 'error'}
        else:
            preview_id = ''
        if preview_id and lp is not None:
            latex_log_available = True
            raw_log, log_truncated = read_text_safe_limited(
                lp,
                runtime().config_values.integer("UI_LOG_TEXT_CHAR_LIMIT"),
            )
            redact_prefixes: list[tuple[str, str]] = [
                (str(workspace.resolve()), '.'),
                (str(runtime().storage_layout.workspace_root.resolve()), '__workspace_root__'),
                (str(runtime().storage_layout.artifacts_root.resolve()), '__artifacts__'),
                (str(runtime().storage_layout.cache_root.resolve()), '__cache__'),
            ]
            log = sanitize_log_text_for_ui(raw_log, path_prefixes=redact_prefixes)
            if not log.strip():
                log = '(empty)'
            tex_ref = re.compile('(?P<file>[\\w./-]+\\.tex):(?P<line>\\d+)')
            for line in log.splitlines():
                m = tex_ref.search(line)
                if m:
                    log_refs_total += 1
                    if len(log_refs) >= runtime().config_values.integer(
                        "PREVIEW_LOG_REF_LIST_LIMIT"
                    ):
                        log_refs_truncated = True
                        continue
                    log_refs.append({'file': m.group('file'), 'line': int(m.group('line')), 'context': line})
        if preview_compile_failed:
            preview_summary = selected_preview_summary or {}
            failed_stage_value = preview_summary.get("failed_stage")
            preview_failed_stage = (
                failed_stage_value if isinstance(failed_stage_value, str) else ""
            )
            if preview_failed_stage == 'statement_examples':
                preview_failure_title = 'Statement examples failed.'
                failure_value = preview_summary.get("error")
                preview_failure_detail = sanitize_log_text_for_ui(
                    failure_value if isinstance(failure_value, str) else ""
                )
                latex_log_available = False
                log = ''
                log_truncated = False
                log_refs = []
                log_refs_total = 0
                log_refs_truncated = False
            else:
                if log_refs:
                    preview_failure_detail = log_refs[0]["context"]
                if not preview_failure_detail:
                    preview_failure_detail = extract_latex_failure_summary(log, selected_preview_summary)
                if len(preview_failure_detail) > 240:
                    preview_failure_detail = preview_failure_detail[:237].rstrip() + '...'
    return_page = 'preview' if str(getattr(request.url, "path")).endswith('/preview') else 'statement'
    statement_assets_dir = STATEMENT_ASSETS_DIR.as_posix()
    statement_compile_assets = statement_compile_asset_rows(workspace)
    contestant_attachments = contestant_attachment_rows(workspace)
    statement_examples_template = _statement_examples_template_editor(workspace)
    return template_response(
        request,
        'preview.html',
        {
            'ctx': ctx,
            'message': message,
            'preview_id': preview_id,
            'previews': previews,
            'statement_sections': statement_sections,
            'statement_assets_dir': statement_assets_dir,
            'statement_template_path': STATEMENT_TEMPLATE_REL.as_posix(),
            'statement_problem_path': STATEMENT_PROBLEM_REL.as_posix(),
            'statement_examples_template': statement_examples_template,
            'statement_style_path': STATEMENT_STYLE_REL.as_posix(),
            'statement_compile_assets': statement_compile_assets,
            'contestant_attachments': contestant_attachments,
            'editor_char_limit': runtime().config_values.integer("TEXTAREA_MAX_BYTES"),
            'log': log,
            'log_truncated': log_truncated,
            'log_char_limit': runtime().config_values.integer("UI_LOG_TEXT_CHAR_LIMIT"),
            'pdf_exists': pdf_exists,
            'preview_artifacts_missing': preview_artifacts_missing,
            'preview_display_status': preview_display_status,
            'preview_is_stale': preview_is_stale,
            'log_refs': log_refs,
            'log_refs_total': log_refs_total,
            'log_refs_truncated': log_refs_truncated,
            'log_refs_limit': runtime().config_values.integer("PREVIEW_LOG_REF_LIST_LIMIT"),
            'preview_compile_failed': preview_compile_failed,
            'preview_failure_title': preview_failure_title,
            'preview_failure_detail': preview_failure_detail,
            'preview_failed_stage': preview_failed_stage,
            'latex_log_available': latex_log_available,
            'problem_mode_values': list(_K.GENERAL_MODE_VALUES),
            'time_limit_min_ms': runtime().config_values.integer("GENERAL_TIME_LIMIT_MIN_MS"),
            'time_limit_max_ms': runtime().config_values.integer("GENERAL_TIME_LIMIT_MAX_MS"),
            'memory_limit_min_mb': runtime().config_values.integer("GENERAL_MEMORY_LIMIT_MIN_MB"),
            'memory_limit_max_mb': runtime().config_values.integer("GENERAL_MEMORY_LIMIT_MAX_MB"),
            'return_page': return_page,
            'statement_mode': safe_mode,
            'available_languages': available_languages,
            'current_language': current_language,
            'statement_language_missing': not has_statement_language,
        },
    )

def preview_run(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language)),
            status_code=303,
            message=str(exc),
        )
    workspace_head = ctx["workspace"].get("head_commit")
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    workspace_key = f'{problem_id}:{workspace_id}'
    details: PreviewCompileDetails = {
        'status': 'failed',
        'preview_id': '',
        'preview_status': 'missing',
        'failed_stage': '',
        'workspace_head': workspace_head,
        'workspace_dirty': workspace_dirty,
        'source': 'sync',
        'source_commit': '',
        'source_ref': '',
        'error': '',
    }
    msg = 'preview compile failed'
    base = statement_redirect_url(problem, user, target_page, language=current_language)
    with runtime().preview_lock:
        if workspace_key in runtime().preview_inflight:
            details['status'] = 'running'
            details['preview_status'] = 'running'
            details['error'] = 'preview compile already running'
            return redirect_response(base, status_code=303, message='preview compile already running')
        runtime().preview_inflight.add(workspace_key)
    try:
        preview_id = runtime().preview_service.compile_preview(problem, user, language=current_language)
        details['preview_id'] = preview_id
        row = runtime().preview_service.get_workspace_preview(
            problem_id, workspace_id, details['preview_id']
        )
        if row is None:
            raise RuntimeError('preview metadata missing after compile')
        preview_status = row['status']
        details['preview_status'] = preview_status
        details['source_commit'] = row["source_commit"]
        details['source_ref'] = row["source_ref"]
        summary_obj = dict(row['summary'])
        if preview_status == 'ok':
            details['status'] = 'ok'
            msg = 'preview compiled'
        else:
            details['status'] = 'failed'
            error_value = summary_obj.get("error")
            details["error"] = (
                error_value if isinstance(error_value, str) else "preview failed"
            )
            failed_stage_value = summary_obj.get("failed_stage")
            failed_stage = (
                failed_stage_value if isinstance(failed_stage_value, str) else ""
            )
            details['failed_stage'] = failed_stage
            msg = (
                'statement examples failed'
                if failed_stage == 'statement_examples'
                else 'preview compile failed'
            )
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        msg = str(exc)
    finally:
        with runtime().preview_lock:
            runtime().preview_inflight.discard(workspace_key)
    redirect_url = base
    preview_id = details["preview_id"]
    if preview_id:
        redirect_url = statement_redirect_url(problem, user, target_page, language=current_language, preview_id=preview_id)
    return redirect_response(redirect_url, status_code=303, message=msg)

def preview_status(problem: str, user: Annotated[str, Depends(require_session_user)], language: str = ""):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError:
        current_language = resolve_statement_page_language(workspace, language)
    problem_title = statement_title_for_language(
        workspace,
        current_language or pick_statement_language(workspace),
        fallback_title=problem_slug_leaf(problem),
    )
    try:
        current_statement_signature = statement_sources_signature(
            workspace,
            problem_title=problem_title,
            tests_spec_max_bytes=runtime().config_values.integer(
                "TEXTAREA_MAX_BYTES"
            ),
            statement_sample_max_bytes=runtime().config_values.integer(
                "STATEMENT_SAMPLE_MAX_BYTES"
            ),
        )
    except RuntimeError:
        current_statement_signature = ""
    workspace_head = str(ctx['workspace'].get('head_commit') or "")
    workspace_key = f'{problem_id}:{workspace_id}'
    with runtime().preview_lock:
        running = workspace_key in runtime().preview_inflight
    row = runtime().preview_service.latest_workspace_preview_state(
        problem_id,
        workspace_id,
        statement_signature=current_statement_signature,
        workspace_head=workspace_head,
        language=current_language,
    )
    latest_preview_id = ''
    latest_status = 'none'
    latest_created_at = ''
    latest_finished_at = ''
    if current_language and row is not None:
        latest_preview_id = row['id']
        latest_status = row['display_status']
        latest_created_at = row['created_at']
        latest_finished_at = row["finished_at"] or ""
    return JSONResponse(
        {
            'running': bool(running),
            'language': current_language,
            'latest_preview_id': latest_preview_id,
            'latest_status': latest_status,
            'latest_created_at': latest_created_at,
            'latest_finished_at': latest_finished_at,
        }
    )


def statement_tex_source(problem: str, user: Annotated[str, Depends(require_session_user)], language: str = ""):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
        with tempfile.TemporaryDirectory(prefix='polygon-replica-statement-tex-') as tmp:
            target_dir = Path(tmp) / "statement"
            tex_path = render_statement_problem_assets_for_language(
                workspace,
                current_language,
                target_dir,
                problem_title=statement_title_for_language(
                    workspace,
                    current_language,
                    fallback_title=problem_slug_leaf(problem),
                ),
                tests_spec_max_bytes=runtime().config_values.integer(
                    "TEXTAREA_MAX_BYTES"
                ),
                statement_sample_max_bytes=runtime().config_values.integer(
                    "STATEMENT_SAMPLE_MAX_BYTES"
                ),
                problem_limits=problem_config_limits(runtime().config_values),
            )
            tex_text = tex_path.read_text(encoding='utf-8')
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    headers = {
        "Content-Disposition": f'inline; filename="statement-{current_language}.tex"',
    }
    return PlainTextResponse(tex_text, media_type="text/plain; charset=utf-8", headers=headers)


def preview_save(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    name_tex: Annotated[str, Form()] = '',
    legend_tex: Annotated[str, Form()] = '',
    input_tex: Annotated[str, Form()] = '',
    output_tex: Annotated[str, Form()] = '',
    interaction_tex: Annotated[str, Form()] = '',
    notes_tex: Annotated[str, Form()] = '',
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language), preview_id=preview_id),
            status_code=303,
            message=str(exc),
        )
    statement_mode = statement_mode_from_ctx(ctx)
    msg = 'statement saved'
    try:
        textarea_limit = runtime().config_values.integer("TEXTAREA_MAX_BYTES")
        safe_name_tex = enforce_textarea_max_bytes(name_tex, label="statement title", max_bytes=textarea_limit)
        safe_legend_tex = enforce_textarea_max_bytes(legend_tex, label="statement legend", max_bytes=textarea_limit)
        safe_input_tex = enforce_textarea_max_bytes(input_tex, label="statement input", max_bytes=textarea_limit)
        safe_output_tex = enforce_textarea_max_bytes(output_tex, label="statement output", max_bytes=textarea_limit)
        safe_notes_tex = enforce_textarea_max_bytes(notes_tex, label="statement notes", max_bytes=textarea_limit)
        safe_interaction_tex = enforce_textarea_max_bytes(interaction_tex, label="statement interaction", max_bytes=textarea_limit)
        with runtime().workspace_service.workspace_lock(workspace):
            section_paths = statement_editor_section_paths(current_language)
            write_plan = {
                'name': safe_name_tex,
                'legend': safe_legend_tex,
                'input': safe_input_tex,
                'output': safe_output_tex,
                'notes': safe_notes_tex,
            }
            if statement_mode != 'pass-fail':
                write_plan['interaction'] = safe_interaction_tex
            for key, content in write_plan.items():
                rel = section_paths[key]
                section_path = safe_workspace_path(workspace, rel.as_posix())
                section_path.parent.mkdir(parents=True, exist_ok=True)
                section_path.write_text(content, encoding='utf-8')
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=current_language, preview_id=preview_id),
        status_code=303,
        message=msg,
    )


def statement_templates_reset(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    current_language = resolve_statement_page_language(workspace, language)
    message = 'default statement templates restored'
    try:
        with runtime().workspace_service.workspace_lock(workspace):
            targets: list[tuple[str, Path, str]] = []
            for rel, content in STATEMENT_DEFAULT_FILES.items():
                target = _statement_template_target(workspace, Path(rel))
                if (
                    target.exists()
                    and (not target.is_file())
                    and (not target.is_symlink())
                ):
                    raise ValueError(f'{rel} must be a regular file')
                targets.append((rel, target, content))
            examples_target = _statement_template_target(
                workspace,
                STATEMENT_EXAMPLES_REL,
            )
            if (
                examples_target.exists()
                and (not examples_target.is_file())
                and (not examples_target.is_symlink())
            ):
                raise ValueError(
                    f'{STATEMENT_EXAMPLES_REL.as_posix()} must be a regular file'
                )
            for _, target, content in targets:
                if target.is_symlink():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding='utf-8')
            if examples_target.is_symlink() or examples_target.is_file():
                examples_target.unlink()
    except (ValueError, OSError, HTTPException) as exc:
        message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=current_language, preview_id=preview_id),
        status_code=303,
        message=message,
    )


def statement_examples_template_toggle(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    enabled: Annotated[bool, Form()] = False,
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=False,
    )
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    current_language = resolve_statement_page_language(workspace, language)
    message = (
        'custom examples template enabled'
        if enabled
        else 'custom examples template disabled'
    )
    try:
        with runtime().workspace_service.workspace_lock(workspace):
            target = _statement_template_target(workspace, STATEMENT_EXAMPLES_REL)
            if enabled:
                if target.is_symlink() or (
                    target.exists() and not target.is_file()
                ):
                    raise ValueError(
                        f'{STATEMENT_EXAMPLES_REL.as_posix()} must be a regular file'
                    )
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
                        encoding='utf-8',
                    )
            else:
                if (
                    target.exists()
                    and (not target.is_file())
                    and (not target.is_symlink())
                ):
                    raise ValueError(
                        f'{STATEMENT_EXAMPLES_REL.as_posix()} must be a regular file'
                    )
                if target.is_symlink() or target.is_file():
                    target.unlink()
    except (ValueError, OSError) as exc:
        message = str(exc)
    return redirect_response(
        statement_redirect_url(
            problem,
            user,
            target_page,
            language=current_language,
            preview_id=preview_id,
        ),
        status_code=303,
        message=message,
    )


def statement_examples_template_save(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    examples_tex: Annotated[str, Form()] = '',
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=False,
    )
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    current_language = resolve_statement_page_language(workspace, language)
    message = 'examples template saved'
    try:
        safe_examples_tex = enforce_textarea_max_bytes(
            examples_tex,
            label='statement examples template',
            max_bytes=runtime().config_values.integer("TEXTAREA_MAX_BYTES"),
        )
        with runtime().workspace_service.workspace_lock(workspace):
            target = _statement_template_target(workspace, STATEMENT_EXAMPLES_REL)
            if target.is_symlink() or not target.exists() or not target.is_file():
                raise ValueError(
                    f'{STATEMENT_EXAMPLES_REL.as_posix()} is not enabled'
                )
            target.write_text(safe_examples_tex, encoding='utf-8')
    except (ValueError, OSError) as exc:
        message = str(exc)
    return redirect_response(
        statement_redirect_url(
            problem,
            user,
            target_page,
            language=current_language,
            preview_id=preview_id,
        ),
        status_code=303,
        message=message,
    )


def statement_compile_asset_delete(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language), preview_id=preview_id),
            status_code=303,
            message=str(exc),
        )
    message = 'statement asset deleted'
    try:
        safe_rel = normalize_statement_compile_asset_target(path)
        with runtime().workspace_service.workspace_lock(workspace):
            attachment_abs = safe_workspace_path(workspace, safe_rel)
            if not attachment_abs.exists() or (not attachment_abs.is_file()):
                raise ValueError('statement asset not found')
            attachment_abs.unlink()
    except (ValueError, OSError) as exc:
        message = str(exc)
    except HTTPException as exc:
        message = str(exc.detail)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=current_language, preview_id=preview_id),
        status_code=303,
        message=message,
    )


async def statement_compile_asset_upload(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    upload: Annotated[UploadFile, File()],
    path: Annotated[str, Form()] = "",
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language), preview_id=preview_id),
            status_code=303,
            message=str(exc),
        )
    message = 'statement asset uploaded'
    tmp_path: Path | None = None
    target_rel = ''
    try:
        target_rel = normalize_statement_compile_asset_target(path, upload_filename=upload.filename or "")
        with runtime().workspace_service.workspace_lock(workspace):
            asset_abs = safe_workspace_path(workspace, target_rel)
            if asset_abs.exists() and asset_abs.is_dir():
                raise ValueError('statement asset target must be a file path')
            asset_abs.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f'.upload-{asset_abs.name}.', suffix='.tmp', dir=str(asset_abs.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, 'wb') as out:
                    await write_upload_file_limited(
                        upload,
                        out,
                        label='statement asset',
                        max_bytes=runtime().config_values.integer("UPLOAD_MAX_BYTES"),
                    )
                os.replace(tmp_path, asset_abs)
                tmp_path = None
            except Exception:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                    tmp_path = None
                raise
    except (ValueError, OSError) as exc:
        message = str(exc)
    except HTTPException as exc:
        message = str(exc.detail)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=current_language, preview_id=preview_id),
        status_code=303,
        message=message,
    )


async def statement_attachment_upload(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    upload: Annotated[UploadFile, File()],
    path: Annotated[str, Form()] = "",
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language), preview_id=preview_id),
            status_code=303,
            message=str(exc),
        )
    message = 'attachment uploaded'
    tmp_path: Path | None = None
    target_rel = ''
    try:
        target_rel = normalize_contestant_attachment_target(
            path, upload_filename=upload.filename or ""
        )
        with runtime().workspace_service.workspace_lock(workspace):
            attachment_abs = safe_workspace_path(workspace, target_rel)
            if attachment_abs.exists() and attachment_abs.is_dir():
                raise ValueError('attachment target must be a file path')
            attachment_abs.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f'.upload-{attachment_abs.name}.', suffix='.tmp', dir=str(attachment_abs.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, 'wb') as out:
                    await write_upload_file_limited(
                        upload,
                        out,
                        label='attachment',
                        max_bytes=runtime().config_values.integer("UPLOAD_MAX_BYTES"),
                    )
                os.replace(tmp_path, attachment_abs)
                tmp_path = None
            except Exception:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                    tmp_path = None
                raise
    except (ValueError, OSError) as exc:
        message = str(exc)
    except HTTPException as exc:
        message = str(exc.detail)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=current_language, preview_id=preview_id),
        status_code=303,
        message=message,
    )


def statement_attachment_delete(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language), preview_id=preview_id),
            status_code=303,
            message=str(exc),
        )
    message = 'attachment deleted'
    try:
        safe_rel = normalize_contestant_attachment_target(path)
        with runtime().workspace_service.workspace_lock(workspace):
            attachment_abs = safe_workspace_path(workspace, safe_rel)
            if not attachment_abs.exists() or (not attachment_abs.is_file()):
                raise ValueError('attachment not found')
            attachment_abs.unlink()
    except (ValueError, OSError) as exc:
        message = str(exc)
    except HTTPException as exc:
        message = str(exc.detail)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=current_language, preview_id=preview_id),
        status_code=303,
        message=message,
    )


def statement_language_add(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()],
    page: Annotated[str, Form()] = 'statement',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    message = 'statement language created'
    try:
        safe_language = normalize_statement_language(language)
        if not safe_language:
            raise ValueError('statement language is required')
        with runtime().workspace_service.workspace_lock(workspace):
            ensure_statement_language_sources(workspace, safe_language)
    except (RuntimeError, ValueError, OSError) as exc:
        safe_language = ""
        message = str(exc)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=safe_language, preview_id=preview_id),
        status_code=303,
        message=message,
    )


def statement_language_delete(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()] = '',
    page: Annotated[str, Form()] = 'statement',
    preview_id: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    next_language = resolve_statement_page_language(workspace, language)
    message = 'statement language deleted'
    try:
        current_language = selected_statement_language(workspace, language)
        with runtime().workspace_service.workspace_lock(workspace):
            language_root = safe_workspace_path(
                workspace,
                (STATEMENT_SECTIONS_DIR / current_language).as_posix(),
            )
            if (not language_root.exists()) or (not language_root.is_dir()) or language_root.is_symlink():
                raise ValueError('statement language not found')
            shutil.rmtree(language_root, ignore_errors=False)
            sections_root = workspace / STATEMENT_SECTIONS_DIR
            try:
                if sections_root.exists() and (not any(sections_root.iterdir())):
                    sections_root.rmdir()
            except OSError:
                pass
            remaining_languages = statement_languages(workspace)
        next_language = remaining_languages[0] if remaining_languages else ''
    except (ValueError, OSError, HTTPException) as exc:
        message = str(exc)
    return redirect_response(
        statement_redirect_url(problem, user, target_page, language=next_language),
        status_code=303,
        message=message,
    )
