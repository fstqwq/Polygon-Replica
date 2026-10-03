"""Canonical Contest property mapping and localized FTL context projection."""

import re
from collections.abc import Mapping

from app.main_constant import PROBLEM_IDENT_RE, PROBLEM_ID_MAX_LEN
from app.service.statement.context import normalize_statement_language


INSERT_BLANK_PAGE_PROPERTY = "insertBlankPage"
BANNER_PROPERTY = "banner"
REQUIRED_CONTEST_PROPERTY_KEYS = ("title",)
CONTEST_TEMPLATE_PROPERTY_DEFAULTS = {
    BANNER_PROPERTY: "",
    INSERT_BLANK_PAGE_PROPERTY: "false",
}
DEFAULT_CONTEST_BANNER = r"""\ifdefined\thecontestname
    \parbox{\textwidth}{
        \sffamily
        \begin{center}
            \contestname
        \end{center}
    }
\fi"""
NON_LOCALIZABLE_CONTEST_PROPERTY_KEYS = (INSERT_BLANK_PAGE_PROPERTY,)
RESERVED_CONTEST_TEMPLATE_KEYS = (
    "contest",
    "language",
    "properties",
    "providedStatementsCommands",
    "shortProblemTitle",
    "statements",
)
_PROPERTY_NAME_RE = re.compile(r"[a-z][A-Za-z0-9_]{0,63}")


def is_contest_problem_mark(key: str) -> bool:
    """Distinguish the problem-mark namespace from localized properties."""
    return key.startswith("mark.") and "/" in key


def contest_property_base_key(key: str) -> str:
    return key if is_contest_problem_mark(key) else key.partition(".")[0]


def localized_contest_property_key(base_key: str, language: str) -> str:
    """Return the canonical ``base.language`` override key."""

    safe_base_key = _normalize_property_name(base_key)
    if safe_base_key in NON_LOCALIZABLE_CONTEST_PROPERTY_KEYS:
        raise ValueError(f"contest property is not localizable: {safe_base_key}")
    safe_language = normalize_statement_language(language)
    if not safe_language:
        raise ValueError("contest property language is required")
    return f"{safe_base_key}.{safe_language}"


def _normalize_property_name(value: str) -> str:
    safe_name = str(value or "").strip()
    if not _PROPERTY_NAME_RE.fullmatch(safe_name):
        raise ValueError(f"invalid contest property name: {safe_name or '(empty)'}")
    if safe_name in RESERVED_CONTEST_TEMPLATE_KEYS:
        raise ValueError(f"contest property name is reserved: {safe_name}")
    return safe_name


def normalize_contest_property_key(key: str) -> str:
    """Validate a user-provided Contest FTL context key."""

    raw_key = str(key).strip()
    if is_contest_problem_mark(raw_key):
        problem = raw_key.removeprefix("mark.")
        if len(problem) > PROBLEM_ID_MAX_LEN or not PROBLEM_IDENT_RE.fullmatch(problem):
            raise ValueError(f"invalid contest problem mark: {raw_key}")
        return raw_key
    if raw_key.count(".") > 1:
        raise ValueError(f"invalid contest property key: {raw_key}")
    base_key, separator, language = raw_key.partition(".")
    safe_base_key = _normalize_property_name(base_key)
    if not separator:
        return safe_base_key
    return localized_contest_property_key(safe_base_key, language)


def contest_property_language(key: str) -> str:
    """Return a property's explicit override language, or an empty string."""

    safe_key = normalize_contest_property_key(key)
    if is_contest_problem_mark(safe_key):
        return ""
    _base_key, separator, language = safe_key.partition(".")
    return language if separator else ""


def contest_property_is_deletable(key: str) -> bool:
    """Return whether a complete property group may be removed."""

    safe_key = normalize_contest_property_key(key)
    base_key = safe_key.partition(".")[0]
    return base_key not in REQUIRED_CONTEST_PROPERTY_KEYS


def localized_contest_properties(
    properties: Mapping[str, str],
    language: str,
) -> dict[str, str]:
    """Resolve every language override over its base FTL context value."""

    result = {
        key: value
        for key, value in properties.items()
        if "." not in key
    }
    safe_language = normalize_statement_language(language)
    if not safe_language:
        return result
    suffix = f".{safe_language}"
    for key, value in properties.items():
        if not is_contest_problem_mark(key) and key.endswith(suffix):
            result[key.removesuffix(suffix)] = value
    return result


def contest_template_properties(
    properties: Mapping[str, str],
) -> dict[str, str | bool]:
    """Project effective property strings into their FTL runtime values."""

    result: dict[str, str | bool] = dict(CONTEST_TEMPLATE_PROPERTY_DEFAULTS)
    result.update((key, value) for key, value in properties.items() if not is_contest_problem_mark(key))
    result[INSERT_BLANK_PAGE_PROPERTY] = (
        result[INSERT_BLANK_PAGE_PROPERTY] == "true"
    )
    return result
