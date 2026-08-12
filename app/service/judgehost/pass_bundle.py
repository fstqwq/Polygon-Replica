import io
import re
import tarfile
from dataclasses import dataclass


MARKER_NAME = ".polygon-pass-bundle"
FINAL_PASS_NAME = "final-pass-number"
HISTORICAL_FEEDBACK_BYTES_NAME = "historical-feedback-bytes"
PASS_FILE_NAMES = frozenset(
    {
        "input",
        "program.out",
        "program.err",
        "system.out",
        "program.meta",
        "compare.meta",
        "judgemessage.txt",
        "teammessage.txt",
    }
)
_PASS_PATH_RE = re.compile(r"^passes/([1-9][0-9]*)/([^/]+)$")
_CANONICAL_NUMBER_RE = re.compile(rb"^[1-9][0-9]*$")
_CANONICAL_BYTE_COUNT_RE = re.compile(rb"^(0|[1-9][0-9]*)$")
_FULL_FILES = frozenset(PASS_FILE_NAMES)
_METADATA_FILES = frozenset({"program.meta", "compare.meta"})
_FINAL_SUPPLEMENT_FILES = frozenset({"input", "teammessage.txt"})


class InvalidPassBundle(ValueError):
    pass


@dataclass(frozen=True)
class BundledPass:
    number: int
    capture_status: str
    files: dict[str, bytes]


@dataclass(frozen=True)
class PassBundle:
    final_pass_number: int
    passes: tuple[BundledPass, ...]
    historical_feedback_bytes: int | None

    def pass_files(self, number: int) -> dict[str, bytes]:
        for item in self.passes:
            if item.number == number:
                return item.files
        raise KeyError(number)


def split_pass_feedback(
    bundle: PassBundle,
    final_snapshot: bytes,
) -> tuple[dict[int, bytes], bytes]:
    """Split DOMjudge's cumulative feedback without introducing content identity."""

    previous_snapshot = b""
    feedback_by_pass: dict[int, bytes] = {}
    for item in bundle.passes[:-1]:
        current_snapshot = item.files.get("judgemessage.txt")
        if current_snapshot is None:
            continue
        feedback_by_pass[item.number] = (
            current_snapshot[len(previous_snapshot) :]
            if current_snapshot.startswith(previous_snapshot)
            else current_snapshot
        )
        previous_snapshot = current_snapshot
    if previous_snapshot:
        final_feedback = (
            final_snapshot[len(previous_snapshot) :]
            if final_snapshot.startswith(previous_snapshot)
            else final_snapshot
        )
        return feedback_by_pass, final_feedback
    if bundle.historical_feedback_bytes is None:
        return feedback_by_pass, final_snapshot
    offset = bundle.historical_feedback_bytes
    if offset > len(final_snapshot):
        raise InvalidPassBundle("feedback byte count exceeds the callback payload")
    return feedback_by_pass, final_snapshot[offset:]


def _looks_like_bundle(payload: bytes) -> bool:
    return (
        payload.startswith(MARKER_NAME.encode("ascii"))
        or (MARKER_NAME.encode("ascii") + b"\0") in payload[:8192]
    )


def _read_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    max_member_bytes: int,
) -> bytes:
    if not member.isreg():
        raise InvalidPassBundle("pass bundle contains a non-regular entry")
    if member.size < 0 or member.size > max_member_bytes:
        raise InvalidPassBundle("pass bundle entry exceeds the byte limit")
    stream = archive.extractfile(member)
    if stream is None:
        raise InvalidPassBundle("pass bundle entry is unreadable")
    payload = stream.read(max_member_bytes + 1)
    if len(payload) != member.size or len(payload) > max_member_bytes:
        raise InvalidPassBundle("pass bundle entry is truncated or oversized")
    return payload


def _capture_status(
    *,
    number: int,
    final_pass_number: int,
    names: frozenset[str],
) -> str:
    if number == final_pass_number:
        if names != _FINAL_SUPPLEMENT_FILES:
            raise InvalidPassBundle("final pass bundle supplement is incomplete")
        return "complete"
    if names == _FULL_FILES:
        return "complete"
    if names == _METADATA_FILES:
        return "metadata-only"
    raise InvalidPassBundle("historical pass capture has an invalid file set")


def parse_pass_bundle(
    payload: bytes,
    *,
    max_bundle_bytes: int,
    max_member_bytes: int,
) -> PassBundle | None:
    """Parse the marker tar carried by DOMjudge's existing team_message field.

    Ordinary team messages are returned as ``None``. Once the strict marker is
    visible, malformed input is an invalid bundle rather than a team message.
    """

    raw = bytes(payload)
    if not raw:
        return None
    looks_like_bundle = _looks_like_bundle(raw)
    if len(raw) > max_bundle_bytes:
        if looks_like_bundle:
            raise InvalidPassBundle("pass bundle exceeds the byte limit")
        return None
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.TarError as exc:
        if looks_like_bundle:
            raise InvalidPassBundle("pass bundle tar is malformed") from exc
        return None
    with archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if MARKER_NAME not in names:
            return None
        if names.count(MARKER_NAME) != 1 or names.count(FINAL_PASS_NAME) != 1:
            raise InvalidPassBundle("pass bundle marker entries are not unique")
        if len(names) != len(set(names)):
            raise InvalidPassBundle("pass bundle contains duplicate entries")
        payload_by_name: dict[str, bytes] = {}
        total_bytes = 0
        for member in members:
            name = member.name
            if name.startswith("/") or "\\" in name or ".." in name.split("/"):
                raise InvalidPassBundle("pass bundle contains an unsafe path")
            if name not in {
                MARKER_NAME,
                FINAL_PASS_NAME,
                HISTORICAL_FEEDBACK_BYTES_NAME,
            }:
                match = _PASS_PATH_RE.fullmatch(name)
                if match is None or match.group(2) not in PASS_FILE_NAMES:
                    raise InvalidPassBundle("pass bundle contains an unknown entry")
            item = _read_member(
                archive,
                member,
                max_member_bytes=max_member_bytes,
            )
            total_bytes += len(item)
            if total_bytes > max_bundle_bytes:
                raise InvalidPassBundle("pass bundle contents exceed the byte limit")
            payload_by_name[name] = item

    if payload_by_name[MARKER_NAME] != b"":
        raise InvalidPassBundle("pass bundle marker must be empty")
    final_number_raw = payload_by_name[FINAL_PASS_NAME].strip()
    if _CANONICAL_NUMBER_RE.fullmatch(final_number_raw) is None:
        raise InvalidPassBundle("final pass number is not canonical")
    final_pass_number = int(final_number_raw)
    feedback_bytes_raw = payload_by_name.get(HISTORICAL_FEEDBACK_BYTES_NAME)
    historical_feedback_bytes: int | None = None
    if feedback_bytes_raw is not None:
        feedback_bytes_text = feedback_bytes_raw.strip()
        if _CANONICAL_BYTE_COUNT_RE.fullmatch(feedback_bytes_text) is None:
            raise InvalidPassBundle("historical feedback byte count is not canonical")
        historical_feedback_bytes = int(feedback_bytes_text)
    pass_files: dict[int, dict[str, bytes]] = {}
    for path, item in payload_by_name.items():
        match = _PASS_PATH_RE.fullmatch(path)
        if match is None:
            continue
        number_text = match.group(1)
        number = int(number_text)
        if str(number) != number_text or number > final_pass_number:
            raise InvalidPassBundle("pass number is not canonical")
        pass_files.setdefault(number, {})[match.group(2)] = item
    expected_numbers = tuple(range(1, final_pass_number + 1))
    if tuple(sorted(pass_files)) != expected_numbers:
        raise InvalidPassBundle("pass bundle numbers are not contiguous")
    passes = tuple(
        BundledPass(
            number=number,
            capture_status=_capture_status(
                number=number,
                final_pass_number=final_pass_number,
                names=frozenset(pass_files[number]),
            ),
            files=pass_files[number],
        )
        for number in expected_numbers
    )
    historical_statuses = {
        item.capture_status for item in passes[:-1]
    }
    if len(historical_statuses) > 1:
        raise InvalidPassBundle("historical passes use mixed capture profiles")
    metadata_only = historical_statuses == {"metadata-only"}
    if metadata_only != (historical_feedback_bytes is not None):
        raise InvalidPassBundle(
            "metadata-only capture requires the historical feedback byte count"
        )
    return PassBundle(
        final_pass_number=final_pass_number,
        passes=passes,
        historical_feedback_bytes=historical_feedback_bytes,
    )
