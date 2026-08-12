from __future__ import annotations

import app.main_constant as _K
import json
import os
import time
from pathlib import Path
from typing import TypedDict
from fastapi import HTTPException
from app.impl.runtime.config import config
from app.impl.workspace.artifact import (
    artifact_root,
)
from app.impl.workspace.context import (
    count_label,
)
from app.impl.workspace.solution import (
    list_solution_sources,
)
from app.impl.workspace.test_spec import (
    read_tests_spec,
    tests_spec_payload_file_path,
    tests_spec_payload_rel_path,
    tests_spec_read_payload,
)
from app.main_util import (
    normalize_workspace_rel_path,
    safe_workspace_path,
)
from app.service.problem.solution_metadata import (
    desc_rel_path_for_source,
    expected_behavior_label,
    normalize_expected_behavior,
    parse_solution_desc,
)
from app.service.problem.build_config import (
    BuildConfig,
    dumps_build_config,
    load_build_config,
)
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    summarize_tests_spec,
)
from app.service.repository.revision import workspace_upstream_revision_display
from app.service.verification.runtime import coerce_int

_C = config.config_values

_STANDARD_CHECKER_CACHE_TTL_SEC = 2.0
_STANDARD_CHECKER_CACHE_TS = 0.0
_STANDARD_CHECKER_CACHE_AVAILABLE = False
_STANDARD_CHECKER_CACHE_NAMES: tuple[str, ...] = ()
_STANDARD_CHECKER_CACHE_SET: frozenset[str] = frozenset()


def _db_revision_display(local: int | None, upstream: int | None) -> str:
    return workspace_upstream_revision_display(local, upstream)

def user_participating_problems(user_id: int, limit: int) -> list[dict]:
    uid = int(user_id)
    cap = max(1, int(limit))
    rows = config.workspace_service.participating_problem_rows(uid, limit=cap)
    items: list[dict] = []
    for row in rows:
        role = normalize_contest_role(row['role'])
        head = row['head_commit']
        if head is None:
            head = ''
        branch = row['branch']
        if branch is None:
            branch = 'main'
        dirty_value = row['dirty']
        dirty = False
        if dirty_value is not None:
            try:
                dirty = int(dirty_value) != 0
            except Exception:
                dirty = bool(dirty_value)
        workspace_path = row['path'] or ''
        revision_local = row['revision_local']
        revision_upstream = row['revision_upstream']
        revision_display = _db_revision_display(revision_local, revision_upstream)
        items.append({'slug': row['slug'], 'role': role, 'workspace_id': row['workspace_id'], 'has_workspace': row['workspace_id'] is not None, 'workspace_path': workspace_path, 'branch': branch, 'head_commit': head, 'head_short': head[:8], 'dirty': dirty, 'revision_local': revision_local, 'revision_upstream': revision_upstream, 'revision_display': revision_display, 'revision_highlight': bool(row['revision_highlight']), 'revision_upstream_higher': bool(row['revision_upstream_higher']), 'revision_missing': bool(row['revision_missing']), 'updated_at': row['updated_at'], 'last_updated_at': row['last_updated_at']})
    return items

def normalize_contest_role(raw: str | None) -> str:
    if raw in {'admin', 'owner', 'write', 'read'}:
        return raw
    return 'read'

def normalize_contest_slug_required(value: str) -> str:
    slug = value.strip()
    if not _K.CONTEST_IDENT_RE.fullmatch(slug):
        raise ValueError('invalid contest slug')
    return slug

def normalize_contest_title_required(value: str) -> str:
    title = value.strip()
    if not title:
        raise ValueError('contest title is required')
    if len(title) > _C.CONTEST_TITLE_MAX_LEN:
        raise ValueError(f'contest title is too long (max {_C.CONTEST_TITLE_MAX_LEN})')
    return title

def user_contests_overview(user_id: int, limit: int) -> list[dict]:
    uid = int(user_id)
    cap = max(1, int(limit))
    return config.contest_service.user_contests_overview(uid, limit=cap)

def audit(actor_user_id: int, problem_id: int | None, action: str, details: dict) -> None:
    config.workspace_service.record_audit_event(
        actor_user_id=int(actor_user_id),
        problem_id=problem_id,
        action=action,
        details=details,
    )

def normalize_page_target(page: str) -> str:
    raw = page.strip().lower()
    allowed = {
        'problems',
        'statement',
        'contests',
        'files',
        'generators',
        'checker',
        'interactor',
        'validator',
        'solutions',
        'git',
        'workspace',
        'tests',
        'preview',
        'run',
        'export',
        'access',
        'history',
        'settings',
    }
    return raw if raw in allowed else 'tests'

def read_text_safe_limited(path: Path, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    with path.open('r', encoding='utf-8', errors='replace') as fh:
        text = fh.read(cap + 1)
    if len(text) <= cap:
        return (text, False)
    clipped = text[:cap]
    if '\n' in clipped:
        clipped = clipped.rsplit('\n', 1)[0] + '\n'
    clipped += f'... [truncated; showing first {cap} characters]\n'
    return (clipped, True)

def read_workspace_source_with_default(workspace: Path, rel: Path, default_text: str) -> tuple[str, bool]:
    try:
        file_path = safe_workspace_path(workspace, rel.as_posix())
    except HTTPException:
        return (default_text, False)
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        return (default_text, False)
    return read_text_safe_limited(file_path, _C.TEXTAREA_MAX_BYTES)

def parse_line_param(raw: str | None, default: int=1) -> int:
    if raw is None:
        return default
    try:
        line = int(raw.strip())
    except Exception:
        return default
    return line if line > 0 else default

def build_line_focus_context(text: str, line: int, radius: int=2) -> dict | None:
    rows = text.splitlines()
    if not rows:
        return None
    target = int(line)
    if target <= 0 or target > len(rows):
        return None
    start = max(1, target - max(0, int(radius)))
    end = min(len(rows), target + max(0, int(radius)))
    snippet: list[str] = []
    for ln in range(start, end + 1):
        marker = '>' if ln == target else ' '
        snippet.append(f'{marker} {ln:5d} | {rows[ln - 1]}')
    return {'start': start, 'end': end, 'line': target, 'text': '\n'.join(snippet)}

def _normalize_repo_dir(raw: str | None) -> str:
    text = '' if raw is None else raw.strip().replace('\\', '/')
    if not text:
        return ''
    if text.startswith('/'):
        return ''
    parts: list[str] = []
    for part in text.split('/'):
        item = part.strip()
        if not item or item == '.':
            continue
        if item == '..':
            return ''
        parts.append(item)
    return '/'.join(parts)

class RepoBrowserEntry(TypedDict):
    name: str
    path: str


class RepoBrowserBreadcrumb(TypedDict):
    label: str
    path: str


class RepoBrowserContext(TypedDict):
    directory: str
    breadcrumbs: list[RepoBrowserBreadcrumb]
    directories: list[RepoBrowserEntry]
    files: list[RepoBrowserEntry]


def build_repo_browser_context(
    workspace: Path,
    paths: list[str],
    browse_dir: str,
    *,
    root_label: str,
) -> RepoBrowserContext:
    entries: list[tuple[str, bool]] = []
    for rel in paths:
        if not rel:
            continue
        is_dir = False
        try:
            abs_path = safe_workspace_path(workspace, rel)
            is_dir = abs_path.exists() and abs_path.is_dir()
        except HTTPException:
            pass
        entries.append((rel, is_dir))
    dir_norm = _normalize_repo_dir(browse_dir)
    prefix = f'{dir_norm}/' if dir_norm else ''
    if dir_norm and (not any((full == dir_norm or full.startswith(prefix) for full, _is_dir in entries))):
        dir_norm = ''
        prefix = ''
    child_dirs: dict[str, str] = {}
    child_files: list[dict[str, str]] = []
    for full, is_dir in entries:
        if full == dir_norm:
            continue
        if prefix and (not full.startswith(prefix)):
            continue
        rel = full[len(prefix):] if prefix else full
        if not rel:
            continue
        if '/' in rel:
            name = rel.split('/', 1)[0]
            if name not in child_dirs:
                child_dirs[name] = f'{prefix}{name}' if prefix else name
        elif is_dir:
            if rel not in child_dirs:
                child_dirs[rel] = f'{prefix}{rel}' if prefix else rel
        else:
            child_files.append({'name': rel, 'path': f'{prefix}{rel}' if prefix else rel})
    dirs = [{'name': name, 'path': child_dirs[name]} for name in sorted(child_dirs)]
    files = sorted(child_files, key=lambda row: row['name'])
    breadcrumbs: list[RepoBrowserBreadcrumb] = [{'label': root_label, 'path': ''}]
    current_path = ''
    for part in dir_norm.split('/') if dir_norm else []:
        current_path = f'{current_path}/{part}' if current_path else part
        breadcrumbs.append({'label': part, 'path': current_path})
    return {
        'directory': dir_norm,
        'breadcrumbs': breadcrumbs,
        'directories': dirs,
        'files': files,
    }

def kind_for_path(path: str) -> str:
    for row in _K.CORE_SOURCE_TARGETS:
        if row['path'] == path:
            return row['kind']
    return ''

def template_for_kind(kind: str) -> str:
    key = kind.strip().lower()
    if key not in _K.FILE_TEMPLATES:
        raise ValueError('unknown template kind')
    return str(_K.FILE_TEMPLATES[key])

def _standard_checker_cache_values() -> tuple[tuple[str, ...], frozenset[str], bool]:
    global _STANDARD_CHECKER_CACHE_TS
    global _STANDARD_CHECKER_CACHE_AVAILABLE
    global _STANDARD_CHECKER_CACHE_NAMES
    global _STANDARD_CHECKER_CACHE_SET
    now = time.monotonic()
    if (now - _STANDARD_CHECKER_CACHE_TS) <= _STANDARD_CHECKER_CACHE_TTL_SEC:
        return (_STANDARD_CHECKER_CACHE_NAMES, _STANDARD_CHECKER_CACHE_SET, _STANDARD_CHECKER_CACHE_AVAILABLE)
    root = _K.STANDARD_CHECKER_ROOT
    available = False
    names: list[str] = []
    try:
        if root.exists() and root.is_dir() and (not root.is_symlink()):
            available = True
            with os.scandir(root) as entries:
                for entry in entries:
                    name = entry.name
                    if Path(name).suffix.lower() != '.cpp':
                        continue
                    if not _K.STANDARD_CHECKER_NAME_RE.fullmatch(name):
                        continue
                    try:
                        if entry.is_symlink() or (not entry.is_file(follow_symlinks=False)):
                            continue
                    except OSError:
                        continue
                    names.append(name)
            names.sort()
    except OSError:
        available = False
        names = []
    _STANDARD_CHECKER_CACHE_TS = now
    _STANDARD_CHECKER_CACHE_AVAILABLE = available
    _STANDARD_CHECKER_CACHE_NAMES = tuple(names)
    _STANDARD_CHECKER_CACHE_SET = frozenset(names)
    return (_STANDARD_CHECKER_CACHE_NAMES, _STANDARD_CHECKER_CACHE_SET, _STANDARD_CHECKER_CACHE_AVAILABLE)

def _standard_checker_options() -> list[str]:
    names, _name_set, _available = _standard_checker_cache_values()
    return list(names)

def standard_checker_catalog() -> list[dict]:
    catalog: list[dict] = []
    for name in _standard_checker_options():
        canonical = f'std::{name}'
        description = _K.STANDARD_CHECKER_DESCRIPTIONS.get(name, 'general-purpose standard checker from testlib')
        catalog.append({'name': name, 'value': canonical, 'description': description, 'label': f'{canonical} - {description}'})
    return catalog

def _normalize_standard_checker_name(raw: str) -> str:
    value = raw.strip()
    if value.startswith('std::'):
        value = value[5:]
    if not value:
        raise ValueError('standard checker name is required')
    if '/' in value or '\\' in value:
        raise ValueError('invalid standard checker name')
    if not value.endswith('.cpp'):
        value += '.cpp'
    if not _K.STANDARD_CHECKER_NAME_RE.fullmatch(value):
        raise ValueError('invalid standard checker name')
    return value

def _canonical_standard_checker_name(raw: str) -> str:
    return f'std::{_normalize_standard_checker_name(raw)}'

def resolve_standard_checker_path(raw_name: str) -> tuple[str, Path]:
    checker_name = _normalize_standard_checker_name(raw_name)
    _names, name_set, available = _standard_checker_cache_values()
    if not available:
        raise ValueError('standard checker catalog is unavailable')
    if checker_name not in name_set:
        raise ValueError(f'unknown standard checker: std::{checker_name}')
    source = _K.STANDARD_CHECKER_ROOT / checker_name
    return (checker_name, source)

def read_build_config(workspace: Path) -> tuple[BuildConfig, Path]:
    cfg_path = safe_workspace_path(workspace, _K.BUILD_CONFIG_REL)
    return (load_build_config(workspace), cfg_path)

def write_build_config(cfg_path: Path, payload: BuildConfig) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(dumps_build_config(payload), encoding='utf-8', newline='\n')

def dedupe_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result

def workspace_rel_file_exists(workspace: Path, rel: str | None) -> bool:
    normalized = normalize_workspace_rel_path(rel)
    if not normalized:
        return False
    try:
        target = safe_workspace_path(workspace, normalized)
    except HTTPException:
        return False
    try:
        if target.is_symlink():
            return False
        return bool(target.exists() and target.is_file())
    except OSError:
        return False

def _text_head_by_bytes(raw: str, max_bytes: int) -> tuple[str, bool]:
    cap = max(1, int(max_bytes))
    encoded = raw.encode('utf-8', errors='replace')
    clipped = len(encoded) > cap
    head = encoded[:cap].decode('utf-8', errors='replace')
    return (head, clipped)

def _file_head_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    cap = max(1, int(max_bytes))
    try:
        with path.open('rb') as f:
            head = f.read(cap + 1)
    except OSError:
        return ('(unreadable)', False)
    clipped = len(head) > cap
    text = head[:cap].decode('utf-8', errors='replace')
    return (text, clipped)

def tests_spec_editor_context(workspace: Path, limit: int) -> dict:
    limits = _C.snapshot()
    entries, path = read_tests_spec(
        workspace,
        document_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
        sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
    )
    summary = summarize_tests_spec(entries)
    rows: list[dict] = []
    cap = max(1, int(limit))
    truncated = len(entries) > cap
    for idx, entry in enumerate(entries[:cap], start=1):
        kind = entry['kind']
        test_id = entry['id']
        sample = entry['sample']
        sample_input = entry['sample_input']
        sample_output = entry['sample_output']
        sample_output_validate = entry['sample_output_validate']
        payload_path = tests_spec_payload_rel_path(test_id, kind) if test_id and kind else ''
        payload_abs: Path | None = None
        if payload_path:
            try:
                payload_abs = tests_spec_payload_file_path(workspace, test_id, kind)
            except (HTTPException, ValueError):
                payload_abs = None
        payload = ''
        preview_source = ''
        payload_size_bytes = 0
        manual_large_payload = False
        preview_clipped = False
        preview_bytes_limit = 0
        payload_file_exists = False
        if payload_abs is not None:
            try:
                payload_file_exists = bool(payload_abs.exists() and payload_abs.is_file() and (not payload_abs.is_symlink()))
            except OSError:
                payload_file_exists = False
        if payload_file_exists and payload_abs is not None:
            try:
                payload_size_bytes = max(0, int(payload_abs.stat().st_size))
            except OSError:
                payload_size_bytes = 0
        if kind == 'manual' and payload_size_bytes > _C.TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES:
            manual_large_payload = True
            preview_bytes_limit = _C.TESTS_SPEC_MANUAL_PREVIEW_BYTES
            if payload_file_exists and payload_abs is not None:
                preview_source, preview_clipped = _file_head_text(payload_abs, _C.TESTS_SPEC_MANUAL_PREVIEW_BYTES)
            else:
                fallback_payload = tests_spec_read_payload(workspace, entry)
                payload_size_bytes = len(fallback_payload.encode('utf-8', errors='replace'))
                preview_source, preview_clipped = _text_head_by_bytes(fallback_payload, _C.TESTS_SPEC_MANUAL_PREVIEW_BYTES)
        else:
            payload = tests_spec_read_payload(workspace, entry)
            preview_source = payload
            if payload_size_bytes <= 0:
                payload_size_bytes = len(payload.encode('utf-8', errors='replace'))
        if manual_large_payload:
            preview_text = preview_source.replace('\r\n', '\n').replace('\r', '\n')
            if not preview_text:
                preview_text = '(empty)'
        else:
            preview_text = _inline_text_preview(preview_source, _C.TESTS_SPEC_PREVIEW_CHARS, _C.TESTS_SPEC_PREVIEW_LINES)
        rows.append(
            {
                'index': idx,
                'id': test_id,
                'kind': kind,
                'sample': sample,
                'sample_input': sample_input,
                'sample_output': sample_output,
                'sample_output_validate': sample_output_validate,
                'custom_sample_input': bool(sample_input),
                'custom_sample_output': bool(sample_output),
                'payload_path': payload_path,
                'payload': payload,
                'preview': preview_text,
                'payload_size_bytes': payload_size_bytes,
                'payload_size_human': _human_size(payload_size_bytes),
                'manual_large_payload': manual_large_payload,
                'preview_bytes_limit': preview_bytes_limit,
                'preview_clipped': preview_clipped,
            }
        )
    return {'path': TESTS_SPEC_REL.as_posix(), 'exists': bool(path.exists() and path.is_file() and (not path.is_symlink())), 'entries': entries, 'rows': rows, 'summary': summary, 'total': len(entries), 'shown': len(rows), 'truncated': truncated}

def _tests_spec_status_context(workspace: Path) -> dict:
    limits = _C.snapshot()
    try:
        entries, _path = read_tests_spec(
            workspace,
            document_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
        )
    except ValueError:
        return {'mode': 'invalid', 'display': 'invalid', 'total': 0, 'manual': 0, 'gen': 0, 'sample': 0}
    summary = summarize_tests_spec(entries)
    total = summary['total']
    manual = summary['manual']
    gen = summary['gen']
    sample = summary['sample']
    if total <= 0:
        return {'mode': 'empty', 'display': 'empty', 'total': 0, 'manual': 0, 'gen': 0, 'sample': 0}
    return {'mode': 'ready', 'display': f'{total} ({count_label(sample, "sample")})', 'total': total, 'manual': manual, 'gen': gen, 'sample': sample}

def _list_sources_with_extensions(workspace: Path, folder: str, extensions: set[str], limit: int=64) -> tuple[list[str], bool]:
    base = workspace / folder
    try:
        if not base.exists() or not base.is_dir() or base.is_symlink():
            return ([], False)
    except OSError:
        return ([], False)
    names: list[str] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = entry.name
                if Path(name).suffix.lower() not in extensions:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(f'{folder}/{name}')
    except OSError:
        return ([], False)
    names.sort()
    truncated = len(names) > int(limit)
    if truncated:
        names = names[:int(limit)]
    return (names, truncated)

def solution_metadata_entry(workspace: Path, source_rel: str) -> dict:
    source_path = source_rel
    desc_path = desc_rel_path_for_source(source_path)
    desc_exists = workspace_rel_file_exists(workspace, desc_path)
    expected = 'unknown'
    note = ''
    errors: list[str] = []
    origin = 'missing'
    if desc_exists:
        try:
            desc_abs = safe_workspace_path(workspace, desc_path)
            desc_text, _ = read_text_safe_limited(desc_abs, _C.SOLUTION_NOTE_CHAR_LIMIT * 8)
            parsed = parse_solution_desc(desc_text)
            expected = parsed['expected_behavior']
            note = parsed['note']
            origin = 'metadata'
        except Exception as exc:
            errors = [str(exc)]
            origin = 'invalid'
    else:
        errors = [f'{desc_path}: required descriptor is missing']
    note_preview = note
    if len(note_preview) > 160:
        note_preview = note_preview[:157] + '...'
    return {'source_path': source_path, 'file_name': Path(source_path).name, 'expected_behavior': expected, 'expected_behavior_label': expected_behavior_label(expected), 'note': note, 'note_preview': note_preview, 'desc_path': desc_path, 'desc_exists': desc_exists, 'desc_origin': origin, 'desc_errors': errors, 'is_accepted': expected == 'accepted'}

def list_solution_entries(workspace: Path) -> tuple[list[dict], bool]:
    sources, truncated = list_solution_sources(workspace, limit=_C.SOLUTION_LIST_LIMIT)
    entries = [solution_metadata_entry(workspace, rel) for rel in sources]
    return (entries, truncated)

def resolve_build_accepted_solution_source(workspace: Path) -> str:
    build_cfg, _ = read_build_config(workspace)
    return build_cfg.get('accepted_solution_source', '')

def _solutions_status_context(workspace: Path) -> dict:
    entries, truncated = list_solution_entries(workspace)
    total = len(entries)
    accepted_source = resolve_build_accepted_solution_source(workspace)
    accepted_exists = bool(accepted_source) and workspace_rel_file_exists(workspace, accepted_source)
    if truncated:
        count_display = f'{total}+ {"file" if total == 1 else "files"}'
    elif total == 1:
        count_display = '1 file'
    else:
        count_display = f'{total} files'
    if accepted_exists:
        mode = 'ready'
        display = f'{accepted_source} ({count_display})'
    elif total > 0:
        mode = 'missing-main'
        display = f'main missing ({count_display})'
    else:
        mode = 'missing'
        display = 'missing'
    return {'mode': mode, 'display': display, 'accepted_source': accepted_source, 'accepted_exists': accepted_exists, 'count': total, 'count_display': count_display, 'truncated': bool(truncated)}

def run_solution_options_context(workspace: Path) -> tuple[list[dict], str, bool]:
    entries, truncated = list_solution_entries(workspace)
    default_path = resolve_build_accepted_solution_source(workspace)
    if default_path and (not any((row['source_path'] == default_path) for row in entries)):
        default_path = ''
    options: list[dict] = []
    for row in entries:
        path = row['source_path']
        if not path:
            continue
        behavior = row['expected_behavior_label']
        label = path if not behavior else f'{path} ({behavior})'
        expected_behavior = row['expected_behavior']
        options.append({'path': path, 'label': label, 'is_accepted': expected_behavior == 'accepted', 'expected_behavior': normalize_expected_behavior(expected_behavior)})
    return (options, default_path, bool(truncated))

def _tests_meta_text_field(item: dict[str, object], key: str) -> str:
    value = item.get(key, '')
    if value is None:
        return ''
    return str(value)

def _run_test_options_from_verification(problem: str, verification_id: str, limit: int) -> tuple[list[dict], bool]:
    options: list[dict] = []
    truncated = False
    try:
        root = artifact_root(problem, verification_id)
    except HTTPException:
        return (options, truncated)
    tests_dir = root / 'tests'
    try:
        if not tests_dir.exists() or not tests_dir.is_dir() or tests_dir.is_symlink():
            return (options, truncated)
    except OSError:
        return (options, truncated)
    names: list[str] = []
    try:
        with os.scandir(tests_dir) as entries:
            for entry in entries:
                name = entry.name
                if not _K.RUN_TEST_NAME_RE.fullmatch(name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(name)
    except OSError:
        return (options, truncated)
    names.sort()
    cap = max(1, int(limit))
    truncated = len(names) > cap
    names = names[:cap]
    tests_meta_by_name: dict[str, dict[str, str | bool]] = {}
    tests_meta_path = root / 'logs' / 'tests_meta.json'
    try:
        if tests_meta_path.exists() and tests_meta_path.is_file() and (not tests_meta_path.is_symlink()):
            tests_meta_text, _ = read_text_safe_limited(tests_meta_path, _C.UI_JSON_CHAR_LIMIT)
            payload = json.loads(tests_meta_text)
            for item in payload:
                index = coerce_int(item.get('index'), 0, 1, 10 ** 7)
                if index <= 0:
                    continue
                tests_meta_by_name[f'{index:03d}.in'] = {
                    'id': _tests_meta_text_field(item, 'id'),
                    'kind': _tests_meta_text_field(item, 'kind'),
                    'sample': bool(item.get('sample')),
                    'desc': _tests_meta_text_field(item, 'desc'),
                }
    except Exception:
        tests_meta_by_name = {}
    for name in names:
        item = tests_meta_by_name.get(name)
        parts: list[str] = []
        if item is not None:
            test_id = item['id']
            if test_id:
                parts.append(f'id={test_id}')
            kind = item['kind']
            if kind in {'manual', 'gen'}:
                parts.append(kind)
            if item['sample']:
                parts.append('sample')
            desc = item['desc']
            if desc and desc not in {'manual', 'gen'}:
                parts.append(desc)
        suffix = f" ({'; '.join(parts)})" if parts else ''
        options.append({'name': name, 'label': f'{name}{suffix}'})
    return (options, truncated)

def _run_test_options_from_spec(workspace: Path, limit: int) -> tuple[list[dict], bool]:
    limits = _C.snapshot()
    options: list[dict] = []
    try:
        entries, _ = read_tests_spec(
            workspace,
            document_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
        )
    except Exception:
        return (options, False)
    cap = max(1, int(limit))
    truncated = len(entries) > cap
    for idx, row in enumerate(entries[:cap], start=1):
        name = f'{idx:03d}.in'
        parts: list[str] = []
        test_id = row['id']
        if test_id:
            parts.append(f'id={test_id}')
        kind = row['kind']
        if kind in {'manual', 'gen'}:
            parts.append(kind)
        if row['sample']:
            parts.append('sample')
        suffix = f" ({'; '.join(parts)})" if parts else ''
        options.append({'name': name, 'label': f'{name}{suffix}'})
    return (options, truncated)

def run_test_options_context(problem: str, workspace: Path, active_verification: dict | None) -> tuple[list[dict], bool, str]:
    verification_id = ''
    if active_verification is not None:
        verification_id = active_verification['id']
    if verification_id:
        build_options, build_truncated = _run_test_options_from_verification(problem, verification_id, limit=_C.RUN_TEST_SELECTOR_LIMIT)
        if build_options:
            return (build_options, build_truncated, f'verification {verification_id}')
    spec_options, spec_truncated = _run_test_options_from_spec(workspace, limit=_C.RUN_TEST_SELECTOR_LIMIT)
    if spec_options:
        return (spec_options, spec_truncated, 'tests/spec.json')
    return ([], False, '')

def _human_size(num_bytes: int) -> str:
    size = max(0, int(num_bytes))
    if size < 1024:
        return f'{size} B'
    value = float(size)
    for unit in ('KB', 'MB', 'GB'):
        value /= 1024.0
        if value < 1024.0 or unit == 'GB':
            return f'{value:.1f} {unit}'
    return f'{size} B'

def _inline_text_preview(raw: str, max_chars: int, max_lines: int) -> str:
    text = raw.replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.rstrip('\n\r') for line in text.splitlines()]
    clipped_by_lines = False
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[:max_lines]
        clipped_by_lines = True
    preview = '\n'.join(lines).strip()
    if not preview:
        preview = '(empty)'
    clipped_by_chars = len(preview) > max_chars
    if clipped_by_chars:
        preview = preview[:max_chars - 3].rstrip() + '...'
    elif clipped_by_lines:
        preview += ' ...'
    return preview

def generator_sources_from_build_cfg(build_cfg: BuildConfig) -> list[str]:
    return list(build_cfg['generator_sources'])
