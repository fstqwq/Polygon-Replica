import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass

from app.service.judgehost.pass_bundle import (
    InvalidPassBundle,
    PassBundle,
    parse_pass_bundle,
    split_pass_feedback,
)
from app.service.judgehost.result_normalizer import (
    CapturedCaseArtifact,
    pass_cache_file_name,
)
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore


_PASS_CAPTURE_TASK_KINDS = frozenset({"main-correct", "solution-run"})


@dataclass(frozen=True, slots=True)
class CaseArtifactRequest:
    payload: Mapping[str, object]
    task_kind: str
    interactive: bool
    pass_limit: int
    callback_pass: int
    bundle_limit_bytes: int


@dataclass(frozen=True, slots=True)
class PreparedCaseArtifacts:
    files: dict[str, bytes]
    pass_bundle: PassBundle | None
    warning: str


@dataclass(frozen=True, slots=True)
class CapturedCaseArtifacts:
    payloads: dict[str, PayloadFile]
    artifacts: dict[str, CapturedCaseArtifact]
    pass_bundle: PassBundle | None
    warning: str


def _decode_base64(value: str) -> bytes:
    try:
        raw = value.strip().encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "DOMjudge payload must be base64 ASCII text"
        ) from exc
    if not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("DOMjudge payload is not valid base64") from exc


def decode_callback_blob(value: object) -> bytes:
    """Decode one canonical bounded upload field without coercive allocation."""

    if value is None:
        return b""
    if isinstance(value, str):
        return _decode_base64(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise RuntimeError(
        "DOMjudge payload must be base64 text or raw bytes"
    )


class CaseArtifactCapture:
    """Own bounded final-result payload selection and runtime blob capture."""

    def __init__(self, blob_store: RuntimeBlobStore) -> None:
        self._blob_store = blob_store

    @staticmethod
    def prepare(
        request: CaseArtifactRequest,
    ) -> PreparedCaseArtifacts:
        """Validate and select bounded callback fields without writing blobs."""

        payload = request.payload
        files: dict[str, bytes] = {}

        def capture_payload_file(
            name: str,
            value: object,
            *,
            allow_empty: bool = False,
        ) -> bytes:
            if value is None:
                if allow_empty:
                    files[name] = b""
                return b""
            raw = decode_callback_blob(value)
            if not raw and not allow_empty:
                return b""
            files[name] = raw
            return raw

        if request.task_kind != "compile-only":
            # Generator output is semantic input for downstream tasks.
            capture_payload_file(
                "program.out",
                payload.get("output_run"),
                allow_empty=True,
            )
        capture_payload_file(
            "program.err",
            payload.get("output_error"),
            allow_empty=True,
        )
        capture_payload_file(
            "system.out",
            payload.get("output_system"),
            allow_empty=True,
        )
        capture_payload_file(
            "judgemessage.txt",
            payload.get("output_diff"),
            allow_empty=True,
        )
        capture_payload_file(
            "program.meta",
            payload.get("metadata"),
            allow_empty=True,
        )
        capture_payload_file(
            "compare.meta",
            payload.get("compare_metadata"),
            allow_empty=True,
        )
        team_message = decode_callback_blob(payload.get("team_message"))
        capture_expected = request.task_kind in _PASS_CAPTURE_TASK_KINDS and (
            request.interactive or request.pass_limit > 1
        )
        pass_bundle: PassBundle | None = None
        warning = ""
        rejected_bundle = False
        if capture_expected:
            try:
                pass_bundle = parse_pass_bundle(
                    team_message,
                    max_bundle_bytes=request.bundle_limit_bytes,
                    max_member_bytes=request.bundle_limit_bytes,
                )
                if (
                    pass_bundle is not None
                    and request.callback_pass > 0
                    and request.callback_pass != pass_bundle.final_pass_number
                ):
                    raise InvalidPassBundle(
                        "callback pass does not match final-pass-number"
                    )
            except InvalidPassBundle as exc:
                pass_bundle = None
                rejected_bundle = True
                warning = (
                    "historical pass artifact capture was incomplete: "
                    + str(exc)
                )
        if pass_bundle is None:
            files["teammessage.txt"] = b"" if rejected_bundle else team_message
            if capture_expected and not warning:
                warning = (
                    "historical pass artifact capture was incomplete: "
                    "bundle missing"
                )
        else:
            historical_feedback: dict[int, bytes] = {}
            final_feedback = files["judgemessage.txt"]
            try:
                historical_feedback, final_feedback = split_pass_feedback(
                    pass_bundle,
                    final_feedback,
                )
            except InvalidPassBundle as exc:
                warning = (
                    "historical pass artifact capture was incomplete: "
                    + str(exc)
                )
            for bundled_pass in pass_bundle.passes:
                for name, content in bundled_pass.files.items():
                    stored_content = (
                        historical_feedback.get(bundled_pass.number, content)
                        if name == "judgemessage.txt"
                        else content
                    )
                    cache_name = pass_cache_file_name(bundled_pass.number, name)
                    files[cache_name] = stored_content
            final_files = pass_bundle.pass_files(pass_bundle.final_pass_number)
            files["teammessage.txt"] = final_files["teammessage.txt"]
            files["judgemessage.txt"] = final_feedback
            reduced: dict[str, list[int]] = {}
            for bundled_pass in pass_bundle.passes[:-1]:
                if bundled_pass.capture_status != "complete":
                    reduced.setdefault(bundled_pass.capture_status, []).append(
                        bundled_pass.number
                    )
            if reduced:
                groups = [
                    f"passes {', '.join(str(number) for number in numbers)} "
                    f"{status}"
                    for status, numbers in reduced.items()
                ]
                reduced_warning = (
                    "historical pass artifacts were reduced: " + "; ".join(groups)
                )
                warning = (
                    f"{warning}; {reduced_warning}"
                    if warning
                    else reduced_warning
                )
        return PreparedCaseArtifacts(
            files=files,
            pass_bundle=pass_bundle,
            warning=warning,
        )

    def capture(
        self,
        prepared: PreparedCaseArtifacts,
    ) -> CapturedCaseArtifacts:
        """Persist one prepared field set and return canonical blob locators."""

        payloads = {
            name: self._blob_store.put_bytes(content)
            for name, content in prepared.files.items()
        }
        artifacts = {
            name: CapturedCaseArtifact(
                content=content,
                blob_ref=payloads[name].blob_ref or "",
            )
            for name, content in prepared.files.items()
        }
        return CapturedCaseArtifacts(
            payloads=payloads,
            artifacts=artifacts,
            pass_bundle=prepared.pass_bundle,
            warning=prepared.warning,
        )
