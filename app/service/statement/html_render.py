"""Sandboxed best-effort conversion of rendered Polygon statements to HTML."""

from __future__ import annotations

from html import escape
from importlib import import_module
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config import ConfigValues
from app.service.sandbox.base import ExecResult, ExecSpec, SandboxBackend


nh3 = import_module("nh3")

RESOURCE_PLACEHOLDER = "__STATEMENT_PREVIEW_RESOURCE__/"
_FILTER_PATH = Path(__file__).with_name("pandoc_statement.lua")
_PANDOC_SINGLE_CAPABILITY = ("+RTS", "-N1", "-RTS")
_SAFE_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MAX_IMAGE_COUNT = 128
_STATEMENT_TITLE_START = re.compile(r"<h2(?:\s[^>]*)?>", re.IGNORECASE)
_MATHML_TAGS = {
    "annotation",
    "math",
    "mfrac",
    "mi",
    "mn",
    "mo",
    "mover",
    "mroot",
    "mrow",
    "mspace",
    "msqrt",
    "msub",
    "msubsup",
    "msup",
    "mtable",
    "mtd",
    "mtext",
    "mtr",
    "munder",
    "munderover",
    "semantics",
}


@dataclass(frozen=True)
class StatementHtmlRenderResult:
    fragment: str
    warnings: tuple[str, ...]
    resources: tuple[str, ...]
    log_text: str


class StatementHtmlRenderError(RuntimeError):
    """A renderer failure whose complete tool output can be shown to the user."""

    def __init__(self, message: str, *, log_text: str = "") -> None:
        super().__init__(message)
        self.log_text = log_text


def number_statement_fragment(fragment: str, idx: str) -> str:
    """Prefix the first sanitized Statement title while preserving its tag."""

    match = _STATEMENT_TITLE_START.search(fragment)
    if match is None:
        raise ValueError("Statement HTML fragment is missing its title heading.")
    return (
        fragment[: match.end()]
        + f"{escape(idx)}. "
        + fragment[match.end() :]
    )


class StatementHtmlRenderer:
    """Convert one self-contained statement render tree without network access."""

    def __init__(
        self,
        sandbox_backend: SandboxBackend,
        config_values: ConfigValues,
    ) -> None:
        self._sandbox = sandbox_backend
        self._config_values = config_values

    def render(
        self,
        render_root: Path,
        output_root: Path,
        *,
        subject_token: str,
    ) -> StatementHtmlRenderResult:
        source_root = render_root.resolve()
        output = output_root.resolve()
        problem_tex = source_root / "problem.tex"
        if not self._safe_file(source_root, problem_tex):
            raise RuntimeError("rendered problem.tex is missing")
        output.mkdir(parents=True, exist_ok=True)
        resources_root = output / "resources"
        resources_root.mkdir(parents=True, exist_ok=True)
        ast_path = output / "statement.json"
        html_path = output / "content.html"
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/tmp",
            "STATEMENT_PREVIEW_ID": self._safe_subject_token(subject_token),
            "STATEMENT_RENDER_ROOT": str(source_root),
        }
        parse_result = self._run(
            [
                "pandoc",
                *_PANDOC_SINGLE_CAPABILITY,
                str(problem_tex),
                "--from=latex+raw_tex+latex_macros",
                "--to=json",
                f"--lua-filter={_FILTER_PATH}",
                f"--output={ast_path}",
            ],
            cwd=output,
            read_only_mounts=(source_root, _FILTER_PATH),
            env=env,
        )
        if parse_result.returncode != 0 or parse_result.timed_out:
            raise StatementHtmlRenderError(
                self._failure("Pandoc statement parsing failed", parse_result.stderr),
                log_text=parse_result.stderr,
            )
        self._check_output_file(ast_path, label="Pandoc statement AST")
        try:
            document = json.loads(ast_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Pandoc returned an invalid statement AST") from exc
        warnings = self._raw_tex_warnings(document)
        resources = self._rewrite_images(
            document,
            source_root=source_root,
            resources_root=resources_root,
        )
        ast_path.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        html_result = self._run(
            [
                "pandoc",
                *_PANDOC_SINGLE_CAPABILITY,
                str(ast_path),
                "--from=json",
                "--to=html5",
                "--mathml",
                f"--output={html_path}",
            ],
            cwd=output,
            writable_mounts=(output,),
            env=env,
        )
        if html_result.returncode != 0 or html_result.timed_out:
            raise StatementHtmlRenderError(
                self._failure("Pandoc HTML rendering failed", html_result.stderr),
                log_text="\n".join(
                    part
                    for part in (
                        parse_result.stderr.strip(),
                        html_result.stderr.strip(),
                    )
                    if part
                ),
            )
        self._check_output_file(html_path, label="Pandoc statement HTML")
        try:
            raw_html = html_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("Pandoc did not create statement HTML") from exc
        fragment = self._sanitize(raw_html)
        if not fragment.strip():
            raise RuntimeError("statement HTML is empty after sanitization")
        log_text = "\n".join(
            part for part in (parse_result.stderr.strip(), html_result.stderr.strip()) if part
        )
        return StatementHtmlRenderResult(
            fragment=fragment,
            warnings=tuple(warnings),
            resources=tuple(resources),
            log_text=log_text,
        )

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        read_only_mounts: tuple[Path, ...] = (),
        writable_mounts: tuple[Path, ...] = (),
        env: dict[str, str],
    ) -> ExecResult:
        return self._sandbox.run(
            ExecSpec(
                command=command,
                cwd=cwd,
                read_only_mounts=read_only_mounts,
                writable_mounts=writable_mounts,
                env=env,
                timeout_sec=self._config_values.integer("PREVIEW_HTML_TIMEOUT_SEC"),
                memory_mb=self._config_values.integer("PREVIEW_HTML_MEMORY_MB"),
                process_limit=self._config_values.integer("PREVIEW_HTML_PROCESS_LIMIT"),
                output_kb=self._config_values.integer("PREVIEW_HTML_OUTPUT_KB"),
            )
        )

    def _rewrite_images(
        self,
        document: object,
        *,
        source_root: Path,
        resources_root: Path,
    ) -> list[str]:
        resources: list[str] = []
        serial = 0

        def visit(node: object) -> None:
            nonlocal serial
            if isinstance(node, dict):
                if node.get("t") == "Image":
                    if serial >= _MAX_IMAGE_COUNT:
                        raise RuntimeError("statement contains too many images")
                    content = node.get("c")
                    if not isinstance(content, list) or len(content) != 3:
                        raise RuntimeError("Pandoc returned an invalid image node")
                    target = content[2]
                    if not isinstance(target, list) or len(target) != 2:
                        raise RuntimeError("Pandoc returned an invalid image target")
                    source = str(target[0])
                    page = self._image_page(content[0])
                    serial += 1
                    name = self._copy_image(
                        source,
                        page=page,
                        serial=serial,
                        source_root=source_root,
                        resources_root=resources_root,
                    )
                    target[0] = RESOURCE_PLACEHOLDER + name
                    resources.append(name)
                    total_bytes = sum(
                        (resources_root / item).stat().st_size for item in resources
                    )
                    if total_bytes > self._output_limit_bytes():
                        raise RuntimeError(
                            "statement image resources exceed the preview limit"
                        )
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(document)
        return resources

    def _check_output_file(self, path: Path, *, label: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"{label} is missing")
        if path.stat().st_size > self._output_limit_bytes():
            raise RuntimeError(f"{label} exceeds the preview limit")

    def _output_limit_bytes(self) -> int:
        return self._config_values.integer("PREVIEW_HTML_OUTPUT_KB") * 1024

    def _copy_image(
        self,
        source: str,
        *,
        page: int,
        serial: int,
        source_root: Path,
        resources_root: Path,
    ) -> str:
        relative = self._safe_relative_resource(source)
        source_path = source_root.joinpath(*relative.parts)
        if not self._safe_file(source_root, source_path):
            raise RuntimeError(f"statement image is missing: {source}")
        suffix = source_path.suffix.lower()
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source_path.stem).strip("-.") or "image"
        if suffix in _SAFE_RASTER_SUFFIXES:
            name = f"{serial}-{stem}{suffix}"
            shutil.copy2(source_path, resources_root / name)
            return name
        if suffix == ".pdf":
            name = f"{serial}-{stem}.png"
            output_stem = resources_root / name.removesuffix(".png")
            result = self._run(
                [
                    "pdftocairo",
                    "-png",
                    "-singlefile",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-scale-to",
                    "2400",
                    str(source_path),
                    str(output_stem),
                ],
                cwd=resources_root,
                read_only_mounts=(source_root,),
                writable_mounts=(resources_root,),
                env=self._tool_env(),
            )
            if result.returncode != 0 or not (resources_root / name).is_file():
                raise StatementHtmlRenderError(
                    self._failure(
                        f"PDF image conversion failed: {source}",
                        result.stderr,
                    ),
                    log_text=result.stderr,
                )
            return name
        if suffix == ".svg":
            name = f"{serial}-{stem}.png"
            result = self._run(
                ["rsvg-convert", "--keep-aspect-ratio", "--width", "2400", "--output", name, str(source_path)],
                cwd=resources_root,
                read_only_mounts=(source_root,),
                writable_mounts=(resources_root,),
                env=self._tool_env(),
            )
            if result.returncode != 0 or not (resources_root / name).is_file():
                raise StatementHtmlRenderError(
                    self._failure(
                        f"SVG image conversion failed: {source}",
                        result.stderr,
                    ),
                    log_text=result.stderr,
                )
            return name
        raise RuntimeError(f"unsupported statement image format: {source_path.suffix or '(none)'}")

    @staticmethod
    def _image_page(attributes: object) -> int:
        if not isinstance(attributes, list) or len(attributes) != 3:
            return 1
        pairs = attributes[2]
        if not isinstance(pairs, list):
            return 1
        for pair in pairs:
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == "page":
                try:
                    page = int(pair[1])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("statement image page must be an integer") from exc
                if page < 1 or page > 100:
                    raise RuntimeError("statement image page is out of range")
                return page
        return 1

    @staticmethod
    def _raw_tex_warnings(document: object) -> list[str]:
        warnings: list[str] = []

        def visit(node: object) -> None:
            if isinstance(node, dict):
                if node.get("t") in {"RawBlock", "RawInline"}:
                    payload = node.get("c")
                    if isinstance(payload, list) and len(payload) == 2 and payload[0] == "latex":
                        text = re.sub(r"\s+", " ", str(payload[1])).strip()
                        if text:
                            warnings.append(f"Unsupported TeX was omitted: {text[:120]}")
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(document)
        return warnings

    @staticmethod
    def _sanitize(fragment: str) -> str:
        return nh3.clean(
            fragment,
            tags={
                "a", "article", "blockquote", "br", "code", "div", "em",
                "figure", "figcaption", "h2", "h3", "h4", "h5", "img",
                "li", "ol", "p", "pre", "section", "span", "strong",
                "sub", "sup", "table", "tbody", "td", "th", "thead", "tr",
                "ul", "var",
            } | _MATHML_TAGS,
            attributes={
                "*": {"class", "id"},
                "a": {"href", "title"},
                "img": {"alt", "height", "src", "title", "width"},
                "math": {"display", "xmlns"},
                "annotation": {"encoding"},
                "mo": {"form", "stretchy"},
                "mtable": {"columnalign", "columnspacing", "rowspacing"},
                "mtd": {"columnalign"},
            },
            url_schemes=set(),
        )

    @staticmethod
    def _safe_relative_resource(value: str) -> PurePosixPath:
        if "\\" in value or ":" in value or value.startswith(("/", "//")):
            raise RuntimeError(f"unsafe statement image path: {value}")
        relative = PurePosixPath(value.split("#", 1)[0].split("?", 1)[0])
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise RuntimeError(f"unsafe statement image path: {value}")
        return relative

    @staticmethod
    def _safe_file(root: Path, path: Path) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            resolved_root = root.resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents

    @staticmethod
    def _safe_subject_token(value: str) -> str:
        token = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
        return token[:80] or "problem"

    @staticmethod
    def _failure(label: str, detail: str) -> str:
        compact = re.sub(r"\s+", " ", detail).strip()
        return f"{label}: {compact[:300]}" if compact else label

    @staticmethod
    def _tool_env() -> dict[str, str]:
        return {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/tmp",
        }
