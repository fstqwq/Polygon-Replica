import app.main_constant as _K
import os
import time
from pathlib import Path
from typing import TypedDict
from fastapi import HTTPException
from app.impl.runtime.dependency import runtime
from app.main_util import (
    normalize_workspace_rel_path,
    safe_workspace_path,
)
from app.service.problem.build_config import (
    BuildConfig,
    dumps_build_config,
    load_build_config,
)
from app.service.repository.revision import workspace_upstream_revision_display
from app.service.access.policy import access_role


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
    rows = runtime().workspace_service.participating_problem_rows(uid, limit=cap)
    items: list[dict] = []
    for row in rows:
        role = access_role(row['role'])
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

def normalize_contest_slug_required(value: str) -> str:
    slug = value.strip()
    if not _K.CONTEST_IDENT_RE.fullmatch(slug):
        raise ValueError('invalid contest slug')
    return slug

def normalize_contest_title_required(value: str) -> str:
    title = value.strip()
    if not title:
        raise ValueError('contest title is required')
    if len(title) > runtime().config_values.CONTEST_TITLE_MAX_LEN:
        raise ValueError(f'contest title is too long (max {runtime().config_values.CONTEST_TITLE_MAX_LEN})')
    return title

def user_contests_overview(user_id: int, limit: int) -> list[dict]:
    uid = int(user_id)
    cap = max(1, int(limit))
    return runtime().contest_service.user_contests_overview(uid, limit=cap)

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
    return read_text_safe_limited(file_path, runtime().config_values.TEXTAREA_MAX_BYTES)

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

def tests_spec_editor_context(workspace: Path, limit: int) -> dict:
    return runtime().problem_source_query_service.tests_spec_editor(
        workspace,
        limit,
    )

def _tests_spec_status_context(workspace: Path) -> dict:
    return runtime().problem_source_query_service.tests_spec_status(workspace)

def solution_metadata_entry(workspace: Path, source_rel: str) -> dict:
    return runtime().problem_source_query_service.solution_entry(
        workspace,
        source_rel,
    )

def list_solution_entries(workspace: Path) -> tuple[list[dict], bool]:
    return runtime().problem_source_query_service.solution_entries(workspace)

def resolve_build_accepted_solution_source(workspace: Path) -> str:
    return runtime().problem_source_query_service.accepted_solution_source(workspace)

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
    return runtime().problem_source_query_service.run_solution_options(workspace)


def run_test_options_context(workspace: Path) -> tuple[list[dict], bool, str]:
    return runtime().problem_source_query_service.run_test_options(workspace)
