from __future__ import annotations

import io
import json
import re
import shutil
import xml.etree.ElementTree
ET = xml.etree.ElementTree
import zipfile
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

from app.service.platform.testlib_source import maintained_testlib_header
from app.service.problem.solution_metadata import normalize_expected_behavior, render_solution_desc
from app.service.statement.constant import (
    DEFAULT_PROBLEM_TITLE,
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    DEFAULT_STATEMENT_TEMPLATE,
    STATEMENT_PROBLEM_REL,
    STATEMENT_RENDERED_DIR_REL,
    STATEMENT_SECTIONS_DIR,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
)
from app.service.statement.render import default_olymp_sty_text
from app.service.problem.test_spec import (
    dumps_tests_spec,
    normalize_gen_command,
    normalize_imported_manual_input,
    parse_gen_command_tokens,
    payload_rel_path_for_test,
)


ZIP_MAX_BYTES = 256 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 64 * 1024 * 1024
ZIP_TEXT_MAX_BYTES = 8 * 1024 * 1024
SOURCE_SUFFIX_ALLOW = {".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".py", ".java"}
GENERATOR_CPP_SUFFIX_ALLOW = {".cpp", ".cc", ".cxx", ".c++"}
STATEMENT_SECTION_SAMPLE_FILE_RE = re.compile(r"^example\.\d+(?:\.a)?$", re.IGNORECASE)
POLYGON_SOLUTION_TAG_EXPECTED: dict[str, str] = {
    "main": "accepted",
    "accepted": "accepted",
    "wrong-answer": "wrong_answer",
    "presentation-error": "wrong_answer",
    "time-limit-exceeded": "time_limit_exceeded",
    "time-limit-exceeded-or-accepted": "tle_or_correct",
    "time-limit-exceeded-or-memory-limit-exceeded": "tle_or_re",
    "memory-limit-exceeded": "run_time_error",
    "rejected": "rejected",
    "failed": "rejected",
    "do-not-run": "unknown",
}


PolygonTestRow = TypedDict(
    "PolygonTestRow",
    {
        "method": str,
        "sample": bool,
        "cmd": str,
    },
)

PolygonSolutionRow = TypedDict(
    "PolygonSolutionRow",
    {
        "path": str,
        "tag": str,
        "source_type": str,
    },
)

PolygonMeta = TypedDict(
    "PolygonMeta",
    {
        "title": str,
        "time_limit_ms": int,
        "memory_limit_bytes": int,
        "run_count_raw": str,
        "pass_limit": int,
        "has_multipass_property": bool,
        "is_multipass": bool,
        "tests": list[PolygonTestRow],
        "input_pattern": str,
        "answer_pattern": str,
        "statement_template_path": str,
        "problem_template_path": str,
        "style_path": str,
        "checker_name": str,
        "checker_source": str,
        "validator_sources": list[str],
        "interactor_source": str,
        "solutions": list[PolygonSolutionRow],
        "executables": list[str],
        "statement_languages": list[str],
    },
)

StatementImportSummary = TypedDict(
    "StatementImportSummary",
    {
        "copied_files": int,
        "language": str,
        "language_warning": str,
        "prebuilt_pdf_count": int,
        "prebuilt_pdf_languages": list[str],
    },
)

TestsImportSummary = TypedDict(
    "TestsImportSummary",
    {
        "manual": int,
        "gen": int,
        "total": int,
        "generated_fallback_to_manual": int,
        "answers": int,
    },
)

ComponentImportSummary = TypedDict(
    "ComponentImportSummary",
    {
        "testlib_source": str,
        "checker_standard": str | None,
        "checker_source": str | None,
        "validator_source": str | None,
        "interactor_source": str | None,
        "generator_sources": list[str],
    },
)

SolutionImportSummary = TypedDict(
    "SolutionImportSummary",
    {
        "count": int,
        "accepted_source": str,
    },
)


def _xml_attr(node: ET.Element | None, name: str) -> str:
    if node is None:
        return ""
    raw = node.get(name)
    if raw is None:
        return ""
    return raw.strip()


def _xml_text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    raw = node.findtext(path)
    if raw is None:
        return ""
    return raw.strip()


def _normalize_zip_path(raw: str | None) -> str:
    if raw is None:
        return ""
    text = raw.replace("\\", "/").strip()
    if not text:
        return ""
    pure = PurePosixPath(text)
    if pure.is_absolute():
        return ""
    parts: list[str] = []
    for part in pure.parts:
        token = part.strip()
        if not token or token == ".":
            continue
        if token == "..":
            return ""
        parts.append(token)
    return "/".join(parts)


def _bool_attr(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _safe_read_json(path: Path) -> dict[str, object]:
    try:
        if path.exists() and path.is_file() and (not path.is_symlink()):
            return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}
    return {}


def _read_text_from_zip(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if int(info.file_size) > ZIP_TEXT_MAX_BYTES:
        raise ValueError(f"zip entry too large for text: {info.filename}")
    with zf.open(info, "r") as fh:
        raw = fh.read(ZIP_TEXT_MAX_BYTES + 1)
    if len(raw) > ZIP_TEXT_MAX_BYTES:
        raise ValueError(f"zip entry too large for text: {info.filename}")
    return raw.decode("utf-8", errors="replace")


def _read_bytes_from_zip(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if int(info.file_size) > ZIP_MAX_FILE_BYTES:
        raise ValueError(f"zip entry too large: {info.filename}")
    with zf.open(info, "r") as fh:
        raw = fh.read(ZIP_MAX_FILE_BYTES + 1)
    if len(raw) > ZIP_MAX_FILE_BYTES:
        raise ValueError(f"zip entry too large: {info.filename}")
    return raw


def _normalize_text_newlines_bytes(payload: bytes) -> bytes:
    return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _entry_map_from_zip(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    raw: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        normalized = _normalize_zip_path(info.filename)
        if not normalized:
            continue
        raw[normalized] = info
    if "problem.xml" in raw:
        return raw
    candidates = sorted([p for p in raw if p.endswith("/problem.xml")], key=len)
    if not candidates:
        raise ValueError("problem.xml not found in package")
    prefix = candidates[0][:-len("problem.xml")]
    mapped: dict[str, zipfile.ZipInfo] = {}
    for path, info in raw.items():
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        if rel:
            mapped[rel] = info
    if "problem.xml" not in mapped:
        raise ValueError("problem.xml not found in package")
    return mapped


def _expand_pattern(pattern: str, index: int) -> str:
    token = pattern.strip()
    if not token:
        return ""
    try:
        return token % int(index)
    except Exception:
        pass
    # Minimal fallback for common styles.
    return token.replace("%02d", f"{int(index):02d}").replace("%03d", f"{int(index):03d}").replace("%d", str(int(index)))


def _unique_rel_path(workspace: Path, parent_rel: Path, filename: str) -> str:
    safe_name = Path(filename).name if filename else ""
    if not safe_name:
        safe_name = "source.cpp"
    candidate = parent_rel / safe_name
    if not (workspace / candidate).exists():
        return candidate.as_posix()
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    idx = 2
    while True:
        item = parent_rel / f"{stem}-{idx}{suffix}"
        if not (workspace / item).exists():
            return item.as_posix()
        idx += 1


class PolygonPackageImportService:
    def _parse_problem_xml(self, xml_text: str) -> PolygonMeta:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"invalid problem.xml: {exc}") from exc

        names: list[dict[str, str]] = []
        for node in root.findall("./names/name"):
            language = _xml_attr(node, "language")
            value = _xml_attr(node, "value")
            if language and value:
                names.append({"language": language, "value": value})

        problem_title = DEFAULT_PROBLEM_TITLE
        for row in names:
            if row["language"].lower() == "english":
                problem_title = row["value"]
                break
        if problem_title == DEFAULT_PROBLEM_TITLE and names:
            problem_title = names[0]["value"]

        judging_node = root.find("./judging")
        run_count_raw = _xml_attr(judging_node, "run-count")
        pass_limit = max(1, _coerce_int(run_count_raw, 1))
        multipass_property = False
        for node in root.findall("./properties/property"):
            name = _xml_attr(node, "name").lower().replace("_", "-")
            if name in {"multipass", "multi-pass"}:
                multipass_property = _bool_attr(_xml_attr(node, "value"))
                if multipass_property:
                    break

        testset = root.find("./judging/testset")
        time_limit_ms = max(1, _coerce_int(_xml_text(testset, "time-limit"), 2000))
        memory_limit_bytes = max(1, _coerce_int(_xml_text(testset, "memory-limit"), 1024 * 1024 * 1024))
        input_pattern = _xml_text(testset, "input-path-pattern")
        answer_pattern = _xml_text(testset, "answer-path-pattern")

        tests: list[PolygonTestRow] = []
        for node in root.findall("./judging/testset/tests/test"):
            method = _xml_attr(node, "method").lower()
            tests.append(
                {
                    "method": method,
                    "sample": _bool_attr(_xml_attr(node, "sample")),
                    "cmd": normalize_gen_command(_xml_attr(node, "cmd")) if method == "generated" else "",
                }
            )

        statement_template_path = "files/statements.ftl"
        problem_template_path = "files/problem.tex"
        style_path = "files/olymp.sty"
        for node in root.findall("./files/resources/file"):
            path = _xml_attr(node, "path")
            normalized_path = _normalize_zip_path(path)
            source_name = PurePosixPath(normalized_path or path).name.lower()
            if source_name == "statements.ftl":
                statement_template_path = path
            elif source_name == "problem.tex":
                problem_template_path = path
            elif source_name == "olymp.sty":
                style_path = path

        checker_node = root.find("./assets/checker")
        checker_name = _xml_attr(checker_node, "name")
        checker_source = _xml_attr(checker_node.find("./source") if checker_node is not None else None, "path")

        validator_sources: list[str] = []
        for node in root.findall("./assets/validators/validator/source"):
            path = _xml_attr(node, "path")
            if path:
                validator_sources.append(path)

        interactor_source = _xml_attr(root.find("./assets/interactor/source"), "path")

        solutions: list[PolygonSolutionRow] = []
        for node in root.findall("./assets/solutions/solution"):
            source_node = node.find("./source")
            source_path = _xml_attr(source_node, "path")
            if not source_path:
                continue
            solutions.append(
                {
                    "path": source_path,
                    "tag": _xml_attr(node, "tag").lower(),
                    "source_type": _xml_attr(source_node, "type").lower(),
                }
            )

        executables: list[str] = []
        for node in root.findall("./files/executables/executable/source"):
            path = _xml_attr(node, "path")
            if path:
                executables.append(path)

        statement_languages: list[str] = []
        for node in root.findall("./statements/statement"):
            statement_type = _xml_attr(node, "type").lower()
            language = _xml_attr(node, "language")
            if statement_type == "application/x-tex" and language:
                statement_languages.append(language)

        return {
            "title": problem_title,
            "time_limit_ms": time_limit_ms,
            "memory_limit_bytes": memory_limit_bytes,
            "run_count_raw": run_count_raw,
            "pass_limit": pass_limit,
            "has_multipass_property": multipass_property,
            "is_multipass": multipass_property or (pass_limit > 1),
            "tests": tests,
            "input_pattern": input_pattern,
            "answer_pattern": answer_pattern,
            "statement_template_path": statement_template_path,
            "problem_template_path": problem_template_path,
            "style_path": style_path,
            "checker_name": checker_name,
            "checker_source": checker_source,
            "validator_sources": validator_sources,
            "interactor_source": interactor_source,
            "solutions": solutions,
            "executables": executables,
            "statement_languages": statement_languages,
        }

    def _write_text(self, workspace: Path, rel: Path, text: str) -> None:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _write_bytes(self, workspace: Path, rel: Path, payload: bytes) -> None:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    def _copy_zip_entry(self, zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], source_rel: str, workspace: Path, target_rel: Path) -> bool:
        normalized = _normalize_zip_path(source_rel)
        if not normalized:
            return False
        info = entries.get(normalized)
        if info is None:
            return False
        self._write_bytes(workspace, target_rel, _read_bytes_from_zip(zf, info))
        return True

    def _import_statement(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        meta: PolygonMeta,
    ) -> StatementImportSummary:
        shutil.rmtree(workspace / STATEMENT_RENDERED_DIR_REL, ignore_errors=True)
        shutil.rmtree(workspace / STATEMENT_SECTIONS_DIR, ignore_errors=True)

        template_ok = self._copy_zip_entry(
            zf,
            entries,
            meta["statement_template_path"],
            workspace,
            STATEMENT_TEMPLATE_REL,
        )
        if not template_ok:
            self._write_text(workspace, STATEMENT_TEMPLATE_REL, DEFAULT_STATEMENT_TEMPLATE)
        problem_ok = self._copy_zip_entry(
            zf,
            entries,
            meta["problem_template_path"],
            workspace,
            STATEMENT_PROBLEM_REL,
        )
        if not problem_ok:
            self._write_text(workspace, STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
        style_ok = self._copy_zip_entry(
            zf,
            entries,
            meta["style_path"],
            workspace,
            STATEMENT_STYLE_REL,
        )
        if not style_ok:
            self._write_text(workspace, STATEMENT_STYLE_REL, default_olymp_sty_text())

        copied = 0
        for path, info in entries.items():
            if not path.startswith("statement-sections/"):
                continue
            rel = Path(path.replace("\\", "/"))
            if len(rel.parts) >= 3 and STATEMENT_SECTION_SAMPLE_FILE_RE.fullmatch(rel.name):
                continue
            self._write_bytes(workspace, rel, _read_bytes_from_zip(zf, info))
            copied += 1

        copied_prebuilt_pdf = 0
        prebuilt_pdf_languages: list[str] = []
        for path, info in entries.items():
            rel = path.replace("\\", "/")
            if not rel.startswith("statements/.pdf/") or not rel.endswith("/problem.pdf"):
                continue
            parts = Path(rel).parts
            if len(parts) < 4:
                continue
            language = parts[-2]
            if not language:
                continue
            self._write_bytes(workspace, STATEMENT_SECTIONS_DIR / language / "problem.pdf", _read_bytes_from_zip(zf, info))
            copied_prebuilt_pdf += 1
            if language not in prebuilt_pdf_languages:
                prebuilt_pdf_languages.append(language)

        languages = sorted(
            {
                p.relative_to(workspace / STATEMENT_SECTIONS_DIR).parts[0]
                for p in (workspace / STATEMENT_SECTIONS_DIR).glob("*")
                if p.is_dir()
            }
        )
        preferred = "english" if "english" in languages else (languages[0] if languages else "")
        if not preferred:
            preferred = "english"
        language_warning = ""
        if languages and ("english" not in languages):
            language_warning = f"statement language english not found; defaulting to {preferred}"
        return {
            "copied_files": copied,
            "language": preferred,
            "language_warning": language_warning,
            "prebuilt_pdf_count": copied_prebuilt_pdf,
            "prebuilt_pdf_languages": prebuilt_pdf_languages,
        }

    def _supported_generator_tokens(self, meta: PolygonMeta) -> set[str]:
        tokens: set[str] = set()
        for source_path in meta["executables"]:
            source = _normalize_zip_path(source_path)
            if not source:
                continue
            source_name = Path(source).name
            source_stem = Path(source).stem
            suffix = Path(source).suffix.lower()
            if suffix not in GENERATOR_CPP_SUFFIX_ALLOW:
                continue
            if source_name:
                tokens.add(source_name)
            if source_stem:
                tokens.add(source_stem)
        return tokens

    def _generator_command_supported(self, command: str, meta: PolygonMeta) -> bool:
        if not command:
            return False
        try:
            tokens = parse_gen_command_tokens(command)
        except Exception:
            return False
        if not tokens:
            return False
        command_token = Path(tokens[0].replace("\\", "/")).name
        if not command_token:
            return False
        token_stem = Path(command_token).stem
        supported = self._supported_generator_tokens(meta)
        return (command_token in supported) or (token_stem in supported)

    def _import_tests(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        meta: PolygonMeta,
        *,
        normalize_test_data_newlines: bool = False,
    ) -> TestsImportSummary:
        tests = meta["tests"]

        manual_dir = workspace / "tests" / "manual"
        gen_dir = workspace / "tests" / "generator"
        answers_dir = workspace / "tests" / "answers"
        shutil.rmtree(manual_dir, ignore_errors=True)
        shutil.rmtree(gen_dir, ignore_errors=True)
        shutil.rmtree(answers_dir, ignore_errors=True)
        manual_dir.mkdir(parents=True, exist_ok=True)
        gen_dir.mkdir(parents=True, exist_ok=True)
        answers_dir.mkdir(parents=True, exist_ok=True)

        spec_entries: list[dict[str, object]] = []
        input_pattern = meta["input_pattern"]
        answer_pattern = meta["answer_pattern"]
        manual_count = 0
        gen_count = 0
        generated_fallback_to_manual = 0
        answer_count = 0

        for idx, row in enumerate(tests, start=1):
            is_generated = row["method"] == "generated"
            sample = row["sample"]
            test_id = f"{idx:03d}"
            answer_rel = _normalize_zip_path(_expand_pattern(answer_pattern, idx)) if answer_pattern else ""
            sample_output_text = ""
            if answer_rel:
                answer_info = entries.get(answer_rel)
                if answer_info is not None:
                    answer_payload = _read_bytes_from_zip(zf, answer_info)
                    if normalize_test_data_newlines:
                        answer_payload = _normalize_text_newlines_bytes(answer_payload)
                    self._write_bytes(workspace, Path("tests") / "answers" / f"{test_id}.ans", answer_payload)
                    sample_output_text = answer_payload.decode("utf-8", errors="replace")
                    answer_count += 1
            spec_row: dict[str, object] = {"id": test_id, "sample": sample}
            if sample and sample_output_text:
                spec_row["sample_output"] = sample_output_text
            if is_generated:
                cmd = row["cmd"]
                if self._generator_command_supported(cmd, meta):
                    spec_entries.append({**spec_row, "kind": "gen"})
                    self._write_text(workspace, Path(payload_rel_path_for_test(test_id, "gen")), cmd)
                    gen_count += 1
                    continue

            input_rel = _normalize_zip_path(_expand_pattern(input_pattern, idx))
            if not input_rel:
                raise ValueError(f"cannot resolve test input path for test #{idx}")
            info = entries.get(input_rel)
            if info is None:
                if is_generated:
                    cmd = row["cmd"]
                    spec_entries.append({**spec_row, "kind": "gen"})
                    self._write_text(workspace, Path(payload_rel_path_for_test(test_id, "gen")), cmd)
                    gen_count += 1
                    continue
                raise ValueError(f"missing test input file in package: {input_rel}")
            payload_text = normalize_imported_manual_input(_read_bytes_from_zip(zf, info).decode("utf-8", errors="replace"))
            spec_entries.append({**spec_row, "kind": "manual"})
            self._write_text(workspace, Path(payload_rel_path_for_test(test_id, "manual")), payload_text)
            manual_count += 1
            if is_generated:
                generated_fallback_to_manual += 1

        self._write_text(workspace, Path("tests/spec.json"), dumps_tests_spec(spec_entries))
        return {
            "manual": manual_count,
            "gen": gen_count,
            "total": len(spec_entries),
            "generated_fallback_to_manual": generated_fallback_to_manual,
            "answers": answer_count,
        }

    def _copy_source_from_zip(self, zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], source_path: str, workspace: Path, target_folder: str, target_name: str) -> str:
        normalized = _normalize_zip_path(source_path)
        if not normalized:
            return ""
        info = entries.get(normalized)
        if info is None:
            return ""
        suffix = Path(normalized).suffix.lower()
        if suffix and suffix not in SOURCE_SUFFIX_ALLOW:
            return ""
        rel = Path(target_folder) / target_name
        payload = _read_bytes_from_zip(zf, info)
        self._write_bytes(workspace, rel, payload)
        return rel.as_posix()

    def _write_maintained_testlib(self, workspace: Path) -> str:
        target_rel = Path("third_party") / "testlib" / "testlib.h"
        self._write_bytes(workspace, target_rel, maintained_testlib_header(repo_root=Path(__file__).resolve().parents[3]).read_bytes())
        return "third_party/upstream/testlib/testlib.h"

    def _import_components(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        meta: PolygonMeta,
    ) -> ComponentImportSummary:
        build_cfg = _safe_read_json(workspace / "config" / "build.json")

        imported_testlib = self._write_maintained_testlib(workspace)

        checker_name = meta["checker_name"]
        checker_source_path = meta["checker_source"]
        checker_standard: str | None = None
        imported_checker_source: str | None = None
        if checker_name.startswith("std::"):
            checker_standard = checker_name
            build_cfg["checker_standard"] = checker_name
            build_cfg.pop("checker_source", None)
        else:
            imported_checker_source = self._copy_source_from_zip(
                zf,
                entries,
                checker_source_path,
                workspace,
                "checkers",
                "checker.cpp",
            )
            if imported_checker_source:
                build_cfg["checker_source"] = imported_checker_source
            else:
                build_cfg.pop("checker_source", None)
            build_cfg.pop("checker_standard", None)

        validator_source: str | None = None
        validator_sources = meta["validator_sources"]
        if validator_sources:
            imported_validator_source = self._copy_source_from_zip(
                zf,
                entries,
                validator_sources[0],
                workspace,
                "validators",
                "validator.cpp",
            )
            if imported_validator_source:
                validator_source = imported_validator_source
                build_cfg["validator_source"] = imported_validator_source
            else:
                build_cfg.pop("validator_source", None)
        else:
            build_cfg.pop("validator_source", None)

        imported_interactor_source = self._copy_source_from_zip(
            zf,
            entries,
            meta["interactor_source"],
            workspace,
            "interactors",
            "interactor.cpp",
        )
        interactor_source: str | None = None
        if imported_interactor_source:
            interactor_source = imported_interactor_source
            build_cfg["interactor_source"] = imported_interactor_source
        else:
            build_cfg.pop("interactor_source", None)

        used = {checker_source_path, meta["interactor_source"], *validator_sources}
        generator_names = {
            row["cmd"].split()[0]
            for row in meta["tests"]
            if row["method"] == "generated" and row["cmd"]
        }
        generator_sources: list[str] = []
        for source in meta["executables"]:
            if source in used:
                continue
            stem = Path(source).stem
            if (stem not in generator_names) and (not stem.lower().startswith("gen")):
                continue
            suffix = Path(source).suffix.lower()
            imported = self._copy_source_from_zip(zf, entries, source, workspace, "generators", Path(source).name)
            if imported and suffix in GENERATOR_CPP_SUFFIX_ALLOW:
                generator_sources.append(imported)
        generator_sources = sorted(dict.fromkeys(generator_sources))
        build_cfg["generator_sources"] = generator_sources

        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "build.json").write_text(
            json.dumps(build_cfg, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        return {
            "testlib_source": imported_testlib,
            "checker_standard": checker_standard,
            "checker_source": imported_checker_source,
            "validator_source": validator_source,
            "interactor_source": interactor_source,
            "generator_sources": generator_sources,
        }

    def _solution_expected_from_tag(self, tag: str) -> str:
        if not tag:
            return "unknown"
        direct = POLYGON_SOLUTION_TAG_EXPECTED.get(tag)
        if direct is not None:
            return normalize_expected_behavior(direct)
        expected = normalize_expected_behavior(tag)
        if expected != "unknown":
            return expected
        normalized = tag.replace("-", "_").replace(" ", "_")
        expected = normalize_expected_behavior(normalized)
        if expected != "unknown":
            return expected
        return "unknown"

    @staticmethod
    def _solution_suffix_from_source_type(source_type: str) -> str:
        if not source_type:
            return ""
        token = source_type
        if ("python" in token) or ("pypy" in token):
            return ".py"
        if "java" in token:
            return ".java"
        if ("cpp" in token) or ("c++" in token) or ("g++" in token) or ("clang++" in token):
            return ".cpp"
        return ""

    def _solution_filename_for_import(self, source_path: str, source_type: str) -> str:
        safe_name = Path(source_path).name
        if not safe_name:
            safe_name = "solution"
        expected_suffix = self._solution_suffix_from_source_type(source_type)
        if not expected_suffix:
            return safe_name
        lower_name = safe_name.lower()
        if lower_name.endswith(expected_suffix):
            return safe_name
        current_suffix = Path(safe_name).suffix.lower()
        if (not current_suffix) or (current_suffix in {".cpp", ".cc", ".cxx", ".c++", ".py", ".java"}):
            return f"{safe_name}{expected_suffix}"
        return safe_name

    def _import_solutions(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        meta: PolygonMeta,
    ) -> SolutionImportSummary:
        solution_rows = meta["solutions"]

        solutions_dir_rel = Path("solutions")
        solutions_dir = workspace / solutions_dir_rel
        solutions_dir.mkdir(parents=True, exist_ok=True)
        accepted_source = ""
        imported_count = 0
        for row in solution_rows:
            source_path = _normalize_zip_path(row["path"])
            if not source_path:
                continue
            info = entries.get(source_path)
            if info is None:
                continue
            source_type = row["source_type"]
            filename = self._solution_filename_for_import(source_path, source_type)
            target_rel = _unique_rel_path(workspace, solutions_dir_rel, filename)
            payload = _read_bytes_from_zip(zf, info)
            self._write_bytes(workspace, Path(target_rel), payload)
            tag = row["tag"]
            expected = self._solution_expected_from_tag(tag)
            self._write_text(workspace, Path(f"{target_rel}.desc"), render_solution_desc(expected, ""))
            if not accepted_source and (expected == "accepted"):
                accepted_source = target_rel
            if tag == "main":
                accepted_source = target_rel
            imported_count += 1
        return {"count": imported_count, "accepted_source": accepted_source}

    def _write_problem_config(
        self,
        workspace: Path,
        meta: PolygonMeta,
        components: ComponentImportSummary,
    ) -> dict[str, object]:
        cfg = _safe_read_json(workspace / "config" / "problem.json")
        pass_limit = int(meta["pass_limit"])
        explicit_run_count = str(meta.get("run_count_raw") or "").strip()
        if bool(meta.get("has_multipass_property")) and explicit_run_count in {"", "1"}:
            raise ValueError("multipass Polygon package is missing explicit pass limit")
        mode = "interactive" if components["interactor_source"] else "pass-fail"
        cfg["time_limit_ms"] = meta["time_limit_ms"]
        cfg["memory_limit_mb"] = max(1, meta["memory_limit_bytes"] // (1024 * 1024))
        cfg["mode"] = mode
        cfg["pass_limit"] = pass_limit
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "problem.json").write_text(
            json.dumps(cfg, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return cfg

    def import_package(
        self,
        workspace: Path,
        package_name: str,
        package_bytes: bytes,
        *,
        normalize_test_data_newlines: bool = False,
    ) -> dict[str, object]:
        raw = package_bytes
        if not raw:
            raise ValueError("empty package file")
        if len(raw) > ZIP_MAX_BYTES:
            raise ValueError("package file is too large")

        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            entry_map = _entry_map_from_zip(zf)
            xml_info = entry_map.get("problem.xml")
            if xml_info is None:
                raise ValueError("problem.xml not found in package")
            meta = self._parse_problem_xml(_read_text_from_zip(zf, xml_info))
            statement_summary = self._import_statement(zf, entry_map, workspace, meta)
            tests_summary = self._import_tests(
                zf,
                entry_map,
                workspace,
                meta,
                normalize_test_data_newlines=normalize_test_data_newlines,
            )
            component_summary = self._import_components(zf, entry_map, workspace, meta)
            solutions_summary = self._import_solutions(zf, entry_map, workspace, meta)
            build_cfg = _safe_read_json(workspace / "config" / "build.json")
            build_cfg_changed = False
            if solutions_summary["accepted_source"]:
                build_cfg["accepted_solution_source"] = solutions_summary["accepted_source"]
                build_cfg_changed = True
            if build_cfg_changed:
                (workspace / "config" / "build.json").write_text(
                    json.dumps(build_cfg, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            problem_cfg = self._write_problem_config(workspace, meta, component_summary)
            return {
                "package_name": package_name.strip(),
                "title": meta["title"],
                "statement": statement_summary,
                "tests": tests_summary,
                "components": component_summary,
                "solutions": solutions_summary,
                "problem_cfg": problem_cfg,
            }
