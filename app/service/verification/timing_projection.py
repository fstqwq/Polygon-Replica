"""Small, lossless timing evidence for the browser's candidate-limit preview."""

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TypedDict

from app.service.verification.result_match import expected_status_rule, run_verdict_short
from app.service.verification.types import VerificationTaskRow, VerificationTaskStatus


class TimingCase(TypedDict):
    test: str
    cpu: float | None
    wall: float | None
    verdict: str
    correct: bool
    censored: bool
    complete: bool


class TimingProgram(TypedDict):
    id: str
    name: str
    source: str
    required: tuple[str, ...]
    allowed: tuple[str, ...]
    cases: list[TimingCase]


class TimingPreview(TypedDict):
    current_ms: int
    programs: list[TimingProgram]


def build_timing_preview(
    tasks: Sequence[VerificationTaskRow], *, program_ids: Sequence[str],
    current_ms: int, mode: str,
) -> TimingPreview:
    selected = set(program_ids)
    programs: dict[str, TimingProgram] = {}
    for task in tasks:
        program_id = task["program_id"]
        if program_id not in selected or task["task_kind"] not in {"solution-run", "main-correct"}:
            continue
        if program_id not in programs:
            required, allowed = expected_status_rule(task["expected_behavior"])
            programs[program_id] = {
                "id": program_id, "name": PurePosixPath(task["source_path"]).name,
                "source": task["source_path"], "required": required, "allowed": allowed,
                "cases": [],
            }
        result = task["result"]
        verdict = run_verdict_short(result.verdict)
        if verdict == "SK":
            continue
        usage = result.outcome.usage
        cpu, wall = usage.cpu_sec, usage.wall_sec
        # Aggregates take the maximum known pass usage. A missing pass metric
        # makes that maximum incomplete evidence for a hypothetical limit.
        if any(item.usage.cpu_sec is None for item in result.passes):
            cpu = None
        if any(item.usage.wall_sec is None for item in result.passes):
            wall = None
        correct = verdict == "AC" or result.answer_correct or bool(result.final_pass and result.final_pass.answer_correct)
        censored = verdict == "TL" and not correct
        programs[program_id]["cases"].append({
            "test": task["test_name"],
            "cpu": None if cpu is None else cpu * 1000,
            "wall": None if wall is None else wall * 1000,
            "verdict": verdict,
            "correct": correct,
            # A TL without a checked correct answer cannot establish completion;
            # its exact CPU/wall termination cause need not be inferred from logs.
            "censored": censored,
            "complete": mode == "pass-fail" and task["status"] in {
                VerificationTaskStatus.DONE, VerificationTaskStatus.FAILED,
            } and verdict in {"AC", "WA", "TL", "RE", "CE", "FL"},
        })
    return {
        "current_ms": current_ms,
        "programs": [programs[key] for key in program_ids if key in programs],
    }
