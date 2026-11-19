"""Resolve display samples from one language of an extracted Native Package."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.service.problem_package.manifest import NativePackageManifest, StatementEventPath, statement_sample_resource
from app.service.statement.sample_columns import aligned_event_columns


_PAIR = re.compile(r"\\StatementSampleFile\{(\d+)\}\{([^{}]+)\}\{([^{}]+)\}")
_LEGACY_PAIR = re.compile(r"\\exmpfile\{([^{}]+)\}\{([^{}]+)\}")
_PASS_PAIR = re.compile(r"\\StatementSamplePassFile\{(\d+)\}\{(\d+)\}\{([^{}]+)\}\{([^{}]+)\}")
_INTERACTION = re.compile(r"\\begin\{StatementSampleInteraction\}(?:\[(\d+)\])?\{(\d+)\}")
_EVENT = re.compile(r"\\StatementSampleEventFile\{(interactor|solution)\}\{([^{}]+)\}")


@dataclass
class _Declarations:
    pairs: list[tuple[str, str]] = field(default_factory=list)
    events: list[list[StatementEventPath]] = field(default_factory=list)


def _read_declarations(template: Path, test_ids: list[str]) -> dict[str, _Declarations]:
    if not template.is_file():
        return {}
    records: dict[str, _Declarations] = {}
    lines = iter(template.read_text(encoding="utf-8").splitlines())
    for raw in lines:
        line = raw.partition("%")[0].strip()
        pair = _PAIR.fullmatch(line)
        legacy_pair = _LEGACY_PAIR.fullmatch(line)
        multi = _PASS_PAIR.fullmatch(line)
        interaction = _INTERACTION.fullmatch(line)
        if pair:
            number, phase, input_path, output_path = pair[1], "1", pair[2], pair[3]
        elif legacy_pair:
            number, phase = str(len(records) + 1), "1"
            input_path, output_path = legacy_pair.groups()
        elif multi:
            number, phase, input_path, output_path = multi.groups()
        elif interaction:
            number, phase = interaction[2], interaction[1] or "1"
        else:
            continue
        ordinal = int(number)
        if not 1 <= ordinal <= len(test_ids):
            raise ValueError(f"Unexpected sample number {number} in {template}")
        test_id = test_ids[ordinal - 1]
        record = records.setdefault(test_id, _Declarations())
        if int(phase) != len(record.pairs) + len(record.events) + 1:
            raise ValueError(f"Unexpected pass {phase} for sample {test_id} in {template}")
        if not interaction and not record.events:
            record.pairs.append((input_path, output_path))
            continue
        if not interaction or record.pairs:
            raise ValueError(f"Mixed presentations for sample {test_id} in {template}")
        events: list[StatementEventPath] = []
        for body in lines:
            body = body.partition("%")[0].strip()
            if not body:
                continue
            if body == r"\end{StatementSampleInteraction}":
                break
            event = _EVENT.fullmatch(body)
            if not event:
                raise ValueError(f"Invalid event declaration for sample {test_id} in {template}")
            events.append({"source": "interactor" if event[1] == "interactor" else "solution", "path": event[2]})
        else:
            raise ValueError(f"Unclosed interaction for sample {test_id} in {template}")
        record.events.append(events)
    return records


@dataclass(frozen=True)
class FrozenDisplaySample:
    test_index: int
    test_id: str
    build: Path
    pair: tuple[str, str] | None
    event_passes: list[list[StatementEventPath]]

    def read(self, path: str) -> str:
        return statement_sample_resource(self.build, self.test_id, path).read_bytes().decode("utf-8")

    def columns(self) -> tuple[str, str]:
        if self.pair is not None:
            return self.read(self.pair[0]), self.read(self.pair[1])
        return aligned_event_columns((event["source"], self.read(event["path"])) for event in self.event_passes[0])


def frozen_display_samples(
    manifest: NativePackageManifest, build: Path, *, include_events: bool = False,
) -> Iterator[FrozenDisplaySample]:
    tests = [(i, t["id"]) for i, t in enumerate(manifest["tests"], 1) if t["sample"]]
    index = manifest.get("statement_samples")
    declarations = {}
    if tests and (index is None or (
        include_events and manifest["mode"] == "interactive" and any("event_passes" not in r for r in index.values())
    )):
        ids = [test_id for _, test_id in tests]
        declarations = _read_declarations(build / "examples.tex", ids)
        if not declarations:
            declarations = _read_declarations(build / "problem.tex", ids)
    for test_index, test_id in tests:
        entry = index.get(test_id) if index is not None else None
        declared = declarations.get(test_id)
        pair = None
        events = declared.events if declared else []
        if declared:
            for input_path, output_path in declared.pairs:
                statement_sample_resource(build, test_id, input_path)
                statement_sample_resource(build, test_id, output_path)
        if entry is not None:
            pair = entry["input"], entry["output"]
            events = entry.get("event_passes", events)
        elif declared and declared.pairs:
            pair = declared.pairs[0]
        if pair is None and not events:
            raise ValueError(f"statement sample {test_id} ({build.name}): missing display references in manifest or examples.tex")
        if events and manifest["mode"] != "interactive":
            raise ValueError(f"Unexpected interaction events for ordinary sample {test_id} ({build.name})")
        for phase in events:
            for event in phase:
                statement_sample_resource(build, test_id, event["path"])
        yield FrozenDisplaySample(test_index, test_id, build, pair, events)
