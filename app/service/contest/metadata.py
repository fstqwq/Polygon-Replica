"""Contest-owned metadata transformation for canonical problem packages."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import uuid
import zipfile

from app.main_constant import CONTEST_IDENT_RE
from app.service.importing.archive import (
    ArchiveView,
    PACKAGE_METADATA_MAX_BYTES,
    problem_archive_policy,
)


_DOMJUDGE_METADATA_NAME = "domjudge-problem.ini"
_SHORT_NAME_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t]*short-name[ \t]*=[ \t]*)(?P<value>.*)$",
    re.IGNORECASE,
)


def _validated_short_name(value: str) -> str:
    if not CONTEST_IDENT_RE.fullmatch(value):
        raise ValueError("invalid contest problem short-name")
    return value


def _validate_member(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise ValueError(f"encrypted contest package entry: {info.filename}")
    mode = (int(info.external_attr) >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise ValueError(f"contest package contains a symlink: {info.filename}")
    if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(f"contest package contains a special file: {info.filename}")


def _rewrite_short_name(payload: bytes, short_name: str) -> bytes:
    if len(payload) > PACKAGE_METADATA_MAX_BYTES:
        raise ValueError("DOMjudge problem metadata is too large")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("DOMjudge problem metadata is not UTF-8") from exc
    if "\x00" in text:
        raise ValueError("DOMjudge problem metadata contains a NUL byte")

    lines = text.splitlines(keepends=True)
    matches: list[tuple[int, re.Match[str], str]] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = _SHORT_NAME_LINE_RE.fullmatch(body)
        if match is not None:
            matches.append((index, match, ending))
    if len(matches) != 1:
        raise ValueError(
            "DOMjudge problem metadata must contain exactly one short-name entry"
        )

    index, match, ending = matches[0]
    existing = match.group("value").strip()
    if not existing or any(ord(char) < 32 for char in existing):
        raise ValueError("DOMjudge problem metadata has an invalid short-name")
    if index + 1 < len(lines):
        following = lines[index + 1].rstrip("\r\n")
        if following.startswith((" ", "\t")) and following.strip():
            raise ValueError("DOMjudge short-name must be a single line")

    lines[index] = f"{match.group('prefix')}{short_name}{ending}"
    return "".join(lines).encode("utf-8")


def _extract_package(
    archive: ArchiveView,
    destination: Path,
) -> list[tuple[str, zipfile.ZipInfo]]:
    members = list(archive.entries.items())
    for name, info in members:
        _validate_member(info)
        target = destination / Path(*name.split("/"))
        mode = (int(info.external_attr) >> 16) & 0o7777
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            archive.zip_file.copy_to(info, target)
        if mode:
            target.chmod(mode)
    return members


def _write_archive(
    target: Path,
    source_root: Path,
    members: list[tuple[str, zipfile.ZipInfo]],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    try:
        with zipfile.ZipFile(
            partial,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, info in members:
                source = source_root / Path(*name.split("/"))
                archive.write(
                    source,
                    arcname=f"{name}/" if info.is_dir() else name,
                )
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def materialize_contest_problem_package(
    canonical_archive: Path,
    target_archive: Path,
    *,
    short_name: str,
    staging_parent: Path,
    max_expanded_bytes: int,
) -> None:
    """Create one contest-owned package variant from a canonical ICPC ZIP."""

    safe_short_name = _validated_short_name(short_name)
    source = canonical_archive.resolve()
    if canonical_archive.is_symlink() or not source.is_file():
        raise ValueError("canonical problem package is unavailable")
    if target_archive.resolve() == source:
        raise ValueError("contest package target must differ from canonical artifact")

    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = staging_parent / f"contest-package-{uuid.uuid4().hex}"
    extraction = staging / "package"
    staged_archive = staging / "canonical.zip"
    try:
        extraction.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source, staged_archive)
        with ArchiveView(
            staged_archive,
            problem_archive_policy(max_expanded_bytes),
        ) as archive:
            metadata_names = [
                name
                for name in archive.entries
                if name.casefold() == _DOMJUDGE_METADATA_NAME.casefold()
            ]
            if metadata_names != [_DOMJUDGE_METADATA_NAME]:
                raise ValueError(
                    "contest package requires one root domjudge-problem.ini"
                )
            if archive.entries[_DOMJUDGE_METADATA_NAME].is_dir():
                raise ValueError("domjudge-problem.ini must be a regular file")
            members = _extract_package(archive, extraction)

        metadata_path = extraction / _DOMJUDGE_METADATA_NAME
        if metadata_path.stat().st_size > PACKAGE_METADATA_MAX_BYTES:
            raise ValueError("DOMjudge problem metadata is too large")
        metadata_path.write_bytes(
            _rewrite_short_name(metadata_path.read_bytes(), safe_short_name)
        )
        _write_archive(target_archive, extraction, members)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
