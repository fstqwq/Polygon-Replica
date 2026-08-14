# ascii-lint: allow; reason=chinese-test

import io
import unittest
from decimal import Decimal

from app.service.judgehost.callback.runpipe_transcript import parse_runpipe_transcript


def _frame(milliseconds: int, direction: bytes, payload: bytes) -> bytes:
    seconds, millis = divmod(milliseconds, 1000)
    header = f"[{seconds:3d}.{millis:03d}s/{len(payload)}]".encode("ascii")
    return header + direction + b": " + payload + b"\n"


def _eof(milliseconds: int, direction: bytes) -> bytes:
    seconds, millis = divmod(milliseconds, 1000)
    header = f"[{seconds:3d}.{millis:03d}s/0]".encode("ascii")
    return header + direction


def _parse(raw: bytes, **kwargs: int):
    return parse_runpipe_transcript(
        io.BytesIO(raw),
        raw_size_bytes=len(raw),
        **kwargs,
    )


class _ShortReadStream:
    def __init__(self, raw: bytes, chunk_size: int) -> None:
        self._stream = io.BytesIO(raw)
        self._chunk_size = chunk_size

    def read(self, length: int = -1) -> bytes:
        if length < 0:
            length = self._chunk_size
        return self._stream.read(min(length, self._chunk_size))


class TestRunpipeTranscript(unittest.TestCase):
    def test_parses_multiline_and_consecutive_frames_without_guessing_payload(self) -> None:
        first = b"jury:\n> fake\n[  9.999s/1]<: x"
        second = "答案 ✓".encode()
        raw = _frame(19, b">", first) + _frame(24, b"<", second)

        transcript = _parse(raw)

        self.assertEqual(transcript["state"], "ok")
        self.assertEqual(transcript["events_total"], 2)
        self.assertEqual(
            [event["source"] for event in transcript["events"]],
            ["interactor", "solution"],
        )
        self.assertEqual(transcript["events"][0]["payload_display"], first.decode())
        self.assertEqual(
            transcript["events"][1]["payload_display"],
            "答案 ✓",
        )
        self.assertEqual(transcript["events"][0]["timestamp_seconds"], Decimal("0.019"))

    def test_header_and_payload_may_span_short_stream_reads(self) -> None:
        raw = _frame(1001, b">", b"abcdef") + _frame(1002, b"<", b"ghijkl")
        stream = _ShortReadStream(raw, chunk_size=2)

        transcript = parse_runpipe_transcript(stream, raw_size_bytes=len(raw))

        self.assertEqual(transcript["state"], "ok")
        self.assertEqual(
            [event["payload_display"] for event in transcript["events"]],
            ["abcdef", "ghijkl"],
        )

    def test_sanitizes_invalid_utf8_controls_and_bidi_but_preserves_tabs_and_lines(self) -> None:
        payload = b"a\tline\ncr\rnull\x00bad\xff" + "\u202e\u200b".encode()

        event = _parse(_frame(1, b">", payload))["events"][0]

        self.assertEqual(
            event["payload_display"],
            "a\tline\ncr\\x0dnull\\x00bad\\xff\\u202e\\u200b",
        )
        self.assertEqual(event["payload_bytes"], len(payload))

    def test_zero_length_data_and_adjacent_eof_markers_are_distinct_events(self) -> None:
        raw = _frame(1, b">", b"") + _eof(2, b"]") + _eof(3, b"[")

        transcript = _parse(raw)

        self.assertEqual(transcript["state"], "ok")
        self.assertEqual(
            [
                (event["kind"], event["source"], event["payload_display"])
                for event in transcript["events"]
            ],
            [
                ("data", "interactor", "(empty)"),
                ("eof", "interactor", "closed output"),
                ("eof", "solution", "closed output"),
            ],
        )

    def test_malformed_suffix_keeps_valid_prefix_and_reports_byte_offset(self) -> None:
        prefix = _frame(4, b">", b"ok")
        raw = prefix + b"broken"

        transcript = _parse(raw)

        self.assertEqual(transcript["state"], "malformed")
        self.assertEqual(transcript["events_total"], 1)
        self.assertEqual(transcript["events"][0]["payload_display"], "ok")
        self.assertEqual(transcript["error_offset"], len(prefix))
        self.assertIn("does not start", transcript["error_reason"] or "")

        invalid_header = _parse(b"[  0.01s/1]>: x\n")
        self.assertEqual(invalid_header["state"], "malformed")
        self.assertEqual(invalid_header["error_offset"], 0)
        self.assertEqual(invalid_header["error_reason"], "invalid event header")

    def test_reports_truncated_payload_and_invalid_delimiter(self) -> None:
        header = b"[  0.001s/5]>: "
        truncated = _parse(header + b"abc\n")
        self.assertEqual(truncated["state"], "malformed")
        self.assertEqual(truncated["error_offset"], len(header))
        self.assertEqual(truncated["error_reason"], "truncated event payload")

        invalid_delimiter_raw = b"[  0.001s/3]>: abc!"
        invalid_delimiter = _parse(invalid_delimiter_raw)
        self.assertEqual(invalid_delimiter["state"], "malformed")
        self.assertEqual(invalid_delimiter["error_offset"], len(invalid_delimiter_raw) - 1)
        self.assertEqual(invalid_delimiter["error_reason"], "invalid event delimiter")

    def test_event_limit_counts_full_stream_and_payload_limit_preserves_sync(self) -> None:
        large_payload = b"x" * (8 * 1024 + 37)
        raw = _frame(1, b">", large_payload) + _frame(2, b"<", b"next")
        raw += b"".join(_frame(index + 3, b">", str(index).encode()) for index in range(100))

        transcript = _parse(raw)

        self.assertEqual(transcript["state"], "ok")
        self.assertEqual(transcript["events_shown"], 100)
        self.assertEqual(transcript["events_total"], 102)
        self.assertEqual(transcript["events_omitted"], 2)
        self.assertEqual(transcript["events"][0]["payload_bytes_shown"], 8 * 1024)
        self.assertEqual(transcript["events"][0]["payload_bytes_omitted"], 37)
        self.assertEqual(transcript["events"][1]["payload_display"], "next")


if __name__ == "__main__":
    unittest.main()
