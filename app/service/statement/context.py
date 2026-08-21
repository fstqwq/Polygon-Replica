import re
from pathlib import Path

from app.service.statement.constant import STATEMENT_SECTIONS_DIR

_LANGUAGE_PRIORITY = ("english", "chinese")
_LANGUAGE_TOKEN_RE = re.compile(r"[^a-z0-9-]+")


def statement_language_sort_key(language: str) -> tuple[int, str]:
    try:
        return (_LANGUAGE_PRIORITY.index(language), language)
    except ValueError:
        return (len(_LANGUAGE_PRIORITY), language)


def statement_languages(workspace: Path) -> list[str]:
    """Return available languages sorted by priority: english, chinese, then alphabetical."""
    root = workspace / STATEMENT_SECTIONS_DIR
    if not root.exists() or not root.is_dir() or root.is_symlink():
        return []
    raw: list[str] = []
    try:
        for child in root.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            token = child.name.strip()
            if token:
                raw.append(token)
    except OSError:
        return []

    return sorted(raw, key=statement_language_sort_key)


def normalize_statement_language(raw: object) -> str:
    token = str(raw or "").strip().lower().replace("_", "-")
    token = _LANGUAGE_TOKEN_RE.sub("-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token


def pick_statement_language(workspace: Path) -> str:
    """Return the first language by priority order, defaulting to ``"english"``."""
    languages = statement_languages(workspace)
    if languages:
        return languages[0]
    return "english"
