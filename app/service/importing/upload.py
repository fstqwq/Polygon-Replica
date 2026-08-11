"""Bounded upload spooling for archive import entry points."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
import os
from pathlib import Path
import tempfile
from typing import BinaryIO

from fastapi import UploadFile

from app.main_util import write_fileobj_limited, write_upload_file_limited


def _temporary_path(root: Path) -> tuple[int, Path]:
    root.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix="archive-upload-", suffix=".zip", dir=root)
    return descriptor, Path(raw_path)


@contextmanager
def spool_fileobj(
    fileobj: BinaryIO,
    *,
    root: Path,
    max_bytes: int,
    label: str,
) -> Iterator[Path]:
    """Write a synchronous multipart stream to a temporary archive file."""

    descriptor, path = _temporary_path(root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            written = write_fileobj_limited(
                fileobj,
                handle,
                max_bytes=max_bytes,
                label=label,
            )
        if written == 0:
            raise ValueError(f"{label} is empty")
        yield path
    finally:
        path.unlink(missing_ok=True)


@asynccontextmanager
async def spool_upload(
    upload: UploadFile,
    *,
    root: Path,
    max_bytes: int,
    label: str,
) -> AsyncIterator[Path]:
    """Write an asynchronous multipart stream to a temporary archive file."""

    descriptor, path = _temporary_path(root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            written = await write_upload_file_limited(
                upload,
                handle,
                max_bytes=max_bytes,
                label=label,
            )
        if written == 0:
            raise ValueError(f"{label} is empty")
        yield path
    finally:
        path.unlink(missing_ok=True)
