"""Polygon contest-package metadata and bounded archive views."""

import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote, urlparse

from app.service.contest.statement_meta import infer_contest_header_fields
from app.service.contest.statement_source_contract import (
    CONTEST_STATEMENT_OUTPUT_NAME,
    CONTEST_STATEMENT_TEMPLATE_NAME,
)
from app.service.importing.archive import ArchivePolicy, ArchiveView
from app.service.statement.context import normalize_statement_language


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
    titles: dict[str, str]
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
    localized_properties: dict[str, str]
    problems: list[ImportedContestProblem]
    statement_files: list[ImportedContestStatementFile]
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


def _contest_idx_from_sequence(seq: int) -> str:
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

    def _infer_statement_header_properties(
        self,
        package: ArchiveView,
        statement_files: list[ImportedContestStatementFile],
    ) -> tuple[dict[str, str], dict[str, str]]:
        rows_by_key = {row["key"]: row for row in statement_files}
        statement_keys = sorted(
            key
            for key in rows_by_key
            if key.endswith(f"/{CONTEST_STATEMENT_TEMPLATE_NAME}")
        )
        english_key = f"statements/english/{CONTEST_STATEMENT_TEMPLATE_NAME}"
        candidate_keys = (
            [english_key, *[key for key in statement_keys if key != english_key]]
            if english_key in statement_keys
            else statement_keys
        )
        entries = package.entries
        preferred = {"title": "", "location": "", "date": ""}
        localized: dict[str, str] = {}
        for key in statement_keys:
            row = rows_by_key.get(key)
            info = entries.get(row["archive_path"]) if row is not None else None
            if info is None:
                continue
            text = package.read_metadata(info, label=key).decode(
                "utf-8", errors="replace"
            )
            inferred = infer_contest_header_fields(text)
            language = normalize_statement_language(
                row["language"] if row is not None else ""
            )
            if language:
                for property_key in ("title", "location", "date"):
                    value = inferred[property_key]
                    if value:
                        localized[f"{property_key}.{language}"] = value
        for key in candidate_keys:
            row = rows_by_key.get(key)
            if row is None:
                continue
            language = normalize_statement_language(row["language"])
            for property_key in preferred:
                value = localized.get(f"{property_key}.{language}", "")
                if value and not preferred[property_key]:
                    preferred[property_key] = value
            if all(preferred.values()):
                break
        return preferred, localized

    @staticmethod
    def _statement_source_rows(
        entries: dict[str, zipfile.ZipInfo],
    ) -> list[ImportedContestStatementFile]:
        rows: list[ImportedContestStatementFile] = []
        archive_paths_by_key: dict[str, str] = {}
        for path in sorted(entries):
            if not path.startswith("statements/"):
                continue
            parts = path.split("/")
            if len(parts) < 3:
                continue
            language = parts[1].strip().lower()
            if language:
                key_parts = list(parts)
                if (
                    len(key_parts) == 3
                    and key_parts[-1] == CONTEST_STATEMENT_OUTPUT_NAME
                ):
                    key_parts[-1] = CONTEST_STATEMENT_TEMPLATE_NAME
                key = "/".join(key_parts)
                existing_archive_path = archive_paths_by_key.get(key)
                if existing_archive_path is not None:
                    raise ValueError(
                        "contest package has ambiguous statement sources: "
                        f"{existing_archive_path} and {path}"
                    )
                archive_paths_by_key[key] = path
                rows.append(
                    {"key": key, "language": language, "archive_path": path}
                )
        return rows

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
        titles = {
            language: row["value"]
            for row in names
            if (language := normalize_statement_language(row["language"]))
        }
        title = next(
            (
                row["value"]
                for row in names
                if normalize_statement_language(row["language"]) == "english"
            ),
            names[0]["value"] if names else "",
        )
        problems: list[ContestProblemRow] = []
        for seq, node in enumerate(root.findall("./problems/problem"), start=1):
            index = _xml_attr(node, "index") or _contest_idx_from_sequence(seq)
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
        return {"title": title, "titles": titles, "problems": problems}

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
        if not any(
            row["key"].endswith(f"/{CONTEST_STATEMENT_TEMPLATE_NAME}")
            for row in statement_files
        ):
            raise ValueError(
                "contest package missing statements/<language>/"
                f"{CONTEST_STATEMENT_TEMPLATE_NAME}"
            )
        inferred, localized_properties = self._infer_statement_header_properties(
            rooted,
            statement_files,
        )
        localized_properties.update(
            {
                f"title.{language}": value
                for language, value in contest["titles"].items()
            }
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
            "title": (
                contest["title"]
                or inferred["title"]
                or Path(package_name or "imported-contest").stem
            ),
            "location": inferred["location"],
            "date": inferred["date"],
            "localized_properties": localized_properties,
            "problems": imported_rows,
            "statement_files": statement_files,
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
