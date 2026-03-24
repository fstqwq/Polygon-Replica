from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse

from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.artifact import artifact_root, safe_artifact_path
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_operation import (
    audit,
    read_text_safe_limited,
    read_workspace_source_with_default,
)
from app.main_util import normalize_workspace_rel_path, safe_workspace_path, sanitize_log_text_for_ui
from app.service.statement.constant import (
    STATEMENT_PROBLEM_REL,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
)
from app.service.statement.context import statement_editor_content_rel
from app.service.statement.signature import statement_sources_signature

_C = config.constants
_STATEMENT_ATTACHMENT_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
    ".pdf",
}



def is_statement_attachment_image_path(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in _STATEMENT_ATTACHMENT_IMAGE_EXTENSIONS


def statement_attachment_rows(workspace: Path, section_dir_rel: str) -> list[dict[str, str]]:
    safe_section_dir = normalize_workspace_rel_path(section_dir_rel)
    if not safe_section_dir:
        return []
    try:
        section_dir_abs = safe_workspace_path(workspace, safe_section_dir)
    except HTTPException:
        return []
    if not section_dir_abs.exists() or (not section_dir_abs.is_dir()) or section_dir_abs.is_symlink():
        return []
    workspace_root = workspace.resolve()
    rows: list[dict[str, str]] = []
    try:
        for item in sorted(section_dir_abs.rglob("*")):
            if not item.is_file() or item.is_symlink():
                continue
            try:
                rel = item.resolve().relative_to(workspace_root).as_posix()
            except (ValueError, OSError):
                continue
            if not is_statement_attachment_image_path(rel):
                continue
            rows.append({"path": rel, "path_q": quote_plus(rel)})
    except OSError:
        return rows
    return rows


def statement_mode_from_ctx(ctx: dict) -> str:
    return ctx["general_cfg"]["mode"]


def statement_editor_section_paths(workspace: Path) -> dict[str, Path]:
    section_root = statement_editor_content_rel(workspace).parent
    return {
        "legend": section_root / "legend.tex",
        "input": section_root / "input.tex",
        "output": section_root / "output.tex",
        "interaction": section_root / "interaction.tex",
        "notes": section_root / "notes.tex",
    }


def normalize_statement_target_page(page: str) -> str:
    return page if page in {"statement", "preview"} else "preview"


def statement_editor_sections(workspace: Path, mode: str) -> tuple[list[dict[str, object]], dict[str, str], bool]:
    section_paths = statement_editor_section_paths(workspace)
    interaction_enabled = mode != "pass-fail"
    specs: tuple[tuple[str, str, str, str], ...] = (
        ("legend", "legend_tex", "Legend", ""),
        ("input", "input_tex", "Input", ""),
        ("output", "output_tex", "Output", ""),
        ("interaction", "interaction_tex", "Interaction Protocol", ""),
        ("notes", "notes_tex", "Notes", ""),
    )
    rows: list[dict[str, object]] = []
    path_map: dict[str, str] = {}
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
        path_map[key] = rel.as_posix()
    return rows, path_map, interaction_enabled


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
        return summary_obj.get("error") or ""
    return ""


def preview_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx['workspace']['id']
    problem_id = int(ctx['problem']['id'])
    workspace = Path(ctx['workspace']['path'])
    problem_title = ctx["problem"]["name"]
    current_statement_signature = statement_sources_signature(workspace, problem_title=problem_title)
    requested_preview_id = request.query_params.get("preview_id", "")
    preview_id = requested_preview_id
    message = ''
    previews = config.preview_service.list_workspace_previews(problem_id, workspace_id)

    def _preview_has_visible_output(candidate_id: str) -> bool:
        if not candidate_id:
            return False
        try:
            artifact_root(problem, candidate_id)
        except HTTPException:
            return False
        try:
            safe_artifact_path(problem, candidate_id, 'statement_preview/statement.pdf')
            return True
        except HTTPException:
            pass
        try:
            safe_artifact_path(problem, candidate_id, 'logs/latex.log')
            return True
        except HTTPException:
            return False
    if not preview_id:
        head_commit = ctx["workspace"].get("head_commit")
        dirty = bool(ctx['workspace'].get('dirty'))
        if head_commit and (not dirty):
            cached_id = config.preview_service.find_cached_preview_id(
                problem,
                problem_id,
                workspace_id,
                source_commit=head_commit,
                statement_signature=current_statement_signature,
                allow_cache_mutation=False,
            )
            if cached_id:
                preview_id = cached_id
        elif dirty:
            cached_id = config.preview_service.find_cached_preview_id(
                problem,
                problem_id,
                workspace_id,
                source_commit=None,
                statement_signature=current_statement_signature,
                allow_cache_mutation=False,
            )
            if cached_id:
                preview_id = cached_id
    if preview_id and (not requested_preview_id) and (not _preview_has_visible_output(preview_id)):
        preview_id = ''
    safe_mode = statement_mode_from_ctx(ctx)
    statement_sections, section_path_map, interaction_section_enabled = statement_editor_sections(workspace, safe_mode)
    log = ''
    log_truncated = False
    pdf_exists = False
    log_refs = []
    log_refs_total = 0
    log_refs_truncated = False
    selected_preview_nav: dict[str, object] | None = None
    selected_preview_summary: dict[str, object] | None = None
    selected_preview_status = 'none'
    preview_compile_failed = False
    preview_failed_stage = ''
    preview_failure_title = 'Compile failed.'
    preview_failure_detail = ''
    latex_log_href = ''

    def _selected_preview_nav_status(candidate_id: str) -> dict[str, object]:
        if not candidate_id:
            return {'text': 'none', 'danger': True, 'warn': False}
        row = config.preview_service.get_workspace_preview(problem_id, workspace_id, candidate_id)
        if row is None:
            return {'text': 'missing', 'danger': True, 'warn': False}
        preview_status = row['status']
        preview_text = preview_status
        preview_danger = preview_status in {'none', 'missing', 'failed', 'error'}
        preview_warn = False
        if preview_status == 'ok':
            has_pdf_output = False
            try:
                safe_artifact_path(problem, candidate_id, 'statement_preview/statement.pdf')
                has_pdf_output = True
            except HTTPException:
                has_pdf_output = False
            if not has_pdf_output:
                return {'text': 'missing', 'danger': True, 'warn': False}
            summary_obj = dict(row['summary'])
            preview_signature = summary_obj.get("statement_signature", "")
            preview_source_commit = row["source_commit"]
            workspace_head = ctx["workspace"].get("head_commit")
            stale_by_signature = bool(preview_signature and current_statement_signature and (preview_signature != current_statement_signature))
            stale_by_head = bool((not preview_signature or not current_statement_signature) and preview_source_commit and workspace_head and (preview_source_commit != workspace_head))
            if stale_by_signature or stale_by_head:
                preview_text = 'stale'
                preview_danger = False
                preview_warn = True
            else:
                preview_text = 'ok'
                preview_danger = False
        return {'text': preview_text, 'danger': preview_danger, 'warn': preview_warn}

    if preview_id:
        preview_row = config.preview_service.get_workspace_preview(problem_id, workspace_id, preview_id)
        if preview_row is None:
            preview_id = ''
        else:
            selected_preview_status = preview_row['status']
            summary_obj = dict(preview_row['summary'])
            selected_preview_summary = summary_obj
            preview_signature = summary_obj.get("statement_signature", "")
            if (not requested_preview_id) and (preview_signature != current_statement_signature):
                preview_id = ''
    if preview_id:
        try:
            safe_artifact_path(problem, preview_id, 'statement_preview/statement.pdf')
            pdf_exists = True
        except HTTPException:
            pdf_exists = False
        try:
            lp = safe_artifact_path(problem, preview_id, 'logs/latex.log')
        except HTTPException:
            lp = None
        if lp is not None:
            latex_log_href = f'/problems/{problem}/{user}/artifacts/{preview_id}/logs/latex.log'
            raw_log, log_truncated = read_text_safe_limited(lp, _C.UI_LOG_TEXT_CHAR_LIMIT)
            redact_prefixes: list[tuple[str, str]] = [(str(workspace.resolve()), '.'), (str(config.settings.workspace_root.resolve()), '__workspace_root__'), (str(config.settings.artifacts_root.resolve()), '__artifacts__'), (str(config.settings.run_root.resolve()), '__runs__'), (str(config.settings.cache_root.resolve()), '__cache__')]
            log = sanitize_log_text_for_ui(raw_log, path_prefixes=redact_prefixes)
            if not log.strip():
                log = '(empty)'
            tex_ref = re.compile('(?P<file>[\\w./-]+\\.tex):(?P<line>\\d+)')
            for line in log.splitlines():
                m = tex_ref.search(line)
                if m:
                    log_refs_total += 1
                    if len(log_refs) >= _C.PREVIEW_LOG_REF_LIST_LIMIT:
                        log_refs_truncated = True
                        continue
                    log_refs.append({'file': m.group('file'), 'line': int(m.group('line')), 'context': line})
        selected_preview_nav = _selected_preview_nav_status(preview_id)
        preview_compile_failed = selected_preview_status in {'failed', 'error'}
        if preview_compile_failed:
            if selected_preview_summary is not None:
                preview_failed_stage = selected_preview_summary.get("failed_stage", "")
            if preview_failed_stage == 'sample_sync':
                preview_failure_title = 'Sample verification failed.'
                preview_failure_detail = sanitize_log_text_for_ui(selected_preview_summary.get("error", ""))
                latex_log_href = ''
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
    if selected_preview_nav is not None:
        ctx['nav_status']['preview'] = selected_preview_nav
    return_page = 'preview' if str(getattr(request.url, "path")).endswith('/preview') else 'statement'
    statement_section_dir = Path(section_path_map["legend"]).parent.as_posix()
    statement_attachments = statement_attachment_rows(workspace, statement_section_dir)
    return template_response(
        request,
        'preview.html',
        {
            'ctx': ctx,
            'message': message,
            'preview_id': preview_id,
            'previews': previews,
            'statement_sections': statement_sections,
            'statement_section_paths': section_path_map,
            'statement_section_dir': statement_section_dir,
            'interaction_section_enabled': bool(interaction_section_enabled),
            'statement_template_path': STATEMENT_TEMPLATE_REL.as_posix(),
            'statement_problem_path': STATEMENT_PROBLEM_REL.as_posix(),
            'statement_style_path': STATEMENT_STYLE_REL.as_posix(),
            'statement_attachments': statement_attachments,
            'editor_char_limit': _C.STATEMENT_EDITOR_CHAR_LIMIT,
            'log': log,
            'log_truncated': log_truncated,
            'log_char_limit': _C.UI_LOG_TEXT_CHAR_LIMIT,
            'pdf_exists': pdf_exists,
            'log_refs': log_refs,
            'log_refs_total': log_refs_total,
            'log_refs_truncated': log_refs_truncated,
            'log_refs_limit': _C.PREVIEW_LOG_REF_LIST_LIMIT,
            'preview_compile_failed': preview_compile_failed,
            'preview_failure_title': preview_failure_title,
            'preview_failure_detail': preview_failure_detail,
            'preview_failed_stage': preview_failed_stage,
            'latex_log_href': latex_log_href,
            'problem_name_max_len': _C.PROBLEM_NAME_MAX_LEN,
            'problem_mode_values': list(_C.GENERAL_MODE_VALUES),
            'time_limit_min_ms': _C.GENERAL_TIME_LIMIT_MIN_MS,
            'time_limit_max_ms': _C.GENERAL_TIME_LIMIT_MAX_MS,
            'memory_limit_min_mb': _C.GENERAL_MEMORY_LIMIT_MIN_MB,
            'memory_limit_max_mb': _C.GENERAL_MEMORY_LIMIT_MAX_MB,
            'return_page': return_page,
            'statement_mode': safe_mode,
        },
    )

def preview_run(problem: str, user: str, page: str=Form('statement')):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace_head = ctx["workspace"].get("head_commit")
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    workspace_key = f'{problem_id}:{workspace_id}'
    details: dict[str, object] = {
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
    base = f'/problems/{problem}/{user}/{target_page}'
    with config.preview_lock:
        if workspace_key in config.preview_inflight:
            details['status'] = 'running'
            details['preview_status'] = 'running'
            details['error'] = 'preview compile already running'
            audit(ctx['user']['id'], problem_id, 'preview.run', details)
            return redirect_response(base, status_code=303, message='preview compile already running')
        config.preview_inflight.add(workspace_key)
    try:
        preview_id = config.preview_service.compile_preview(problem, user)
        details['preview_id'] = preview_id
        row = config.preview_service.get_workspace_preview(problem_id, workspace_id, details['preview_id'])
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
            details["error"] = summary_obj.get("error") or "preview failed"
            failed_stage = summary_obj.get("failed_stage", "")
            details['failed_stage'] = failed_stage
            msg = 'sample verification failed' if failed_stage == 'sample_sync' else 'preview compile failed'
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        msg = str(exc)
    finally:
        with config.preview_lock:
            config.preview_inflight.discard(workspace_key)
        audit(ctx['user']['id'], problem_id, 'preview.run', details)
    redirect_url = base
    preview_id = details["preview_id"]
    if preview_id:
        redirect_url = f'{base}?preview_id={preview_id}'
    return redirect_response(redirect_url, status_code=303, message=msg)

def preview_status(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace_key = f'{problem_id}:{workspace_id}'
    with config.preview_lock:
        running = workspace_key in config.preview_inflight
    row = config.preview_service.latest_workspace_preview(problem_id, workspace_id)
    latest_preview_id = ''
    latest_status = 'missing'
    latest_created_at = ''
    latest_finished_at = ''
    if row is not None:
        latest_preview_id = row['id']
        latest_status = row['status']
        latest_created_at = row['created_at']
        latest_finished_at = row["finished_at"] or ""
    return JSONResponse(
        {
            'running': bool(running),
            'latest_preview_id': latest_preview_id,
            'latest_status': latest_status,
            'latest_created_at': latest_created_at,
            'latest_finished_at': latest_finished_at,
        }
    )

def preview_save(
    problem: str,
    user: str,
    legend_tex: str=Form(''),
    input_tex: str=Form(''),
    output_tex: str=Form(''),
    interaction_tex: str=Form(''),
    notes_tex: str=Form(''),
    page: str=Form('statement'),
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    statement_mode = statement_mode_from_ctx(ctx)
    with config.workspace_service.workspace_lock(workspace):
        section_paths = statement_editor_section_paths(workspace)
        write_plan = {
            'legend': legend_tex,
            'input': input_tex,
            'output': output_tex,
            'notes': notes_tex,
        }
        if statement_mode != 'pass-fail':
            write_plan['interaction'] = interaction_tex
        for key, content in write_plan.items():
            rel = section_paths[key]
            section_path = safe_workspace_path(workspace, rel.as_posix())
            section_path.parent.mkdir(parents=True, exist_ok=True)
            section_path.write_text(content, encoding='utf-8')
    audit(
        ctx['user']['id'],
        ctx['problem']['id'],
        'preview.save_sources',
        {
            'mode': statement_mode,
            'legend_bytes': len(legend_tex.encode('utf-8')),
            'input_bytes': len(input_tex.encode('utf-8')),
            'output_bytes': len(output_tex.encode('utf-8')),
            'notes_bytes': len(notes_tex.encode('utf-8')),
            'interaction_bytes': len(interaction_tex.encode('utf-8')) if statement_mode != 'pass-fail' else 0,
        },
    )
    return redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message='statement saved')

def statement_attachment_delete(problem: str, user: str, path: str=Form(...), page: str=Form('statement')):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    section_dir_rel = statement_editor_section_paths(workspace)['legend'].parent.as_posix()
    message = 'attachment deleted'
    try:
        safe_rel = normalize_workspace_rel_path(path)
        if not safe_rel:
            raise ValueError('attachment path is required')
        section_prefix = section_dir_rel.rstrip('/') + '/'
        if safe_rel != section_dir_rel and not safe_rel.startswith(section_prefix):
            raise ValueError('attachment must be under statement section directory')
        if not is_statement_attachment_image_path(safe_rel):
            raise ValueError('only image attachments are supported')
        with config.workspace_service.workspace_lock(workspace):
            attachment_abs = safe_workspace_path(workspace, safe_rel)
            if not attachment_abs.exists() or (not attachment_abs.is_file()):
                raise ValueError('attachment not found')
            attachment_abs.unlink()
        audit(ctx['user']['id'], ctx['problem']['id'], 'statement.attachment.delete', {'path': safe_rel})
    except (ValueError, OSError) as exc:
        message = str(exc)
    except HTTPException as exc:
        message = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message=message)

