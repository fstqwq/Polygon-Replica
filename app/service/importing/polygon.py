from __future__ import annotations

import io
import json
import re
import shutil
import xml.etree.ElementTree
ET = xml.etree.ElementTree
import zipfile
from pathlib import Path, PurePosixPath

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
    normalize_manual_input,
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


def _normalize_zip_path(raw: str) -> str:
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        return ""
    pure = PurePosixPath(text)
    if pure.is_absolute():
        return ""
    parts: list[str] = []
    for part in pure.parts:
        token = str(part or "").strip()
        if not token or token == ".":
            continue
        if token == "..":
            return ""
        parts.append(token)
    return "/".join(parts)


def _bool_attr(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw or "").strip())
    except Exception:
        return int(default)


def _bool_text(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_read_json(path: Path) -> dict:
    try:
        if path.exists() and path.is_file() and (not path.is_symlink()):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
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
    return bytes(payload or b"").replace(b"\r\n", b"\n").replace(b"\r", b"\n")


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
    token = str(pattern or "").strip()
    if not token:
        return ""
    try:
        return token % int(index)
    except Exception:
        pass
    # Minimal fallback for common styles.
    return token.replace("%02d", f"{int(index):02d}").replace("%03d", f"{int(index):03d}").replace("%d", str(int(index)))


def _unique_rel_path(workspace: Path, parent_rel: Path, filename: str) -> str:
    safe_name = Path(str(filename or "")).name
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


def _find_file_upwards(start: Path, rel: Path) -> Path | None:
    base = start.resolve()
    if base.is_file():
        base = base.parent
    for parent in (base, *base.parents):
        candidate = parent / rel
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


class PolygonPackageImportService:
    def _parse_problem_xml(self, xml_text: str) -> dict[str, object]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"invalid problem.xml: {exc}") from exc

        names: list[dict[str, str]] = []
        for node in root.findall("./names/name"):
            language = str(node.get("language") or "").strip()
            value = str(node.get("value") or "").strip()
            if language and value:
                names.append({"language": language, "value": value})
        problem_title = ""
        for row in names:
            if row["language"].lower() == "english":
                problem_title = row["value"]
                break
        if not problem_title and names:
            problem_title = names[0]["value"]
        if not problem_title:
            problem_title = DEFAULT_PROBLEM_TITLE

        judging_node = root.find("./judging")
        run_count = _coerce_int(judging_node.get("run-count") if judging_node is not None else None, 1)
        multipass_property = False
        for node in root.findall("./properties/property"):
            name = str(node.get("name") or "").strip().lower().replace("_", "-")
            if name in {"multipass", "multi-pass"}:
                multipass_property = _bool_text(node.get("value"))
                if multipass_property:
                    break
        testset = root.find("./judging/testset")
        time_limit_ms = _coerce_int(testset.findtext("time-limit") if testset is not None else None, 2000)
        memory_limit_bytes = _coerce_int(testset.findtext("memory-limit") if testset is not None else None, 1024 * 1024 * 1024)
        input_pattern = str(testset.findtext("input-path-pattern") if testset is not None else "").strip()
        answer_pattern = str(testset.findtext("answer-path-pattern") if testset is not None else "").strip()
        tests: list[dict[str, object]] = []
        for node in root.findall("./judging/testset/tests/test"):
            tests.append(
                {
                    "method": str(node.get("method") or "").strip().lower(),
                    "sample": _bool_attr(node.get("sample")),
                    "cmd": str(node.get("cmd") or "").strip(),
                }
            )

        statement_template_path = ""
        problem_template_path = ""
        style_path = ""
        for node in root.findall("./files/resources/file"):
            path = str(node.get("path") or "").strip()
            normalized = _normalize_zip_path(path)
            source_name = PurePosixPath(normalized or path).name.lower()
            if source_name == "statements.ftl":
                statement_template_path = path
            elif source_name == "problem.tex":
                problem_template_path = path
            elif source_name == "olymp.sty":
                style_path = path
        statement_template_path = statement_template_path or "files/statements.ftl"
        problem_template_path = problem_template_path or "files/problem.tex"
        style_path = style_path or "files/olymp.sty"

        checker_node = root.find("./assets/checker")
        checker_name = str(checker_node.get("name") or "").strip() if checker_node is not None else ""
        checker_source = ""
        if checker_node is not None:
            source_node = checker_node.find("./source")
            if source_node is not None:
                checker_source = str(source_node.get("path") or "").strip()

        validator_sources: list[str] = []
        for node in root.findall("./assets/validators/validator/source"):
            path = str(node.get("path") or "").strip()
            if path:
                validator_sources.append(path)

        interactor_source = ""
        interactor_node = root.find("./assets/interactor/source")
        if interactor_node is not None:
            interactor_source = str(interactor_node.get("path") or "").strip()

        solutions: list[dict[str, str]] = []
        for node in root.findall("./assets/solutions/solution"):
            tag = str(node.get("tag") or "").strip()
            src = node.find("./source")
            source_path = str(src.get("path") or "").strip() if src is not None else ""
            source_type = str(src.get("type") or "").strip() if src is not None else ""
            if source_path:
                solutions.append({"path": source_path, "tag": tag, "source_type": source_type})

        executables: list[str] = []
        for node in root.findall("./files/executables/executable/source"):
            path = str(node.get("path") or "").strip()
            if path:
                executables.append(path)

        statement_languages: list[str] = []
        for node in root.findall("./statements/statement"):
            stype = str(node.get("type") or "").strip().lower()
            lang = str(node.get("language") or "").strip()
            if stype == "application/x-tex" and lang:
                statement_languages.append(lang)

        return {
            "title": problem_title,
            "time_limit_ms": max(1, time_limit_ms),
            "memory_limit_bytes": max(1, memory_limit_bytes),
            "run_count": max(1, run_count),
            "is_multipass": bool(multipass_property or (run_count > 1)),
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
        target.write_text(str(text or ""), encoding="utf-8")

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
        payload = _read_bytes_from_zip(zf, info)
        self._write_bytes(workspace, target_rel, payload)
        return True

    def _import_statement(self, zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], workspace: Path, meta: dict[str, object]) -> dict[str, object]:
        shutil.rmtree(workspace / STATEMENT_RENDERED_DIR_REL, ignore_errors=True)
        shutil.rmtree(workspace / STATEMENT_SECTIONS_DIR, ignore_errors=True)

        template_ok = self._copy_zip_entry(
            zf,
            entries,
            str(meta.get("statement_template_path") or ""),
            workspace,
            STATEMENT_TEMPLATE_REL,
        )
        if not template_ok:
            self._write_text(workspace, STATEMENT_TEMPLATE_REL, DEFAULT_STATEMENT_TEMPLATE)
        problem_ok = self._copy_zip_entry(
            zf,
            entries,
            str(meta.get("problem_template_path") or ""),
            workspace,
            STATEMENT_PROBLEM_REL,
        )
        if not problem_ok:
            self._write_text(workspace, STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
        style_ok = self._copy_zip_entry(
            zf,
            entries,
            str(meta.get("style_path") or ""),
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
            if len(rel.parts) >= 3 and STATEMENT_SECTION_SAMPLE_FILE_RE.fullmatch(str(rel.name or "")):
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
            language = str(parts[-2] or "").strip()
            if not language:
                continue
            target_rel = STATEMENT_SECTIONS_DIR / language / "problem.pdf"
            self._write_bytes(workspace, target_rel, _read_bytes_from_zip(zf, info))
            copied_prebuilt_pdf += 1
            if language not in prebuilt_pdf_languages:
                prebuilt_pdf_languages.append(language)

        languages = sorted(
            {
                str(p.relative_to(workspace / STATEMENT_SECTIONS_DIR).parts[0])
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

    def _supported_generator_tokens(self, meta: dict[str, object]) -> set[str]:
        tokens: set[str] = set()
        executable_paths = meta.get("executables")
        if not isinstance(executable_paths, list):
            return tokens
        for raw in executable_paths:
            source = _normalize_zip_path(str(raw or ""))
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

    def _generator_command_supported(self, command: str, meta: dict[str, object]) -> bool:
        cmd = normalize_gen_command(str(command or "").strip())
        if not cmd:
            return False
        try:
            tokens = parse_gen_command_tokens(cmd)
        except Exception:
            return False
        if not tokens:
            return False
        command_token = Path(str(tokens[0]).replace("\\", "/")).name
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
        meta: dict[str, object],
        *,
        normalize_test_data_newlines: bool = False,
    ) -> dict[str, object]:
        tests = meta.get("tests")
        if not isinstance(tests, list):
            tests = []

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
        input_pattern = str(meta.get("input_pattern") or "").strip()
        answer_pattern = str(meta.get("answer_pattern") or "").strip()
        manual_count = 0
        gen_count = 0
        generated_fallback_to_manual = 0
        answer_count = 0

        for idx, row in enumerate(tests, start=1):
            if not isinstance(row, dict):
                continue
            method = str(row.get("method") or "").strip().lower()
            is_generated = method == "generated"
            sample = bool(row.get("sample"))
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
                cmd = normalize_gen_command(str(row.get("cmd") or "").strip())
                if self._generator_command_supported(cmd, meta):
                    spec_entries.append({**spec_row, "kind": "gen"})
                    payload_rel = payload_rel_path_for_test(test_id, "gen")
                    self._write_text(workspace, Path(payload_rel), cmd)
                    gen_count += 1
                    continue

            input_rel = _normalize_zip_path(_expand_pattern(input_pattern, idx))
            if not input_rel:
                raise ValueError(f"cannot resolve test input path for test #{idx}")
            info = entries.get(input_rel)
            if info is None:
                if is_generated:
                    # Keep generated entry if package does not provide a concrete materialized input.
                    cmd = normalize_gen_command(str(row.get("cmd") or "").strip())
                    spec_entries.append({**spec_row, "kind": "gen"})
                    payload_rel = payload_rel_path_for_test(test_id, "gen")
                    self._write_text(workspace, Path(payload_rel), cmd)
                    gen_count += 1
                    continue
                raise ValueError(f"missing test input file in package: {input_rel}")
            payload_text = normalize_manual_input(_read_bytes_from_zip(zf, info).decode("utf-8", errors="replace"))
            spec_entries.append({**spec_row, "kind": "manual"})
            payload_rel = payload_rel_path_for_test(test_id, "manual")
            self._write_text(workspace, Path(payload_rel), payload_text)
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
        upstream = maintained_testlib_header(repo_root=Path(__file__).resolve().parents[3])
        self._write_bytes(workspace, target_rel, upstream.read_bytes())
        return "third_party/upstream/testlib/testlib.h"

    def _import_components(self, zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], workspace: Path, meta: dict[str, object]) -> dict[str, object]:
        build_cfg = _safe_read_json(workspace / "config" / "build.json")

        imported_testlib = self._write_maintained_testlib(workspace)

        checker_name = str(meta.get("checker_name") or "").strip()
        checker_source = str(meta.get("checker_source") or "").strip()
        if checker_name.startswith("std::"):
            build_cfg["checker_standard"] = checker_name
            build_cfg.pop("checker_source", None)
        else:
            imported_checker = self._copy_source_from_zip(zf, entries, checker_source, workspace, "checkers", "checker.cpp")
            if imported_checker:
                build_cfg["checker_source"] = imported_checker
                build_cfg.pop("checker_standard", None)
            else:
                build_cfg.pop("checker_source", None)

        validator_source = ""
        validator_sources = meta.get("validator_sources")
        if isinstance(validator_sources, list) and validator_sources:
            validator_source = self._copy_source_from_zip(
                zf, entries, str(validator_sources[0]), workspace, "validators", "validator.cpp"
            )
        if validator_source:
            build_cfg["validator_source"] = validator_source
        else:
            build_cfg.pop("validator_source", None)

        interactor_source = self._copy_source_from_zip(
            zf, entries, str(meta.get("interactor_source") or ""), workspace, "interactors", "interactor.cpp"
        )
        if interactor_source:
            build_cfg["interactor_source"] = interactor_source
        else:
            build_cfg.pop("interactor_source", None)

        executable_paths = [str(x) for x in (meta.get("executables") or []) if str(x).strip()] if isinstance(meta.get("executables"), list) else []
        used = {checker_source, str(meta.get("interactor_source") or "")}
        if isinstance(validator_sources, list):
            used.update([str(x) for x in validator_sources])
        generator_sources: list[str] = []
        test_rows = meta.get("tests") if isinstance(meta.get("tests"), list) else []
        generator_names = {
            str(str(row.get("cmd") or "").strip().split()[0]).strip()
            for row in test_rows
            if isinstance(row, dict) and str(row.get("method") or "").strip().lower() == "generated" and str(row.get("cmd") or "").strip()
        }
        for source in executable_paths:
            if source in used:
                continue
            stem = Path(source).stem
            if (stem not in generator_names) and (not stem.lower().startswith("gen")):
                continue
            suffix = Path(source).suffix.lower()
            imported = self._copy_source_from_zip(zf, entries, source, workspace, "generators", Path(source).name)
            if imported and suffix in GENERATOR_CPP_SUFFIX_ALLOW:
                generator_sources.append(imported)
        build_cfg["generator_sources"] = sorted(dict.fromkeys(generator_sources))

        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "build.json").write_text(json.dumps(build_cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        return {
            "testlib_source": imported_testlib,
            "checker_standard": str(build_cfg.get("checker_standard") or ""),
            "checker_source": str(build_cfg.get("checker_source") or ""),
            "validator_source": str(build_cfg.get("validator_source") or ""),
            "interactor_source": str(build_cfg.get("interactor_source") or ""),
            "generator_sources": [str(x) for x in build_cfg.get("generator_sources", []) if str(x).strip()],
        }

    def _solution_expected_from_tag(self, tag: str) -> str:
        raw_tag = str(tag or "").strip().lower()
        if not raw_tag:
            return "unknown"
        direct = POLYGON_SOLUTION_TAG_EXPECTED.get(raw_tag)
        if direct is not None:
            return normalize_expected_behavior(direct)
        expected = normalize_expected_behavior(raw_tag)
        if expected != "unknown":
            return expected
        normalized = raw_tag.replace("-", "_").replace(" ", "_")
        expected = normalize_expected_behavior(normalized)
        if expected != "unknown":
            return expected
        return "unknown"

    @staticmethod
    def _solution_suffix_from_source_type(source_type: str) -> str:
        token = str(source_type or "").strip().lower()
        if not token:
            return ""
        if ("python" in token) or ("pypy" in token):
            return ".py"
        if "java" in token:
            return ".java"
        if ("cpp" in token) or ("c++" in token) or ("g++" in token) or ("clang++" in token):
            return ".cpp"
        return ""

    def _solution_filename_for_import(self, source_path: str, source_type: str) -> str:
        safe_name = Path(str(source_path or "")).name
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

    def _import_solutions(self, zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], workspace: Path, meta: dict[str, object]) -> dict[str, object]:
        solution_rows = meta.get("solutions")
        if not isinstance(solution_rows, list):
            solution_rows = []

        solutions_dir_rel = Path("solutions")
        solutions_dir = workspace / solutions_dir_rel
        solutions_dir.mkdir(parents=True, exist_ok=True)
        accepted_source = ""
        imported_count = 0
        for row in solution_rows:
            if not isinstance(row, dict):
                continue
            source_path = _normalize_zip_path(str(row.get("path") or ""))
            if not source_path:
                continue
            info = entries.get(source_path)
            if info is None:
                continue
            source_type = str(row.get("source_type") or "").strip()
            filename = self._solution_filename_for_import(source_path, source_type)
            target_rel = _unique_rel_path(workspace, solutions_dir_rel, filename)
            payload = _read_bytes_from_zip(zf, info)
            self._write_bytes(workspace, Path(target_rel), payload)
            expected = self._solution_expected_from_tag(str(row.get("tag") or ""))
            self._write_text(workspace, Path(f"{target_rel}.desc"), render_solution_desc(expected, ""))
            if not accepted_source and (expected == "accepted"):
                accepted_source = target_rel
            if str(row.get("tag") or "").strip().lower() == "main":
                accepted_source = target_rel
            imported_count += 1
        return {"count": imported_count, "accepted_source": accepted_source}

    def _write_problem_config(self, workspace: Path, meta: dict[str, object], components: dict[str, object]) -> dict[str, object]:
        cfg = _safe_read_json(workspace / "config" / "problem.json")
        run_count = _coerce_int(str(meta.get("run_count") or "1"), 1)
        is_multipass = bool(meta.get("is_multipass")) or (run_count > 1)
        mode = "pass-fail"
        if is_multipass:
            mode = "multi-pass"
        elif str(components.get("interactor_source") or "").strip():
            mode = "interactive"
        cfg["input_file"] = "stdin"
        cfg["output_file"] = "stdout"
        cfg["time_limit_ms"] = max(1, int(meta.get("time_limit_ms") or 2000))
        memory_bytes = max(1, int(meta.get("memory_limit_bytes") or 1024 * 1024 * 1024))
        cfg["memory_limit_mb"] = max(1, memory_bytes // (1024 * 1024))
        cfg["mode"] = mode
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "problem.json").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return cfg

    def import_package(
        self,
        workspace: Path,
        package_name: str,
        package_bytes: bytes,
        *,
        normalize_test_data_newlines: bool = False,
    ) -> dict[str, object]:
        raw = bytes(package_bytes or b"")
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
                normalize_test_data_newlines=bool(normalize_test_data_newlines),
            )
            component_summary = self._import_components(zf, entry_map, workspace, meta)
            solutions_summary = self._import_solutions(zf, entry_map, workspace, meta)
            build_cfg = _safe_read_json(workspace / "config" / "build.json")
            build_cfg_changed = False
            if solutions_summary.get("accepted_source"):
                build_cfg["accepted_solution_source"] = str(solutions_summary.get("accepted_source"))
                build_cfg_changed = True
            run_count = _coerce_int(str(meta.get("run_count") or "1"), 1)
            if bool(meta.get("is_multipass")) or (run_count > 1):
                build_cfg["max_passes"] = max(1, run_count)
                build_cfg_changed = True
            if build_cfg_changed:
                (workspace / "config" / "build.json").write_text(
                    json.dumps(build_cfg, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            problem_cfg = self._write_problem_config(workspace, meta, component_summary)
            return {
                "package_name": str(package_name or "").strip(),
                "title": str(meta.get("title") or DEFAULT_PROBLEM_TITLE),
                "statement": statement_summary,
                "tests": tests_summary,
                "components": component_summary,
                "solutions": solutions_summary,
                "problem_cfg": problem_cfg,
            }
