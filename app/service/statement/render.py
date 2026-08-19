import os
import shutil
from pathlib import Path, PurePosixPath

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
    DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    DEFAULT_STATEMENT_TEMPLATE,
    STATEMENT_ASSETS_DIR,
    STATEMENT_CANONICAL_SECTION_FILES,
    STATEMENT_DEFAULT_FILES,
    STATEMENT_DIR,
    STATEMENT_EXAMPLES_REL,
    STATEMENT_MAIN_REL,
    STATEMENT_PROBLEM_REL,
    STATEMENT_RENDERED_DIR_REL,
    STATEMENT_SECTIONS_DIR,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    _read_required_text,
)
from app.service.statement.examples import StatementExamplesBundle
from app.service.statement.context import (
    normalize_statement_language,
    pick_statement_language,
)
from app.service.statement.ftl.renderer import render_ftl_template


PROBLEM_TITLE_MAX_LEN = 255


def normalize_problem_title(raw: object, *, fallback_title: str) -> str:
    fallback = str(fallback_title).strip()
    title = ("" if raw is None else str(raw).strip()) or fallback
    if not title:
        raise ValueError("problem title is required")
    if "\n" in title or "\r" in title:
        raise ValueError("problem title must be a single line")
    if len(title) > PROBLEM_TITLE_MAX_LEN:
        raise ValueError(
            f"problem title is too long (max {PROBLEM_TITLE_MAX_LEN})"
        )
    return title


def default_olymp_sty_text() -> str:
    return DEFAULT_OLYMP_STY


def statement_templates_are_default(workspace: Path) -> bool:
    """Return whether the Workspace uses the canonical Statement templates."""

    for rel, expected in STATEMENT_DEFAULT_FILES.items():
        path = workspace / rel
        try:
            if path.is_symlink() or not path.is_file():
                return False
            if path.read_text(encoding="utf-8") != expected:
                return False
        except OSError:
            return False
    examples_path = workspace / STATEMENT_EXAMPLES_REL
    try:
        return not examples_path.exists()
    except OSError:
        return False


def _safe_read_text(path: Path, fallback: str) -> str:
    try:
        if path.exists() and path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return fallback


def _statement_examples_template_text(workspace: Path) -> str:
    path = workspace / STATEMENT_EXAMPLES_REL
    try:
        if path.is_symlink():
            raise RuntimeError(
                f"statement examples template must be a regular file: "
                f"{STATEMENT_EXAMPLES_REL.as_posix()}"
            )
        if not path.exists():
            return DEFAULT_STATEMENT_EXAMPLES_TEMPLATE
        if not path.is_file():
            raise RuntimeError(
                f"statement examples template is not a file: "
                f"{STATEMENT_EXAMPLES_REL.as_posix()}"
            )
    except OSError as exc:
        raise RuntimeError(
            f"failed to inspect statement examples template "
            f"{STATEMENT_EXAMPLES_REL.as_posix()}: {exc}"
        ) from exc
    return _read_required_text(
        path,
        label=(
            "statement examples template "
            f"({STATEMENT_EXAMPLES_REL.as_posix()})"
        ),
        allow_empty=True,
    )


def _statement_section_text(workspace: Path, language: str, section_name: str, fallback: str = "") -> str:
    return _safe_read_text(workspace / (STATEMENT_SECTIONS_DIR / language / section_name), fallback)


def _optional_statement_section_text(
    workspace: Path,
    language: str,
    section_name: str,
) -> str:
    text = _statement_section_text(workspace, language, section_name, fallback="")
    return text if text.strip() else ""


def default_statement_title_for_workspace(workspace: Path) -> str:
    return problem_slug_leaf(workspace.name) or DEFAULT_PROBLEM_TITLE


def statement_title_for_language(workspace: Path, language: str, fallback_title: str | None = None) -> str:
    title_from_section = _statement_section_text(workspace, language, "name.tex", fallback="").strip()
    fallback = str(fallback_title or "").strip() or default_statement_title_for_workspace(workspace)
    return title_from_section or fallback


def statement_title_from_snapshot(
    snapshot: Path,
    *,
    fallback_title: str,
    language: str | None = None,
) -> str:
    selected_language = language or pick_statement_language(snapshot)
    return normalize_problem_title(
        statement_title_for_language(
            snapshot,
            selected_language,
            fallback_title=fallback_title,
        ),
        fallback_title=fallback_title,
    )


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
    examples_bundle: StatementExamplesBundle | None = None,
    problem_limits: ProblemConfigLimits,
) -> dict[str, object]:
    cfg = load_problem_config(workspace, limits=problem_limits)
    input_file = "standard input"
    output_file = "standard output"
    time_limit_ms = cfg["time_limit_ms"]
    memory_limit_mb = cfg["memory_limit_mb"]
    resolved_title = statement_title_for_language(workspace, language, problem_title)
    context: dict[str, object] = {
        "name": resolved_title,
        "inputFile": input_file,
        "outputFile": output_file,
        "timeLimit": time_limit_ms,
        "memoryLimit": memory_limit_mb * 1024 * 1024,
        "legend": _statement_section_text(workspace, language, "legend.tex", fallback=""),
        "input": _statement_section_text(workspace, language, "input.tex", fallback=""),
        "output": _statement_section_text(workspace, language, "output.tex", fallback=""),
        "interaction": _statement_section_text(workspace, language, "interaction.tex", fallback=""),
        "notes": _optional_statement_section_text(
            workspace,
            language,
            "notes.tex",
        ),
        "sampleTests": list(
            examples_bundle.get("sample_tests", [])
            if examples_bundle is not None
            else (sample_tests or [])
        ),
    }
    if examples_bundle is not None:
        context["examples"] = examples_bundle["context"]
    return context


def _write_statement_example_resources(
    target_dir: Path,
    bundle: StatementExamplesBundle | None,
) -> None:
    if bundle is None:
        return
    target_root = target_dir.resolve()
    observed: set[str] = set()
    for resource in bundle["resources"]:
        rel = PurePosixPath(resource["path"])
        if (
            rel.is_absolute()
            or not rel.parts
            or any(part in {"", ".", ".."} for part in rel.parts)
            or rel.as_posix() in observed
        ):
            raise RuntimeError("statement example resource path is invalid")
        observed.add(rel.as_posix())
        target = (target_dir / Path(*rel.parts)).resolve()
        if target_root not in target.parents:
            raise RuntimeError("statement example resource escapes compile tree")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(resource["content"], encoding="utf-8", newline="\n")


def _statement_problem_template_context(
    problem_ctx: dict[str, object],
    language: str,
) -> dict[str, object]:
    return {
        "problem": problem_ctx,
        "language": language,
        "contest": {
            "name": "",
            "location": "",
            "date": "",
            "language": language,
        },
        "shortProblemTitle": False,
        "providedStatementsCommands": [],
        "statements": [],
    }


def _write_rendered_problem_templates(
    target_dir: Path,
    *,
    problem_template_text: str,
    examples_template_text: str,
    context: dict[str, object],
) -> Path:
    rendered_examples_tex = render_ftl_template(examples_template_text, context)
    (target_dir / "examples.tex").write_text(
        rendered_examples_tex,
        encoding="utf-8",
    )
    rendered_problem_tex = render_ftl_template(problem_template_text, context)
    problem_tex = target_dir / "problem.tex"
    problem_tex.write_text(rendered_problem_tex, encoding="utf-8")
    return problem_tex


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
    examples_bundle: StatementExamplesBundle | None = None,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> Path:
    template_text = _read_required_text(
        workspace / STATEMENT_TEMPLATE_REL,
        label=f"statement template ({STATEMENT_TEMPLATE_REL.as_posix()})",
    )
    problem_template_text = _safe_read_text(
        workspace / STATEMENT_PROBLEM_REL,
        DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    )
    examples_template_text = _statement_examples_template_text(workspace)
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
        if include_sample_tests and examples_bundle is None
        else []
    )
    _write_statement_example_resources(rendered_lang_root, examples_bundle)
    problem_ctx = _problem_context_for_language(
        workspace,
        safe_language,
        problem_title,
        sample_tests=sample_tests,
        examples_bundle=examples_bundle,
        problem_limits=problem_limits,
    )
    problem_template_context = _statement_problem_template_context(
        problem_ctx,
        safe_language,
    )
    _write_rendered_problem_templates(
        rendered_lang_root,
        problem_template_text=problem_template_text,
        examples_template_text=examples_template_text,
        context=problem_template_context,
    )

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


def _render_statement_problem_assets_for_language(
    workspace: Path,
    language: str,
    target_dir: Path,
    *,
    problem_title: str | None = None,
    examples_bundle: StatementExamplesBundle | None = None,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> tuple[Path, dict[str, object]]:
    template_text = _safe_read_text(
        workspace / STATEMENT_PROBLEM_REL,
        DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    )
    examples_template_text = _statement_examples_template_text(workspace)
    _read_required_text(
        workspace / STATEMENT_STYLE_REL,
        label=f"statement olymp style ({STATEMENT_STYLE_REL.as_posix()})",
    )
    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise RuntimeError("statement language is required")
    _prepare_statement_language_compile_tree(workspace, safe_language, target_dir)
    sample_tests = (
        _collect_sample_tests(
            workspace,
            target_dir,
            tests_spec_max_bytes=tests_spec_max_bytes,
            statement_sample_max_bytes=statement_sample_max_bytes,
        )
        if examples_bundle is None
        else []
    )
    _write_statement_example_resources(target_dir, examples_bundle)
    problem_ctx = _problem_context_for_language(
        workspace,
        safe_language,
        problem_title,
        sample_tests=sample_tests,
        examples_bundle=examples_bundle,
        problem_limits=problem_limits,
    )
    problem_tex = _write_rendered_problem_templates(
        target_dir,
        problem_template_text=template_text,
        examples_template_text=examples_template_text,
        context=_statement_problem_template_context(problem_ctx, safe_language),
    )
    return problem_tex, problem_ctx


def render_statement_problem_assets_for_language(
    workspace: Path,
    language: str,
    target_dir: Path,
    *,
    problem_title: str | None = None,
    examples_bundle: StatementExamplesBundle | None = None,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> Path:
    problem_tex, _problem_ctx = _render_statement_problem_assets_for_language(
        workspace,
        language,
        target_dir,
        problem_title=problem_title,
        examples_bundle=examples_bundle,
        tests_spec_max_bytes=tests_spec_max_bytes,
        statement_sample_max_bytes=statement_sample_max_bytes,
        problem_limits=problem_limits,
    )
    return problem_tex


def render_statement_offline_tree(
    workspace: Path,
    language: str,
    target_dir: Path,
    *,
    problem_title: str | None = None,
    examples_bundle: StatementExamplesBundle | None = None,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> Path:
    """Render one self-contained language tree with ``statements.tex`` entrypoint."""

    template_text = _read_required_text(
        workspace / STATEMENT_TEMPLATE_REL,
        label=f"statement template ({STATEMENT_TEMPLATE_REL.as_posix()})",
    )
    style_path = workspace / STATEMENT_STYLE_REL
    _read_required_text(
        style_path,
        label=f"statement olymp style ({STATEMENT_STYLE_REL.as_posix()})",
    )
    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise RuntimeError("statement language is required")
    _problem_tex, problem_ctx = _render_statement_problem_assets_for_language(
        workspace,
        safe_language,
        target_dir,
        problem_title=problem_title,
        examples_bundle=examples_bundle,
        tests_spec_max_bytes=tests_spec_max_bytes,
        statement_sample_max_bytes=statement_sample_max_bytes,
        problem_limits=problem_limits,
    )
    shutil.copy2(style_path, target_dir / "olymp.sty")
    rendered_main = render_ftl_template(
        template_text,
        {
            "contest": {
                "name": "",
                "location": "",
                "date": "",
                "language": safe_language,
            },
            "language": safe_language,
            "shortProblemTitle": True,
            "providedStatementsCommands": [],
            "statements": [{"file": "problem.tex"}],
            "problem": problem_ctx,
        },
    )
    entrypoint = target_dir / "statements.tex"
    entrypoint.write_text(rendered_main, encoding="utf-8")
    return entrypoint


def seed_statement_sources(workspace: Path) -> None:
    _seed_polygon_statement_sources(workspace)


def render_statement_main(
    statement_root: Path,
    problem_title: str | None = None,
    *,
    language: str,
    include_sample_tests: bool = True,
    examples_bundle: StatementExamplesBundle | None = None,
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
        examples_bundle=examples_bundle,
        tests_spec_max_bytes=tests_spec_max_bytes,
        statement_sample_max_bytes=statement_sample_max_bytes,
        problem_limits=problem_limits,
    )
