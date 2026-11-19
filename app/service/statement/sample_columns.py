"""Two-column presentation of ordered interaction events."""

from collections.abc import Iterable


def aligned_event_columns(events: Iterable[tuple[str, str]]) -> tuple[str, str]:
    inputs: list[str] = []
    outputs: list[str] = []
    for speaker, text in events:
        aligned = text if text.endswith("\n") else text + "\n"
        padding = "\n" * max(1, len(text.splitlines()))
        inputs.append(aligned if speaker == "interactor" else padding)
        outputs.append(padding if speaker == "interactor" else aligned)
    return "".join(inputs), "".join(outputs)
