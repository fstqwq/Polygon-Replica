from __future__ import annotations

from pathlib import Path

from app.service.statement.constant import STATEMENT_LANGUAGE_REL, STATEMENT_SECTIONS_DIR


def statement_languages(workspace: Path) -> list[str]:
    root = workspace / STATEMENT_SECTIONS_DIR
    if not root.exists() or not root.is_dir() or root.is_symlink():
        return []
    result: list[str] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.is_symlink() or not child.is_dir():
                continue
            token = str(child.name or "").strip()
            if token:
                result.append(token)
    except OSError:
        return []
    return result


def read_statement_language(workspace: Path) -> str:
    marker = workspace / STATEMENT_LANGUAGE_REL
    try:
        if marker.exists() and marker.is_file() and (not marker.is_symlink()):
            token = str(marker.read_text(encoding="utf-8")).strip()
            if token:
                return token
    except OSError:
        return ""
    return ""


def pick_statement_language(workspace: Path) -> str:
    configured = read_statement_language(workspace)
    languages = statement_languages(workspace)
    if configured and configured in languages:
        return configured
    if "english" in languages:
        return "english"
    if languages:
        return languages[0]
    return "english"


def statement_editor_content_rel(workspace: Path) -> Path:
    language = pick_statement_language(workspace)
    return STATEMENT_SECTIONS_DIR / language / "legend.tex"


