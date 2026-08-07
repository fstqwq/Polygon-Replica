from __future__ import annotations

from collections.abc import Iterable
from unittest import TestCase


def assert_html_contract(
    testcase: TestCase,
    html: str,
    *,
    contains: Iterable[str] = (),
    excludes: Iterable[str] = (),
    label: str = "HTML",
) -> None:
    """Report grouped copy/layout regressions without hiding the useful diff."""
    required = tuple(dict.fromkeys(contains))
    forbidden = tuple(dict.fromkeys(excludes))
    missing = [fragment for fragment in required if fragment not in html]
    present = [fragment for fragment in forbidden if fragment in html]
    testcase.assertFalse(
        missing or present,
        f"{label}: missing={missing!r}; forbidden={present!r}",
    )
