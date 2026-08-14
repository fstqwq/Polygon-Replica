import re
import shlex
from pathlib import Path

from app.service.judgehost.configuration import JudgehostSettings
from app.service.judgehost.domjudge.codec import decode_basename, decode_text
from app.service.judgehost.domjudge.compile_spec import compile_spec
from app.service.platform.hashing import compile_command_digest


def language_extensions(source_name: str) -> tuple[str, tuple[str, ...]]:
    name = decode_text(lower=True, raw=source_name)
    if name.endswith(".java"):
        return ("java", ("java",))
    if name.endswith(".py"):
        return ("py", ("py",))
    if name.endswith(".c"):
        return ("c", ("c",))
    return ("cpp", ("cpp", "cc", "cxx", "c++"))


class DomjudgeScriptCatalog:
    """Build the executable scripts served to DOMjudge Judgehosts."""

    def __init__(self) -> None:
        self._root = (Path(__file__).resolve().parent / "scripts").resolve()

    def toolchain_cmd_digest(
        self,
        settings: JudgehostSettings,
        source_name: str,
        *,
        manual_validate_only: bool = False,
    ) -> str:
        if manual_validate_only:
            return compile_command_digest("skip.compile", [])
        language, _extensions = language_extensions(source_name)
        spec = compile_spec(settings.values, language)
        return compile_command_digest(spec.command, spec.digest_arguments)

    def public_compile_specs(
        self, settings: JudgehostSettings
    ) -> list[dict[str, object]]:
        specs = (
            compile_spec(settings.values, language)
            for language in ("c", "cpp", "java", "py")
        )
        return [
            {
                "language_id": spec.language_id,
                "command": spec.command,
                "arguments": list(spec.public_arguments),
            }
            for spec in specs
        ]

    def load(self, name: str) -> str:
        safe_name = decode_basename(raw=name)
        if safe_name != name:
            raise RuntimeError(f"invalid judgehost script asset name: {name}")
        path = (self._root / safe_name).resolve()
        if path.parent != self._root:
            raise RuntimeError(f"invalid judgehost script asset path: {name}")
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"missing judgehost script asset: {safe_name}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def render(template: str, values: dict[str, str]) -> str:
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", value)
        unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", rendered)))
        if unresolved:
            raise RuntimeError(
                f"unresolved judgehost script template tokens: {', '.join(unresolved)}"
            )
        return rendered

    def compile(
        self,
        settings: JudgehostSettings,
        source_name: str,
        *,
        manual_validate_only: bool = False,
        compile_only: bool = False,
    ) -> bytes:
        if manual_validate_only:
            return self.load("skip.compile").encode("utf-8")
        language, _extensions = language_extensions(source_name)
        spec = compile_spec(settings.values, language)
        command = " ".join(
            shlex.quote(token) for token in (spec.command, *spec.command_arguments)
        )
        if spec.family == "native":
            script_name = "native.compile-only" if compile_only else "native.compile"
            values = {
                "NATIVE_COMPILE_CMD": command,
                "NATIVE_BEFORE_SOURCE": " ".join(
                    shlex.quote(token) for token in spec.fixed_arguments
                ),
                "NATIVE_AFTER_OUTPUT": " ".join(
                    shlex.quote(token) for token in spec.trailing_arguments
                ),
            }
        elif spec.family == "java":
            script_name = "java.compile-only" if compile_only else "java.compile"
            values = {"JAVA_COMPILE_CMD": command}
        else:
            script_name = "python.compile-only" if compile_only else "python.compile"
            values = {
                "PYTHON_COMPILE_FLAG_SUFFIX": "".join(
                    f" {shlex.quote(token)}" for token in spec.command_arguments
                )
            }
        return self.render(self.load(script_name), values).encode("utf-8")

    def cpp_executable_build(
        self,
        settings: JudgehostSettings,
        source_name: str,
        *,
        role: str,
    ) -> bytes:
        compiler = settings.values["TOOLCHAIN_CPP_COMPILER"]
        if not isinstance(compiler, str):
            raise RuntimeError(
                "invalid internal text configuration: TOOLCHAIN_CPP_COMPILER"
            )
        safe_source = shlex.quote(
            decode_basename(raw=source_name, default="interactor.cpp")
        )
        safe_role = decode_text(raw=role, default="executable")
        template = self.load(
            "cpp.interactor.build"
            if safe_role == "interactor"
            else "cpp.executable.build"
        )
        return self.render(
            template,
            {
                "ROLE": safe_role,
                "CPP_EXECUTABLE_BUILD_CMD": (
                    f"{shlex.quote(compiler)} -Wall -DDOMJUDGE -O2"
                ),
                "SOURCE_NAME": safe_source,
            },
        ).encode("utf-8")

    def pass_capture(self, *, max_bytes: int) -> bytes:
        return self.render(
            self.load("pass-capture"),
            {"BUNDLE_MAX_BYTES": str(max(1024, max_bytes))},
        ).encode("utf-8")

    def run(
        self,
        interactive: bool,
        *,
        main_correct: bool = False,
        compile_only: bool = False,
        generate_mode: bool = False,
        manual_validate_only: bool = False,
    ) -> bytes:
        del main_correct
        if interactive:
            return self.load("interactive.run").encode("utf-8")
        if compile_only or manual_validate_only:
            script_name = "skip.run"
        elif generate_mode:
            script_name = "generate.run"
        else:
            script_name = "normal.run"
        return self.load(script_name).encode("utf-8")

    def compare(
        self, *, main_correct: bool = False, generate_mode: bool = False
    ) -> bytes:
        if main_correct:
            script_name = "main.compare"
        elif generate_mode:
            script_name = "generate.compare"
        else:
            script_name = "normal.compare"
        return self.load(script_name).encode("utf-8")
