"""Canonical Judgehost submission-language catalog."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JudgehostLanguage:
    language_id: str
    label: str
    extensions: tuple[str, ...]


JUDGEHOST_LANGUAGES = (
    JudgehostLanguage(
        language_id="cpp",
        label="C++",
        extensions=("cpp", "cc", "cxx", "c++"),
    ),
    JudgehostLanguage(
        language_id="java",
        label="Java",
        extensions=("java",),
    ),
    JudgehostLanguage(
        language_id="py",
        label="Python",
        extensions=("py",),
    ),
)

JUDGEHOST_LANGUAGE_BY_ID = {
    language.language_id: language for language in JUDGEHOST_LANGUAGES
}


def judgehost_language_for_source(source_name: str) -> JudgehostLanguage:
    """Return the supported language selected by one source basename."""

    normalized_name = source_name.lower()
    for language in JUDGEHOST_LANGUAGES:
        if any(
            normalized_name.endswith(f".{extension}")
            for extension in language.extensions
        ):
            return language
    suffix = normalized_name.rsplit(".", maxsplit=1)[-1]
    display = f".{suffix}" if "." in normalized_name else source_name
    raise ValueError(f"unsupported judgehost source extension: {display}")
