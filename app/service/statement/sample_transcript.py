"""Convert one canonical runpipe transcript into authored sample events."""

from pathlib import Path

from app.service.judgehost.callback.runpipe_transcript import parse_runpipe_transcript
from app.service.problem.sample_json import SampleJsonEvent


def statement_sample_events_from_transcript(
    path: Path,
    *,
    raw_size_bytes: int,
    max_bytes: int,
    label: str,
) -> list[SampleJsonEvent]:
    """Read all data frames using the same semantics as Statement rendering."""

    try:
        with path.open("rb") as stream:
            transcript = parse_runpipe_transcript(
                stream,
                raw_size_bytes=raw_size_bytes,
                event_limit=max(1, max_bytes),
                payload_preview_limit=max(1, max_bytes + 1),
            )
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if transcript["state"] == "limited":
        raise ValueError(f"{label} has too many events")
    if transcript["state"] != "ok":
        raise ValueError(
            f"{label} is malformed at byte {transcript['error_offset']}: "
            f"{transcript['error_reason']}"
        )
    if transcript["events_omitted"]:
        raise ValueError(f"{label} has too many events")

    events: list[SampleJsonEvent] = []
    for event in transcript["events"]:
        if event["kind"] == "eof":
            continue
        if event["payload_bytes_omitted"]:
            raise ValueError(f"{label} event exceeds the sample limit")
        events.append(
            {
                "source": event["source"],
                "content": ""
                if event["payload_bytes"] == 0
                else event["payload_display"],
            }
        )
    return events
