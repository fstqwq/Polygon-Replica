from __future__ import annotations

import codecs
import tempfile
import zipfile
from pathlib import Path


def validate_zip_entry_size(
    info: zipfile.ZipInfo,
    *,
    total_before: int,
    max_file_bytes: int,
    max_total_bytes: int,
    display_name: str,
    entry_too_large_prefix: str,
    payload_too_large_prefix: str,
) -> int:
    entry_size = int(info.file_size)
    if entry_size > max_file_bytes:
        raise ValueError(f"{entry_too_large_prefix}: {display_name}")
    if total_before + entry_size > max_total_bytes:
        raise ValueError(f"{payload_too_large_prefix}: {display_name}")
    return entry_size


def _normalize_decoded_text_chunk(text: str, *, pending_cr: bool, final: bool) -> tuple[str, bool]:
    prefix = ""
    if pending_cr:
        prefix = "\n"
        if text.startswith("\n"):
            text = text[1:]
    next_pending_cr = bool(text.endswith("\r") and not final)
    if next_pending_cr:
        text = text[:-1]
    return (prefix + text.replace("\r\n", "\n").replace("\r", "\n"), next_pending_cr)


def extract_zip_entry_to_path_limited(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    total_before: int,
    max_file_bytes: int,
    max_total_bytes: int,
    display_name: str,
    entry_too_large_prefix: str,
    payload_too_large_prefix: str,
    normalize_utf8_newlines: bool,
    chunk_size: int = 1024 * 1024,
) -> int:
    validate_zip_entry_size(
        info,
        total_before=total_before,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        display_name=display_name,
        entry_too_large_prefix=entry_too_large_prefix,
        payload_too_large_prefix=payload_too_large_prefix,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if normalize_utf8_newlines:
        return _extract_zip_text_or_binary_entry(
            archive,
            info,
            target,
            total_before=total_before,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            display_name=display_name,
            entry_too_large_prefix=entry_too_large_prefix,
            payload_too_large_prefix=payload_too_large_prefix,
            chunk_size=chunk_size,
        )
    return _extract_zip_binary_entry(
        archive,
        info,
        target,
        total_before=total_before,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        display_name=display_name,
        entry_too_large_prefix=entry_too_large_prefix,
        payload_too_large_prefix=payload_too_large_prefix,
        chunk_size=chunk_size,
    )


def _check_written_size(
    written: int,
    *,
    total_before: int,
    max_file_bytes: int,
    max_total_bytes: int,
    display_name: str,
    entry_too_large_prefix: str,
    payload_too_large_prefix: str,
) -> None:
    if written > max_file_bytes:
        raise ValueError(f"{entry_too_large_prefix}: {display_name}")
    if total_before + written > max_total_bytes:
        raise ValueError(f"{payload_too_large_prefix}: {display_name}")


def _extract_zip_binary_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    total_before: int,
    max_file_bytes: int,
    max_total_bytes: int,
    display_name: str,
    entry_too_large_prefix: str,
    payload_too_large_prefix: str,
    chunk_size: int,
) -> int:
    written = 0
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent, prefix=f".{target.name}.zip-", suffix=".tmp") as handle:
            tmp_path = Path(handle.name)
            with archive.open(info, "r") as source:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    written += len(chunk)
                    _check_written_size(
                        written,
                        total_before=total_before,
                        max_file_bytes=max_file_bytes,
                        max_total_bytes=max_total_bytes,
                        display_name=display_name,
                        entry_too_large_prefix=entry_too_large_prefix,
                        payload_too_large_prefix=payload_too_large_prefix,
                    )
                    handle.write(chunk)
        assert tmp_path is not None
        tmp_path.replace(target)
        return written
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _extract_zip_text_or_binary_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    total_before: int,
    max_file_bytes: int,
    max_total_bytes: int,
    display_name: str,
    entry_too_large_prefix: str,
    payload_too_large_prefix: str,
    chunk_size: int,
) -> int:
    raw_tmp: Path | None = None
    text_tmp: Path | None = None
    text_possible = True
    pending_cr = False
    decoder = codecs.getincrementaldecoder("utf-8")()
    written = 0
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent, prefix=f".{target.name}.zip-raw-", suffix=".tmp") as raw_handle:
            raw_tmp = Path(raw_handle.name)
            with tempfile.NamedTemporaryFile(delete=False, dir=target.parent, prefix=f".{target.name}.zip-text-", suffix=".tmp") as text_handle:
                text_tmp = Path(text_handle.name)
                with archive.open(info, "r") as source:
                    while True:
                        chunk = source.read(chunk_size)
                        if not chunk:
                            break
                        written += len(chunk)
                        _check_written_size(
                            written,
                            total_before=total_before,
                            max_file_bytes=max_file_bytes,
                            max_total_bytes=max_total_bytes,
                            display_name=display_name,
                            entry_too_large_prefix=entry_too_large_prefix,
                            payload_too_large_prefix=payload_too_large_prefix,
                        )
                        raw_handle.write(chunk)
                        if text_possible:
                            try:
                                decoded = decoder.decode(chunk, final=False)
                                normalized, pending_cr = _normalize_decoded_text_chunk(decoded, pending_cr=pending_cr, final=False)
                                text_handle.write(normalized.encode("utf-8"))
                            except UnicodeDecodeError:
                                text_possible = False
                    if text_possible:
                        try:
                            decoded_tail = decoder.decode(b"", final=True)
                            normalized_tail, pending_cr = _normalize_decoded_text_chunk(decoded_tail, pending_cr=pending_cr, final=True)
                            text_handle.write(normalized_tail.encode("utf-8"))
                        except UnicodeDecodeError:
                            text_possible = False
        assert raw_tmp is not None
        assert text_tmp is not None
        source_tmp = text_tmp if text_possible else raw_tmp
        source_tmp.replace(target)
        if source_tmp != raw_tmp:
            raw_tmp.unlink(missing_ok=True)
        if source_tmp != text_tmp:
            text_tmp.unlink(missing_ok=True)
        return written
    except Exception:
        if raw_tmp is not None:
            raw_tmp.unlink(missing_ok=True)
        if text_tmp is not None:
            text_tmp.unlink(missing_ok=True)
        raise
