"""Project canonical verification evidence into statement example resources."""

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from app.service.execution.model import CAPTURE_COMPLETE, ExecutionPassResult
from app.service.judgehost.callback.runpipe_transcript import parse_runpipe_transcript
from app.service.problem.runtime_config import ProblemConfigLimits, load_problem_config
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    TestSpecEntry,
    load_tests_spec,
    payload_rel_path_for_test,
    read_statement_sample_text,
)
from app.service.verification.task_store import VerificationTaskRow
from app.service.verification.types import VerificationStatus, VerificationTaskStatus

if TYPE_CHECKING:
    from app.service.verification.detail_read_model import VerificationDetailReadModel
    from app.service.verification.service import VerificationService


class StatementExampleResource(TypedDict):
    path: str
    content: str


class StatementExampleEvent(TypedDict):
    source: Literal["interactor", "solution"]
    textFile: str


class StatementExamplePass(TypedDict, total=False):
    number: int
    inputFile: str
    outputFile: str
    events: list[StatementExampleEvent]


class StatementExampleSample(TypedDict):
    number: int
    presentation: Literal["pair", "interaction"]
    passes: list[StatementExamplePass]


class StatementExamplesContext(TypedDict):
    samples: list[StatementExampleSample]


class StatementExamplesBundle(TypedDict):
    context: StatementExamplesContext
    resources: list[StatementExampleResource]
    verification_id: str


def _sample_rows(
    workspace: Path,
    *,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
) -> list[tuple[int, TestSpecEntry]]:
    try:
        entries = load_tests_spec(
            workspace / TESTS_SPEC_REL,
            document_max_bytes=tests_spec_max_bytes,
            sample_max_bytes=statement_sample_max_bytes,
        )
    except Exception as exc:
        raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
    return [
        (index, row)
        for index, row in enumerate(entries, start=1)
        if row["sample"]
    ]


def statement_examples_require_verification(
    workspace: Path,
    *,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    problem_limits: ProblemConfigLimits,
) -> bool:
    """Return whether a statement preview needs execution evidence."""

    mode = load_problem_config(workspace, limits=problem_limits)["mode"]
    for _index, row in _sample_rows(
        workspace,
        tests_spec_max_bytes=tests_spec_max_bytes,
        statement_sample_max_bytes=statement_sample_max_bytes,
    ):
        sample_input = row["sample_input"]
        sample_output = row["sample_output"]
        has_override = bool(sample_input or sample_output)
        if has_override:
            if row["sample_output_validate"] and sample_output:
                return True
            if mode == "interactive":
                if not (sample_input and sample_output):
                    return True
                continue
            if not sample_output:
                return True
            if sample_input:
                continue
            if row["kind"] == "gen":
                return True
            input_path = workspace / payload_rel_path_for_test(row["id"], row["kind"])
            if input_path.is_symlink() or not input_path.is_file():
                return True
            continue
        return True
    return False


def _safe_authored_input(
    workspace: Path,
    row: TestSpecEntry,
    *,
    max_bytes: int,
) -> str:
    if row["kind"] != "manual":
        raise RuntimeError(f"sample {row['id']} requires verification input evidence")
    rel = Path(payload_rel_path_for_test(row["id"], row["kind"]))
    try:
        root = workspace.resolve()
        path = (workspace / rel).resolve()
    except OSError as exc:
        raise RuntimeError(f"sample {row['id']} input is unavailable") from exc
    if root not in path.parents:
        raise RuntimeError(f"sample {row['id']} input path is invalid")
    try:
        return read_statement_sample_text(path, max_bytes=max_bytes)
    except ValueError as exc:
        raise RuntimeError(f"sample {row['id']} input is invalid: {exc}") from exc


def _resource_path(relative: str) -> str:
    value = PurePosixPath("examples", relative).as_posix()
    if value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise RuntimeError("statement example resource path is invalid")
    return value


class _SampleResources:
    def __init__(self, *, sample_id: str, max_bytes: int) -> None:
        self._sample_id = sample_id
        self._max_bytes = max_bytes
        self._size = 0
        self.rows: list[StatementExampleResource] = []
        self._paths: set[str] = set()

    def add(self, path: str, content: str) -> str:
        encoded_size = len(content.encode("utf-8"))
        if self._size + encoded_size > self._max_bytes:
            raise RuntimeError(
                f"sample {self._sample_id} statement resources exceed byte limit"
            )
        if path in self._paths:
            raise RuntimeError(f"duplicate statement example resource: {path}")
        self._size += encoded_size
        self._paths.add(path)
        self.rows.append({"path": path, "content": content})
        return path


def _descriptor_text(
    verification_service: "VerificationService",
    artifact_ref: str,
    *,
    label: str,
    max_bytes: int,
) -> str:
    if not artifact_ref:
        raise RuntimeError(f"{label} is missing")
    descriptor = verification_service.artifact_descriptor(artifact_ref)
    if descriptor is None:
        raise RuntimeError(f"{label} is unavailable")
    try:
        return read_statement_sample_text(descriptor.path, max_bytes=max_bytes)
    except ValueError as exc:
        raise RuntimeError(f"{label} is invalid: {exc}") from exc


def _verification_evidence(
    verification_service: "VerificationService",
    verification_id: str,
) -> tuple[
    "VerificationDetailReadModel",
    dict[str, tuple[int, str]],
    dict[str, VerificationTaskRow],
]:
    read_model = verification_service.verification_detail_read_model(verification_id)
    if read_model is None:
        raise RuntimeError(f"statement examples verification is missing: {verification_id}")
    record = read_model["record"]
    if record["status"] != VerificationStatus.OK:
        reason = record["fail_reason"] or "verification is not complete"
        raise RuntimeError(f"statement examples verification failed: {reason}")

    tests_meta = read_model["details"].get("tests_meta_rows")
    if not isinstance(tests_meta, list):
        raise RuntimeError("statement examples verification has no test metadata")
    test_by_id: dict[str, tuple[int, str]] = {}
    observed_ordinals: set[int] = set()
    observed_test_names: set[str] = set()
    for raw in tests_meta:
        if not isinstance(raw, dict) or not bool(raw.get("sample")):
            continue
        source_id = str(raw.get("id") or "")
        test_name = str(raw.get("test_name") or "")
        ordinal = raw.get("index")
        if (
            not source_id
            or not test_name
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 1
            or source_id in test_by_id
            or ordinal in observed_ordinals
            or test_name in observed_test_names
        ):
            raise RuntimeError("statement examples verification test metadata is invalid")
        test_by_id[source_id] = (ordinal, test_name)
        observed_ordinals.add(ordinal)
        observed_test_names.add(test_name)

    tasks: dict[str, VerificationTaskRow] = {}
    main_correct_programs: set[str] = set()
    for task in read_model["tasks"]:
        if task["task_kind"] != "main-correct":
            continue
        main_correct_programs.add(task["program_id"])
        test_name = task["test_name"]
        if not test_name or test_name in tasks:
            raise RuntimeError("statement examples main-correct tasks are ambiguous")
        tasks[test_name] = task
    if len(main_correct_programs) != 1:
        raise RuntimeError("statement examples require exactly one main-correct program")
    return read_model, test_by_id, tasks


def _complete_passes(
    task: VerificationTaskRow,
    *,
    sample_id: str,
) -> tuple[ExecutionPassResult, ...]:
    if task["status"] != VerificationTaskStatus.DONE:
        raise RuntimeError(f"sample {sample_id} main-correct task is incomplete")
    passes = task["result"].passes
    if not passes:
        raise RuntimeError(f"sample {sample_id} has no captured passes")
    for expected, pass_result in enumerate(passes, start=1):
        if pass_result.number != expected:
            raise RuntimeError(f"sample {sample_id} pass numbers are not contiguous")
        if pass_result.capture_status != CAPTURE_COMPLETE:
            raise RuntimeError(
                f"sample {sample_id} pass {pass_result.number} was not fully captured"
            )
    return passes


def _pair_override(
    workspace: Path,
    row: TestSpecEntry,
    *,
    sample_number: int,
    mode: str,
    passes: tuple[ExecutionPassResult, ...],
    verification_service: "VerificationService | None",
    max_bytes: int,
) -> tuple[StatementExampleSample, list[StatementExampleResource]]:
    resources = _SampleResources(sample_id=row["id"], max_bytes=max_bytes)
    input_text = row["sample_input"]
    output_text = row["sample_output"]
    if not input_text:
        if passes and verification_service is not None:
            input_text = _descriptor_text(
                verification_service,
                passes[0].artifacts.input_ref,
                label=f"sample {row['id']} input",
                max_bytes=max_bytes,
            )
        else:
            input_text = _safe_authored_input(workspace, row, max_bytes=max_bytes)
    if not output_text:
        if mode == "interactive":
            raise RuntimeError(
                f"interactive sample {row['id']} requires sample_output for pair presentation"
            )
        if not passes or verification_service is None:
            raise RuntimeError(f"sample {row['id']} requires verification output evidence")
        output_text = _descriptor_text(
            verification_service,
            passes[-1].artifacts.output_ref,
            label=f"sample {row['id']} final output",
            max_bytes=max_bytes,
        )
    input_path = resources.add(
        _resource_path(f"sample-{sample_number}/display.in"), input_text
    )
    output_path = resources.add(
        _resource_path(f"sample-{sample_number}/display.ans"), output_text
    )
    return (
        {
            "number": sample_number,
            "presentation": "pair",
            "passes": [{"number": 1, "inputFile": input_path, "outputFile": output_path}],
        },
        resources.rows,
    )


def _pass_fail_sample(
    row: TestSpecEntry,
    *,
    sample_number: int,
    passes: tuple[ExecutionPassResult, ...],
    verification_service: "VerificationService",
    max_bytes: int,
) -> tuple[StatementExampleSample, list[StatementExampleResource]]:
    resources = _SampleResources(sample_id=row["id"], max_bytes=max_bytes)
    projected: list[StatementExamplePass] = []
    for pass_result in passes:
        pass_number = pass_result.number
        input_text = _descriptor_text(
            verification_service,
            pass_result.artifacts.input_ref,
            label=f"sample {row['id']} pass {pass_number} input",
            max_bytes=max_bytes,
        )
        output_text = _descriptor_text(
            verification_service,
            pass_result.artifacts.output_ref,
            label=f"sample {row['id']} pass {pass_number} output",
            max_bytes=max_bytes,
        )
        input_path = resources.add(
            _resource_path(f"sample-{sample_number}/pass-{pass_number}.in"),
            input_text,
        )
        output_path = resources.add(
            _resource_path(f"sample-{sample_number}/pass-{pass_number}.ans"),
            output_text,
        )
        projected.append(
            {
                "number": pass_number,
                "inputFile": input_path,
                "outputFile": output_path,
            }
        )
    return (
        {"number": sample_number, "presentation": "pair", "passes": projected},
        resources.rows,
    )


def _interactive_sample(
    row: TestSpecEntry,
    *,
    sample_number: int,
    passes: tuple[ExecutionPassResult, ...],
    verification_service: "VerificationService",
    max_bytes: int,
) -> tuple[StatementExampleSample, list[StatementExampleResource]]:
    resources = _SampleResources(sample_id=row["id"], max_bytes=max_bytes)
    projected: list[StatementExamplePass] = []
    for pass_result in passes:
        pass_number = pass_result.number
        artifact_ref = pass_result.artifacts.transcript_ref
        if not artifact_ref:
            raise RuntimeError(f"sample {row['id']} pass {pass_number} transcript is missing")
        descriptor = verification_service.artifact_descriptor(artifact_ref)
        if descriptor is None:
            raise RuntimeError(f"sample {row['id']} pass {pass_number} transcript is unavailable")
        try:
            with descriptor.path.open("rb") as stream:
                transcript = parse_runpipe_transcript(
                    stream,
                    raw_size_bytes=descriptor.size,
                    event_limit=max(1, max_bytes),
                    payload_preview_limit=max(1, max_bytes + 1),
                )
        except OSError as exc:
            raise RuntimeError(
                f"sample {row['id']} pass {pass_number} transcript is unavailable"
            ) from exc
        if transcript["state"] != "ok":
            raise RuntimeError(
                f"sample {row['id']} pass {pass_number} transcript is malformed at byte "
                f"{transcript['error_offset']}: {transcript['error_reason']}"
            )
        if transcript["events_omitted"]:
            raise RuntimeError(f"sample {row['id']} pass {pass_number} has too many transcript events")
        events: list[StatementExampleEvent] = []
        data_index = 0
        for event in transcript["events"]:
            if event["kind"] == "eof":
                continue
            if event["payload_bytes_omitted"]:
                raise RuntimeError(
                    f"sample {row['id']} pass {pass_number} transcript event is too large"
                )
            data_index += 1
            event_path = resources.add(
                _resource_path(
                    f"sample-{sample_number}/pass-{pass_number}/event-{data_index}.txt"
                ),
                event["payload_display"],
            )
            events.append(
                {
                    "source": cast(Literal["interactor", "solution"], event["source"]),
                    "textFile": event_path,
                }
            )
        projected.append({"number": pass_number, "events": events})
    return (
        {
            "number": sample_number,
            "presentation": "interaction",
            "passes": projected,
        },
        resources.rows,
    )


class StatementExamplesProducer:
    """Build one statement render-context bundle from canonical verification evidence."""

    def __init__(self, verification_service: "VerificationService") -> None:
        self._verification_service = verification_service

    def produce(
        self,
        workspace: Path,
        *,
        verification_id: str = "",
        tests_spec_max_bytes: int,
        statement_sample_max_bytes: int,
        problem_limits: ProblemConfigLimits,
    ) -> StatementExamplesBundle:
        sample_rows = _sample_rows(
            workspace,
            tests_spec_max_bytes=tests_spec_max_bytes,
            statement_sample_max_bytes=statement_sample_max_bytes,
        )
        mode = load_problem_config(workspace, limits=problem_limits)["mode"]
        verification_mode = mode
        test_by_id: dict[str, tuple[int, str]] = {}
        tasks: dict[str, VerificationTaskRow] = {}
        if verification_id:
            read_model, test_by_id, tasks = _verification_evidence(
                self._verification_service, verification_id
            )
            read_model_mode = read_model["mode"]
            if read_model_mode not in {"pass-fail", "interactive"}:
                raise RuntimeError("statement examples verification mode is malformed")
            verification_mode = cast(
                Literal["pass-fail", "interactive"], read_model_mode
            )
            if {row["id"] for _ordinal, row in sample_rows} != set(test_by_id):
                raise RuntimeError(
                    "statement samples do not match verification test metadata"
                )

        samples: list[StatementExampleSample] = []
        all_resources: list[StatementExampleResource] = []
        resource_paths: set[str] = set()
        for sample_number, (ordinal, row) in enumerate(sample_rows, start=1):
            passes: tuple[ExecutionPassResult, ...] = ()
            if verification_id:
                test_identity = test_by_id.get(row["id"])
                if test_identity is None or test_identity[0] != ordinal:
                    raise RuntimeError(
                        f"sample {row['id']} is missing from verification evidence"
                    )
                test_name = test_identity[1]
                task = tasks.get(test_name)
                if task is None:
                    raise RuntimeError(
                        f"sample {row['id']} main-correct evidence is missing"
                    )
                passes = _complete_passes(task, sample_id=row["id"])
            has_override = bool(row["sample_input"] or row["sample_output"])
            if has_override:
                sample, resources = _pair_override(
                    workspace,
                    row,
                    sample_number=sample_number,
                    mode=verification_mode,
                    passes=passes,
                    verification_service=(
                        self._verification_service if verification_id else None
                    ),
                    max_bytes=statement_sample_max_bytes,
                )
            else:
                if not verification_id:
                    raise RuntimeError(
                        f"sample {row['id']} requires verification evidence"
                    )
                if verification_mode == "interactive":
                    sample, resources = _interactive_sample(
                        row,
                        sample_number=sample_number,
                        passes=passes,
                        verification_service=self._verification_service,
                        max_bytes=statement_sample_max_bytes,
                    )
                else:
                    sample, resources = _pass_fail_sample(
                        row,
                        sample_number=sample_number,
                        passes=passes,
                        verification_service=self._verification_service,
                        max_bytes=statement_sample_max_bytes,
                    )
            samples.append(sample)
            for resource in resources:
                if resource["path"] in resource_paths:
                    raise RuntimeError(
                        f"duplicate statement example resource: {resource['path']}"
                    )
                resource_paths.add(resource["path"])
                all_resources.append(resource)
        return {
            "context": {"samples": samples},
            "resources": all_resources,
            "verification_id": verification_id,
        }
