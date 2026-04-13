from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.latex_process import detect_latex_engine
from app.service.sandbox.base import ExecResult, ExecSpec, SandboxBackend
from app.service.sandbox.tex_backend import TexSandboxBackend


@dataclass(frozen=True)
class TexCompileResult:
    engine: str
    proc: ExecResult
    log_text: str
    pdf_path: Path


class TexCompileService:
    def __init__(
        self,
        *,
        sandbox_backend: SandboxBackend | None = None,
        constants: RuntimeValues | None = None,
    ) -> None:
        self.sandbox = sandbox_backend or TexSandboxBackend()
        self.timeout_sec = 120
        self.memory_mb = 1024
        self.process_limit = 64
        self.output_kb = 131072
        self.passes = 2
        self.apply_runtime_values(constants or build_runtime_values())

    def _coerce_int(self, raw: object, default: int, min_value: int, max_value: int) -> int:
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.timeout_sec = self._coerce_int(
            values.get("PREVIEW_TEX_TIMEOUT_SEC", 120),
            default=120,
            min_value=5,
            max_value=1800,
        )
        self.memory_mb = self._coerce_int(
            values.get("PREVIEW_TEX_MEMORY_MB", 1024),
            default=1024,
            min_value=16,
            max_value=262144,
        )
        self.process_limit = self._coerce_int(
            values.get("PREVIEW_TEX_PROCESS_LIMIT", 64),
            default=64,
            min_value=1,
            max_value=4096,
        )
        self.output_kb = self._coerce_int(
            values.get("PREVIEW_TEX_OUTPUT_KB", 131072),
            default=131072,
            min_value=64,
            max_value=1048576,
        )
        self.passes = self._coerce_int(
            values.get("PREVIEW_TEX_PASSES", 2),
            default=2,
            min_value=1,
            max_value=4,
        )

    def run(
        self,
        *,
        command: list[str],
        cwd: Path,
        extra_mounts: tuple[Path, ...] = (),
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return self.sandbox.run(
            ExecSpec(
                command=command,
                cwd=cwd,
                extra_mounts=extra_mounts,
                env=env,
                timeout_sec=self.timeout_sec,
                output_kb=self.output_kb,
                memory_mb=self.memory_mb,
                process_limit=self.process_limit,
            )
        )

    def compile_pdf(self, tex_path: Path) -> TexCompileResult:
        engine = detect_latex_engine(tex_path)
        final_proc: ExecResult | None = None
        final_log_text = ""
        for _ in range(max(1, int(self.passes))):
            proc = self.run(
                command=[
                    engine,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    str(tex_path.name),
                ],
                cwd=tex_path.parent,
            )
            final_proc = proc
            output_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            log_text = output_text
            generated_log = tex_path.parent / f"{tex_path.stem}.log"
            if generated_log.exists():
                try:
                    log_text = generated_log.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    log_text = output_text
            final_log_text = log_text
            if proc.timed_out or int(proc.returncode or 0) != 0:
                break
        if final_proc is None:
            raise RuntimeError("tex compile did not execute")
        return TexCompileResult(
            engine=engine,
            proc=final_proc,
            log_text=final_log_text,
            pdf_path=tex_path.with_suffix(".pdf"),
        )
