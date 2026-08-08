from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from fastapi import HTTPException

from app.main_util import problem_slug_leaf
from app.impl.auth.shared import redirect_response
from app.impl.runtime.config import config
from app.impl.contest.common import _contest_problem_slug_file_token
from app.impl.workspace.context_operation import audit, normalize_contest_slug_required
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.access import workspace_access_context
from app.service.sandbox.base import ExecResult
from app.service.statement.constant import DEFAULT_OLYMP_STY
from app.service.statement.context import normalize_statement_language
from app.service.statement.render import render_statement_problem_assets_for_language
from app.service.statement.title import statement_title_from_snapshot
from app.service.platform.git_process import run_git
from app.service.verification.runtime import coerce_int, normalize_problem_mode
from app.impl.workspace.problem_config import read_problem_config

_C = config.constants

_CONTEST_PROPERTY_LOCATION = "location"
_CONTEST_PROPERTY_DATE = "date"
_CONTEST_JOB_TYPE_PDF = "pdf"
_CONTEST_JOB_TYPE_PACKAGE = "package"
_CONTEST_JOB_TYPE_BUILD = "build"
_CONTEST_EXTRACTBB_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".pdf", ".png"}
_CONTEST_LATEX_JOB_NAME = "statements"
_CONTEST_LATEX_WRAPPER_NAME = "__contest_wrapper__.tex"
_CONTEST_BLANK_PAGES_MARKER = r"%\intentionallyblankpagestrue"
_CONTEST_BLANK_PAGES_ENABLED = _CONTEST_BLANK_PAGES_MARKER.removeprefix("%")
_CONTEST_CJK_ITALIC_OPTIONS = (
    r"[ItalicFont={[FandolKai-Regular.otf]},"
    r"BoldItalicFont={[FandolKai-Regular.otf]}]"
)
_CONTEST_CJK_MAIN_FONT_LINE = (
    rf"\setCJKmainfont{{Noto Serif CJK SC}}{_CONTEST_CJK_ITALIC_OPTIONS}"
)
_CONTEST_CJK_SANS_FONT_LINE = (
    rf"\setCJKsansfont{{Noto Sans CJK SC}}{_CONTEST_CJK_ITALIC_OPTIONS}"
)
_CONTEST_CJK_PREAMBLE_LINES = [
    r"% --- Engine-adaptive font loading ---",
    r"\usepackage{fontspec}",
    r"\usepackage{xeCJK}",
    _CONTEST_CJK_MAIN_FONT_LINE,
    _CONTEST_CJK_SANS_FONT_LINE,
    r"\setCJKmonofont{Noto Sans CJK SC}",
]


def _contest_nav(contest_slug: str, active: str) -> list[dict[str, str | bool]]:
    base = f"/contests/{contest_slug}"
    return [
        {"key": "overview", "label": "Overview", "href": f"{base}/overview", "active": active == "overview"},
        {"key": "problems", "label": "Problems", "href": f"{base}/problems", "active": active == "problems"},
        {"key": "properties", "label": "Properties", "href": f"{base}/properties", "active": active == "properties"},
        {"key": "access", "label": "Access", "href": f"{base}/access", "active": active == "access"},
        {
            "key": "packages",
            "label": "Statements & Builds",
            "href": f"{base}/packages",
            "active": active == "packages",
        },
    ]


def _contest_ctx(contest_slug: str, user: str, active_page: str) -> dict:
    gctx = global_user_ctx(user)
    safe_slug = normalize_contest_slug_required(contest_slug)
    contest_row = config.contest_service.contest_context(safe_slug)
    if contest_row is None:
        raise HTTPException(status_code=404, detail="contest not found")
    access = config.contest_service.access_context(int(contest_row["id"]), int(gctx["user"]["id"]))
    if not access.get("can_read"):
        read_block_reason = access.get("read_block_reason")
        raise HTTPException(
            status_code=403,
            detail=str(read_block_reason) if read_block_reason is not None else "contest access required",
        )
    return {
        "user": gctx["user"],
        "contest": {
            "id": int(contest_row["id"]),
            "slug": str(contest_row["slug"]),
            "title": str(contest_row["title"]),
            "owner_user_id": int(contest_row["owner_user_id"]),
            "status": str(contest_row["status"]),
            "source_generation": int(contest_row["source_generation"]),
            "location": str(contest_row["location"]),
            "date": str(contest_row["date"]),
            "statement_default_language": str(contest_row["statement_default_language"]),
            "created_at": contest_row["created_at"],
        },
        "access": access,
        "active_main": "contests",
        "contest_nav": _contest_nav(str(contest_row["slug"]), active_page),
    }


def _contest_problem_rows(contest_id: int, username: str, user_id: int) -> list[dict[str, object]]:
    del username
    rows = config.contest_service.contest_problems(int(contest_id))
    access_by_problem = config.workspace_service.access_contexts(
        [int(row["problem_id"]) for row in rows],
        int(user_id),
    )
    result: list[dict] = []
    for row in rows:
        problem_id = int(row["problem_id"])
        problem_slug = str(row["problem_slug"])
        slug_owner, _separator, slug_leaf = problem_slug.partition("/")
        problem_access = access_by_problem[problem_id]
        can_problem_write = bool(problem_access.get("can_write"))
        readiness = config.problem_package_service.readiness(problem_id)
        published_revision = readiness["published_revision_number"]
        revision_display = "unpublished" if published_revision is None else f"v{published_revision}"
        revision_warn = not bool(readiness["current_is_materialized"])
        tl_ms = int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"])
        ml_mb = int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"])
        mode = str(_C.GENERAL_CONFIG_DEFAULTS["mode"])
        if bool(problem_access.get("can_read")):
            try:
                revision = config.problem_package_service.published_revision(problem_id)
                general_cfg = config.problem_package_service.published_config(revision)
                tl_ms = coerce_int(
                    general_cfg.get("time_limit_ms"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
                    _C.GENERAL_TIME_LIMIT_MIN_MS,
                    _C.GENERAL_TIME_LIMIT_MAX_MS,
                )
                ml_mb = coerce_int(
                    general_cfg.get("memory_limit_mb"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
                    _C.GENERAL_MEMORY_LIMIT_MIN_MB,
                    _C.GENERAL_MEMORY_LIMIT_MAX_MB,
                )
                mode = normalize_problem_mode(general_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
            except Exception:
                revision_display = "unavailable"
                revision_warn = True
        else:
            revision_display = "no problem access"
            revision_warn = True
        result.append(
            {
                "contest_problem_id": int(row["contest_problem_id"]),
                "idx": str(row["idx"]),
                "problem_id": problem_id,
                "statement_folder": str(row["statement_folder"]),
                "problem_slug": problem_slug,
                "slug_owner": slug_owner,
                "slug_leaf": slug_leaf,
                "time_limit_ms": tl_ms,
                "memory_limit_mb": ml_mb,
                "mode": mode,
                "revision_display": revision_display,
                "revision_warn": revision_warn,
                "dirty": False,
                "published_commit": str(readiness["published_commit"]),
                "published_revision_number": published_revision,
                "materialized_commit": str(readiness["materialized_commit"]),
                "materialized_revision_number": readiness["materialized_revision_number"],
                "materialization_id": str(readiness["materialization_id"]),
                "current_is_materialized": bool(readiness["current_is_materialized"]),
                "statement_languages": list(readiness["statement_languages"]),
                "readiness_reason": str(readiness["missing_reason"]),
                "can_problem_write": can_problem_write,
                "created_at": row["created_at"],
            }
        )
    return result


def _ensure_zip_bundle(job_root: Path, bundle_name: str, source_dir: Path) -> Path:
    safe_name = Path(str(bundle_name or "").strip() or "contest-bundle").stem
    if not safe_name:
        safe_name = "contest-bundle"
    target_base = job_root / safe_name
    out = Path(shutil.make_archive(str(target_base), "zip", root_dir=source_dir, base_dir="."))
    return out.resolve()


def _contest_compile_target(root: Path, *parts: str) -> Path:
    safe_root = root.resolve()
    target = (root / Path(*parts)).resolve()
    if safe_root not in target.parents:
        raise RuntimeError("invalid contest compile path")
    return target


def _contest_latex_escape_text(value: object) -> str:
    text = str(value or "")
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def _contest_statement_languages(contest_id: int) -> list[str]:
    languages: list[str] = []
    seen: set[str] = set()
    for row in config.contest_service.statement_attachment_rows(int(contest_id)):
        parts = Path(str(row.get("rel_path") or "")).parts
        if len(parts) < 3 or parts[0] != "statements" or parts[2] != "statements.tex":
            continue
        language = normalize_statement_language(parts[1])
        if language and language not in seen:
            seen.add(language)
            languages.append(language)
    return languages


def _resolve_contest_statement_language(contest_id: int) -> str:
    configured = normalize_statement_language(config.contest_service.statement_default_language(int(contest_id)))
    if configured:
        return configured
    languages = _contest_statement_languages(int(contest_id))
    if "english" in languages:
        return "english"
    if languages:
        return languages[0]
    return "english"


def _contest_problem_source_folder(entry: dict[str, object], source_folder_map: dict[int, str]) -> str:
    problem_id = int(entry["problem_id"])
    mapped = str(source_folder_map.get(problem_id, "") or "").strip()
    if mapped:
        return mapped
    idx_token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(entry.get("idx") or "").strip().lower()).strip("-")
    slug_token = _contest_problem_slug_file_token(str(entry.get("problem_slug") or ""))
    if idx_token:
        return f"{idx_token}-{slug_token}"
    return slug_token


def contest_default_statements_tex(
    *,
    contest_id: int,
    contest_slug: str,
    language: str,
    problem_entries: list[dict[str, object]],
    source_folder_map: dict[int, str],
) -> str:
    contest_row = config.contest_service.contest_context(contest_slug) or {}
    props = config.contest_service.overview_properties_map(int(contest_id), contest_slug)
    lines = [
        r"\documentclass[11pt,a4paper,oneside]{article}",
        *_CONTEST_CJK_PREAMBLE_LINES,
        r"\usepackage{amsmath}",
        r"\usepackage{amssymb}",
        r"\usepackage{olymp}",
        r"\usepackage{comment}",
        r"\usepackage{epigraph}",
        r"\usepackage{expdlist}",
        r"\usepackage{import}",
        r"\usepackage{graphicx}",
        r"\usepackage{tikz}",
        r"\usepackage{pgfplots}",
        r"\usepackage{multirow}",
        r"\usepackage{siunitx}",
        r"\usepackage[normalem]{ulem}",
        r"\usepackage{xparse}",
        r"\usepackage{wrapfig}",
        r"\usepackage{algorithm}",
        r"\usepackage{algpseudocode}",
        _CONTEST_BLANK_PAGES_MARKER,
        r"\begin{document}",
        r"\contest",
        "{" + _contest_latex_escape_text(contest_row.get("title", "")) + "}%",
        "{" + _contest_latex_escape_text(props.get("location", "")) + "}%",
        "{" + _contest_latex_escape_text(props.get("date", "")) + "}%",
        r"\binoppenalty=10000",
        r"\relpenalty=10000",
        "",
    ]
    for entry in problem_entries:
        source_folder = _contest_problem_source_folder(entry, source_folder_map)
        import_path = f"../../problems/{source_folder}/statements/{language}/"
        lines.extend(
            [
                r"\clearpage",
                r"\graphicspath{{" + import_path + r"}}",
                r"\def\ProblemIndex{" + _contest_latex_escape_text(entry.get("idx", "")) + "}",
                r"\import{" + import_path + r"}{./problem.tex}",
                "",
            ]
        )
    lines.append(r"\end{document}")
    return "\n".join(lines) + "\n"


def _latex_uses_package(text: str, package: str) -> bool:
    package_re = re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
    for match in package_re.finditer(text):
        names = [name.strip() for name in str(match.group(1) or "").split(",")]
        if package in names:
            return True
    return False


def _ensure_contest_statements_tex_cjk_support(statements_tex: Path) -> None:
    """Patch the compile copy only, so custom contest sources are not rewritten."""
    text = statements_tex.read_text(encoding="utf-8", errors="replace")
    insert_lines: list[str] = []
    if not _latex_uses_package(text, "fontspec"):
        insert_lines.append(r"\usepackage{fontspec}")
    if not _latex_uses_package(text, "xeCJK"):
        insert_lines.append(r"\usepackage{xeCJK}")
    if r"\setCJKmainfont" not in text:
        insert_lines.append(_CONTEST_CJK_MAIN_FONT_LINE)
    if r"\setCJKsansfont" not in text:
        insert_lines.append(_CONTEST_CJK_SANS_FONT_LINE)
    if r"\setCJKmonofont" not in text:
        insert_lines.append(r"\setCJKmonofont{Noto Sans CJK SC}")
    if not insert_lines:
        return

    block = "% --- CJK support for XeLaTeX contest compilation ---\n" + "\n".join(insert_lines) + "\n"
    documentclass_match = re.search(r"\\documentclass\s*(?:\[[^\]\n]*\])?\s*\{[^}\n]+\}\s*", text)
    if documentclass_match is None:
        statements_tex.write_text(block + text, encoding="utf-8")
        return
    insert_at = documentclass_match.end()
    statements_tex.write_text(text[:insert_at] + "\n" + block + text[insert_at:], encoding="utf-8")


_TIKZ_LIBRARY_LINE_RE = re.compile(
    r"(?m)^[ \t]*\\usetikzlibrary\s*(?:\[[^\]\n]*\])?\s*\{([^}\n]+)\}[ \t]*%?[ \t]*(?:\r?\n)?"
)
_COLOR_DEFINITION_LINE_RE = re.compile(
    r"(?m)^[ \t]*\\(?:definecolor|providecolor|colorlet)\s*(?:\[[^\]\n]*\])?\{[^}\n]+\}(?:\s*\{[^}\n]*\}){1,2}[ \t]*%?[ \t]*(?:\r?\n)?"
)


def _insert_contest_preamble_lines(statements_tex: Path, lines: list[str]) -> None:
    if not lines:
        return
    text = statements_tex.read_text(encoding="utf-8", errors="replace")
    begin_match = re.search(r"\\begin\s*\{document\}", text)
    preamble = text[: begin_match.start()] if begin_match is not None else text
    missing = [line for line in lines if line not in preamble]
    if not missing:
        return
    block = "% --- Hoisted from problem statement bodies for contest compilation ---\n" + "\n".join(missing) + "\n"
    if begin_match is None:
        statements_tex.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
        return
    insert_at = begin_match.start()
    statements_tex.write_text(text[:insert_at] + block + text[insert_at:], encoding="utf-8")


def _hoist_problem_body_tikz_libraries(problem_tex: Path) -> list[str]:
    if problem_tex.is_symlink() or (not problem_tex.exists()) or (not problem_tex.is_file()):
        return []
    text = problem_tex.read_text(encoding="utf-8", errors="replace")
    libraries: list[str] = []

    def _remove(match: re.Match[str]) -> str:
        for raw_name in str(match.group(1) or "").split(","):
            name = raw_name.strip()
            if name and name not in libraries:
                libraries.append(name)
        return ""

    updated = _TIKZ_LIBRARY_LINE_RE.sub(_remove, text)
    if updated != text:
        problem_tex.write_text(updated, encoding="utf-8")
    return libraries


def _extract_latex_color_definition_lines(path: Path) -> list[str]:
    if path.is_symlink() or (not path.exists()) or (not path.is_file()):
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines: list[str] = []
    for match in _COLOR_DEFINITION_LINE_RE.finditer(text):
        line = str(match.group(0) or "").strip()
        if line and line not in lines:
            lines.append(line)
    return lines


def _hoist_contest_problem_preamble_commands(
    *,
    compile_root: Path,
    statements_root: Path,
    language: str,
    results: list[dict[str, object]],
) -> None:
    tikz_libraries: list[str] = []
    color_definitions: list[str] = []
    for row in results:
        if str(row.get("status") or "") != "success":
            continue
        problem_tex = _contest_compile_target(
            compile_root,
            "problems",
            str(row.get("source_folder") or ""),
            "statements",
            language,
            "problem.tex",
        )
        for library in _hoist_problem_body_tikz_libraries(problem_tex):
            if library not in tikz_libraries:
                tikz_libraries.append(library)
        for line in list(row.get("preamble_lines") or []):
            safe_line = str(line or "").strip()
            if safe_line and safe_line not in color_definitions:
                color_definitions.append(safe_line)
        for line in _extract_latex_color_definition_lines(problem_tex):
            if line not in color_definitions:
                color_definitions.append(line)
    if not tikz_libraries and not color_definitions:
        return
    statements_tex = statements_root / "statements.tex"
    lines: list[str] = []
    statements_text = statements_tex.read_text(encoding="utf-8", errors="replace")
    if color_definitions and not (
        _latex_uses_package(statements_text, "xcolor") or _latex_uses_package(statements_text, "color")
    ):
        lines.append(r"\usepackage{xcolor}")
    lines.extend(color_definitions)
    if tikz_libraries and not _latex_uses_package(statements_text, "tikz"):
        lines.append(r"\usepackage{tikz}")
    if tikz_libraries:
        lines.append(r"\usetikzlibrary{" + ",".join(tikz_libraries) + "}")
    _insert_contest_preamble_lines(statements_tex, lines)


def _read_contest_compile_log_tail(statements_root: Path) -> str:
    log_path = statements_root / f"{_CONTEST_LATEX_JOB_NAME}.log"
    try:
        if log_path.is_symlink() or (not log_path.exists()) or (not log_path.is_file()):
            return ""
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-20000:]


def _contest_latex_compile_error_detail(output_text: str, returncode: int | None) -> str:
    text = str(output_text or "")
    low = text.lower()
    if ("can't find the format file" in low) and ("xelatex.fmt" in low):
        return "missing LaTeX format xelatex.fmt"
    if ("can't find the format file" in low) and ("latex.fmt" in low):
        return "missing LaTeX format latex.fmt"
    if ("can't find the format file" in low) and ("mpost.fmt" in low):
        return "missing MetaPost format mpost.fmt"
    if "dvipdfmx:fatal" in low:
        return "dvipdfmx failed"
    missing_pkg = re.search("File `([^`]+\\.sty)' not found", text)
    if missing_pkg is not None:
        pkg_name = str(missing_pkg.group(1) or "").strip()
        if pkg_name:
            return f"missing LaTeX package {pkg_name}"
    package_error = re.search(r"^!\s*(?:Package\s+)?([^:\n]+?)\s+Error:\s*(.+)$", text, flags=re.MULTILINE)
    if package_error is not None:
        name = str(package_error.group(1) or "").strip()
        detail = str(package_error.group(2) or "").strip()
        if name and detail:
            return f"{name} Error: {detail}"
    bang_error = re.search(r"^!\s*(.+)$", text, flags=re.MULTILINE)
    if bang_error is not None:
        detail = str(bang_error.group(1) or "").strip()
        if detail:
            return detail
    if int(returncode or 0) != 0:
        return "latex compile failed"
    return ""


def _append_contest_job_log(log_path: Path, *, title: str, result: ExecResult) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"== {title} ==\n")
        fh.write(f"status: {result.status}\n")
        fh.write(f"returncode: {result.returncode}\n")
        fh.write(f"elapsed_ms: {result.elapsed_ms}\n")
        fh.write("[stdout]\n")
        fh.write(str(result.stdout or ""))
        fh.write("\n[stderr]\n")
        fh.write(str(result.stderr or ""))
        fh.write("\n\n")


def _run_contest_tex_command(
    command: list[str],
    cwd: Path,
    log_path: Path,
    *,
    title: str,
    extra_mounts: tuple[Path, ...] = (),
    env: dict[str, str] | None = None,
) -> ExecResult:
    merged_env = dict(os.environ)
    if env is not None:
        merged_env.update(env)
    proc = config.tex_compile_service.run(
        command=command,
        cwd=cwd,
        extra_mounts=extra_mounts,
        env=merged_env,
    )
    _append_contest_job_log(log_path, title=title, result=proc)
    return proc


def _prepare_contest_graphics_bounding_boxes(
    compile_root: Path,
    log_path: Path,
    *,
    extra_mounts: tuple[Path, ...] = (),
    env: dict[str, str] | None = None,
) -> str:
    safe_root = compile_root.resolve()
    for file_path in sorted(compile_root.rglob("*")):
        if file_path.is_symlink() or (not file_path.is_file()):
            continue
        if file_path.suffix.lower() not in _CONTEST_EXTRACTBB_SUFFIXES:
            continue
        if safe_root not in file_path.resolve().parents:
            raise RuntimeError("invalid contest compile asset path")
        relative_name = file_path.relative_to(compile_root).as_posix()
        proc = _run_contest_tex_command(
            ["extractbb", file_path.name],
            file_path.parent,
            log_path,
            title=f"extractbb {relative_name}",
            extra_mounts=extra_mounts,
            env=env,
        )
        if proc.timed_out:
            return f"extractbb timeout for {relative_name}"
        if int(proc.returncode or 0) != 0:
            return f"extractbb failed for {relative_name}"
    return ""


def _write_contest_latex_wrapper(statements_root: Path) -> Path:
    wrapper_path = (statements_root / _CONTEST_LATEX_WRAPPER_NAME).resolve()
    wrapper_path.write_text(
        "\\AtBeginDocument{%\n"
        "  \\providecommand{\\url}[1]{\\texttt{#1}}%\n"
        "  \\providecommand{\\href}[2]{#2}%\n"
        "}\n"
        "\\input{statements.tex}\n",
        encoding="utf-8",
    )
    return wrapper_path


def _contest_tex_env(compile_root: Path) -> dict[str, str]:
    texmf_var = _contest_compile_target(compile_root, ".texmf-var")
    texmf_cache = _contest_compile_target(compile_root, ".texmf-cache")
    texmf_config = _contest_compile_target(compile_root, ".texmf-config")
    var_tex_fonts = _contest_compile_target(compile_root, ".texfonts")
    texmf_output = _contest_compile_target(compile_root, ".texmf-output")
    for path in (texmf_var, texmf_cache, texmf_config, var_tex_fonts, texmf_output):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(compile_root),
        "TEXMFVAR": str(texmf_var),
        "TEXMFCACHE": str(texmf_cache),
        "TEXMFCONFIG": str(texmf_config),
        "VARTEXFONTS": str(var_tex_fonts),
        "TEXMFOUTPUT": str(texmf_output),
    }


def _copy_contest_statement_language_tree(
    *,
    source_snapshot: Path,
    language: str,
    compile_root: Path,
) -> Path:
    language_root = source_snapshot / "statements" / language
    if language_root.exists():
        for source_path in language_root.rglob("*"):
            if source_path.is_symlink():
                raise RuntimeError(f"contest statement source is a symlink: {source_path}")
            if source_path.is_dir():
                continue
            if not source_path.is_file():
                raise RuntimeError(f"contest statement source is not regular: {source_path}")
            rel_path = source_path.relative_to(source_snapshot)
            target_path = _contest_compile_target(compile_root, *rel_path.parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
    statements_root = _contest_compile_target(compile_root, "statements", language)
    statements_root.mkdir(parents=True, exist_ok=True)
    olymp_sty = statements_root / "olymp.sty"
    if olymp_sty.is_symlink() or (not olymp_sty.exists()):
        (statements_root / "olymp.sty").write_text(DEFAULT_OLYMP_STY, encoding="utf-8")
    statements_tex = statements_root / "statements.tex"
    if statements_tex.is_symlink() or (not statements_tex.exists()) or (not statements_tex.is_file()):
        raise RuntimeError(f"contest statements.tex missing for language: {language}")
    _ensure_contest_statements_tex_cjk_support(statements_tex)
    return statements_root


def _prepare_contest_pdf_problem(
    *,
    compile_root: Path,
    problem_id: int,
    problem_slug: str,
    idx: str,
    source_folder: str,
    language: str,
    materialization_id: str,
) -> dict[str, object]:
    item: dict[str, object] = {
        "idx": idx,
        "problem_id": int(problem_id),
        "problem_slug": problem_slug,
        "source_folder": source_folder,
        "status": "failed",
        "source_commit": "",
        "error": "",
    }
    if not source_folder:
        item["error"] = f"contest source folder missing for {problem_slug}"
        return item
    try:
        with config.problem_package_service.open_reader(materialization_id) as native:
            target_dir = _contest_compile_target(
                compile_root,
                "problems",
                source_folder,
                "statements",
                language,
            )
            render_statement_problem_assets_for_language(
                native.root,
                language,
                target_dir,
                problem_title=statement_title_from_snapshot(
                    native.root,
                    fallback_title=problem_slug_leaf(problem_slug),
                    language=language,
                ),
            )
            item["preamble_lines"] = _extract_latex_color_definition_lines(native.root / "statement" / "olymp.sty")
            item["source_commit"] = native.manifest["source_commit"]
            item["materialization_id"] = materialization_id
            item["status"] = "success"
    except Exception as exc:
        item["error"] = str(exc)
    return item


def _contest_redirect(
    contest_slug: str,
    page: str,
    *,
    query: str = "",
    fragment: str = "",
    message: str = "",
):
    target = f"/contests/{contest_slug}/{page}"
    if query:
        target += f"?{query}"
    if fragment:
        target += f"#{fragment}"
    return redirect_response(target, status_code=303, message=message)

def _problem_general_payload_map(
    problem_ids: list[str],
    time_limit_ms_values: list[str],
    memory_limit_mb_values: list[str],
) -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    for index, raw_pid in enumerate(list(problem_ids or [])):
        try:
            pid = int(str(raw_pid or "").strip())
        except Exception:
            continue
        if pid <= 0:
            continue
        tl_value = str(time_limit_ms_values[index] if index < len(time_limit_ms_values) else "").strip()
        ml_value = str(memory_limit_mb_values[index] if index < len(memory_limit_mb_values) else "").strip()
        result[pid] = {"time_limit_ms": tl_value, "memory_limit_mb": ml_value}
    return result

def _run_problem_general_update(
    *,
    contest_slug: str,
    actor_username: str,
    actor_user_id: int,
    problem_id: int,
    problem_slug: str,
    requested_time_limit_ms: str,
    requested_memory_limit_mb: str,
) -> dict[str, object]:
    requested: dict[str, object] = {
        "time_limit_ms": str(requested_time_limit_ms or "").strip(),
        "memory_limit_mb": str(requested_memory_limit_mb or "").strip(),
    }
    result: dict[str, object] = {
        "problem_id": int(problem_id),
        "problem_slug": str(problem_slug),
        "requested": requested,
        "status": "failed",
        "commit_id": "",
        "error": "",
    }
    problem_access = workspace_access_context(int(problem_id), int(actor_user_id))
    if not bool(problem_access.get("can_write")):
        result["error"] = "write access to problem is required"
        return result
    try:
        workspace = Path(config.workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True))
        safe_tl = coerce_int(
            requested.get("time_limit_ms"),
            int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            _C.GENERAL_TIME_LIMIT_MIN_MS,
            _C.GENERAL_TIME_LIMIT_MAX_MS,
        )
        safe_ml = coerce_int(
            requested.get("memory_limit_mb"),
            int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
            _C.GENERAL_MEMORY_LIMIT_MIN_MB,
            _C.GENERAL_MEMORY_LIMIT_MAX_MB,
        )
        with config.workspace_service.workspace_lock(workspace):
            has_head = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"]).returncode == 0
            if not has_head:
                raise RuntimeError("bulk TL/ML update requires an initialized repository; create the initial commit first")
            before = config.git_service.status_change_summary(workspace, limit=1)
            if int(before.get("total", 0)) > 0:
                raise RuntimeError("workspace has uncommitted changes")
            payload, general_cfg, cfg_path = read_problem_config(workspace)
            safe_mode = normalize_problem_mode(general_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
            payload.pop("interactive", None)
            payload.update(
                {
                    "time_limit_ms": safe_tl,
                    "memory_limit_mb": safe_ml,
                    "mode": safe_mode,
                    "pass_limit": int(general_cfg.get("pass_limit") or _C.GENERAL_CONFIG_DEFAULTS["pass_limit"]),
                }
            )
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            after = config.git_service.status_change_summary(workspace, limit=1)
            if int(after.get("total", 0)) <= 0:
                result["status"] = "skipped"
                return result
            commit_msg = f"contest {contest_slug}: bulk update TL/ML"
            commit_id = config.git_service.commit(
                workspace,
                commit_msg,
                actor_username,
                f"{actor_username}@polygonlike.local",
            )
            try:
                config.git_service.push(workspace, "main")
            except Exception as exc:
                try:
                    config.git_service.rollback_last_commit(workspace, expected_head=commit_id)
                except Exception as rollback_exc:
                    raise RuntimeError(f"push failed: {exc}; rollback failed: {rollback_exc}") from exc
                raise RuntimeError(f"push failed: {exc}; commit rolled back") from exc
            result["status"] = "success"
            result["commit_id"] = str(commit_id)
        try:
            config.workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True)
        except Exception:
            pass
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result

def _finalize_contest_job_failure_if_running(
    *,
    contest_id: int,
    job_id: str,
    job_type: str,
    error_text: str,
) -> None:
    current_status = config.contest_service.job_status(contest_id, str(job_id or "").strip())
    if current_status != "running":
        return
    config.contest_service.update_job(
        contest_id,
        str(job_id or "").strip(),
        "failed",
        {
            "job_type": str(job_type or "").strip(),
            "error": str(error_text or "").strip() or "worker failed",
        },
        finished=True,
    )

def _run_contest_pdf_job_worker(
    *,
    contest_id: int,
    contest_slug: str,
    actor_user_id: int,
    job_id: str,
    language: str,
    insert_blank_pages: bool,
    finalize: bool = True,
) -> dict[str, object]:
    job_root = config.contest_service.job_root(contest_slug, job_id)
    compile_root = (job_root / "contest-pdf-src").resolve()
    if compile_root.exists():
        shutil.rmtree(compile_root, ignore_errors=True)
    compile_root.mkdir(parents=True, exist_ok=True)
    contest_mounts = (compile_root,)
    contest_tex_env = _contest_tex_env(compile_root)
    log_path = job_root / "logs" / "contest-pdf.log"
    entries = config.contest_service.build_items(job_id)
    source_folder_map = {
        int(entry["problem_id"]): str(entry["statement_folder"])
        for entry in entries
        if str(entry["statement_folder"])
    }
    statements_root = _copy_contest_statement_language_tree(
        source_snapshot=config.contest_service.job_root(contest_slug, job_id) / "contest-sources",
        language=language,
        compile_root=compile_root,
    )
    if insert_blank_pages:
        statements_tex = statements_root / "statements.tex"
        payload = statements_tex.read_bytes()
        statements_tex.write_bytes(
            payload.replace(
                _CONTEST_BLANK_PAGES_MARKER.encode("ascii"),
                _CONTEST_BLANK_PAGES_ENABLED.encode("ascii"),
            )
        )
    results: list[dict[str, object]] = []
    for entry in entries:
        source_folder = _contest_problem_source_folder(dict(entry), source_folder_map)
        item = _prepare_contest_pdf_problem(
            compile_root=compile_root,
            problem_id=int(entry["problem_id"]),
            problem_slug=str(entry["problem_slug"]),
            idx=str(entry["label"]),
            source_folder=source_folder,
            language=language,
            materialization_id=str(entry["materialization_id"]),
        )
        results.append(item)
    success_count = sum((1 for row in results if str(row.get("status")) == "success"))
    failed_count = len(results) - success_count
    summary: dict[str, object] = {
        "job_type": _CONTEST_JOB_TYPE_PDF,
        "contest_slug": contest_slug,
        "language": language,
        "results": results,
        "totals": {
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
        },
    }
    if failed_count > 0:
        first_error = str(next((row.get("error") for row in results if str(row.get("status")) != "success"), "") or "").strip()
        summary["error"] = first_error or "problem preparation failed"
        if finalize:
            config.contest_service.update_job(contest_id, job_id, "failed", summary, finished=True)
        audit(
            actor_user_id,
            None,
            "contest.packages.pdf",
            {
                "contest_id": contest_id,
                "contest_slug": contest_slug,
                "job_id": job_id,
                "language": language,
                "total": len(results),
                "success": success_count,
                "failed": failed_count,
            },
        )
        return summary
    _hoist_contest_problem_preamble_commands(
        compile_root=compile_root,
        statements_root=statements_root,
        language=language,
        results=results,
    )
    for row in results:
        problem_root = _contest_compile_target(
            compile_root,
            "problems",
            str(row["source_folder"]),
            "statements",
            language,
        )
        for mp_file in sorted(problem_root.glob("*.mp")):
            proc = _run_contest_tex_command(
                ["mpost", mp_file.name],
                problem_root,
                log_path,
                title=f"{row['problem_slug']} :: mpost {mp_file.name}",
                extra_mounts=contest_mounts,
                env=contest_tex_env,
            )
            if proc.timed_out:
                summary["error"] = f"mpost timeout for {row['problem_slug']}: {mp_file.name}"
                if finalize:
                    config.contest_service.update_job(contest_id, job_id, "failed", summary, finished=True)
                return summary
            if int(proc.returncode or 0) != 0:
                summary["error"] = f"mpost failed for {row['problem_slug']}: {mp_file.name}"
                if finalize:
                    config.contest_service.update_job(contest_id, job_id, "failed", summary, finished=True)
                return summary
    extractbb_error = _prepare_contest_graphics_bounding_boxes(
        compile_root,
        log_path,
        extra_mounts=contest_mounts,
        env=contest_tex_env,
    )
    if extractbb_error:
        summary["error"] = extractbb_error
        if finalize:
            config.contest_service.update_job(contest_id, job_id, "failed", summary, finished=True)
        return summary
    latex_wrapper = _write_contest_latex_wrapper(statements_root)
    final_output = ""
    for command, title in (
        (
            [
                "xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-jobname={_CONTEST_LATEX_JOB_NAME}",
                latex_wrapper.name,
            ],
            "xelatex pass 1",
        ),
        (
            [
                "xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-jobname={_CONTEST_LATEX_JOB_NAME}",
                latex_wrapper.name,
            ],
            "xelatex pass 2",
        ),
    ):
        proc = _run_contest_tex_command(
            command,
            statements_root,
            log_path,
            title=title,
            extra_mounts=contest_mounts,
            env=contest_tex_env,
        )
        log_output = _read_contest_compile_log_tail(statements_root)
        final_output = "\n".join(
            part
            for part in (str(proc.stdout or ""), str(proc.stderr or ""), log_output)
            if part
        ).strip()
        if proc.timed_out:
            summary["error"] = f"{title} timeout"
            if finalize:
                config.contest_service.update_job(contest_id, job_id, "failed", summary, finished=True)
            return summary
        if int(proc.returncode or 0) != 0:
            error_text = _contest_latex_compile_error_detail(final_output, proc.returncode)
            summary["error"] = error_text or f"{title} failed"
            if finalize:
                config.contest_service.update_job(contest_id, job_id, "failed", summary, finished=True)
            return summary
    generated_pdf = statements_root / "statements.pdf"
    if generated_pdf.is_symlink() or (not generated_pdf.exists()) or (not generated_pdf.is_file()):
        summary["error"] = "contest pdf missing after compile"
        if finalize:
            config.contest_service.update_job(contest_id, job_id, "failed", summary, finished=True)
        return summary
    pdf_dir = job_root / "contest-pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    target_pdf = (pdf_dir / "statements.pdf").resolve()
    shutil.copy2(generated_pdf, target_pdf)
    artifact_id = config.contest_service.record_artifact(
        contest_id=contest_id,
        job_id=job_id,
        artifact_type="contest-pdf",
        filename=f"{contest_slug}-{language}-statements.pdf",
        artifact_path=target_pdf,
    )
    summary["artifact_id"] = artifact_id
    summary["filename"] = target_pdf.name
    summary["pdf_file"] = "contest-pdf/statements.pdf"
    if finalize:
        config.contest_service.update_job(contest_id, job_id, "ok", summary, finished=True)
    audit(
        actor_user_id,
        None,
        "contest.packages.pdf",
        {
            "contest_id": contest_id,
            "contest_slug": contest_slug,
            "job_id": job_id,
            "language": language,
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "artifact_id": artifact_id,
        },
    )
    return summary

def _run_contest_package_job_worker(
    *,
    contest_id: int,
    contest_slug: str,
    actor_user_id: int,
    job_id: str,
    finalize: bool = True,
) -> dict[str, object]:
    job_root = config.contest_service.job_root(contest_slug, job_id)
    packages_dir = job_root / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    entries = config.contest_service.build_items(job_id)
    results: list[dict[str, object]] = []
    for entry in entries:
        problem_id = int(entry["problem_id"])
        idx = str(entry["label"] or "")
        problem_slug = str(entry["problem_slug"] or "")
        item: dict[str, object] = {
            "idx": idx,
            "problem_id": problem_id,
            "problem_slug": problem_slug,
            "status": "failed",
            "source_commit": "",
            "materialization_id": str(entry["materialization_id"]),
            "package_file": "",
            "error": "",
        }
        try:
            head_commit = str(entry["source_commit"])
            item["source_commit"] = head_commit
            export_id, export_path = config.export_service.create_export(
                problem_slug,
                "icpc",
                materialization_id=str(entry["materialization_id"]),
                domjudge_short_name=idx,
            )
            item["export_id"] = export_id
            export_path = Path(export_path).resolve()
            if not export_path.exists() or not export_path.is_file() or export_path.is_symlink():
                raise RuntimeError("package file missing")
            file_token = _contest_problem_slug_file_token(problem_slug)
            output_name = f"{idx}-{file_token}.zip" if idx else f"{file_token}.zip"
            target_package = (packages_dir / output_name).resolve()
            shutil.copy2(export_path, target_package)
            item["package_file"] = f"packages/{output_name}"
            item["status"] = "success"
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
        results.append(item)
    success_count = sum((1 for row in results if str(row.get("status")) == "success"))
    failed_count = sum((1 for row in results if str(row.get("status")) != "success"))
    bundle_root = job_root / "bundle-package"
    if bundle_root.exists():
        shutil.rmtree(bundle_root, ignore_errors=True)
    bundle_root.mkdir(parents=True, exist_ok=True)
    if packages_dir.exists() and any(packages_dir.iterdir()):
        shutil.copytree(packages_dir, bundle_root / "packages", dirs_exist_ok=True)
    summary: dict[str, object] = {
        "job_type": _CONTEST_JOB_TYPE_PACKAGE,
        "contest_slug": contest_slug,
        "results": results,
        "totals": {
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
        },
    }
    failed_errors = [
        str(row.get("error") or "")
        for row in results
        if str(row.get("status") or "") != "success"
    ]
    if (
        failed_count == len(results)
        and failed_count > 1
        and len(set(failed_errors)) == 1
        and failed_errors[0]
    ):
        summary["common_error"] = failed_errors[0]
    (bundle_root / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact_id = ""
    artifact_filename = ""
    if success_count > 0 and failed_count == 0:
        archive_path = _ensure_zip_bundle(job_root, f"{contest_slug}-packages-{job_id}", bundle_root)
        artifact_filename = archive_path.name
        artifact_id = config.contest_service.record_artifact(
            contest_id=contest_id,
            job_id=job_id,
            artifact_type="package-bundle",
            filename=archive_path.name,
            artifact_path=archive_path,
        )
    summary["artifact_id"] = artifact_id
    summary["filename"] = artifact_filename
    if finalize:
        config.contest_service.update_job(
            contest_id,
            job_id,
            "ok" if failed_count == 0 and success_count > 0 else "failed",
            summary,
            finished=True,
        )
    audit(
        actor_user_id,
        None,
        "contest.packages.build",
        {
            "contest_id": contest_id,
            "contest_slug": contest_slug,
            "job_id": job_id,
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "artifact_id": artifact_id,
        },
    )
    return summary


def _freeze_contest_build_items(contest_id: int, job_id: str) -> list[str]:
    items: list[dict[str, object]] = []
    missing: list[str] = []
    for entry in config.contest_service.contest_problems(contest_id):
        problem_id = int(entry["problem_id"])
        materialization = config.problem_package_service.latest_available_materialization(problem_id)
        if materialization is None:
            missing.append(str(entry["problem_slug"]))
            continue
        items.append(
            {
                "contest_problem_id": int(entry["contest_problem_id"]),
                "position": int(entry["position"]),
                "label": str(entry["idx"]),
                "problem_id": problem_id,
                "statement_folder": str(entry["statement_folder"]),
                "source_commit": materialization["source_commit"],
                "revision_number": materialization["revision_number"],
                "materialization_id": materialization["id"],
                "archive_sha256": materialization["archive_sha256"],
            }
        )
    if not missing:
        config.contest_service.freeze_build_items(job_id, items)
    return missing


def _snapshot_contest_sources(
    *,
    contest_id: int,
    contest_slug: str,
    job_id: str,
    language: str,
) -> None:
    source = config.contest_service.contest_source_root(contest_slug)
    target = config.contest_service.job_root(contest_slug, job_id) / "contest-sources"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        parent = Path(dirpath)
        rel_parent = parent.relative_to(source)
        destination = target / rel_parent
        destination.mkdir(parents=True, exist_ok=True)
        dirnames[:] = sorted(dirnames)
        for dirname in dirnames:
            path = parent / dirname
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"contest source is not a regular directory: {path.relative_to(source)}")
        for filename in sorted(filenames):
            path = parent / filename
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"contest source is not a regular file: {path.relative_to(source)}")
            shutil.copy2(path, destination / filename)
    if language:
        statements_root = target / "statements" / language
        statements_root.mkdir(parents=True, exist_ok=True)
        statements_tex = statements_root / "statements.tex"
        if not statements_tex.exists():
            entries = config.contest_service.build_items(job_id)
            source_folder_map = {
                int(entry["problem_id"]): str(entry["statement_folder"])
                for entry in entries
                if str(entry["statement_folder"])
            }
            statements_tex.write_text(
                contest_default_statements_tex(
                    contest_id=contest_id,
                    contest_slug=contest_slug,
                    language=language,
                    problem_entries=entries,
                    source_folder_map=source_folder_map,
                ),
                encoding="utf-8",
            )

def _queue_contest_job(
    *,
    contest_id: int,
    contest_slug: str,
    actor_user_id: int,
    outputs: tuple[str, ...],
    language: str = "",
    insert_blank_pages: bool = False,
) -> tuple[str, bool, str]:
    unknown_outputs = set(outputs).difference({"statement_pdf", "icpc_bundle"})
    if unknown_outputs:
        raise ValueError(f"unsupported contest build output: {sorted(unknown_outputs)[0]}")
    requested_outputs = tuple(
        output for output in ("statement_pdf", "icpc_bundle") if output in set(outputs)
    )
    if not requested_outputs:
        raise ValueError("select at least one contest build output")
    active_id = config.contest_service.running_job_id(contest_id, _CONTEST_JOB_TYPE_BUILD)
    if active_id:
        return (active_id, False, "already_running")
    job_language = ""
    if "statement_pdf" in requested_outputs:
        job_language = (
            normalize_statement_language(language)
            or _resolve_contest_statement_language(contest_id)
        )
    initial_summary = {
        "job_type": _CONTEST_JOB_TYPE_BUILD,
        "contest_slug": str(contest_slug or "").strip(),
        "status": "running",
        "requested_outputs": list(requested_outputs),
        "outputs": {},
    }
    if job_language:
        initial_summary["language"] = job_language
    job_id = config.contest_service.create_job(
        contest_id,
        actor_user_id,
        _CONTEST_JOB_TYPE_BUILD,
        "running",
        initial_summary,
        finished_at=None,
    )
    missing_materializations = _freeze_contest_build_items(contest_id, job_id)
    if missing_materializations:
        detail = ", ".join(missing_materializations[:10])
        config.contest_service.update_job(
            contest_id,
            job_id,
            "failed",
            {
                **initial_summary,
                "error": f"Native materialization required; Export these problems first: {detail}",
                "missing_materializations": missing_materializations,
            },
            finished=True,
        )
        return (job_id, False, "not_ready")
    try:
        _snapshot_contest_sources(
            contest_id=contest_id,
            contest_slug=contest_slug,
            job_id=job_id,
            language=job_language,
        )
    except Exception as exc:
        config.contest_service.update_job(
            contest_id,
            job_id,
            "failed",
            {**initial_summary, "error": f"contest source snapshot failed: {exc}"},
            finished=True,
        )
        return (job_id, False, "source_snapshot_failed")

    def _runner() -> None:
        try:
            output_results: dict[str, dict[str, object]] = {}
            for output in requested_outputs:
                try:
                    if output == "statement_pdf":
                        output_results[output] = _run_contest_pdf_job_worker(
                            contest_id=contest_id,
                            contest_slug=contest_slug,
                            actor_user_id=actor_user_id,
                            job_id=job_id,
                            language=job_language,
                            insert_blank_pages=insert_blank_pages,
                            finalize=False,
                        )
                    else:
                        output_results[output] = _run_contest_package_job_worker(
                            contest_id=contest_id,
                            contest_slug=contest_slug,
                            actor_user_id=actor_user_id,
                            job_id=job_id,
                            finalize=False,
                        )
                except Exception as exc:
                    output_results[output] = {"error": str(exc), "artifact_id": ""}
            successful = [
                output
                for output, summary in output_results.items()
                if str(summary.get("artifact_id") or "") and not str(summary.get("error") or "")
            ]
            if len(successful) == len(requested_outputs):
                status = "ok"
            elif successful:
                status = "partial"
            else:
                status = "failed"
            final_summary: dict[str, object] = {
                **initial_summary,
                "status": status,
                "outputs": output_results,
                "successful_outputs": successful,
            }
            if status != "ok":
                final_summary["error"] = "; ".join(
                    f"{output}: {summary.get('error') or 'build failed'}"
                    for output, summary in output_results.items()
                    if output not in successful
                )
            config.contest_service.update_job(
                contest_id,
                job_id,
                status,
                final_summary,
                finished=True,
            )
        except Exception as exc:
            _finalize_contest_job_failure_if_running(
                contest_id=contest_id,
                job_id=job_id,
                job_type=_CONTEST_JOB_TYPE_BUILD,
                error_text=str(exc),
            )
            raise

    _future, queued, submit_reason = config.worker_queue_service.submit(
        name=f"contest-build-{contest_id}",
        fn=_runner,
        queue_name="contest-build",
        dedupe_key=f"contest:{contest_id}:build",
        job_type="contest-build",
    )
    if not queued:
        config.contest_service.update_job(
            contest_id,
            job_id,
            "failed",
            {
                "job_type": _CONTEST_JOB_TYPE_BUILD,
                "contest_slug": str(contest_slug or "").strip(),
                "error": f"queue rejected ({submit_reason})",
            },
            finished=True,
        )
    return (job_id, bool(queued), str(submit_reason or "").strip())
