from __future__ import annotations

import json
import re
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import TypedDict

import yaml

from app.service.importing.archive import (
    ArchiveView,
    BudgetedZipFile,
    PACKAGE_METADATA_MAX_BYTES,
)
from app.service.importing.statement_assets import ImportedLegacyStatementAsset, merge_imported_statement_assets
from app.service.problem.build_config import dumps_build_config
from app.service.importing.icpc_submissions import (
    consume_generated_expected_results,
    parse_submissions_yaml,
    submission_expected_from_group,
)
from app.service.problem.solution_metadata import render_solution_desc
from app.service.statement.constant import (
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    DEFAULT_STATEMENT_TEMPLATE,
    STATEMENT_MAIN_REL,
    STATEMENT_ASSETS_DIR,
    STATEMENT_PROBLEM_REL,
    STATEMENT_RENDERED_DIR_REL,
    STATEMENT_SECTIONS_DIR,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    is_canonical_statement_section_entry,
    is_ignored_statement_section_entry,
)
from app.service.statement.render import default_olymp_sty_text
from app.service.statement.title import normalize_problem_title
from app.service.problem.test_spec import dumps_tests_spec


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


ProblemMeta = TypedDict(
    "ProblemMeta",
    {
        "title": str,
        "format_version": str,
        "mode": str,
        "pass_limit": int,
        "time_limit_ms": int | None,
        "memory_limit_mb": int | None,
    },
)


DomjudgeMeta = TypedDict(
    "DomjudgeMeta",
    {
        "time_limit_ms": int | None,
        "name": str,
        "external_id": str,
        "short_name": str,
    },
)

StatementSummary = TypedDict(
    "StatementSummary",
    {
        "copied_statement_files": int,
        "copied_section_files": int,
        "language": str,
        "language_warning": str,
        "header": dict[str, str],
    },
)

TestsSummary = TypedDict(
    "TestsSummary",
    {
        "total": int,
        "manual": int,
        "gen": int,
        "answers": int,
        "sample": int,
    },
)

SolutionsSummary = TypedDict(
    "SolutionsSummary",
    {
        "count": int,
        "accepted_source": str,
        "warnings": list[str],
    },
)

ComponentsSummary = TypedDict(
    "ComponentsSummary",
    {
        "validator_files": int,
        "checker_files": int,
        "interactor_files": int,
        "validator_source": str,
        "checker_source": str,
        "interactor_source": str,
    },
)


def _normalize_zip_path(raw: str) -> str:
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


def _normalize_text_newlines_bytes(payload: bytes) -> bytes:
    return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _read_small_bytes(zf: BudgetedZipFile, info: zipfile.ZipInfo) -> bytes:
    return zf.read_metadata(
        info,
        limit=PACKAGE_METADATA_MAX_BYTES,
        label=info.filename,
    )


def _read_json_or_empty(path: Path) -> dict[str, object]:
    try:
        if path.exists() and path.is_file() and (not path.is_symlink()):
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _ini_unquote(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace(r'\"', '"').replace(r"\\", "\\")
    return text


def _parse_domjudge_ini(text: str) -> DomjudgeMeta:
    time_limit_ms: int | None = None
    name = ""
    external_id = ""
    short_name = ""
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key_norm = key.strip().lower().replace("-", "_")
        token = _ini_unquote(value)
        if key_norm == "timelimit":
            time_limit_ms = _time_limit_ms_from_text(token)
            continue
        if key_norm == "name":
            name = token
            continue
        if key_norm == "externalid":
            external_id = token
            continue
        if key_norm == "short_name":
            short_name = token
    return {
        "time_limit_ms": time_limit_ms,
        "name": name,
        "external_id": external_id,
        "short_name": short_name,
    }


def _coerce_int(raw: object, default: int, *, min_value: int = 1) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        value = int(default)
    return max(int(min_value), value)


def _coerce_float(raw: object, default: float) -> float:
    try:
        return float(str(raw).strip())
    except Exception:
        return float(default)


def _normalize_language_token(raw: str) -> str:
    token = raw.strip().lower().replace("_", "-")
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
    text = raw.strip().lower()
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
    text = raw.strip().lower()
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


def _unique_rel_path(workspace: Path, parent_rel: Path, filename: str) -> str:
    filename_leaf = Path(filename).name
    if not filename_leaf:
        filename_leaf = "source.cpp"
    candidate = parent_rel / filename_leaf
    if not (workspace / candidate).exists():
        return candidate.as_posix()
    stem = Path(filename_leaf).stem
    suffix = Path(filename_leaf).suffix
    idx = 2
    while True:
        item = parent_rel / f"{stem}-{idx}{suffix}"
        if not (workspace / item).exists():
            return item.as_posix()
        idx += 1


class ICPCPackageImportService:
    def _parse_problem_yaml(self, text: str) -> ProblemMeta:
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid problem.yaml: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("problem.yaml must contain a mapping")

        format_version = str(loaded.get("problem_format_version") or "").strip()
        if format_version not in {"", "legacy", "legacy-icpc", "2025-09"}:
            raise ValueError(f"unsupported problem_format_version: {format_version}")

        raw_name = loaded.get("name")
        if isinstance(raw_name, dict):
            language_names = {
                str(key): value.strip()
                for key, value in raw_name.items()
                if str(key) and isinstance(value, str) and value.strip()
            }
            title = language_names.get("en") or next(iter(language_names.values()), "")
        else:
            title = str(raw_name or "")

        raw_type = loaded.get("type")
        if isinstance(raw_type, list):
            type_tokens = [str(item).strip().lower() for item in raw_type if str(item).strip()]
        elif isinstance(raw_type, str):
            if format_version == "2025-09":
                type_tokens = [raw_type.strip().lower()] if raw_type.strip() else []
            else:
                type_tokens = [item for item in raw_type.strip().lower().split() if item]
        elif raw_type is None:
            type_tokens = []
        else:
            raise ValueError("problem.yaml type must be a string or sequence")
        unsupported_types = set(type_tokens) - {"pass-fail", "interactive", "multi-pass"}
        if unsupported_types:
            raise ValueError(f"unsupported ICPC problem type: {', '.join(sorted(unsupported_types))}")

        validation_tokens = [
            item
            for item in str(loaded.get("validation") or "").strip().lower().split()
            if item
        ]
        effective_tokens = set(type_tokens) | set(validation_tokens)
        mode = "interactive" if "interactive" in effective_tokens else "pass-fail"
        limits_raw = loaded.get("limits")
        limits = limits_raw if isinstance(limits_raw, dict) else {}
        is_multi_pass = bool({"multi-pass", "multipass"} & effective_tokens)
        if is_multi_pass:
            validation_passes_raw = limits.get("validation_passes", loaded.get("validation_passes"))
            pass_limit = _coerce_int(validation_passes_raw, 0, min_value=1)
            if pass_limit < 2:
                raise ValueError("multi-pass ICPC package requires limits.validation_passes >= 2")
        else:
            pass_limit = 1

        time_raw = limits.get("time_limit", loaded.get("time_limit"))
        memory_raw = limits.get("memory", limits.get("memory_limit", loaded.get("memory_limit")))
        time_limit_ms = _time_limit_ms_from_text(str(time_raw)) if time_raw is not None else None
        memory_limit_mb = _memory_limit_mb_from_text(str(memory_raw)) if memory_raw is not None else None

        return {
            "title": title,
            "format_version": format_version,
            "mode": mode,
            "pass_limit": pass_limit,
            "time_limit_ms": int(time_limit_ms) if time_limit_ms is not None else None,
            "memory_limit_mb": int(memory_limit_mb) if memory_limit_mb is not None else None,
        }

    def _write_text(self, workspace: Path, rel: Path, text: str) -> None:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _import_statement(
        self,
        zf: BudgetedZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        _meta: ProblemMeta,
    ) -> tuple[StatementSummary, list[str]]:
        shutil.rmtree(workspace / STATEMENT_RENDERED_DIR_REL, ignore_errors=True)
        shutil.rmtree(workspace / STATEMENT_SECTIONS_DIR, ignore_errors=True)
        shutil.rmtree(workspace / STATEMENT_ASSETS_DIR, ignore_errors=True)

        copied_statement = 0
        for path, info in entries.items():
            rel = path.replace("\\", "/")
            if not rel.startswith("statement/"):
                continue
            if rel.startswith("statement/rendered/"):
                continue
            zf.copy_to(info, workspace / Path(rel))
            copied_statement += 1

        copied_sections = 0
        shared_assets: dict[str, Path] = {}
        legacy_assets: list[ImportedLegacyStatementAsset] = []
        section_languages: set[str] = set()
        asset_staging = workspace.parent / f".icpc-statement-assets-{uuid.uuid4().hex}"
        try:
            asset_staging.mkdir(parents=True, exist_ok=False)
            for path, info in entries.items():
                rel = path.replace("\\", "/")
                if rel.startswith(f"{STATEMENT_ASSETS_DIR.as_posix()}/"):
                    asset_rel = Path(rel).relative_to(STATEMENT_ASSETS_DIR).as_posix()
                    if asset_rel:
                        source_path = asset_staging / "shared" / asset_rel
                        zf.copy_to(info, source_path)
                        shared_assets[asset_rel] = source_path
                    continue
                if not rel.startswith("statement-sections/"):
                    continue
                rel_path = Path(rel)
                if len(rel_path.parts) < 3:
                    continue
                rel_in_section = Path(*rel_path.parts[2:])
                if is_ignored_statement_section_entry(rel_in_section):
                    continue
                if is_canonical_statement_section_entry(rel_in_section):
                    zf.copy_to(info, workspace / rel_path)
                    copied_sections += 1
                else:
                    source_path = asset_staging / "legacy" / rel_path
                    zf.copy_to(info, source_path)
                    legacy_assets.append(
                        {
                            "language": rel_path.parts[1],
                            "package_path": rel_path.as_posix(),
                            "asset_rel": rel_in_section.as_posix(),
                            "source_path": source_path,
                        }
                    )
                parts = rel_path.parts
                if len(parts) >= 2:
                    section_languages.add(_normalize_language_token(parts[1]))

            asset_merge = merge_imported_statement_assets(
                workspace,
                shared_assets=shared_assets,
                legacy_assets=legacy_assets,
            )
        finally:
            shutil.rmtree(asset_staging, ignore_errors=True)

        if not (workspace / STATEMENT_TEMPLATE_REL).exists():
            self._write_text(workspace, STATEMENT_TEMPLATE_REL, DEFAULT_STATEMENT_TEMPLATE)
        if not (workspace / STATEMENT_PROBLEM_REL).exists():
            self._write_text(workspace, STATEMENT_PROBLEM_REL, DEFAULT_STATEMENT_PROBLEM_TEMPLATE)
        if not (workspace / STATEMENT_STYLE_REL).exists():
            self._write_text(workspace, STATEMENT_STYLE_REL, default_olymp_sty_text())

        fallback_header: dict[str, str] = {}

        selected_language = "english" if "english" in section_languages else (sorted(section_languages)[0] if section_languages else "english")
        title_path = workspace / STATEMENT_SECTIONS_DIR / selected_language / "name.tex"
        title_text = str(_meta["title"] or "").strip()
        if title_text:
            title_path.parent.mkdir(parents=True, exist_ok=True)
            title_path.write_text(title_text + "\n", encoding="utf-8")
        language_warning = ""
        if section_languages and selected_language != "english":
            language_warning = f"statement language english not found; defaulting to {selected_language}"
        (workspace / STATEMENT_MAIN_REL).unlink(missing_ok=True)

        return (
            {
                "copied_statement_files": copied_statement,
                "copied_section_files": copied_sections + asset_merge["copied_files"],
                "language": selected_language,
                "language_warning": language_warning,
                "header": fallback_header,
            },
            asset_merge["warnings"],
        )

    def _secret_input_sort_key(self, rel_path: str) -> tuple[int, int, str]:
        stem = Path(rel_path).stem
        if stem.isdigit():
            return (0, int(stem), rel_path)
        return (1, 0, rel_path)

    def _test_id_from_data_path(self, rel_path: str) -> str:
        stem = Path(rel_path).stem
        if stem.isdigit():
            return stem.zfill(max(3, len(stem)))
        return ""

    def _unique_imported_test_id(self, preferred: str, used_ids: set[str], fallback_index: int) -> str:
        candidate = preferred or f"{fallback_index:03d}"
        while candidate in used_ids:
            fallback_index += 1
            candidate = f"{fallback_index:03d}"
        return candidate

    def _import_tests(
        self,
        zf: BudgetedZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        *,
        normalize_test_data_newlines: bool = False,
        text_limit_bytes: int,
    ) -> TestsSummary:
        manual_dir = workspace / "tests" / "manual"
        gen_dir = workspace / "tests" / "generator"
        shutil.rmtree(manual_dir, ignore_errors=True)
        shutil.rmtree(gen_dir, ignore_errors=True)
        manual_dir.mkdir(parents=True, exist_ok=True)
        gen_dir.mkdir(parents=True, exist_ok=True)

        sample_inputs = sorted(
            [
                rel
                for rel in entries
                if rel.startswith("data/sample/")
                and rel.endswith(".in")
                and Path(rel).name.lower() != ".ds_store"
            ],
            key=self._secret_input_sort_key,
        )
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
        if not sample_inputs and not secret_inputs:
            raise ValueError("data/sample/*.in or data/secret/*.in not found in ICPC package")

        used_ids: set[str] = set()
        entry_by_id: dict[str, dict[str, object]] = {}
        spec_entries: list[dict[str, object]] = []
        answer_count = 0

        def _import_answer(rel: str, test_id: str, spec_row: dict[str, object]) -> None:
            nonlocal answer_count
            ans_rel = rel[:-len(".in")] + ".ans"
            ans_info = entries.get(ans_rel)
            if (
                ans_info is None
                or not rel.startswith("data/sample/")
                or (not bool(spec_row.get("sample")))
                or str(spec_row.get("sample_output") or "")
            ):
                return
            ans_payload = _read_small_bytes(zf, ans_info)
            if normalize_test_data_newlines:
                ans_payload = _normalize_text_newlines_bytes(ans_payload)
            answer_count += 1
            spec_row["sample_output"] = ans_payload.decode("utf-8", errors="replace")

        def _import_input(rel: str, *, sample: bool, fallback_index: int) -> dict[str, object]:
            preferred_id = self._test_id_from_data_path(rel)
            test_id = self._unique_imported_test_id(preferred_id, used_ids, fallback_index)
            target = workspace / "tests" / "manual" / f"{test_id}.in"
            zf.copy_to(
                entries[rel],
                target,
                normalize_newlines=normalize_test_data_newlines,
            )
            spec_row: dict[str, object] = {"id": test_id, "kind": "manual", "sample": sample}
            used_ids.add(test_id)
            entry_by_id[test_id] = spec_row
            spec_entries.append(spec_row)
            _import_answer(rel, test_id, spec_row)
            return spec_row

        for idx, rel in enumerate(sample_inputs, start=1):
            _import_input(rel, sample=True, fallback_index=idx)

        for idx, rel in enumerate(secret_inputs, start=len(spec_entries) + 1):
            preferred_id = self._test_id_from_data_path(rel)
            if preferred_id and preferred_id in entry_by_id:
                _import_answer(rel, preferred_id, entry_by_id[preferred_id])
                continue
            _import_input(rel, sample=False, fallback_index=idx)

        self._write_text(
            workspace,
            Path("tests/spec.json"),
            dumps_tests_spec(spec_entries, max_bytes=text_limit_bytes),
        )
        return {
            "total": len(spec_entries),
            "manual": len(spec_entries),
            "gen": 0,
            "answers": answer_count,
            "sample": sum(1 for row in spec_entries if bool(row.get("sample"))),
        }

    def _submission_yaml_behaviors(
        self,
        zf: BudgetedZipFile,
        entries: dict[str, zipfile.ZipInfo],
    ) -> tuple[dict[str, str], list[str]]:
        info = entries.get("submissions/submissions.yaml")
        if info is None:
            return ({}, [])
        payload = zf.read_metadata(
            info,
            limit=PACKAGE_METADATA_MAX_BYTES,
            label="submissions/submissions.yaml",
        )
        return parse_submissions_yaml(payload.decode("utf-8", errors="replace"))

    def _import_solutions(
        self,
        zf: BudgetedZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
    ) -> SolutionsSummary:
        solutions_dir_rel = Path("solutions")
        solutions_dir = workspace / solutions_dir_rel
        shutil.rmtree(solutions_dir, ignore_errors=True)
        solutions_dir.mkdir(parents=True, exist_ok=True)

        yaml_behaviors, warnings = self._submission_yaml_behaviors(zf, entries)
        consumed_yaml_paths: set[str] = set()
        imported_count = 0
        accepted_source = ""
        for rel in sorted(entries):
            if not rel.startswith("submissions/"):
                continue
            rel_path = Path(rel)
            if len(rel_path.parts) < 3:
                continue
            group = rel_path.parts[1]
            filename = rel_path.name
            suffix = rel_path.suffix.lower()
            if suffix and suffix not in SOURCE_SUFFIX_ALLOW:
                continue
            info = entries.get(rel)
            if info is None:
                continue
            target_rel = _unique_rel_path(workspace, solutions_dir_rel, filename)
            zf.copy_to(info, workspace / Path(target_rel))
            annotation_expected, annotation_warning = consume_generated_expected_results(
                workspace / Path(target_rel)
            )
            if annotation_warning:
                warnings.append(f"{rel}: {annotation_warning}")
            directory_expected = submission_expected_from_group(group)
            yaml_expected = yaml_behaviors.get(rel)
            if yaml_expected is not None:
                consumed_yaml_paths.add(rel)
                expected = yaml_expected
                chosen_source = "submissions.yaml"
                lower_candidates = [
                    ("annotation", annotation_expected),
                    ("directory", directory_expected),
                ]
            elif annotation_expected is not None:
                expected = annotation_expected
                chosen_source = "annotation"
                lower_candidates = [("directory", directory_expected)]
            else:
                expected = directory_expected
                chosen_source = "directory"
                lower_candidates = []
            for source_name, candidate in lower_candidates:
                if candidate not in {None, "unknown"} and candidate != expected:
                    warnings.append(
                        f"{rel}: {chosen_source} expected behavior overrides conflicting {source_name}"
                    )
            self._write_text(workspace, Path(f"{target_rel}.desc"), render_solution_desc(expected, ""))
            if not accepted_source and expected == "accepted":
                accepted_source = target_rel
            imported_count += 1
        for unused_path in sorted(set(yaml_behaviors).difference(consumed_yaml_paths)):
            warnings.append(f"{unused_path}: submissions.yaml entry has no matching source file")
        return {
            "count": imported_count,
            "accepted_source": accepted_source,
            "warnings": warnings,
        }

    def _copy_component_tree(
        self,
        zf: BudgetedZipFile,
        entries: dict[str, zipfile.ZipInfo],
        source_prefix: str,
        workspace: Path,
        target_prefix: str,
    ) -> list[str]:
        copied: list[str] = []
        src_prefix = source_prefix + "/"
        for rel in sorted(entries):
            if not rel.startswith(src_prefix):
                continue
            suffix_rel = rel[len(src_prefix) :]
            if not suffix_rel:
                continue
            target_rel = Path(target_prefix) / suffix_rel
            zf.copy_to(entries[rel], workspace / target_rel)
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
            token = stem
            if not token:
                continue
            for rel_lower, rel in lower_map.items():
                if Path(rel_lower).stem == token:
                    return rel
        source_candidates.sort()
        return source_candidates[0]

    def _import_components(
        self,
        zf: BudgetedZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
        meta: ProblemMeta,
    ) -> ComponentsSummary:
        for folder in ("validators", "checkers", "interactors"):
            shutil.rmtree(workspace / folder, ignore_errors=True)
            (workspace / folder).mkdir(parents=True, exist_ok=True)

        validators = self._copy_component_tree(zf, entries, "input_validators", workspace, "validators")
        validator_source = self._select_source_file(validators, ["validator", "validate"])

        mode = meta["mode"]
        checker_source = ""
        interactor_source = ""
        output_files = self._copy_component_tree(
            zf,
            entries,
            "output_validator",
            workspace,
            "interactors" if mode == "interactive" else "checkers",
        )
        output_files.extend(
            self._copy_component_tree(
                zf,
                entries,
                "output_validators",
                workspace,
                "interactors" if mode == "interactive" else "checkers",
            )
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
        meta: ProblemMeta,
        statement_summary: StatementSummary,
    ) -> dict[str, object]:
        cfg = _read_json_or_empty(workspace / "config" / "problem.json")
        header = statement_summary["header"]

        time_limit_ms = meta["time_limit_ms"]
        if time_limit_ms is None or time_limit_ms <= 0:
            header_time = header.get("time_text", "")
            parsed_time_limit_ms = _time_limit_ms_from_text(header_time)
            time_limit_ms = parsed_time_limit_ms if parsed_time_limit_ms is not None else 2000

        memory_limit_mb = meta["memory_limit_mb"]
        if memory_limit_mb is None or memory_limit_mb <= 0:
            header_memory = header.get("memory_text", "")
            parsed_memory_limit_mb = _memory_limit_mb_from_text(header_memory)
            memory_limit_mb = parsed_memory_limit_mb if parsed_memory_limit_mb is not None else 1024

        file_io_warning = ""
        raw_input_file = str(header.get("input_file", "") or "").strip()
        raw_output_file = str(header.get("output_file", "") or "").strip()
        if raw_input_file and raw_input_file.lower() not in {"stdin", "standard input", ""}:
            file_io_warning = f"package specifies file I/O (input: {raw_input_file}); forced to stdin/stdout"
        elif raw_output_file and raw_output_file.lower() not in {"stdout", "standard output", ""}:
            file_io_warning = f"package specifies file I/O (output: {raw_output_file}); forced to stdin/stdout"
        mode = meta["mode"]
        pass_limit = int(meta["pass_limit"])

        cfg["time_limit_ms"] = _coerce_int(time_limit_ms, 2000, min_value=1)
        cfg["memory_limit_mb"] = _coerce_int(memory_limit_mb, 1024, min_value=1)
        cfg["mode"] = mode
        cfg["pass_limit"] = pass_limit
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "problem.json").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"cfg": cfg, "file_io_warning": file_io_warning}

    def _write_build_config(
        self,
        workspace: Path,
        meta: ProblemMeta,
        components: ComponentsSummary,
        solutions: SolutionsSummary,
    ) -> dict[str, object]:
        build_cfg = _read_json_or_empty(workspace / "config" / "build.json")
        accepted_source = solutions["accepted_source"]
        if accepted_source:
            build_cfg["accepted_solution_source"] = accepted_source

        validator_source = components["validator_source"]
        if validator_source:
            build_cfg["validator_source"] = validator_source
        else:
            build_cfg.pop("validator_source", None)

        checker_source = components["checker_source"]
        if checker_source:
            build_cfg["checker_source"] = checker_source
        else:
            build_cfg.pop("checker_source", None)

        interactor_source = components["interactor_source"]
        if interactor_source:
            build_cfg["interactor_source"] = interactor_source
        else:
            build_cfg.pop("interactor_source", None)

        mode = meta["mode"]
        if mode == "interactive" and not interactor_source:
            raise ValueError("interactive ICPC package is missing output_validator/interactor source")

        build_cfg["generator_sources"] = []
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "config" / "build.json").write_text(
            dumps_build_config(build_cfg),
            encoding="utf-8",
            newline="\n",
        )
        return build_cfg

    def import_package(
        self,
        workspace: Path,
        package_name: str,
        package: ArchiveView,
        *,
        normalize_test_data_newlines: bool = False,
        text_limit_bytes: int,
    ) -> dict[str, object]:
        package_name = package_name.strip()
        rooted = package.rooted_at("problem.yaml")
        zf = rooted.zip_file
        entry_map = {
            rel: info for rel, info in rooted.entries.items() if not info.is_dir()
        }
        yaml_info = entry_map.get("problem.yaml")
        if yaml_info is None:
            raise ValueError("problem.yaml not found in package")
        meta = self._parse_problem_yaml(
            rooted.read_metadata(yaml_info, label="problem.yaml").decode(
                "utf-8", errors="replace"
            )
        )
        domjudge_meta: DomjudgeMeta = {
            "time_limit_ms": None,
            "name": "",
            "external_id": "",
            "short_name": "",
        }
        domjudge_info = entry_map.get("domjudge-problem.ini")
        if domjudge_info is not None:
            domjudge_meta = _parse_domjudge_ini(
                rooted.read_metadata(
                    domjudge_info, label="domjudge-problem.ini"
                ).decode("utf-8", errors="replace")
            )
            if meta["time_limit_ms"] is None:
                meta["time_limit_ms"] = domjudge_meta["time_limit_ms"]
        external_id = domjudge_meta["external_id"].replace("\\", "/")
        public_slug = PurePosixPath(external_id).name
        if not public_slug:
            public_slug = Path(package_name).stem.strip() or "problem"
        meta["title"] = normalize_problem_title(
            meta["title"] or domjudge_meta["name"],
            fallback_title=public_slug,
        )
        statement_summary, statement_warnings = self._import_statement(zf, entry_map, workspace, meta)
        tests_summary = self._import_tests(
            zf,
            entry_map,
            workspace,
            normalize_test_data_newlines=normalize_test_data_newlines,
            text_limit_bytes=text_limit_bytes,
        )
        solutions_summary = self._import_solutions(zf, entry_map, workspace)
        components_summary = self._import_components(zf, entry_map, workspace, meta)
        self._import_attachments(zf, entry_map, workspace)
        problem_result = self._write_problem_config(workspace, meta, statement_summary)
        problem_cfg = problem_result["cfg"]
        file_io_warning = problem_result.get("file_io_warning", "")
        build_cfg = self._write_build_config(workspace, meta, components_summary, solutions_summary)
        result: dict[str, object] = {
            "package_name": package_name,
            "title": meta["title"],
            "statement": statement_summary,
            "tests": tests_summary,
            "solutions": solutions_summary,
            "components": components_summary,
            "problem_cfg": problem_cfg,
            "build_cfg": build_cfg,
            "domjudge": domjudge_meta,
            "warnings": list(statement_warnings) + list(solutions_summary["warnings"]),
        }
        if file_io_warning:
            result["file_io_warning"] = file_io_warning
        return result

    def _import_attachments(
        self,
        zf: BudgetedZipFile,
        entries: dict[str, zipfile.ZipInfo],
        workspace: Path,
    ) -> None:
        copied = self._copy_component_tree(zf, entries, "attachments", workspace, "attachments")
        if not copied:
            return
