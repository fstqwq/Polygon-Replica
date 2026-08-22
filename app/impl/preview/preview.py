import app.main_constant as _K
from app.impl.auth.session import require_session_user

import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, TypedDict
from urllib.parse import quote_plus, urlencode

from fastapi import File, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import PlainTextResponse

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
    statement_templates_are_default,
    statement_title_for_language,
)
from app.service.problem.runtime_config import problem_config_limits

_CONTESTANT_ATTACHMENTS_ROOT = "attachments"


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
) -> str:
    del user
    base = f"/problems/{problem}/{normalize_statement_target_page(page)}"
    query: dict[str, str] = {}
    safe_language = normalize_statement_language(language)
    if safe_language:
        query["language"] = safe_language
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


def preview_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    available_languages = statement_languages(workspace)
    current_language = resolve_statement_page_language(workspace, request.query_params.get("language", ""))
    has_statement_language = bool(current_language)
    message = ''
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
    return_page = 'preview' if str(getattr(request.url, "path")).endswith('/preview') else 'statement'
    statement_assets_dir = STATEMENT_ASSETS_DIR.as_posix()
    statement_compile_assets = statement_compile_asset_rows(workspace)
    contestant_attachments = contestant_attachment_rows(workspace)
    statement_examples_template = _statement_examples_template_editor(workspace)
    statement_templates_customized = not statement_templates_are_default(workspace)
    return template_response(
        request,
        'preview.html',
        {
            'ctx': ctx,
            'message': message,
            'statement_sections': statement_sections,
            'statement_assets_dir': statement_assets_dir,
            'statement_template_path': STATEMENT_TEMPLATE_REL.as_posix(),
            'statement_problem_path': STATEMENT_PROBLEM_REL.as_posix(),
            'statement_examples_template': statement_examples_template,
            'statement_templates_customized': statement_templates_customized,
            'statement_style_path': STATEMENT_STYLE_REL.as_posix(),
            'statement_compile_assets': statement_compile_assets,
            'contestant_attachments': contestant_attachments,
            'editor_char_limit': runtime().config_values.integer("TEXTAREA_MAX_BYTES"),
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
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language)),
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
        statement_redirect_url(problem, user, target_page, language=current_language),
        status_code=303,
        message=msg,
    )


def statement_templates_reset(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
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
        statement_redirect_url(problem, user, target_page, language=current_language),
        status_code=303,
        message=message,
    )


def statement_examples_template_toggle(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    enabled: Annotated[bool, Form()] = False,
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
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
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language)),
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
        statement_redirect_url(problem, user, target_page, language=current_language),
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
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language)),
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
        statement_redirect_url(problem, user, target_page, language=current_language),
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
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language)),
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
        statement_redirect_url(problem, user, target_page, language=current_language),
        status_code=303,
        message=message,
    )


def statement_attachment_delete(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    page: Annotated[str, Form()] = 'statement',
    language: Annotated[str, Form()] = '',
):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        current_language = selected_statement_language(workspace, language)
    except ValueError as exc:
        return redirect_response(
            statement_redirect_url(problem, user, target_page, language=resolve_statement_page_language(workspace, language)),
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
        statement_redirect_url(problem, user, target_page, language=current_language),
        status_code=303,
        message=message,
    )


def statement_language_add(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()],
    page: Annotated[str, Form()] = 'statement',
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
        statement_redirect_url(problem, user, target_page, language=safe_language),
        status_code=303,
        message=message,
    )


def statement_language_delete(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    language: Annotated[str, Form()] = '',
    page: Annotated[str, Form()] = 'statement',
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
