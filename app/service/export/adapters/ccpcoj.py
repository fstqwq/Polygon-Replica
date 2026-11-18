"""Write a CCPCOJ 2.0.315 native problem-import archive tree."""

import hashlib
import json
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.service.export.adapters.shared import (
    EXACT_OUTPUT_CHECKER,
    PackageAdapterPlan,
    PackageAdapterSupport,
    PackageFormat,
)
from app.service.problem_package.layout import STATEMENT_BUILD_DIR
from app.service.problem_package.manifest import (
    StatementEventPath,
    StatementSamplePaths,
    statement_sample_resource,
)
from app.service.problem_package.service import NativePackageReader
from app.service.statement.context import statement_languages
from app.service.statement.html_render import StatementHtmlRenderer
from app.service.statement.render import statement_title_from_snapshot

_GRAPHIC = re.compile(
    r"(?P<comment>(?<!\\)%[^\n]*)|(?P<command>\\includegraphics\*?)\s*(?:\[(?P<options>[^\]]*)\]\s*)?\{(?P<file>[^{}]+)\}"
)


_LANGUAGES = {".c": 0, ".cc": 1, ".cpp": 1, ".cxx": 1, ".c++": 1, ".java": 3, ".py": 6}
_RESULTS = {
    "accepted": (4, "AC"),
    "wrong_answer": (6, "WA"),
    "time_limit_exceeded": (7, "TLE"),
    "run_time_error": (10, "RE"),
    "compile_error": (11, "CE"),
}


_PAIR = re.compile(r"\\StatementSampleFile\{(\d+)\}\{([^{}]+)\}\{([^{}]+)\}")
_INTERACTION = re.compile(r"\\begin\{StatementSampleInteraction\}\{(\d+)\}")
_EVENT = re.compile(r"\\StatementSampleEventFile\{(interactor|solution)\}\{([^{}]+)\}")


_SampleDeclaration = StatementSamplePaths | list[StatementEventPath]


def _declared_samples(build: Path, test_ids: list[str]) -> dict[str, _SampleDeclaration]:
    """Read explicit single-pass references in the packaged statement."""
    template = build / "examples.tex"
    if not template.is_file():
        return {}
    lines = iter(template.read_text(encoding="utf-8").splitlines())
    records: dict[str, _SampleDeclaration] = {}
    for line in lines:
        pair = _PAIR.fullmatch(line.strip())
        interaction = _INTERACTION.fullmatch(line.strip())
        match = pair or interaction
        if not match:
            continue
        number = int(match[1])
        if not 1 <= number <= len(test_ids):
            raise ValueError(f"Unexpected sample number {number} in {template}")
        test_id = test_ids[number - 1]
        if test_id in records:
            raise ValueError(f"Duplicate sample {test_id} in {template}")
        if pair:
            records[test_id] = {"input": pair[2], "output": pair[3]}
            continue
        events: list[StatementEventPath] = []
        for body in lines:
            if body.strip() == r"\end{StatementSampleInteraction}":
                break
            event = _EVENT.fullmatch(body.strip())
            if not event:
                raise ValueError(f"Invalid event declaration for sample {test_id} in {template}")
            events.append({"source": "interactor" if event[1] == "interactor" else "solution", "path": event[2]})
        else:
            raise ValueError(f"Unclosed interaction for sample {test_id} in {template}")
        records[test_id] = events
    return records


def _sample_fields(reader: NativePackageReader, build: Path) -> tuple[str, str]:
    inputs: list[str] = []
    outputs: list[str] = []
    cases: list[dict[str, list[dict[str, str]]]] = []
    interactive = reader.manifest["mode"] == "interactive"
    test_ids = [test["id"] for test in reader.manifest["tests"] if test["sample"]]
    indexed = reader.manifest.get("statement_samples", {})
    declared = {}
    if any(t not in indexed or (interactive and "event_passes" not in indexed[t]) for t in test_ids):
        declared = _declared_samples(build, test_ids)
    for test_id in test_ids:
        sample = indexed.get(test_id)
        declaration = declared.get(test_id)
        if sample is None and isinstance(declaration, dict):
            sample = declaration

        def read(path: str) -> str:
            return statement_sample_resource(build, test_id, path).read_bytes().decode("utf-8")

        passes = sample.get("event_passes") if sample is not None else None
        if passes is None and isinstance(declaration, list):
            passes = [declaration]
        if passes is not None:
            if not interactive or len(passes) != 1 or not passes[0]:
                raise ValueError(
                    f"CCPCOJ cannot represent the event passes for sample {test_id}"
                )
            turns = [
                {
                    "role": "in" if event["source"] == "interactor" else "out",
                    "text": read(event["path"]),
                }
                for event in passes[0]
            ]
        else:
            if sample is None:
                raise ValueError(f"Missing statement sample {test_id} ({build.name}): no resource references in manifest or examples.tex")
            inputs.append(read(sample["input"]))
            outputs.append(read(sample["output"]))
            continue
        cases.append({"turns": turns})
    if cases and inputs:
        raise ValueError("CCPCOJ cannot mix pair and event samples in one sample layout")
    if cases:
        doc = json.dumps(
            {
                "data_type": "json",
                "layout": "interaction",
                "view": "time",
                "data": cases,
            },
            ensure_ascii=False,
        )
        return doc, doc
    return (
        json.dumps({"data_type": "json", "data": inputs}, ensure_ascii=False),
        json.dumps({"data_type": "json", "data": outputs}, ensure_ascii=False),
    )


class CCPCOJPackageAdapter(PackageAdapterSupport):
    format: PackageFormat = "ccpcoj"
    display_name = "CCPCOJ"

    def plan(self, reader: NativePackageReader) -> PackageAdapterPlan:
        if reader.manifest["pass_limit"] != 1:
            raise ValueError("CCPCOJ export supports single-pass judging")
        solutions = tuple(
            row
            for row in reader.manifest["solutions"]
            if row["expected_behavior"] in _RESULTS
            and Path(row["source_path"]).suffix.lower() in _LANGUAGES
        )
        return PackageAdapterPlan(self.format, solutions, "")

    def build(
        self,
        reader: NativePackageReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        plan: PackageAdapterPlan | None = None,
    ) -> str:
        adapter_plan = plan or self.plan(reader)
        if adapter_plan.package_format != self.format:
            raise ValueError("package adapter plan format does not match request")
        self.prepare_target(target)
        root = reader.root
        config = self.native_problem_config(root)
        languages = statement_languages(root)
        if not languages:
            raise ValueError("CCPCOJ requires at least one statement language")
        primary = languages[0]
        title = statement_title_from_snapshot(
            root,
            fallback_title=self.problem_slug_leaf(canonical_problem_slug),
            language=primary,
        )
        self._write_judge_data(reader, target)
        mode = config["mode"]
        digest = hashlib.sha256(
            f"polygon-replica/ccpcoj\n{canonical_problem_slug}".encode()
        ).hexdigest()
        attach = f"{digest[:2]}-{digest[2:4]}-{digest[4:6]}-{UUID(digest[6:38])}"
        locales = self._write_locales(root, target, languages, attach)
        sample_in, sample_out = _sample_fields(
            reader, root / STATEMENT_BUILD_DIR / primary
        )
        record: dict[str, object] = {
            "problem_id": 0,
            "problem_new_id": 1,
            "title": title,
            "attach": attach,
            "spj": "2" if mode == "interactive" else "1",
            "time_limit": str(config["time_limit_ms"] / 1000),
            "memory_limit": config["memory_limit_mb"],
            "sample_input": sample_in,
            "sample_output": sample_out,
            "problem_locales": locales,
            "in_date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "description": "",
            "input": "",
            "output": "",
            "hint": "",
            "source": "",
            "author": "",
            "accepted": 0,
            "submit": 0,
            "solved": 0,
        }
        solutions = []
        for solution in adapter_plan.solutions:
            source = root / solution["source_path"]
            result, tag = _RESULTS[solution["expected_behavior"]]
            solutions.append(
                {
                    "source": source.read_text(encoding="utf-8"),
                    "language": _LANGUAGES[source.suffix.lower()],
                    "result": result,
                    "result_tag": tag,
                }
            )
        record["solutions"] = solutions
        files_root = root / "attachments"
        files = sorted(path for path in files_root.rglob("*") if path.is_file())
        if files:
            dest = target / "ATTACH_00001"
            with zipfile.ZipFile(
                dest / "player-files.zip", "w", zipfile.ZIP_DEFLATED
            ) as archive:
                for path in files:
                    archive.write(path, path.relative_to(files_root))
            record["addition"] = {
                "attachments": [
                    {
                        "name": "player-files.zip",
                        "url": f"/upload/problem_attach/{attach}/player-files.zip",
                    }
                ]
            }
        (target / "problemlist.json").write_text(
            json.dumps([record], ensure_ascii=False), encoding="utf-8"
        )
        (target / "csg_export_timezone.json").write_text(
            '{"iana":"UTC","schema":1}', encoding="utf-8"
        )
        return adapter_plan.warning

    def _write_judge_data(self, reader: NativePackageReader, target: Path) -> None:
        tests = target / "TEST_00001"
        tests.mkdir()
        mode = reader.manifest["mode"]
        programs = self.native_build_config(reader.root, problem_mode=mode)
        checker, interactor, _ = self.package_programs(reader.root, programs, mode=mode)
        program = interactor if mode == "interactive" else checker
        if program is None:
            if mode == "interactive":
                raise ValueError("CCPCOJ requires a C++ interactor")
            (tests / "tpj.cc").write_text(EXACT_OUTPUT_CHECKER, encoding="utf-8")
        else:
            if program.suffix.lower() not in {".cc", ".cpp", ".cxx", ".c++"}:
                raise ValueError(
                    f"CCPCOJ requires a C++ checker or interactor: {program.name}"
                )
            shutil.copyfile(program, tests / "tpj.cc")
        for index, test in enumerate(reader.manifest["tests"], 1):
            input_path = reader.payload(test, "input")
            assert input_path is not None
            shutil.copyfile(input_path, tests / f"{index:04}.in")
            answer = reader.payload(test, "answer")
            out = tests / f"{index:04}.out"
            if answer is None:
                out.write_bytes(b"")
            else:
                shutil.copyfile(answer, out)

    def _write_locales(
        self,
        root: Path,
        target: Path,
        languages: list[str],
        attach: str,
    ) -> list[dict[str, object]]:
        locales: list[dict[str, object]] = []
        image_renderer = StatementHtmlRenderer(
            self._tex_compile_service.sandbox, self._config_values
        )
        image_dir = target / "ATTACH_00001"
        image_dir.mkdir()
        image_urls: dict[tuple[str, str, int], str] = {}
        for index, language in enumerate(languages):
            section_root = root / "statement-sections" / language

            def replace_image(match: re.Match[str]) -> str:
                if match["comment"] is not None:
                    return match[0]
                source = match["file"]
                options = (match["options"] or "").split(",")
                page = 1
                kept = []
                for option in options:
                    key, _, value = option.strip().partition("=")
                    if key.strip() == "page":
                        page = int(value.strip())
                    elif key:
                        kept.append(option)
                cache_key = (language, source, page)
                if cache_key not in image_urls:
                    name = image_renderer.copy_image(
                        source,
                        page=page,
                        serial=len(image_urls) + 1,
                        source_root=root / STATEMENT_BUILD_DIR / language,
                        resources_root=image_dir,
                    )
                    image_urls[cache_key] = f"/upload/problem_attach/{attach}/{name}"
                attributes = "[" + ",".join(kept) + "]" if kept else ""
                return match["command"] + attributes + "{" + image_urls[cache_key] + "}"

            def section(name: str) -> str:
                path = section_root / f"{name}.tex"
                if not path.is_file():
                    return ""
                return _GRAPHIC.sub(
                    replace_image, path.read_text(encoding="utf-8").strip()
                )

            legend = "\n\n".join(
                filter(None, (section("legend"), section("interaction")))
            )
            sections = {
                "description": legend,
                "input": section("input"),
                "output": section("output"),
                "hint": section("notes"),
            }
            locales.append(
                {
                    "locale_key": language,
                    "locale_label": {"chinese": "中文", "english": "English"}.get(
                        language, language
                    ),
                    "locale_visible": 1,
                    "sort_order": index + 1,
                    "use_pdf": 0,
                    "source": "",
                    "author": "",
                    **{
                        key: "__LATEX__\n\n" + (value or "-")
                        for key, value in sections.items()
                    },
                }
            )
        return locales

    @staticmethod
    def write_contest_archive(archive: zipfile.ZipFile, packages: list[Path]) -> None:
        """Join validated standalone packages in roster order for one native import."""
        records = []
        for index, package in enumerate(packages, 1):
            (record,) = json.loads(
                (package / "problemlist.json").read_text(encoding="utf-8")
            )
            record["problem_new_id"] = index
            records.append(record)
            for kind in ("TEST", "ATTACH"):
                folder = package / f"{kind}_00001"
                for source in sorted(folder.rglob("*")):
                    if source.is_file():
                        member = Path(f"{kind}_{index:05}") / source.relative_to(folder)
                        archive.write(
                            source,
                            member.as_posix(),
                            compress_type=zipfile.ZIP_DEFLATED,
                        )
        archive.writestr(
            "problemlist.json",
            json.dumps(records, ensure_ascii=False),
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "csg_export_timezone.json", json.dumps({"iana": "UTC", "schema": 1})
        )
