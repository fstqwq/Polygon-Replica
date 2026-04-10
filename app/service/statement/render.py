from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from app.service.problem.test_spec import TESTS_SPEC_REL, load_tests_spec, payload_rel_path_for_test
from app.service.statement.constant import (
    DEFAULT_OLYMP_STY,
    DEFAULT_PROBLEM_TITLE,
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    DEFAULT_STATEMENT_TEMPLATE,
    STATEMENT_DIR,
    STATEMENT_MAIN_REL,
    STATEMENT_PROBLEM_REL,
    STATEMENT_RENDERED_DIR_REL,
    STATEMENT_SECTIONS_DIR,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    TESTS_ANSWERS_DIR_REL,
    _read_required_text,
)
from app.service.statement.context import pick_statement_language
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


def _safe_read_json(path: Path) -> dict:
    try:
        if path.exists() and path.is_file() and (not path.is_symlink()):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        return {}
    return {}


def _statement_section_text(workspace: Path, language: str, section_name: str, fallback: str = "") -> str:
    return _safe_read_text(workspace / (STATEMENT_SECTIONS_DIR / language / section_name), fallback)


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


def _collect_sample_tests(workspace: Path, rendered_lang_root: Path) -> list[dict[str, str]]:
    spec_path = workspace / TESTS_SPEC_REL
    try:
        entries = load_tests_spec(spec_path)
    except Exception as exc:
        raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
    rows: list[dict[str, str]] = []
    for index, entry in enumerate(entries, start=1):
        if not bool(entry.get("sample")):
            continue
        kind = entry["kind"]
        if kind not in {"manual", "gen"}:
            raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}: {kind}")
        test_id = entry["id"]
        sample_input_text = entry["sample_input"]
        sample_output_text = entry["sample_output"]
        try:
            input_rel = Path(payload_rel_path_for_test(test_id, kind))
        except Exception:
            continue
        answer_rel = TESTS_ANSWERS_DIR_REL / f"{test_id}.ans"
        input_source = None if sample_input_text else _safe_workspace_regular_file(workspace, input_rel)
        answer_source = None if sample_output_text else _safe_workspace_regular_file(workspace, answer_rel)
        if (not sample_input_text) and (input_source is None):
            continue
        if (not sample_output_text) and (answer_source is None):
            continue
        input_name = f"sample.{test_id}.in"
        output_name = f"sample.{test_id}.ans"
        input_target = rendered_lang_root / input_name
        output_target = rendered_lang_root / output_name
        try:
            if sample_input_text:
                input_target.write_text(sample_input_text, encoding="utf-8")
            else:
                shutil.copy2(input_source, input_target)
            if sample_output_text:
                output_target.write_text(sample_output_text, encoding="utf-8")
            else:
                shutil.copy2(answer_source, output_target)
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
) -> dict[str, object]:
    cfg = _safe_read_json(workspace / "config" / "problem.json")
    input_file = "standard input"
    output_file = "standard output"
    time_limit_ms_obj = cfg.get("time_limit_ms", 2000)
    try:
        time_limit_ms = int(time_limit_ms_obj)
    except Exception:
        time_limit_ms = 2000
    memory_limit_mb_obj = cfg.get("memory_limit_mb", 1024)
    try:
        memory_limit_mb = int(memory_limit_mb_obj)
    except Exception:
        memory_limit_mb = 1024
    title_from_section = _statement_section_text(workspace, language, "name.tex", fallback="").strip()
    resolved_title = str(problem_title or "").strip() or title_from_section or DEFAULT_PROBLEM_TITLE
    return {
        "name": resolved_title,
        "inputFile": input_file,
        "outputFile": output_file,
        "timeLimit": time_limit_ms,
        "memoryLimit": max(1, memory_limit_mb) * 1024 * 1024,
        "legend": _statement_section_text(workspace, language, "legend.tex", fallback=""),
        "input": _statement_section_text(workspace, language, "input.tex", fallback=""),
        "output": _statement_section_text(workspace, language, "output.tex", fallback=""),
        "interaction": _statement_section_text(workspace, language, "interaction.tex", fallback=""),
        "scoring": _statement_section_text(workspace, language, "scoring.tex", fallback=""),
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


def _seed_polygon_statement_sources(workspace: Path) -> None:
    statement_root = workspace / STATEMENT_DIR
    sections_root = workspace / STATEMENT_SECTIONS_DIR / "english"
    statement_root.mkdir(parents=True, exist_ok=True)
    sections_root.mkdir(parents=True, exist_ok=True)
    if not (workspace / STATEMENT_TEMPLATE_REL).exists():
        (workspace / STATEMENT_TEMPLATE_REL).write_text(DEFAULT_STATEMENT_TEMPLATE, encoding="utf-8")
    if not (workspace / STATEMENT_PROBLEM_REL).exists():
        (workspace / STATEMENT_PROBLEM_REL).write_text(DEFAULT_STATEMENT_PROBLEM_TEMPLATE, encoding="utf-8")
    if not (workspace / STATEMENT_STYLE_REL).exists():
        (workspace / STATEMENT_STYLE_REL).write_text(default_olymp_sty_text(), encoding="utf-8")
    defaults = {
        "name.tex": DEFAULT_PROBLEM_TITLE + "\n",
        "legend.tex": "",
        "input.tex": "",
        "output.tex": "",
        "notes.tex": "",
    }
    for rel, content in defaults.items():
        path = sections_root / rel
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def _render_polygon_statement(workspace: Path, statement_root: Path, problem_title: str | None = None) -> Path:
    template_text = _read_required_text(
        workspace / STATEMENT_TEMPLATE_REL,
        label=f"statement template ({STATEMENT_TEMPLATE_REL.as_posix()})",
    )
    problem_template_text = _safe_read_text(workspace / STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
    _read_required_text(
        workspace / STATEMENT_STYLE_REL,
        label=f"statement olymp style ({STATEMENT_STYLE_REL.as_posix()})",
    )

    language = pick_statement_language(workspace)
    rendered_lang_root = workspace / STATEMENT_RENDERED_DIR_REL / language
    shutil.rmtree(rendered_lang_root, ignore_errors=True)
    rendered_lang_root.mkdir(parents=True, exist_ok=True)
    _copy_tree_without_symlinks(workspace / STATEMENT_SECTIONS_DIR / language, rendered_lang_root)
    sample_tests = _collect_sample_tests(workspace, rendered_lang_root)
    problem_ctx = _problem_context_for_language(
        workspace,
        language,
        problem_title,
        sample_tests=sample_tests,
    )
    rendered_problem_tex = render_ftl_template(
        problem_template_text,
        {
            "problem": problem_ctx,
            "language": language,
            "contest": {"name": "", "location": "", "date": "", "language": language},
            "shortProblemTitle": False,
            "providedStatementsCommands": [],
            "statements": [],
        },
    )
    (rendered_lang_root / "problem.tex").write_text(rendered_problem_tex, encoding="utf-8")

    rendered_main = render_ftl_template(
        template_text,
        {
            "contest": {"name": "", "location": "", "date": "", "language": language},
            "language": language,
            "shortProblemTitle": True,
            "providedStatementsCommands": [],
            "statements": [{"path": f"rendered/{language}/", "file": "problem.tex"}],
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
) -> Path:
    template_text = _safe_read_text(workspace / STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
    _read_required_text(
        workspace / STATEMENT_STYLE_REL,
        label=f"statement olymp style ({STATEMENT_STYLE_REL.as_posix()})",
    )
    safe_language = str(language or "").strip()
    if not safe_language:
        raise RuntimeError("statement language is required")
    language_root = workspace / STATEMENT_SECTIONS_DIR / safe_language
    if not language_root.exists() or not language_root.is_dir() or language_root.is_symlink():
        raise RuntimeError(f"missing statement sections for language: {safe_language}")
    shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree_without_symlinks(language_root, target_dir)
    sample_tests = _collect_sample_tests(workspace, target_dir)
    problem_ctx = _problem_context_for_language(
        workspace,
        safe_language,
        problem_title,
        sample_tests=sample_tests,
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


def render_statement_main(statement_root: Path, problem_title: str | None = None) -> Path:
    workspace = statement_root.parent
    return _render_polygon_statement(workspace, statement_root, problem_title=problem_title)
