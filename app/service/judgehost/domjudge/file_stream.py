import base64
import json
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from app.service.platform.runtime_blob_store import PayloadFile


# One byte below 16 MiB is divisible by three, avoiding an extra full-chunk
# allocation solely to carry base64 alignment into the next iteration.
_RAW_CHUNK_SIZE = (16 * 1024 * 1024) - 1
_STREAM_SLOTS = threading.BoundedSemaphore(16)


@dataclass(frozen=True, slots=True)
class DomjudgeDownloadFile:
    filename: str
    payload: PayloadFile
    is_executable: bool | None = None


def stream_domjudge_file_array(files: Sequence[DomjudgeDownloadFile]) -> Iterator[bytes]:
    descriptors = tuple(files)
    validate_domjudge_file_array(descriptors)

    _STREAM_SLOTS.acquire()
    try:
        yield b"["
        for index, item in enumerate(descriptors):
            if index:
                yield b","
            prefix = (
                b'{"filename":'
                + json.dumps(item.filename, ensure_ascii=False).encode("utf-8")
                + b',"content":"'
            )
            yield prefix
            carry = b""
            with item.payload.path.open("rb") as handle:
                while chunk := handle.read(_RAW_CHUNK_SIZE):
                    raw = chunk if not carry else carry + chunk
                    encoded_length = len(raw) - (len(raw) % 3)
                    if encoded_length:
                        yield base64.b64encode(raw[:encoded_length])
                    carry = raw[encoded_length:]
            if carry:
                yield base64.b64encode(carry)
            yield b'"'
            if item.is_executable is not None:
                yield b',"is_executable":' + (b"true" if item.is_executable else b"false")
            yield b"}"
        yield b"]"
    finally:
        _STREAM_SLOTS.release()


def validate_domjudge_file_array(files: Sequence[DomjudgeDownloadFile]) -> None:
    descriptors = tuple(files)
    for item in descriptors:
        if item.payload.path.is_symlink() or not item.payload.path.is_file():
            raise FileNotFoundError(item.payload.path)
        if item.payload.path.stat().st_size != item.payload.size:
            raise OSError("payload changed before DOMjudge download")
