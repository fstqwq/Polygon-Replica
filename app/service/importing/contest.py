"""Polygon contest-package metadata and bounded archive views."""

import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote, urlparse

from app.service.contest.statement_meta import infer_contest_header_fields
from app.service.importing.archive import ArchivePolicy, ArchiveView


class ContestName(TypedDict):
    language: str
    value: str


class ContestProblemRow(TypedDict):
    index: str
    url: str
    short_name: str
    slug_hint: str


class ParsedContest(TypedDict):
    title: str
    problems: list[ContestProblemRow]


class ImportedContestProblem(TypedDict):
    index: str
    source_slug: str
    source_folder: str
    package_name: str


class ImportedContestStatementFile(TypedDict):
    key: str
    language: str
    archive_path: str


class StagedContestStatementFile(TypedDict):
    key: str
    language: str
    source_path: Path


class ParsedContestPackage(TypedDict):
    package_name: str
    title: str
    location: str
    date: str
    problems: list[ImportedContestProblem]
    statement_files: list[ImportedContestStatementFile]
    default_language: str
    total_problems: int


def _slugify_problem_token(raw: str) -> str:
    token = raw.strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token[:64].rstrip("-")


def _slug_hint_from_url(raw_url: str) -> str:
    text = raw_url.strip()
    if not text:
        return ""
    try:
        path = urlparse(text).path.strip("/")
    except Exception:
        path = text
    if not path:
        return ""
    return _slugify_problem_token(unquote(path.split("/")[-1]).strip())


def _contest_idx_label(seq: int) -> str:
    value = max(1, seq)
    chars: list[str] = []
    while value > 0:
        value -= 1
        chars.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(chars))


def _xml_attr(node: ET.Element, name: str) -> str:
    return node.get(name, "").strip()


class PolygonContestImportService:
    """Parse contest metadata and expose child archives without copying bytes."""

    def _infer_statement_header_fields(
        self,
        package: ArchiveView,
        statement_files: list[ImportedContestStatementFile],
        default_language: str,
    ) -> dict[str, str]:
        rows_by_key = {row["key"]: row for row in statement_files}
        candidate_keys: list[str] = []
        safe_default_language = default_language.strip().lower()
        if safe_default_language:
            candidate_keys.append(f"statements/{safe_default_language}/statements.tex")
        if "statements/english/statements.tex" not in candidate_keys:
            candidate_keys.append("statements/english/statements.tex")
        candidate_keys.extend(
            key
            for key in sorted(rows_by_key)
            if key.endswith("/statements.tex") and key not in candidate_keys
        )
        entries = package.entries
        for key in candidate_keys:
            row = rows_by_key.get(key)
            info = entries.get(row["archive_path"]) if row is not None else None
            if info is None:
                continue
            text = package.read_metadata(info, label=key).decode(
                "utf-8", errors="replace"
            )
            inferred = infer_contest_header_fields(text)
            if inferred["title"] or inferred["location"] or inferred["date"]:
                return inferred
        return {"title": "", "location": "", "date": ""}

    @staticmethod
    def _statement_source_rows(
        entries: dict[str, zipfile.ZipInfo],
    ) -> list[ImportedContestStatementFile]:
        rows: list[ImportedContestStatementFile] = []
        for path in sorted(entries):
            if not path.startswith("statements/"):
                continue
            parts = path.split("/")
            if len(parts) < 3:
                continue
            language = parts[1].strip().lower()
            if language:
                rows.append(
                    {"key": path, "language": language, "archive_path": path}
                )
        return rows

    @staticmethod
    def _default_statement_language(
        statement_files: list[ImportedContestStatementFile],
    ) -> str:
        roots = {
            row["language"].strip().lower()
            for row in statement_files
            if row["key"].endswith("/statements.tex")
        }
        if "english" in roots:
            return "english"
        if roots:
            return sorted(roots)[0]
        raise ValueError("contest package missing statements/<language>/statements.tex")

    @staticmethod
    def _parse_contest_xml(xml_text: str) -> ParsedContest:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"invalid contest.xml: {exc}") from exc
        names: list[ContestName] = []
        for node in root.findall("./names/name"):
            language = _xml_attr(node, "language")
            value = _xml_attr(node, "value")
            if language and value:
                names.append({"language": language, "value": value})
        title = next(
            (row["value"] for row in names if row["language"].lower() == "english"),
            names[0]["value"] if names else "",
        )
        problems: list[ContestProblemRow] = []
        for seq, node in enumerate(root.findall("./problems/problem"), start=1):
            index = _xml_attr(node, "index") or _contest_idx_label(seq)
            url = _xml_attr(node, "url")
            short_name = _xml_attr(node, "short-name")
            slug_hint = _slug_hint_from_url(url) or _slugify_problem_token(short_name)
            problems.append(
                {
                    "index": index.upper(),
                    "url": url,
                    "short_name": short_name,
                    "slug_hint": slug_hint,
                }
            )
        if not problems:
            raise ValueError("contest.xml has no problems")
        return {"title": title, "problems": problems}

    @staticmethod
    def _problem_folder_map(
        entries: dict[str, zipfile.ZipInfo],
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for path in entries:
            if path.startswith("problems/") and path.endswith("/problem.xml"):
                parts = path.split("/")
                if len(parts) >= 3 and parts[1]:
                    mapping[parts[1].lower()] = parts[1]
        return mapping

    @staticmethod
    def _resolve_problem_folder(
        row: ContestProblemRow,
        folder_map: dict[str, str],
        used: set[str],
    ) -> str:
        candidates: list[str] = []
        if row["slug_hint"]:
            candidates.extend((row["slug_hint"], row["slug_hint"].replace("-", "_")))
        short_name = _slugify_problem_token(row["short_name"])
        if short_name:
            candidates.extend((short_name, short_name.replace("-", "_")))
        for candidate in dict.fromkeys(candidates):
            if candidate not in used and candidate in folder_map:
                return folder_map[candidate]
        return next(
            (folder_map[token] for token in sorted(folder_map) if token not in used),
            "",
        )

    def parse_package(
        self,
        package_name: str,
        package: ArchiveView,
        *,
        problem_policy: ArchivePolicy,
        max_problems: int,
    ) -> ParsedContestPackage:
        rooted = package.rooted_at("contest.xml")
        entries = {
            path: info for path, info in rooted.entries.items() if not info.is_dir()
        }
        xml_info = entries.get("contest.xml")
        if xml_info is None:
            raise ValueError("contest.xml not found in package")
        contest = self._parse_contest_xml(
            rooted.read_metadata(xml_info, label="contest.xml").decode(
                "utf-8", errors="replace"
            )
        )
        if len(contest["problems"]) > int(max_problems):
            raise ValueError(
                "contest package has more than the configured maximum of "
                f"{int(max_problems)} problems"
            )
        statement_files = self._statement_source_rows(entries)
        default_language = self._default_statement_language(statement_files)
        inferred = self._infer_statement_header_fields(
            rooted, statement_files, default_language
        )
        folder_map = self._problem_folder_map(entries)
        if not folder_map:
            raise ValueError("no problem.xml found under problems/ in contest package")
        used_folders: set[str] = set()
        imported_rows: list[ImportedContestProblem] = []
        for seq, row in enumerate(contest["problems"], start=1):
            folder = self._resolve_problem_folder(row, folder_map, used_folders)
            if not folder:
                raise ValueError(
                    f"cannot resolve contest problem folder for index #{seq}"
                )
            child = rooted.subview(f"problems/{folder}", problem_policy)
            child.rooted_at("problem.xml")
            used_folders.add(folder.lower())
            source_slug = _slugify_problem_token(folder) or f"problem-{seq}"
            imported_rows.append(
                {
                    "index": row["index"],
                    "source_slug": source_slug,
                    "source_folder": folder,
                    "package_name": f"{source_slug}.zip",
                }
            )
        package_name = package_name.strip()
        return {
            "package_name": package_name,
            "title": contest["title"] or inferred["title"] or Path(package_name or "imported-contest").stem,
            "location": inferred["location"],
            "date": inferred["date"],
            "problems": imported_rows,
            "statement_files": statement_files,
            "default_language": default_language,
            "total_problems": len(imported_rows),
        }

    @staticmethod
    def problem_archive(
        package: ArchiveView,
        source_folder: str,
        policy: ArchivePolicy,
    ) -> ArchiveView:
        rooted = package.rooted_at("contest.xml")
        return rooted.subview(f"problems/{source_folder}", policy)

    @staticmethod
    def stage_statement_sources(
        package: ArchiveView,
        files: list[ImportedContestStatementFile],
        staging_root: Path,
    ) -> list[StagedContestStatementFile]:
        rooted = package.rooted_at("contest.xml")
        entries = rooted.entries
        shutil.rmtree(staging_root, ignore_errors=True)
        staging_root.mkdir(parents=True, exist_ok=False)
        staged: list[StagedContestStatementFile] = []
        for row in files:
            info = entries.get(row["archive_path"])
            if info is None:
                raise ValueError(f"contest statement member is missing: {row['key']}")
            source_path = staging_root / row["key"]
            rooted.zip_file.copy_to(info, source_path)
            staged.append(
                {
                    "key": row["key"],
                    "language": row["language"],
                    "source_path": source_path,
                }
            )
        return staged
