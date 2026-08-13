"""Read models derived from one canonical authored problem source tree."""

from pathlib import Path
from typing import TypedDict

from app.config import ConfigValues
from app.service.problem.build_config import load_build_config
from app.service.problem.solution_metadata import (
    ExpectedBehavior,
    desc_rel_path_for_source,
    expected_behavior_label,
    load_solution_desc,
)
from app.service.problem.source_file import require_regular_source_file
from app.service.problem.source_tree import solution_sources
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    TestSpecEntry,
    load_tests_spec,
    payload_rel_path_for_test,
    summarize_tests_spec,
)


class SolutionSourceRow(TypedDict):
    source_path: str
    file_name: str
    expected_behavior: ExpectedBehavior
    expected_behavior_label: str
    note: str
    note_preview: str
    desc_path: str
    desc_exists: bool
    desc_origin: str
    desc_errors: list[str]
    is_accepted: bool


class RunSolutionOption(TypedDict):
    path: str
    label: str
    is_accepted: bool
    expected_behavior: ExpectedBehavior


class RunTestOption(TypedDict):
    name: str
    label: str


def _human_size(num_bytes: int) -> str:
    size = max(0, int(num_bytes))
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{size} B"


def _file_head_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    cap = max(1, int(max_bytes))
    try:
        with path.open("rb") as source:
            head = source.read(cap + 1)
    except OSError:
        return "(unreadable)", False
    return head[:cap].decode("utf-8", errors="replace"), len(head) > cap


def _inline_text_preview(raw: str, max_chars: int, max_lines: int) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip("\n\r") for line in text.splitlines()]
    clipped_by_lines = max_lines > 0 and len(lines) > max_lines
    if clipped_by_lines:
        lines = lines[:max_lines]
    preview = "\n".join(lines).strip() or "(empty)"
    if len(preview) > max_chars:
        return preview[: max_chars - 3].rstrip() + "..."
    if clipped_by_lines:
        return preview + " ..."
    return preview


class ProblemSourceQueryService:
    """Build reusable UI-independent views from authored Source files."""

    def __init__(self, config_values: ConfigValues) -> None:
        self._config_values = config_values

    def _tests(self, workspace: Path) -> tuple[list[TestSpecEntry], Path]:
        values = self._config_values.snapshot()
        path = workspace / TESTS_SPEC_REL
        return (
            load_tests_spec(
                path,
                document_max_bytes=int(values["TEXTAREA_MAX_BYTES"]),
                sample_max_bytes=int(values["STATEMENT_SAMPLE_MAX_BYTES"]),
            ),
            path,
        )

    @staticmethod
    def _payload_path(workspace: Path, entry: TestSpecEntry) -> Path | None:
        relative = payload_rel_path_for_test(entry["id"], entry["kind"])
        try:
            return require_regular_source_file(workspace, relative)
        except ValueError:
            return None

    def tests_spec_editor(self, workspace: Path, limit: int) -> dict:
        entries, path = self._tests(workspace)
        summary = summarize_tests_spec(entries)
        rows: list[dict] = []
        cap = max(1, int(limit))
        values = self._config_values
        for index, entry in enumerate(entries[:cap], start=1):
            payload_path = payload_rel_path_for_test(entry["id"], entry["kind"])
            payload_abs = self._payload_path(workspace, entry)
            payload_size = payload_abs.stat().st_size if payload_abs is not None else 0
            payload = ""
            preview_source = ""
            manual_large = (
                entry["kind"] == "manual"
                and payload_size > int(values.TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES)
            )
            preview_limit = 0
            preview_clipped = False
            if manual_large:
                preview_limit = int(values.TESTS_SPEC_MANUAL_PREVIEW_BYTES)
                assert payload_abs is not None
                preview_source, preview_clipped = _file_head_text(
                    payload_abs,
                    preview_limit,
                )
            else:
                payload = (
                    payload_abs.read_text(encoding="utf-8")
                    if payload_abs is not None
                    else ""
                )
                preview_source = payload
            preview = (
                preview_source.replace("\r\n", "\n").replace("\r", "\n")
                or "(empty)"
                if manual_large
                else _inline_text_preview(
                    preview_source,
                    int(values.TESTS_SPEC_PREVIEW_CHARS),
                    int(values.TESTS_SPEC_PREVIEW_LINES),
                )
            )
            rows.append(
                {
                    "index": index,
                    **entry,
                    "custom_sample_input": bool(entry["sample_input"]),
                    "custom_sample_output": bool(entry["sample_output"]),
                    "payload_path": payload_path,
                    "payload": payload,
                    "preview": preview,
                    "payload_size_bytes": payload_size,
                    "payload_size_human": _human_size(payload_size),
                    "manual_large_payload": manual_large,
                    "preview_bytes_limit": preview_limit,
                    "preview_clipped": preview_clipped,
                }
            )
        return {
            "path": TESTS_SPEC_REL.as_posix(),
            "exists": True,
            "entries": entries,
            "rows": rows,
            "summary": summary,
            "total": len(entries),
            "shown": len(rows),
            "truncated": len(entries) > cap,
        }

    def tests_spec_status(self, workspace: Path) -> dict:
        try:
            entries, _path = self._tests(workspace)
        except ValueError:
            return {
                "mode": "invalid",
                "display": "invalid",
                "total": 0,
                "manual": 0,
                "gen": 0,
                "sample": 0,
            }
        summary = summarize_tests_spec(entries)
        total = summary["total"]
        if total == 0:
            return {"mode": "empty", "display": "empty", **summary}
        sample = summary["sample"]
        sample_label = "sample" if sample == 1 else "samples"
        return {
            "mode": "ready",
            "display": f"{total} ({sample} {sample_label})",
            **summary,
        }

    @staticmethod
    def _solution_entry(
        workspace: Path,
        source_rel: str,
        accepted_source: str,
    ) -> SolutionSourceRow:
        desc_path = desc_rel_path_for_source(source_rel)
        try:
            exists = (workspace / desc_path).is_file()
            descriptor = load_solution_desc(workspace, source_rel)
            behavior = descriptor["expected_behavior"]
            note = descriptor["note"]
            origin = "metadata" if exists else "default"
            errors: list[str] = []
        except ValueError as exc:
            behavior = "unknown"
            note = ""
            origin = "invalid"
            errors = [str(exc)]
            exists = (workspace / desc_path).is_file()
            if not exists:
                origin = "missing"
        if source_rel == accepted_source:
            behavior = "accepted"
        preview = note if len(note) <= 160 else note[:157] + "..."
        return {
            "source_path": source_rel,
            "file_name": Path(source_rel).name,
            "expected_behavior": behavior,
            "expected_behavior_label": expected_behavior_label(behavior),
            "note": note,
            "note_preview": preview,
            "desc_path": desc_path,
            "desc_exists": exists,
            "desc_origin": origin,
            "desc_errors": errors,
            "is_accepted": behavior == "accepted",
        }

    def solution_entry(
        self,
        workspace: Path,
        source_rel: str,
    ) -> SolutionSourceRow:
        return self._solution_entry(
            workspace,
            source_rel,
            self.accepted_solution_source(workspace),
        )

    def solution_entries(self, workspace: Path) -> tuple[list[SolutionSourceRow], bool]:
        sources = solution_sources(workspace)
        limit = int(self._config_values.SOLUTION_LIST_LIMIT)
        accepted_source = self.accepted_solution_source(workspace)
        return (
            [
                self._solution_entry(workspace, source, accepted_source)
                for source in sources[:limit]
            ],
            len(sources) > limit,
        )

    def accepted_solution_source(self, workspace: Path) -> str:
        return load_build_config(workspace).get("accepted_solution_source", "")

    def run_solution_options(
        self,
        workspace: Path,
    ) -> tuple[list[RunSolutionOption], str, bool]:
        entries, truncated = self.solution_entries(workspace)
        default_path = self.accepted_solution_source(workspace)
        if default_path not in {row["source_path"] for row in entries}:
            default_path = ""
        options = [
            {
                "path": row["source_path"],
                "label": (
                    f'{row["source_path"]} ({row["expected_behavior_label"]})'
                ),
                "is_accepted": row["is_accepted"],
                "expected_behavior": row["expected_behavior"],
            }
            for row in entries
        ]
        return options, default_path, truncated

    def run_test_options(
        self,
        workspace: Path,
    ) -> tuple[list[RunTestOption], bool, str]:
        try:
            entries, _path = self._tests(workspace)
        except ValueError:
            return [], False, ""
        limit = int(self._config_values.RUN_TEST_SELECTOR_LIMIT)
        options: list[RunTestOption] = []
        for index, row in enumerate(entries[:limit], start=1):
            parts = [f'id={row["id"]}', row["kind"]]
            if row["sample"]:
                parts.append("sample")
            name = f"{index:03d}.in"
            options.append({"name": name, "label": f'{name} ({"; ".join(parts)})'})
        return options, len(entries) > limit, "tests/spec.json" if options else ""
