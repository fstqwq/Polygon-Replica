from pathlib import Path

from app.impl.runtime.dependency import runtime
from app.service.problem.build_config import BuildConfig
from app.service.problem.test_spec import (
    generator_source_paths,
    parse_gen_command_tokens,
    resolve_generator_source,
)
from app.impl.workspace.context_operation import (
    read_build_config,
    workspace_rel_file_exists,
)
from app.impl.workspace.test_spec import read_tests_spec, tests_spec_read_payload



def _configured_component_source(
    workspace: Path,
    build_cfg: BuildConfig,
    config_key: str,
    default_source: str,
) -> tuple[str, bool]:
    configured = build_cfg.get(config_key)
    if configured is None:
        return default_source, False
    return configured, workspace_rel_file_exists(workspace, configured)

def _source_basename_label(path: str) -> str:
    raw = path.strip()
    if not raw:
        return ''
    name = Path(raw).name.strip()
    return name or raw

def _generator_reference_counts(
    workspace: Path,
    source_catalog: tuple[str, ...],
) -> dict[str, int]:
    limits = runtime().config_values.snapshot()
    counts = {path: 0 for path in source_catalog}
    if not source_catalog:
        return counts
    try:
        entries, _ = read_tests_spec(
            workspace,
            document_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
        )
    except Exception:
        return counts
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
        try:
            resolved = resolve_generator_source(tokens[0], source_catalog)
        except ValueError:
            continue
        counts[resolved] = counts.get(resolved, 0) + 1
    return counts

def generator_status_context(workspace: Path) -> dict:
    try:
        all_generator_sources = tuple(generator_source_paths(workspace))
    except ValueError:
        all_generator_sources = ()
    generator_candidates_truncated = len(all_generator_sources) > 64
    generator_candidates = all_generator_sources[:64]
    all_sources = generator_candidates
    repo_source = all_sources[0] if all_sources else 'generators/generator.cpp'
    repo_exists = workspace_rel_file_exists(workspace, repo_source)
    reference_counts = _generator_reference_counts(workspace, all_generator_sources)
    has_declared_or_discovered = bool(all_sources)
    source_rows: list[dict[str, object]] = []
    for rel in all_sources:
        exists = workspace_rel_file_exists(workspace, rel)
        if not exists:
            continue
        source_rows.append({
            'path': rel,
            'exists': exists,
            'reference_count': reference_counts.get(rel, 0),
        })
    if repo_exists:
        mode = 'repository'
        display = _source_basename_label(repo_source)
    elif has_declared_or_discovered:
        mode = 'missing'
        display = 'missing'
    else:
        mode = 'empty'
        display = '0 files'
    return {
        'mode': mode,
        'display': display,
        'repo_source': repo_source,
        'repo_source_exists': bool(repo_exists),
        'source_rows': source_rows,
        'source_rows_truncated': bool(generator_candidates_truncated),
    }

def validator_status_context(
    workspace: Path,
    build_cfg: BuildConfig | None = None,
) -> dict:
    if build_cfg is None:
        build_cfg, _ = read_build_config(workspace)
    repo_source, repo_exists = _configured_component_source(
        workspace,
        build_cfg,
        'validator_source',
        'validators/validator.cpp',
    )
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

def interactor_status_context(
    workspace: Path,
    build_cfg: BuildConfig | None = None,
) -> dict:
    if build_cfg is None:
        build_cfg, _ = read_build_config(workspace)
    repo_source, repo_exists = _configured_component_source(
        workspace,
        build_cfg,
        'interactor_source',
        'interactors/interactor.cpp',
    )
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

def checker_status_context(
    workspace: Path,
    build_cfg: BuildConfig | None = None,
) -> dict:
    if build_cfg is None:
        build_cfg, _ = read_build_config(workspace)
    repo_source, repo_exists = _configured_component_source(
        workspace,
        build_cfg,
        'checker_source',
        'checkers/checker.cpp',
    )
    standard_name = ''
    expected_standard_name = ''
    standard_warning = ''
    if repo_exists:
        from app.service.verification.standard_checker import detect_standard_checker, standard_checker_hash_map
        from app.service.platform.workspace_path import safe_workspace_path
        try:
            abs_path = safe_workspace_path(workspace, repo_source)
            detected = detect_standard_checker(abs_path)
            if detected:
                standard_name = f'std::{detected}'
            standard_filenames = set(standard_checker_hash_map().values())
            repo_filename = Path(repo_source).name
            if repo_filename in standard_filenames:
                expected_standard_name = f'std::{repo_filename}'
            if expected_standard_name and standard_name != expected_standard_name:
                if standard_name:
                    standard_warning = f'File name matches {expected_standard_name}, but content matches {standard_name}.'
                else:
                    standard_warning = f'File name matches {expected_standard_name}, but content differs.'
        except Exception:
            pass
    return {
        'mode': 'repository' if repo_exists else 'missing',
        'display': _source_basename_label(repo_source) if repo_exists else 'missing',
        'standard_checker': standard_name,
        'standard_expected_checker': expected_standard_name,
        'standard_warning': standard_warning,
        'standard_valid': True,
        'repo_source': repo_source,
        'repo_source_exists': bool(repo_exists),
    }
