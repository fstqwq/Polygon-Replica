"""Strict authored JSON contract for structured statement samples."""

import json
from typing import Literal, TypedDict


class SampleJsonEvent(TypedDict):
    source: Literal["interactor", "solution"]
    content: str


class SampleJsonPass(TypedDict, total=False):
    number: int
    input: str
    output: str
    events: list[SampleJsonEvent]


class SampleJson(TypedDict):
    presentation: Literal["pair", "interaction"]
    passes: list[SampleJsonPass]


def _reject_unknown_fields(
    value: dict[object, object],
    *,
    allowed: set[str],
    label: str,
) -> None:
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            raise ValueError(f"{label}: unknown field '{key}'")


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}: must be a string")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _pair_pass(raw: dict[object, object], *, index: int) -> tuple[SampleJsonPass, int]:
    label = f"sample_json.passes[{index}]"
    _reject_unknown_fields(
        raw,
        allowed={"number", "input", "output"},
        label=label,
    )
    number = raw.get("number", index)
    if isinstance(number, bool) or not isinstance(number, int) or number != index:
        raise ValueError(f"{label}.number: must be {index}")
    if "input" not in raw or "output" not in raw:
        raise ValueError(f"{label}: input and output are required")
    input_text = _text(raw["input"], label=f"{label}.input")
    output_text = _text(raw["output"], label=f"{label}.output")
    size = len(input_text.encode("utf-8")) + len(output_text.encode("utf-8"))
    return {"number": number, "input": input_text, "output": output_text}, size


def _interaction_pass(
    raw: dict[object, object],
    *,
    index: int,
) -> tuple[SampleJsonPass, int]:
    label = f"sample_json.passes[{index}]"
    _reject_unknown_fields(raw, allowed={"number", "events"}, label=label)
    number = raw.get("number", index)
    if isinstance(number, bool) or not isinstance(number, int) or number != index:
        raise ValueError(f"{label}.number: must be {index}")
    raw_events = raw.get("events")
    if not isinstance(raw_events, list):
        raise ValueError(f"{label}.events: must be an array")
    events: list[SampleJsonEvent] = []
    size = 0
    for event_index, raw_event in enumerate(raw_events, start=1):
        event_label = f"{label}.events[{event_index}]"
        if not isinstance(raw_event, dict):
            raise ValueError(f"{event_label}: must be an object")
        _reject_unknown_fields(
            raw_event,
            allowed={"source", "content"},
            label=event_label,
        )
        source = raw_event.get("source")
        if source not in {"interactor", "solution"}:
            raise ValueError(
                f"{event_label}.source: must be 'interactor' or 'solution'"
            )
        content = _text(raw_event.get("content"), label=f"{event_label}.content")
        size += len(content.encode("utf-8"))
        events.append({"source": source, "content": content})
    return {"number": number, "events": events}, size


def normalize_sample_json(raw: object, *, max_bytes: int) -> SampleJson | None:
    """Validate and canonicalize a structured sample value."""

    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"sample_json: invalid JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc
    if not isinstance(raw, dict):
        raise ValueError("sample_json: must be an object")
    _reject_unknown_fields(
        raw,
        allowed={"presentation", "passes"},
        label="sample_json",
    )
    presentation = raw.get("presentation")
    if presentation not in {"pair", "interaction"}:
        raise ValueError("sample_json.presentation: must be 'pair' or 'interaction'")
    raw_passes = raw.get("passes")
    if not isinstance(raw_passes, list) or not raw_passes:
        raise ValueError("sample_json.passes: must be a non-empty array")

    passes: list[SampleJsonPass] = []
    content_bytes = 0
    for index, raw_pass in enumerate(raw_passes, start=1):
        label = f"sample_json.passes[{index}]"
        if not isinstance(raw_pass, dict):
            raise ValueError(f"{label}: must be an object")
        if presentation == "pair":
            normalized_pass, pass_bytes = _pair_pass(raw_pass, index=index)
        else:
            normalized_pass, pass_bytes = _interaction_pass(raw_pass, index=index)
        passes.append(normalized_pass)
        content_bytes += pass_bytes

    if content_bytes > max(1, int(max_bytes)):
        raise ValueError("sample_json: content exceeds statement sample byte limit")
    return {"presentation": presentation, "passes": passes}


def dumps_sample_json(value: SampleJson | None) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2)
