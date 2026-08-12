from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.main_util import problem_slug_leaf
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    load_tests_spec,
    payload_rel_path_for_test,
    read_statement_sample_text,
)
from app.service.problem.runtime_config import ProblemConfigLimits, load_problem_config
from app.service.statement.constant import (
    DEFAULT_OLYMP_STY,
    DEFAULT_PROBLEM_TITLE,
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    DEFAULT_STATEMENT_TEMPLATE,
    STATEMENT_ASSETS_DIR,
    STATEMENT_CANONICAL_SECTION_FILES,
    STATEMENT_DIR,
    STATEMENT_MAIN_REL,
    STATEMENT_PROBLEM_REL,
    STATEMENT_RENDERED_DIR_REL,
    STATEMENT_SECTIONS_DIR,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    _read_required_text,
)
from app.service.statement.context import normalize_statement_language
from app.service.statement.ftl.renderer import render_ftl_template


def default_olymp_sty_text() -> str:
    return DEFAULT_OLYMP_STY


def _safe_read_text(path: Path, fallback: str) -> str:
    try:
        if path.exists() and path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return fallback


def _statement_section_text(workspace: Path, language: str, section_name: str, fallback: str = "") -> str:
    return _safe_read_text(workspace / (STATEMENT_SECTIONS_DIR / language / section_name), fallback)


def default_statement_title_for_workspace(workspace: Path) -> str:
    return problem_slug_leaf(workspace.name) or DEFAULT_PROBLEM_TITLE


def statement_title_for_language(workspace: Path, language: str, fallback_title: str | None = None) -> str:
    title_from_section = _statement_section_text(workspace, language, "name.tex", fallback="").strip()
    fallback = str(fallback_title or "").strip() or default_statement_title_for_workspace(workspace)
    return title_from_section or fallback


def _safe_workspace_regular_file(workspace: Path, rel: Path) -> Path | None:
    try:
        workspace_resolved = workspace.resolve()
        candidate = (workspace / rel).resolve()
    except OSError:
        return None
    if workspace_resolved not in candidate.parents:
        return None
    try:
        if candidate.is_symlink() or not candidate.exists() or not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate


def _collect_sample_tests(
    workspace: Path,
    rendered_lang_root: Path,
    *,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
) -> list[dict[str, str]]:
    spec_path = workspace / TESTS_SPEC_REL
    try:
        entries = load_tests_spec(
            spec_path,
            document_max_bytes=tests_spec_max_bytes,
            sample_max_bytes=statement_sample_max_bytes,
        )
    except Exception as exc:
        raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
    rows: list[dict[str, str]] = []
    for index, entry in enumerate(entries, start=1):
        if not entry["sample"]:
            continue
        kind = entry["kind"]
        test_id = entry["id"]
        sample_input_text = entry["sample_input"]
        sample_output_text = entry["sample_output"]
        input_rel = Path(payload_rel_path_for_test(test_id, kind))
        input_source = None if sample_input_text else _safe_workspace_regular_file(workspace, input_rel)
        if (not sample_input_text) and (input_source is None):
            continue
        if not sample_output_text:
            continue
        if input_source is not None:
            try:
                sample_input_text = read_statement_sample_text(
                    input_source,
                    max_bytes=(
                        statement_sample_max_bytes
                        - len(sample_output_text.encode("utf-8"))
                    ),
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"invalid statement sample at tests/spec.json entry {index}: {exc}"
                ) from exc
        input_name = f"sample.{test_id}.in"
        output_name = f"sample.{test_id}.ans"
        input_target = rendered_lang_root / input_name
        output_target = rendered_lang_root / output_name
        try:
            input_target.write_text(sample_input_text, encoding="utf-8")
            if sample_output_text:
                output_target.write_text(sample_output_text, encoding="utf-8")
        except OSError:
            continue
        rows.append({"inputFile": input_name, "outputFile": output_name})
    return rows


def _problem_context_for_language(
    workspace: Path,
    language: str,
    problem_title: str | None,
    *,
    sample_tests: list[dict[str, str]] | None = None,
    problem_limits: ProblemConfigLimits,
) -> dict[str, object]:
    cfg = load_problem_config(workspace, limits=problem_limits)
    input_file = "standard input"
    output_file = "standard output"
    time_limit_ms = cfg["time_limit_ms"]
    memory_limit_mb = cfg["memory_limit_mb"]
    resolved_title = statement_title_for_language(workspace, language, problem_title)
    return {
        "name": resolved_title,
        "inputFile": input_file,
        "outputFile": output_file,
        "timeLimit": time_limit_ms,
        "memoryLimit": memory_limit_mb * 1024 * 1024,
        "legend": _statement_section_text(workspace, language, "legend.tex", fallback=""),
        "input": _statement_section_text(workspace, language, "input.tex", fallback=""),
        "output": _statement_section_text(workspace, language, "output.tex", fallback=""),
        "interaction": _statement_section_text(workspace, language, "interaction.tex", fallback=""),
        "notes": _statement_section_text(workspace, language, "notes.tex", fallback=""),
        "sampleTests": list(sample_tests or []),
    }


def _copy_tree_without_symlinks(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir() or src.is_symlink():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
        current = Path(dirpath)
        safe_dirs: list[str] = []
        for name in dirnames:
            child = current / name
            if child.is_symlink():
                continue
            safe_dirs.append(name)
            rel = child.relative_to(src)
            (dst / rel).mkdir(parents=True, exist_ok=True)
        dirnames[:] = safe_dirs
        for name in filenames:
            source_file = current / name
            if source_file.is_symlink() or not source_file.is_file():
                continue
            rel = source_file.relative_to(src)
            target_file = dst / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)


def _copy_statement_shared_assets(workspace: Path, target_dir: Path) -> None:
    _copy_tree_without_symlinks(workspace / STATEMENT_ASSETS_DIR, target_dir)


def _copy_statement_language_sources(workspace: Path, language: str, target_dir: Path) -> None:
    language_root = workspace / STATEMENT_SECTIONS_DIR / language
    if not language_root.exists() or not language_root.is_dir() or language_root.is_symlink():
        raise RuntimeError(f"missing statement sections for language: {language}")
    target_dir.mkdir(parents=True, exist_ok=True)
    for file_name in sorted(STATEMENT_CANONICAL_SECTION_FILES):
        source_file = language_root / file_name
        if source_file.is_symlink() or (not source_file.exists()) or (not source_file.is_file()):
            continue
        target_file = target_dir / file_name
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)


def _prepare_statement_language_compile_tree(workspace: Path, language: str, target_dir: Path) -> None:
    shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    _copy_statement_shared_assets(workspace, target_dir)
    _copy_statement_language_sources(workspace, language, target_dir)


def _statement_language_seed_defaults(workspace: Path) -> dict[str, str]:
    return {
        "name.tex": default_statement_title_for_workspace(workspace) + "\n",
        "legend.tex": "",
        "input.tex": "",
        "output.tex": "",
        "interaction.tex": "",
        "notes.tex": "",
    }


def ensure_statement_language_sources(workspace: Path, language: str) -> None:
    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise RuntimeError("statement language is required")
    statement_root = workspace / STATEMENT_DIR
    sections_root = workspace / STATEMENT_SECTIONS_DIR / safe_language
    statement_root.mkdir(parents=True, exist_ok=True)
    sections_root.mkdir(parents=True, exist_ok=True)
    if not (workspace / STATEMENT_TEMPLATE_REL).exists():
        (workspace / STATEMENT_TEMPLATE_REL).write_text(DEFAULT_STATEMENT_TEMPLATE, encoding="utf-8")
    if not (workspace / STATEMENT_PROBLEM_REL).exists():
        (workspace / STATEMENT_PROBLEM_REL).write_text(DEFAULT_STATEMENT_PROBLEM_TEMPLATE, encoding="utf-8")
    if not (workspace / STATEMENT_STYLE_REL).exists():
        (workspace / STATEMENT_STYLE_REL).write_text(default_olymp_sty_text(), encoding="utf-8")
    for rel, content in _statement_language_seed_defaults(workspace).items():
        path = sections_root / rel
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def _seed_polygon_statement_sources(workspace: Path) -> None:
    ensure_statement_language_sources(workspace, "english")


def _render_polygon_statement(
    workspace: Path,
    statement_root: Path,
    problem_title: str | None = None,
    *,
    language: str,
    include_sample_tests: bool = True,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> Path:
    template_text = _read_required_text(
        workspace / STATEMENT_TEMPLATE_REL,
        label=f"statement template ({STATEMENT_TEMPLATE_REL.as_posix()})",
    )
    problem_template_text = _safe_read_text(workspace / STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
    _read_required_text(
        workspace / STATEMENT_STYLE_REL,
        label=f"statement olymp style ({STATEMENT_STYLE_REL.as_posix()})",
    )

    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise RuntimeError("statement language is required")
    rendered_lang_root = workspace / STATEMENT_RENDERED_DIR_REL / safe_language
    _prepare_statement_language_compile_tree(workspace, safe_language, rendered_lang_root)
    sample_tests = (
        _collect_sample_tests(
            workspace,
            rendered_lang_root,
            tests_spec_max_bytes=tests_spec_max_bytes,
            statement_sample_max_bytes=statement_sample_max_bytes,
        )
        if include_sample_tests
        else []
    )
    problem_ctx = _problem_context_for_language(
        workspace,
        safe_language,
        problem_title,
        sample_tests=sample_tests,
        problem_limits=problem_limits,
    )
    rendered_problem_tex = render_ftl_template(
        problem_template_text,
        {
            "problem": problem_ctx,
            "language": safe_language,
            "contest": {"name": "", "location": "", "date": "", "language": safe_language},
            "shortProblemTitle": False,
            "providedStatementsCommands": [],
            "statements": [],
        },
    )
    (rendered_lang_root / "problem.tex").write_text(rendered_problem_tex, encoding="utf-8")

    rendered_main = render_ftl_template(
        template_text,
        {
            "contest": {"name": "", "location": "", "date": "", "language": safe_language},
            "language": safe_language,
            "shortProblemTitle": True,
            "providedStatementsCommands": [],
            "statements": [{"path": f"rendered/{safe_language}/", "file": "problem.tex"}],
            "problem": problem_ctx,
        },
    )
    main_path = workspace / STATEMENT_MAIN_REL
    main_path.parent.mkdir(parents=True, exist_ok=True)
    main_path.write_text(rendered_main, encoding="utf-8")
    return main_path


def render_statement_problem_assets_for_language(
    workspace: Path,
    language: str,
    target_dir: Path,
    *,
    problem_title: str | None = None,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> Path:
    template_text = _safe_read_text(workspace / STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
    _read_required_text(
        workspace / STATEMENT_STYLE_REL,
        label=f"statement olymp style ({STATEMENT_STYLE_REL.as_posix()})",
    )
    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise RuntimeError("statement language is required")
    _prepare_statement_language_compile_tree(workspace, safe_language, target_dir)
    sample_tests = _collect_sample_tests(
        workspace,
        target_dir,
        tests_spec_max_bytes=tests_spec_max_bytes,
        statement_sample_max_bytes=statement_sample_max_bytes,
    )
    problem_ctx = _problem_context_for_language(
        workspace,
        safe_language,
        problem_title,
        sample_tests=sample_tests,
        problem_limits=problem_limits,
    )
    rendered_problem_tex = render_ftl_template(
        template_text,
        {
            "problem": problem_ctx,
            "language": safe_language,
            "contest": {"name": "", "location": "", "date": "", "language": safe_language},
            "shortProblemTitle": False,
            "providedStatementsCommands": [],
            "statements": [],
        },
    )
    problem_tex = target_dir / "problem.tex"
    problem_tex.write_text(rendered_problem_tex, encoding="utf-8")
    return problem_tex


def seed_statement_sources(workspace: Path) -> None:
    _seed_polygon_statement_sources(workspace)


def render_statement_main(
    statement_root: Path,
    problem_title: str | None = None,
    *,
    language: str,
    include_sample_tests: bool = True,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> Path:
    workspace = statement_root.parent
    return _render_polygon_statement(
        workspace,
        statement_root,
        problem_title=problem_title,
        language=language,
        include_sample_tests=include_sample_tests,
        tests_spec_max_bytes=tests_spec_max_bytes,
        statement_sample_max_bytes=statement_sample_max_bytes,
        problem_limits=problem_limits,
    )
