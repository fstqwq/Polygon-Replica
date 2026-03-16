from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from app.impl.runtime.config import config

from app.main_util import (
    normalize_workspace_rel_path,
    safe_workspace_path,
)
from app.service.problem.test_spec import parse_gen_command_tokens

from .context_operation import (
    _canonical_standard_checker_name,
    dedupe_preserve_order,
    generator_sources_from_build_cfg,
    _list_cpp_sources,
    read_build_config,
    resolve_standard_checker_path,
    workspace_rel_file_exists,
)
from .test_spec import read_tests_spec, tests_spec_read_payload

_C = config.constants
def _checker_standard_from_build_cfg(build_cfg: dict[str, object]) -> str:
    raw = build_cfg.get('checker_standard') or ''
    if not raw:
        return ''
    try:
        return _canonical_standard_checker_name(raw)
    except ValueError:
        return ''

def _component_repo_source_from_build_cfg(workspace: Path, build_cfg: dict, config_key: str, folder: str, default_filename: str) -> tuple[str, bool]:
    configured = normalize_workspace_rel_path(build_cfg.get(config_key))
    if configured:
        try:
            configured_abs = safe_workspace_path(workspace, configured)
            if configured_abs.exists() and configured_abs.is_file():
                return (configured, True)
        except HTTPException:
            pass
    default_rel = f'{folder}/{default_filename}'
    try:
        default_abs = safe_workspace_path(workspace, default_rel)
        if default_abs.exists() and default_abs.is_file():
            return (default_rel, True)
    except HTTPException:
        pass
    folder_path = workspace / folder
    names: list[str] = []
    try:
        if folder_path.exists() and folder_path.is_dir() and (not folder_path.is_symlink()):
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    name = entry.name
                    if not name.endswith('.cpp'):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    names.append(name)
    except OSError:
        names = []
    if not names:
        return (default_rel, False)
    names.sort()
    return (f'{folder}/{names[0]}', True)

def _source_basename_label(path: str) -> str:
    raw = path.strip()
    if not raw:
        return ''
    name = Path(raw).name.strip()
    return name or raw

def _resolve_generator_source_from_token_for_nav(token: str, source_paths: list[str]) -> str:
    raw = token.strip().replace('\\', '/')
    while raw.startswith('./'):
        raw = raw[2:]
    if not raw:
        return ''
    if any((part == '..' for part in raw.split('/'))):
        return ''
    normalized_sources = dedupe_preserve_order(
        [path.strip().replace('\\', '/') for path in source_paths if path.strip()]
    )
    if not normalized_sources:
        return ''
    source_set = set(normalized_sources)
    token_path = Path(raw)
    suffix = token_path.suffix.lower()
    candidates: list[str] = []
    if raw.startswith('generators/'):
        if suffix in _C.CPP_SOURCE_EXTENSIONS:
            candidates.append(raw)
        else:
            for ext in _C.CPP_SOURCE_EXTENSIONS:
                candidates.append(f'{raw}{ext}')
    elif suffix in _C.CPP_SOURCE_EXTENSIONS:
        candidates.append(f'generators/{raw}')
    else:
        candidates.append(f'generators/{raw}')
        for ext in _C.CPP_SOURCE_EXTENSIONS:
            candidates.append(f'generators/{raw}{ext}')
    seen: set[str] = set()
    for rel in candidates:
        rel_key = rel.strip()
        if not rel_key or rel_key in seen:
            continue
        seen.add(rel_key)
        if rel_key in source_set:
            return rel_key
    name = token_path.name
    if suffix in _C.CPP_SOURCE_EXTENSIONS:
        exact = [rel for rel in normalized_sources if Path(rel).name == name]
        if len(exact) == 1:
            return exact[0]
    else:
        stem_matches = [rel for rel in normalized_sources if Path(rel).stem == token_path.name]
        if len(stem_matches) == 1:
            return stem_matches[0]
    return ''

def _count_used_configured_generators(workspace: Path, configured_sources: list[str], source_paths: list[str]) -> int:
    configured = dedupe_preserve_order(
        [path.strip().replace('\\', '/') for path in configured_sources if path.strip()]
    )
    if not configured:
        return 0
    configured_set = set(configured)
    source_catalog = dedupe_preserve_order(
        [*(path.strip().replace('\\', '/') for path in source_paths if path.strip()), *configured]
    )
    try:
        entries, _ = read_tests_spec(workspace)
    except Exception:
        return 0
    used: set[str] = set()
    for row in entries:
        if row.get('kind') != 'gen':
            continue
        try:
            command = tests_spec_read_payload(workspace, row)
        except Exception:
            command = ''
        if not command:
            continue
        try:
            tokens = parse_gen_command_tokens(command)
        except Exception:
            continue
        resolved = _resolve_generator_source_from_token_for_nav(tokens[0], source_catalog)
        if resolved and resolved in configured_set:
            used.add(resolved)
    return len(used)

def generator_status_context(workspace: Path) -> dict:
    build_cfg, _ = read_build_config(workspace)
    configured_sources = generator_sources_from_build_cfg(build_cfg)
    generator_candidates, generator_candidates_truncated = _list_cpp_sources(workspace, 'generators')
    repo_source = ''
    repo_exists = False
    for rel in configured_sources:
        if workspace_rel_file_exists(workspace, rel):
            repo_source = rel
            repo_exists = True
            break
    if not repo_source:
        for rel in generator_candidates:
            if workspace_rel_file_exists(workspace, rel):
                repo_source = rel
                repo_exists = True
                break
    if not repo_source and configured_sources:
        repo_source = configured_sources[0]
    if not repo_source:
        repo_source = 'generators/generator.cpp'
        repo_exists = workspace_rel_file_exists(workspace, repo_source)
    else:
        repo_exists = workspace_rel_file_exists(workspace, repo_source)
    configured_set = set(configured_sources)
    all_sources = dedupe_preserve_order([*configured_sources, *generator_candidates])
    has_declared_or_discovered = bool(all_sources)
    source_rows: list[dict[str, object]] = []
    for rel in all_sources:
        exists = workspace_rel_file_exists(workspace, rel)
        if not exists:
            continue
        source_rows.append({'path': rel, 'exists': exists, 'configured': rel in configured_set})
    if repo_exists:
        mode = 'repository'
        display = _source_basename_label(repo_source)
    elif has_declared_or_discovered:
        mode = 'missing'
        display = 'missing'
    else:
        mode = 'empty'
        display = '0 files'
    return {'mode': mode, 'display': display, 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists), 'configured_sources': source_rows, 'source_rows_truncated': bool(generator_candidates_truncated)}

def validator_status_context(workspace: Path) -> dict:
    build_cfg, _ = read_build_config(workspace)
    repo_source, repo_exists = _component_repo_source_from_build_cfg(workspace, build_cfg, 'validator_source', 'validators', 'validator.cpp')
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

def interactor_status_context(workspace: Path) -> dict:
    build_cfg, _ = read_build_config(workspace)
    repo_source, repo_exists = _component_repo_source_from_build_cfg(workspace, build_cfg, 'interactor_source', 'interactors', 'interactor.cpp')
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

def checker_status_context(workspace: Path) -> dict:
    build_cfg, _ = read_build_config(workspace)
    standard = _checker_standard_from_build_cfg(build_cfg)
    if standard:
        valid = True
        try:
            resolve_standard_checker_path(standard)
        except ValueError:
            valid = False
        return {'mode': 'standard', 'display': standard, 'standard_checker': standard, 'standard_valid': valid, 'repo_source': '', 'repo_source_exists': False}
    repo_source, repo_exists = _component_repo_source_from_build_cfg(workspace, build_cfg, 'checker_source', 'checkers', 'checker.cpp')
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'standard_checker': '', 'standard_valid': True, 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}




