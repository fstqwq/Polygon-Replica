"""Shared LaTeX failure text for disposable Statement previews."""

from collections.abc import Sequence

from app.service.platform.error_text import (
    sanitize_log_text_for_ui,
    truncate_utf8_text_bytes,
)


def latex_log_for_display(
    raw: str,
    *,
    path_prefixes: Sequence[tuple[str, str]] = (),
) -> str:
    """Return a safe complete LaTeX log while preserving TeX commands."""

    return sanitize_log_text_for_ui(
        raw,
        path_prefixes=list(path_prefixes),
        normalize_path_separators=False,
    )


def latex_error_excerpt(
    raw: str,
    *,
    max_bytes: int,
    path_prefixes: Sequence[tuple[str, str]] = (),
    require_error: bool = False,
) -> str:
    """Return bounded context beginning at the first real LaTeX error.

    When ``require_error`` is true, the result begins at a TeX ``!`` diagnostic.
    """

    lines = [
        line.rstrip()
        for line in latex_log_for_display(
            raw,
            path_prefixes=path_prefixes,
        ).splitlines()
    ]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    error_line = next(
        (
            index
            for index, line in enumerate(lines)
            if (stripped := line.lstrip()).startswith("!")
            and stripped[1:].strip()
        ),
        None,
    )
    if require_error and error_line is None:
        return ""
    relevant = lines[error_line:] if error_line is not None else lines
    return truncate_utf8_text_bytes(
        "\n".join(relevant),
        max_bytes=max_bytes,
    )[0]


def latex_failure_text(error: str, latex_log: str) -> str:
    """Combine the bounded error context and complete log for plain text UI."""

    detail = error.rstrip()
    complete_log = latex_log.rstrip()
    if not complete_log:
        return detail + "\n"
    return f"{detail}\n\nlatex.log\n\n{complete_log}\n"
