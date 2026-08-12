from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass


CAPTURE_COMPLETE = "complete"
CAPTURE_METADATA_ONLY = "metadata-only"
CAPTURE_STATUSES = frozenset((CAPTURE_COMPLETE, CAPTURE_METADATA_ONLY))


@dataclass(frozen=True)
class ExecutionUsage:
    runtime_sec: float | None = None
    cpu_sec: float | None = None
    wall_sec: float | None = None
    memory_kb: int | None = None


@dataclass(frozen=True)
class PassArtifacts:
    input_ref: str = ""
    output_ref: str = ""
    transcript_ref: str = ""
    stderr_ref: str = ""
    system_ref: str = ""
    judge_message_ref: str = ""
    team_message_ref: str = ""
    metadata_ref: str = ""
    compare_metadata_ref: str = ""

    def refs(self) -> tuple[str, ...]:
        return tuple(
            ref
            for ref in (
                self.input_ref,
                self.output_ref,
                self.transcript_ref,
                self.stderr_ref,
                self.system_ref,
                self.judge_message_ref,
                self.team_message_ref,
                self.metadata_ref,
                self.compare_metadata_ref,
            )
            if ref
        )


@dataclass(frozen=True)
class ExecutionPassResult:
    number: int
    capture_status: str
    runresult: str
    verdict: str
    score_text: str
    answer_correct: bool
    usage: ExecutionUsage
    feedback: str
    artifacts: PassArtifacts


@dataclass(frozen=True)
class ExecutionOutcome:
    verdict: str = ""
    score_text: str = ""
    answer_correct: bool = False
    usage: ExecutionUsage = ExecutionUsage()
    error: str = ""
    feedback: str = ""


@dataclass(frozen=True)
class CompileDiagnostic(Mapping[str, object]):
    fields: tuple[tuple[str, object], ...]

    def __getitem__(self, key: str) -> object:
        for name, value in self.fields:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _value in self.fields)

    def __len__(self) -> int:
        return len(self.fields)


@dataclass(frozen=True)
class ExecutionWarning:
    message: str


@dataclass(frozen=True)
class CompileResult:
    log: str = ""
    diagnostics: tuple[CompileDiagnostic, ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ExecutionOutcome = ExecutionOutcome()
    compile: CompileResult = CompileResult()
    passes: tuple[ExecutionPassResult, ...] = ()
    warnings: tuple[ExecutionWarning, ...] = ()

    @property
    def final_pass(self) -> ExecutionPassResult | None:
        return None if not self.passes else self.passes[-1]

    @property
    def runresult(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.runresult

    @property
    def verdict(self) -> str:
        return self.outcome.verdict

    @property
    def runtime_sec(self) -> float | None:
        return self.outcome.usage.runtime_sec

    @property
    def cpu_sec(self) -> float | None:
        return self.outcome.usage.cpu_sec

    @property
    def wall_sec(self) -> float | None:
        return self.outcome.usage.wall_sec

    @property
    def memory_kb(self) -> int | None:
        return self.outcome.usage.memory_kb

    @property
    def score_text(self) -> str:
        return self.outcome.score_text

    @property
    def answer_correct(self) -> bool:
        return self.outcome.answer_correct

    @property
    def feedback_text(self) -> str:
        return self.outcome.feedback

    @property
    def output_run_ref(self) -> str:
        final_pass = self.final_pass
        if final_pass is None:
            return ""
        return final_pass.artifacts.output_ref or final_pass.artifacts.transcript_ref

    @property
    def output_error_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.stderr_ref

    @property
    def output_system_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.system_ref

    @property
    def output_diff_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.judge_message_ref

    @property
    def team_message_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.team_message_ref

    @property
    def metadata_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.metadata_ref

    @property
    def compare_metadata_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.compare_metadata_ref

    @property
    def feedback_files(self) -> tuple[str, ...]:
        final_pass = self.final_pass
        if final_pass is None or not final_pass.feedback:
            return ()
        artifacts = final_pass.artifacts
        if final_pass.runresult in {"run-error", "timelimit", "output-limit"}:
            return (artifacts.stderr_ref,) if artifacts.stderr_ref else ()
        if artifacts.judge_message_ref:
            return (artifacts.judge_message_ref,)
        if artifacts.team_message_ref:
            return (artifacts.team_message_ref,)
        return (artifacts.stderr_ref,) if artifacts.stderr_ref else ()

    def artifact_refs(self) -> set[str]:
        return set(iter_artifact_refs(self))


def iter_artifact_refs(result: ExecutionResult) -> Iterable[str]:
    for pass_result in result.passes:
        yield from pass_result.artifacts.refs()
