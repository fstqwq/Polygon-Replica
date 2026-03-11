from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from app.impl.auth.public import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.public import (
    allocate_invocation_id,
    allocate_run_id,
    artifact_root,
    audit,
    normalize_page_target,
    parse_summary_json,
    read_tests_spec,
    read_text_safe_limited,
    read_workspace_source_with_default,
    require_write_access,
    run_solution_options_context,
    safe_artifact_path,
    start_verification_job,
    tests_spec_bool_flag,
    tests_spec_editor_context,
    tests_spec_form_text,
    tests_spec_payload_file_path,
    tests_spec_read_payload,
    tests_spec_remove_payload,
    tests_spec_resolve_index,
    tests_spec_write_payload,
    workspace_rel_file_exists,
    write_tests_spec,
    page_ctx,
)
from app.main_util import (
    normalize_workspace_rel_path,
    safe_workspace_path,
    sanitize_log_text_for_ui,
)
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.statement.constant import (
    STATEMENT_PROBLEM_REL,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
)
from app.service.statement.context import statement_editor_content_rel
from app.service.statement.signature import statement_sources_signature
from app.service.problem.test_spec import (
    TESTS_SPEC_MANUAL_MAX_CHARS,
    TESTS_SPEC_REL,
    next_test_id,
    normalize_gen_command,
    normalize_manual_input,
    normalize_sample_input,
    normalize_sample_output,
    normalize_test_id,
    normalize_test_kind,
    normalize_tests_spec_entry,
)

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
    return Path(str(rel_path or '')).suffix.lower() in _STATEMENT_ATTACHMENT_IMAGE_EXTENSIONS

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
        for item in sorted(section_dir_abs.rglob('*')):
            if not item.is_file() or item.is_symlink():
                continue
            try:
                rel = item.resolve().relative_to(workspace_root).as_posix()
            except (ValueError, OSError):
                continue
            if not is_statement_attachment_image_path(rel):
                continue
            rows.append({'path': rel, 'path_q': quote_plus(rel)})
    except OSError:
        return rows
    return rows

def tests_spec_gen_script_context(workspace: Path) -> dict[str, object]:
    lines: list[str] = []
    with config.workspace_service.workspace_lock(workspace):
        entries, _spec_path = read_tests_spec(workspace)
        for entry in entries:
            kind = str(entry.get('kind') or '').strip().lower()
            if kind != 'gen':
                continue
            command = str(tests_spec_read_payload(workspace, entry) or '').replace('\r\n', '\n').replace('\r', '\n').strip()
            if not command:
                continue
            lines.append(command)
    return {'text': '\n'.join(lines), 'count': len(lines)}

def parse_gen_script_lines(raw: object) -> list[str]:
    normalized = str(raw or '').replace('\r\n', '\n').replace('\r', '\n')
    commands: list[str] = []
    for line in normalized.split('\n'):
        cmd = str(line or '').strip()
        if not cmd:
            continue
        commands.append(normalize_gen_command(cmd))
    return commands

def tests_spec_sample_input_value(raw: object | None, fallback: object = '') -> str:
    if raw is None:
        return normalize_sample_input(fallback)
    return normalize_sample_input(tests_spec_form_text(raw))

def tests_spec_sample_output_value(raw: object | None, fallback: object = '') -> str:
    if raw is None:
        return normalize_sample_output(fallback)
    return normalize_sample_output(tests_spec_form_text(raw))

def tests_spec_sample_output_validate_value(raw: object | None, fallback: object = True) -> bool:
    if raw is None:
        return tests_spec_bool_flag(fallback)
    return tests_spec_bool_flag(tests_spec_form_text(raw))

def tests_spec_row(
    *,
    test_id: str,
    kind: str,
    sample: bool,
    sample_input: str = '',
    sample_output: str = '',
    sample_output_validate: bool = True,
    index: int = 0,
) -> dict:
    payload: dict[str, object] = {
        'id': normalize_test_id(test_id),
        'kind': normalize_test_kind(kind),
        'sample': bool(sample),
    }
    safe_sample_input = normalize_sample_input(sample_input)
    safe_sample_output = normalize_sample_output(sample_output)
    if safe_sample_input:
        payload['sample_input'] = safe_sample_input
    if safe_sample_output:
        payload['sample_output'] = safe_sample_output
    if not bool(sample_output_validate):
        payload['sample_output_validate'] = False
    return normalize_tests_spec_entry(payload, index=index)

def tests_spec_add_single_entry(
    workspace: Path,
    *,
    requested_id: str,
    kind: str,
    sample: bool,
    payload: str,
    sample_input: str,
    sample_output: str,
    sample_output_validate: bool,
) -> tuple[int, str]:
    entries, spec_path = read_tests_spec(workspace)
    safe_test_id = normalize_test_id(requested_id) if requested_id else next_test_id(entries)
    if any((str(row.get('id') or '').strip() == safe_test_id for row in entries)):
        raise ValueError(f'test id already exists: {safe_test_id}')
    entries.append(
        tests_spec_row(
            test_id=safe_test_id,
            kind=kind,
            sample=sample,
            sample_input=sample_input,
            sample_output=sample_output,
            sample_output_validate=sample_output_validate,
            index=len(entries) + 1,
        )
    )
    write_tests_spec(spec_path, entries)
    tests_spec_write_payload(workspace, safe_test_id, kind, payload)
    return len(entries), safe_test_id

def statement_mode_from_ctx(ctx: dict) -> str:
    raw_mode = str(((ctx.get('general_cfg') or {}).get('mode')) or '').strip().lower()
    allowed = {str(item).strip().lower() for item in _C.GENERAL_MODE_VALUES}
    if raw_mode in allowed:
        return raw_mode
    return str(_C.GENERAL_CONFIG_DEFAULTS.get('mode') or 'pass-fail').strip().lower()

def statement_editor_section_paths(workspace: Path) -> dict[str, Path]:
    legend_rel = statement_editor_content_rel(workspace)
    section_root = legend_rel.parent
    return {
        'legend': section_root / 'legend.tex',
        'input': section_root / 'input.tex',
        'output': section_root / 'output.tex',
        'interaction': section_root / 'interaction.tex',
        'notes': section_root / 'notes.tex',
    }

def normalize_statement_target_page(page: str) -> str:
    target_page = normalize_page_target(page)
    if target_page in {'problems', 'contests'}:
        return 'statement'
    if target_page not in {'statement', 'preview'}:
        return 'preview'
    return target_page

def normalize_verification_target_page(page: str) -> str:
    target_page = normalize_page_target(page)
    if target_page in {'problems', 'contests', 'settings'}:
        return 'statement'
    if target_page == 'git':
        return 'workspace'
    return target_page

def statement_editor_sections(workspace: Path, mode: str) -> tuple[list[dict[str, object]], dict[str, str], bool]:
    section_paths = statement_editor_section_paths(workspace)
    interaction_enabled = str(mode or '').strip().lower() != 'pass-fail'
    specs: tuple[tuple[str, str, str, str], ...] = (
        ('legend', 'legend_tex', 'Legend', ''),
        ('input', 'input_tex', 'Input', ''),
        ('output', 'output_tex', 'Output', ''),
        ('interaction', 'interaction_tex', 'Interaction Protocol', ''),
        ('notes', 'notes_tex', 'Notes', ''),
    )
    rows: list[dict[str, object]] = []
    path_map: dict[str, str] = {}
    for key, field_name, label, fallback in specs:
        rel = section_paths[key]
        content_text, content_truncated = read_workspace_source_with_default(workspace, rel, fallback)
        enabled = key != 'interaction' or interaction_enabled
        rows.append(
            {
                'key': key,
                'label': label,
                'field_name': field_name,
                'path': rel.as_posix(),
                'content': content_text,
                'truncated': bool(content_truncated),
                'enabled': bool(enabled),
            }
        )
        path_map[key] = rel.as_posix()
    return rows, path_map, interaction_enabled

def extract_latex_failure_summary(log_text: str, summary_obj: dict[str, object] | None=None) -> str:
    text = str(log_text or '')
    lines = text.splitlines()
    if lines:
        file_hint = ''
        star_file_re = re.compile('^\\*\\*(?P<file>[^\\s]+\\.tex)\\s*$')
        open_file_re = re.compile('\\((?:\\./)?(?P<file>[^()\\s]+\\.tex)\\b')
        line_re = re.compile('^l\\.(?P<line>\\d+)\\s*(?P<context>.*)$')
        for raw in lines:
            stripped = str(raw or '').strip()
            if not stripped:
                continue
            m_star = star_file_re.match(stripped)
            if m_star:
                file_hint = str(m_star.group('file') or '').strip()
                break
            m_open = open_file_re.search(stripped)
            if m_open:
                file_hint = str(m_open.group('file') or '').strip()
                break
        for idx, raw in enumerate(lines):
            stripped = str(raw or '').strip()
            if not stripped.startswith('!'):
                continue
            error_msg = stripped[1:].strip()
            if not error_msg:
                continue
            line_no = ''
            for j in range(idx + 1, min(len(lines), idx + 8)):
                probe = str(lines[j] or '').strip()
                m_line = line_re.match(probe)
                if not m_line:
                    continue
                line_no = str(m_line.group('line') or '').strip()
                break
            if file_hint and line_no:
                return f'{file_hint}:{line_no} {error_msg}'
            if line_no:
                return f'line {line_no}: {error_msg}'
            return error_msg
        noise_prefixes = (
            'this is pdftex',
            'entering extended mode',
            'restricted /write18 enabled',
            '%&-line parsing enabled',
            '**',
        )
        for raw in lines:
            stripped = str(raw or '').strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if any((lowered.startswith(prefix) for prefix in noise_prefixes)):
                continue
            return stripped
    if isinstance(summary_obj, dict):
        return str(summary_obj.get('error') or '').strip()
    return ''


