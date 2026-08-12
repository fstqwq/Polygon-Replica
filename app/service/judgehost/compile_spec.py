import shlex
from dataclasses import dataclass
from typing import Literal, cast

from app.config import ConfigValues


CompileFamily = Literal["native", "java", "python"]


@dataclass(frozen=True)
class JudgehostCompileSpec:
    """Canonical compiler invocation shared by dispatch, cache identity, and UI."""

    language_id: str
    family: CompileFamily
    command: str
    command_arguments: tuple[str, ...]
    fixed_arguments: tuple[str, ...]
    trailing_arguments: tuple[str, ...] = ()

    @property
    def digest_arguments(self) -> tuple[str, ...]:
        return (*self.command_arguments, *self.fixed_arguments, *self.trailing_arguments)

    @property
    def public_arguments(self) -> tuple[str, ...]:
        if self.family == "native":
            return (
                *self.command_arguments,
                *self.fixed_arguments,
                "<source>",
                "-o",
                "<executable>",
                *self.trailing_arguments,
            )
        return (*self.command_arguments, *self.fixed_arguments, "<source>")


def _tokens(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        return tuple(shlex.split(raw))
    except ValueError:
        return tuple(raw.split())


def compile_spec(config_values: ConfigValues, language_id: str) -> JudgehostCompileSpec:
    snapshot = config_values.snapshot()
    if language_id == "c":
        return JudgehostCompileSpec(
            language_id="c",
            family="native",
            command="gcc",
            command_arguments=("-O2", "-std=gnu11", "-pipe"),
            fixed_arguments=("-I.",),
            trailing_arguments=("-lm",),
        )
    if language_id == "cpp":
        return JudgehostCompileSpec(
            language_id="cpp",
            family="native",
            command=cast(str, snapshot["TOOLCHAIN_CPP_COMPILER"]),
            command_arguments=_tokens(cast(str, snapshot["TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS"])),
            fixed_arguments=("-I.",),
        )
    if language_id == "java":
        return JudgehostCompileSpec(
            language_id="java",
            family="java",
            command=cast(str, snapshot["TOOLCHAIN_JAVA_COMPILER"]),
            command_arguments=_tokens(cast(str, snapshot["TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS"])),
            fixed_arguments=("-encoding", "UTF-8", "-sourcepath", ".", "-d", "."),
        )
    if language_id == "py":
        return JudgehostCompileSpec(
            language_id="py",
            family="python",
            command="pypy3",
            command_arguments=_tokens(cast(str, snapshot["TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS"])),
            fixed_arguments=("-m", "py_compile"),
        )
    raise ValueError(f"unsupported judgehost language: {language_id}")
