from dataclasses import dataclass
from pathlib import Path

from app.config import ConfigValues
from app.service.platform.latex_process import detect_latex_engine
from app.service.sandbox.base import ExecResult, ExecSpec, SandboxBackend
from app.service.sandbox.tex_backend import TexSandboxBackend


@dataclass(frozen=True)
class TexCompileResult:
    engine: str
    proc: ExecResult
    log_text: str
    pdf_path: Path


@dataclass(frozen=True)
class TexCompilePolicy:
    timeout_sec: int
    memory_mb: int
    process_limit: int
    output_kb: int


class TexCompileService:
    def __init__(
        self,
        *,
        config_values: ConfigValues,
        sandbox_backend: SandboxBackend | None = None,
    ) -> None:
        self.sandbox = sandbox_backend or TexSandboxBackend()
        self._config_values = config_values

    def _policy(self) -> TexCompilePolicy:
        return TexCompilePolicy(
            timeout_sec=self._config_values.integer("PREVIEW_TEX_TIMEOUT_SEC"),
            memory_mb=self._config_values.integer("PREVIEW_TEX_MEMORY_MB"),
            process_limit=self._config_values.integer("PREVIEW_TEX_PROCESS_LIMIT"),
            output_kb=self._config_values.integer("PREVIEW_TEX_OUTPUT_KB"),
        )

    def run(
        self,
        *,
        command: list[str],
        cwd: Path,
        read_only_mounts: tuple[Path, ...] = (),
        writable_mounts: tuple[Path, ...] = (),
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return self._run_with_policy(
            self._policy(),
            command=command,
            cwd=cwd,
            read_only_mounts=read_only_mounts,
            writable_mounts=writable_mounts,
            env=env,
        )

    def _run_with_policy(
        self,
        policy: TexCompilePolicy,
        *,
        command: list[str],
        cwd: Path,
        read_only_mounts: tuple[Path, ...] = (),
        writable_mounts: tuple[Path, ...] = (),
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return self.sandbox.run(
            ExecSpec(
                command=command,
                cwd=cwd,
                read_only_mounts=read_only_mounts,
                writable_mounts=writable_mounts,
                env=env,
                timeout_sec=policy.timeout_sec,
                output_kb=policy.output_kb,
                memory_mb=policy.memory_mb,
                process_limit=policy.process_limit,
            )
        )

    def compile_pdf(self, tex_path: Path) -> TexCompileResult:
        policy = self._policy()
        engine = detect_latex_engine(tex_path)
        final_proc: ExecResult | None = None
        final_log_text = ""
        for _ in range(2):
            proc = self._run_with_policy(
                policy,
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
