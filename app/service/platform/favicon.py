"""Stable favicon selection for problem and contest pages."""

import hashlib


_PROBLEM_FIRST_CARD = 1
_PROBLEM_CARD_COUNT = 16
_CONTEST_FIRST_CARD = 17
_CONTEST_CARD_COUNT = 5


def problem_favicon_asset(problem_slug: str) -> str:
    """Return the stable Major Arcana favicon assigned to one problem."""
    return _major_arcana_asset(
        namespace="problem",
        identity=problem_slug,
        first_card=_PROBLEM_FIRST_CARD,
        card_count=_PROBLEM_CARD_COUNT,
    )


def contest_favicon_asset(contest_slug: str) -> str:
    """Return the stable Major Arcana favicon assigned to one contest."""
    return _major_arcana_asset(
        namespace="contest",
        identity=contest_slug,
        first_card=_CONTEST_FIRST_CARD,
        card_count=_CONTEST_CARD_COUNT,
    )


def _major_arcana_asset(
    *,
    namespace: str,
    identity: str,
    first_card: int,
    card_count: int,
) -> str:
    payload = f"{namespace}\0{identity}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    offset = int.from_bytes(digest[:8], byteorder="big") % card_count
    return f"favicon/major-arcana/{first_card + offset:02d}.png"
