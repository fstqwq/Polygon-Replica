from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from app.service.problem.solution_metadata import normalize_expected_behavior, render_solution_desc
from app.service.statement.constant import (
    DEFAULT_PROBLEM_TITLE,
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    DEFAULT_STATEMENT_TEMPLATE,
    STATEMENT_MAIN_REL,
    STATEMENT_PROBLEM_REL,
    STATEMENT_RENDERED_DIR_REL,
    STATEMENT_SECTIONS_DIR,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
)
from app.service.statement.render import default_olymp_sty_text
from app.service.problem.test_spec import dumps_tests_spec


ZIP_MAX_BYTES = 256 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 64 * 1024 * 1024
ZIP_TEXT_MAX_BYTES = 8 * 1024 * 1024
SOURCE_SUFFIX_ALLOW = {
    ".cpp",
    ".cc",
    ".cxx",
    ".c++",
    ".c",
    ".h",
    ".hpp",
    ".py",
    ".java",
    ".kt",
    ".rs",
    ".go",
    ".pas",
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


def _normalize_text_newlines_bytes(payload: bytes) -> bytes:
    return bytes(payload or b"").replace(b"\r\n", b"\n").replace(b"\r", b"\n")


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


def _safe_read_json(path: Path) -> dict:
    try:
        if path.exists() and path.is_file() and (not path.is_symlink()):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        return {}
    return {}


def _entry_map_from_zip(zf: zipfile.ZipFile, marker: str) -> dict[str, zipfile.ZipInfo]:
    raw: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        normalized = _normalize_zip_path(info.filename)
        if not normalized:
            continue
        raw[normalized] = info
    if marker in raw:
        return raw
    candidates = sorted([p for p in raw if p.endswith(f"/{marker}")], key=len)
    if not candidates:
        raise ValueError(f"{marker} not found in package")
    prefix = candidates[0][: -len(marker)]
    mapped: dict[str, zipfile.ZipInfo] = {}
    for path, info in raw.items():
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix) :]
        if rel:
            mapped[rel] = info
    if marker not in mapped:
        raise ValueError(f"{marker} not found in package")
    return mapped


def _yaml_strip_comment(raw: str) -> str:
    line = str(raw or "")
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(line) and line[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (not in_single) and (not in_double):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _yaml_unquote(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        inner = text[1:-1]
        inner = inner.replace(r"\"", '"').replace(r"\\", "\\")
        inner = inner.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\r", "\r")
        return inner
    return text


def _yaml_parse_inline_list(raw: str) -> list[str]:
    value = str(raw or "").strip()
    if not value:
        return []
    if not (value.startswith("[") and value.endswith("]")):
        token = _yaml_unquote(value).strip()
        return [token] if token else []
    body = value[1:-1].strip()
    if not body:
        return []
    items: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(body) and body[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if ch == "," and (not in_single) and (not in_double):
            token = _yaml_unquote("".join(buf)).strip()
            if token:
                items.append(token)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    token = _yaml_unquote("".join(buf)).strip()
    if token:
        items.append(token)
    return items


def _coerce_int(raw: object, default: int, *, min_value: int = 1) -> int:
    try:
        value = int(str(raw or "").strip())
    except Exception:
        value = int(default)
    return max(int(min_value), value)


def _coerce_float(raw: object, default: float) -> float:
    try:
        return float(str(raw or "").strip())
    except Exception:
        return float(default)


def _normalize_language_token(raw: str) -> str:
    token = str(raw or "").strip().lower().replace("_", "-")
    if token in {"en", "eng", "english"}:
        return "english"
    if token in {"ru", "rus", "russian"}:
        return "russian"
    if token in {"zh", "cn", "chinese", "chinese-simplified"}:
        return "chinese"
    token = re.sub(r"[^a-z0-9-]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token or "english"


def _time_limit_ms_from_text(raw: str) -> int | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if m is None:
        return None
    value = _coerce_float(m.group(1), 2.0)
    if "ms" in text:
        return max(1, int(round(value)))
    if "sec" in text or "second" in text or text.endswith("s"):
        return max(1, int(round(value * 1000.0)))
    if value <= 30.0:
        return max(1, int(round(value * 1000.0)))
    return max(1, int(round(value)))


def _memory_limit_mb_from_text(raw: str) -> int | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if m is None:
        return None
    value = _coerce_float(m.group(1), 1024.0)
    if "gb" in text or "gib" in text or text.endswith("g"):
        return max(1, int(round(value * 1024.0)))
    if "kb" in text or "kib" in text or text.endswith("k"):
        return max(1, int(round(value / 1024.0)))
    return max(1, int(round(value)))


def _submission_expected_from_group(raw_group: str) -> str:
    token = str(raw_group or "").strip().lower().replace("-", "_")
    direct = normalize_expected_behavior(token)
    if direct != "unknown":
        return direct
    if token == "wrong_answer":
        return "wrong_answer"
    if token in {"time_limit_exceeded", "tle"}:
        return "time_limit_exceeded"
    if token in {"run_time_error", "runtime_error", "rte"}:
        return "run_time_error"
    if token in {"accepted", "ac"}:
        return "accepted"
    if token in {"rejected", "reject"}:
        return "rejected"
    return "unknown"


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


class ICPCPackageImportService:
    def _parse_problem_yaml(self, text: str) -> dict[str, object]:
        top: dict[str, str] = {}
        limits: dict[str, str] = {}
        in_limits = False
        for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = _yaml_strip_comment(raw_line)
            if not line.strip():
                continue
            if line[:1].isspace():
                if not in_limits:
                    continue
                nested = line.strip()
                if ":" not in nested:
                    continue
                key, value = nested.split(":", 1)
                key_norm = str(key or "").strip().lower().replace("-", "_")
                if key_norm in {"time_limit", "memory_limit"}:
                    limits[key_norm] = str(value or "").strip()
                continue
            in_limits = False
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key_norm = str(key or "").strip().lower().replace("-", "_")
            value_text = str(value or "").strip()
            top[key_norm] = value_text
            if key_norm == "limits" and not value_text:
                in_limits = True

        if "problem_format_version" not in top:
            raise ValueError("problem.yaml is missing problem_format_version")

        title = _yaml_unquote(top.get("name", "")).strip() or DEFAULT_PROBLEM_TITLE
        type_tokens = [item.lower().replace("_", "-") for item in _yaml_parse_inline_list(top.get("type", ""))]
        mode = "pass-fail"
        if any(token in {"multi-pass", "multipass"} for token in type_tokens):
            mode = "multi-pass"
        elif any(token == "interactive" for token in type_tokens):
            mode = "interactive"

        time_raw = limits.get("time_limit") or top.get("time_limit") or ""
        memory_raw = limits.get("memory_limit") or top.get("memory_limit") or ""
        time_limit_ms = _time_limit_ms_from_text(_yaml_unquote(time_raw)) if time_raw else None
        memory_limit_mb = _memory_limit_mb_from_text(_yaml_unquote(memory_raw)) if memory_raw else None

        return {
            "title": title,
            "mode": mode,
            "time_limit_ms": int(time_limit_ms) if time_limit_ms is not None else None,
            "memory_limit_mb": int(memory_limit_mb) if memory_limit_mb is not None else None,
        }

    def _write_text(self, workspace: Path, rel: Path, text: str) -> None:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(text or ""), encoding="utf-8")

    def _write_bytes(self, workspace: Path, rel: Path, payload: bytes) -> None:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(payload or b""))

    def _import_statement(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        meta: dict[str, object],
    ) -> dict[str, object]:
        shutil.rmtree(workspace / STATEMENT_RENDERED_DIR_REL, ignore_errors=True)
        shutil.rmtree(workspace / STATEMENT_SECTIONS_DIR, ignore_errors=True)

        copied_statement = 0
        for path, info in entries.items():
            rel = path.replace("\\", "/")
            if not rel.startswith("statement/"):
                continue
            if rel.startswith("statement/rendered/"):
                continue
            self._write_bytes(workspace, Path(rel), _read_bytes_from_zip(zf, info))
            copied_statement += 1

        copied_sections = 0
        section_languages: set[str] = set()
        for path, info in entries.items():
            rel = path.replace("\\", "/")
            if not rel.startswith("statement-sections/"):
                continue
            rel_path = Path(rel)
            self._write_bytes(workspace, rel_path, _read_bytes_from_zip(zf, info))
            copied_sections += 1
            parts = rel_path.parts
            if len(parts) >= 2:
                section_languages.add(_normalize_language_token(parts[1]))

        if not (workspace / STATEMENT_TEMPLATE_REL).exists():
            self._write_text(workspace, STATEMENT_TEMPLATE_REL, DEFAULT_STATEMENT_TEMPLATE)
        if not (workspace / STATEMENT_PROBLEM_REL).exists():
            self._write_text(workspace, STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
        if not (workspace / STATEMENT_STYLE_REL).exists():
            self._write_text(workspace, STATEMENT_STYLE_REL, default_olymp_sty_text())

        fallback_header: dict[str, str] = {}

        selected_language = "english" if "english" in section_languages else (sorted(section_languages)[0] if section_languages else "english")
        language_warning = ""
        if section_languages and selected_language != "english":
            language_warning = f"statement language english not found; defaulting to {selected_language}"
        (workspace / STATEMENT_MAIN_REL).unlink(missing_ok=True)

        return {
            "copied_statement_files": copied_statement,
            "copied_section_files": copied_sections,
            "language": selected_language,
            "language_warning": language_warning,
            "header": fallback_header,
        }

    def _secret_input_sort_key(self, rel_path: str) -> tuple[int, int, str]:
        stem = Path(rel_path).stem
        if stem.isdigit():
            return (0, int(stem), rel_path)
        return (1, 0, rel_path)

    def _import_tests(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        *,
        normalize_test_data_newlines: bool = False,
    ) -> dict[str, object]:
        manual_dir = workspace / "tests" / "manual"
        gen_dir = workspace / "tests" / "generator"
        answers_dir = workspace / "tests" / "answers"
        shutil.rmtree(manual_dir, ignore_errors=True)
        shutil.rmtree(gen_dir, ignore_errors=True)
        shutil.rmtree(answers_dir, ignore_errors=True)
        manual_dir.mkdir(parents=True, exist_ok=True)
        gen_dir.mkdir(parents=True, exist_ok=True)
        answers_dir.mkdir(parents=True, exist_ok=True)

        secret_inputs = sorted(
            [
                rel
                for rel in entries
                if rel.startswith("data/secret/")
                and rel.endswith(".in")
                and Path(rel).name.lower() != ".ds_store"
            ],
            key=self._secret_input_sort_key,
        )
        if not secret_inputs:
            raise ValueError("data/secret/*.in not found in ICPC package")

        sample_payload = b""
        sample_info = entries.get("data/sample/1.in")
        if sample_info is not None:
            sample_payload = _read_bytes_from_zip(zf, sample_info)
            if normalize_test_data_newlines:
                sample_payload = _normalize_text_newlines_bytes(sample_payload)

        spec_entries: list[dict[str, object]] = []
        sample_index = 0
        answer_count = 0
        answer_text_by_index: dict[int, str] = {}
        for idx, rel in enumerate(secret_inputs, start=1):
            info = entries[rel]
            payload = _read_bytes_from_zip(zf, info)
            if normalize_test_data_newlines:
                payload = _normalize_text_newlines_bytes(payload)
            if sample_payload and sample_index <= 0 and payload == sample_payload:
                sample_index = idx
            test_id = f"{idx:03d}"
            text = payload.decode("utf-8", errors="replace")
            self._write_text(workspace, Path("tests") / "manual" / f"{test_id}.in", text)

            ans_rel = rel[:-len(".in")] + ".ans"
            ans_info = entries.get(ans_rel)
            if ans_info is not None:
                ans_payload = _read_bytes_from_zip(zf, ans_info)
                if normalize_test_data_newlines:
                    ans_payload = _normalize_text_newlines_bytes(ans_payload)
                self._write_bytes(workspace, Path("tests") / "answers" / f"{test_id}.ans", ans_payload)
                answer_text_by_index[idx] = ans_payload.decode("utf-8", errors="replace")
                answer_count += 1

            spec_entries.append({"id": test_id, "kind": "manual", "sample": False})

        if sample_index > 0 and sample_index <= len(spec_entries):
            spec_entries[sample_index - 1]["sample"] = True
            sample_answer_text = str(answer_text_by_index.get(sample_index) or "")
            if sample_answer_text:
                spec_entries[sample_index - 1]["sample_output"] = sample_answer_text

        self._write_text(workspace, Path("tests/spec.json"), dumps_tests_spec(spec_entries))
        return {
            "total": len(spec_entries),
            "manual": len(spec_entries),
            "gen": 0,
            "answers": answer_count,
            "sample": 1 if sample_index > 0 else 0,
        }

    def _import_solutions(self, zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], workspace: Path) -> dict[str, object]:
        solutions_dir_rel = Path("solutions")
        solutions_dir = workspace / solutions_dir_rel
        shutil.rmtree(solutions_dir, ignore_errors=True)
        solutions_dir.mkdir(parents=True, exist_ok=True)

        imported_count = 0
        accepted_source = ""
        for rel in sorted(entries):
            if not rel.startswith("submissions/"):
                continue
            rel_path = Path(rel)
            if len(rel_path.parts) < 3:
                continue
            group = str(rel_path.parts[1] or "")
            filename = rel_path.name
            suffix = rel_path.suffix.lower()
            if suffix and suffix not in SOURCE_SUFFIX_ALLOW:
                continue
            info = entries.get(rel)
            if info is None:
                continue
            target_rel = _unique_rel_path(workspace, solutions_dir_rel, filename)
            self._write_bytes(workspace, Path(target_rel), _read_bytes_from_zip(zf, info))
            expected = _submission_expected_from_group(group)
            self._write_text(workspace, Path(f"{target_rel}.desc"), render_solution_desc(expected, ""))
            if not accepted_source and expected == "accepted":
                accepted_source = target_rel
            imported_count += 1
        return {"count": imported_count, "accepted_source": accepted_source}

    def _copy_component_tree(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        source_prefix: str,
        workspace: Path,
        target_prefix: str,
    ) -> list[str]:
        copied: list[str] = []
        safe_source = str(source_prefix or "").replace("\\", "/").strip("/")
        safe_target = str(target_prefix or "").replace("\\", "/").strip("/")
        if not safe_source or not safe_target:
            return copied
        src_prefix = safe_source + "/"
        for rel in sorted(entries):
            if not rel.startswith(src_prefix):
                continue
            suffix_rel = rel[len(src_prefix) :].strip("/")
            if not suffix_rel:
                continue
            target_rel = Path(safe_target) / suffix_rel
            info = entries[rel]
            self._write_bytes(workspace, target_rel, _read_bytes_from_zip(zf, info))
            copied.append(target_rel.as_posix())
        return copied

    def _select_source_file(self, rel_paths: list[str], preferred_stems: list[str]) -> str:
        source_candidates: list[str] = []
        for rel in rel_paths:
            suffix = Path(rel).suffix.lower()
            if suffix and suffix in SOURCE_SUFFIX_ALLOW:
                source_candidates.append(rel)
        if not source_candidates:
            return ""
        lower_map = {rel.lower(): rel for rel in source_candidates}
        for stem in preferred_stems:
            token = str(stem or "").strip().lower()
            if not token:
                continue
            for rel_lower, rel in lower_map.items():
                if Path(rel_lower).stem == token:
                    return rel
        source_candidates.sort()
        return source_candidates[0]

    def _import_components(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        meta: dict[str, object],
    ) -> dict[str, object]:
        for folder in ("validators", "checkers", "interactors"):
            shutil.rmtree(workspace / folder, ignore_errors=True)
            (workspace / folder).mkdir(parents=True, exist_ok=True)

        validators = self._copy_component_tree(zf, entries, "input_validators", workspace, "validators")
        validator_source = self._select_source_file(validators, ["validator", "validate"])

        mode = str(meta.get("mode") or "pass-fail")
        checker_source = ""
        interactor_source = ""
        output_files = self._copy_component_tree(
            zf,
            entries,
            "output_validator",
            workspace,
            "interactors" if mode == "interactive" else "checkers",
        )
        if mode == "interactive":
            interactor_source = self._select_source_file(output_files, ["interactor", "checker"])
        else:
            checker_source = self._select_source_file(output_files, ["checker", "interactor"])

        if mode != "interactive":
            extra_interactors = self._copy_component_tree(zf, entries, "interactors", workspace, "interactors")
            if extra_interactors:
                interactor_source = self._select_source_file(extra_interactors, ["interactor"])

        return {
            "validator_files": len(validators),
            "checker_files": len(output_files) if mode != "interactive" else 0,
            "interactor_files": len(output_files) if mode == "interactive" else 0,
            "validator_source": validator_source,
            "checker_source": checker_source,
            "interactor_source": interactor_source,
        }

    def _write_problem_config(
        self,
        workspace: Path,
        meta: dict[str, object],
        statement_summary: dict[str, object],
    ) -> dict[str, object]:
        cfg = _safe_read_json(workspace / "config" / "problem.json")
        header = statement_summary.get("header")
        header_obj = header if isinstance(header, dict) else {}

        time_limit_ms = meta.get("time_limit_ms")
        if not isinstance(time_limit_ms, int) or time_limit_ms <= 0:
            time_limit_ms = _time_limit_ms_from_text(str(header_obj.get("time_text") or "")) or 2000

        memory_limit_mb = meta.get("memory_limit_mb")
        if not isinstance(memory_limit_mb, int) or memory_limit_mb <= 0:
            memory_limit_mb = _memory_limit_mb_from_text(str(header_obj.get("memory_text") or "")) or 1024

        input_file = str(header_obj.get("input_file") or "").strip() or "stdin"
        output_file = str(header_obj.get("output_file") or "").strip() or "stdout"
        mode = str(meta.get("mode") or "pass-fail")
        if mode not in {"pass-fail", "interactive", "multi-pass"}:
            mode = "pass-fail"

        cfg["input_file"] = input_file
        cfg["output_file"] = output_file
        cfg["time_limit_ms"] = _coerce_int(time_limit_ms, 2000, min_value=1)
        cfg["memory_limit_mb"] = _coerce_int(memory_limit_mb, 1024, min_value=1)
        cfg["mode"] = mode
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "problem.json").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return cfg

    def _write_build_config(
        self,
        workspace: Path,
        meta: dict[str, object],
        components: dict[str, object],
        solutions: dict[str, object],
    ) -> dict[str, object]:
        build_cfg = _safe_read_json(workspace / "config" / "build.json")
        accepted_source = str(solutions.get("accepted_source") or "").strip()
        if accepted_source:
            build_cfg["accepted_solution_source"] = accepted_source

        validator_source = str(components.get("validator_source") or "").strip()
        if validator_source:
            build_cfg["validator_source"] = validator_source
            build_cfg["require_validator"] = True
        else:
            build_cfg.pop("validator_source", None)
            build_cfg["require_validator"] = False

        checker_source = str(components.get("checker_source") or "").strip()
        if checker_source:
            build_cfg["checker_source"] = checker_source
            build_cfg.pop("checker_standard", None)
            build_cfg["require_checker"] = True
        else:
            build_cfg.pop("checker_source", None)
            build_cfg.pop("checker_standard", None)
            build_cfg["require_checker"] = False

        interactor_source = str(components.get("interactor_source") or "").strip()
        if interactor_source:
            build_cfg["interactor_source"] = interactor_source
        else:
            build_cfg.pop("interactor_source", None)

        mode = str(meta.get("mode") or "pass-fail")
        if mode == "interactive" and not interactor_source:
            raise ValueError("interactive ICPC package is missing output_validator/interactor source")

        build_cfg["generator_sources"] = []
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "build.json").write_text(
            json.dumps(build_cfg, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return build_cfg

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
            entry_map = _entry_map_from_zip(zf, "problem.yaml")
            yaml_info = entry_map.get("problem.yaml")
            if yaml_info is None:
                raise ValueError("problem.yaml not found in package")
            meta = self._parse_problem_yaml(_read_text_from_zip(zf, yaml_info))
            statement_summary = self._import_statement(zf, entry_map, workspace, meta)
            tests_summary = self._import_tests(
                zf,
                entry_map,
                workspace,
                normalize_test_data_newlines=bool(normalize_test_data_newlines),
            )
            solutions_summary = self._import_solutions(zf, entry_map, workspace)
            components_summary = self._import_components(zf, entry_map, workspace, meta)
            problem_cfg = self._write_problem_config(workspace, meta, statement_summary)
            build_cfg = self._write_build_config(workspace, meta, components_summary, solutions_summary)
            return {
                "package_name": str(package_name or "").strip(),
                "title": str(meta.get("title") or DEFAULT_PROBLEM_TITLE),
                "statement": statement_summary,
                "tests": tests_summary,
                "solutions": solutions_summary,
                "components": components_summary,
                "problem_cfg": problem_cfg,
                "build_cfg": build_cfg,
            }
