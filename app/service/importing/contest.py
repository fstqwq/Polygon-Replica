from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import unquote, urlparse

from app.service.contest.statement_meta import infer_contest_header_fields

ZIP_MAX_BYTES = 512 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 128 * 1024 * 1024
ZIP_TEXT_MAX_BYTES = 8 * 1024 * 1024


ContestName = TypedDict(
    "ContestName",
    {
        "language": str,
        "value": str,
    },
)

ContestProblemRow = TypedDict(
    "ContestProblemRow",
    {
        "index": str,
        "url": str,
        "short_name": str,
        "slug_hint": str,
    },
)

ParsedContest = TypedDict(
    "ParsedContest",
    {
        "title": str,
        "problems": list[ContestProblemRow],
    },
)

ImportedContestProblem = TypedDict(
    "ImportedContestProblem",
    {
        "index": str,
        "source_slug": str,
        "source_folder": str,
        "package_name": str,
        "package_bytes": bytes,
    },
)

ImportedContestStatementFile = TypedDict(
    "ImportedContestStatementFile",
    {
        "key": str,
        "language": str,
        "package_bytes": bytes,
    },
)

ParsedContestPackage = TypedDict(
    "ParsedContestPackage",
    {
        "package_name": str,
        "title": str,
        "location": str,
        "date": str,
        "problems": list[ImportedContestProblem],
        "statement_files": list[ImportedContestStatementFile],
        "default_language": str,
        "total_problems": int,
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
        if not part or part == ".":
            continue
        if part == "..":
            return ""
        parts.append(part)
    return "/".join(parts)


def _read_text_from_zip(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if info.file_size > ZIP_TEXT_MAX_BYTES:
        raise ValueError(f"zip entry too large for text: {info.filename}")
    with zf.open(info, "r") as fh:
        raw = fh.read(ZIP_TEXT_MAX_BYTES + 1)
    if len(raw) > ZIP_TEXT_MAX_BYTES:
        raise ValueError(f"zip entry too large for text: {info.filename}")
    return raw.decode("utf-8", errors="replace")


def _read_bytes_from_zip(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > ZIP_MAX_FILE_BYTES:
        raise ValueError(f"zip entry too large: {info.filename}")
    with zf.open(info, "r") as fh:
        raw = fh.read(ZIP_MAX_FILE_BYTES + 1)
    if len(raw) > ZIP_MAX_FILE_BYTES:
        raise ValueError(f"zip entry too large: {info.filename}")
    return raw


def _entry_map_from_zip(zf: zipfile.ZipFile, anchor_name: str) -> dict[str, zipfile.ZipInfo]:
    raw_entries: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        normalized = _normalize_zip_path(info.filename)
        if not normalized:
            continue
        raw_entries[normalized] = info
    anchor = _normalize_zip_path(anchor_name)
    if not anchor:
        raise ValueError("invalid package anchor")
    if anchor in raw_entries:
        return raw_entries
    suffix = f"/{anchor}"
    candidates = sorted([path for path in raw_entries if path.endswith(suffix)], key=len)
    if not candidates:
        raise ValueError(f"{anchor} not found in package")
    prefix = candidates[0][:-len(anchor)]
    mapped_entries: dict[str, zipfile.ZipInfo] = {}
    for path, info in raw_entries.items():
        if not path.startswith(prefix):
            continue
        relative_path = path[len(prefix):]
        if relative_path:
            mapped_entries[relative_path] = info
    if anchor not in mapped_entries:
        raise ValueError(f"{anchor} not found in package")
    return mapped_entries


def _slugify_problem_token(raw: str) -> str:
    token = raw.strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > 64:
        token = token[:64].rstrip("-")
    return token


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
    leaf = unquote(path.split("/")[-1]).strip()
    return _slugify_problem_token(leaf)


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
    def _infer_statement_header_fields(
        self,
        statement_files: list[ImportedContestStatementFile],
        default_language: str,
    ) -> dict[str, str]:
        text_by_key: dict[str, str] = {}
        for row in statement_files:
            key = str(row["key"]).strip()
            if not key.endswith("/statements.tex"):
                continue
            try:
                text_by_key[key] = bytes(row["package_bytes"]).decode("utf-8", errors="replace")
            except Exception:
                continue
        candidate_keys: list[str] = []
        safe_default_language = str(default_language or "").strip().lower()
        if safe_default_language:
            candidate_keys.append(f"statements/{safe_default_language}/statements.tex")
        if "statements/english/statements.tex" not in candidate_keys:
            candidate_keys.append("statements/english/statements.tex")
        for key in sorted(text_by_key):
            if key not in candidate_keys:
                candidate_keys.append(key)
        for key in candidate_keys:
            text = text_by_key.get(key, "")
            if not text:
                continue
            inferred = infer_contest_header_fields(text)
            if inferred["title"] or inferred["location"] or inferred["date"]:
                return inferred
        return {"title": "", "location": "", "date": ""}

    def _statement_source_rows(self, zf: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo]) -> list[ImportedContestStatementFile]:
        rows: list[ImportedContestStatementFile] = []
        for path, info in sorted(entries.items()):
            if not path.startswith("statements/"):
                continue
            parts = path.split("/")
            if len(parts) < 3:
                continue
            language = parts[1].strip().lower()
            if not language:
                continue
            rows.append(
                {
                    "key": path,
                    "language": language,
                    "package_bytes": _read_bytes_from_zip(zf, info),
                }
            )
        return rows

    def _default_statement_language(self, statement_files: list[ImportedContestStatementFile]) -> str:
        languages: set[str] = set()
        statement_roots: set[str] = set()
        for row in statement_files:
            language = row["language"].strip().lower()
            key = row["key"].strip()
            if not language or not key:
                continue
            languages.add(language)
            if key.endswith("/statements.tex"):
                statement_roots.add(language)
        if "english" in statement_roots:
            return "english"
        if statement_roots:
            return sorted(statement_roots)[0]
        raise ValueError("contest package missing statements/<language>/statements.tex")

    def _parse_contest_xml(self, xml_text: str) -> ParsedContest:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"invalid contest.xml: {exc}") from exc
        title = ""
        names: list[ContestName] = []
        for node in root.findall("./names/name"):
            language = _xml_attr(node, "language")
            value = _xml_attr(node, "value")
            if language and value:
                names.append({"language": language, "value": value})
        for row in names:
            if row["language"].lower() == "english":
                title = row["value"]
                break
        if not title and names:
            title = names[0]["value"]
        problems: list[ContestProblemRow] = []
        for seq, node in enumerate(root.findall("./problems/problem"), start=1):
            index = _xml_attr(node, "index")
            if not index:
                index = _contest_idx_label(seq)
            url = _xml_attr(node, "url")
            short_name = _xml_attr(node, "short-name")
            slug_hint = _slug_hint_from_url(url)
            if not slug_hint and short_name:
                slug_hint = _slugify_problem_token(short_name)
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

    def _problem_folder_map(self, entries: dict[str, zipfile.ZipInfo]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for path in entries:
            if not path.startswith("problems/") or not path.endswith("/problem.xml"):
                continue
            parts = path.split("/")
            if len(parts) < 3:
                continue
            folder = parts[1]
            if not folder:
                continue
            mapping[folder.lower()] = folder
        return mapping

    def _resolve_problem_folder(
        self,
        row: ContestProblemRow,
        folder_map: dict[str, str],
        used: set[str],
    ) -> str:
        candidates: list[str] = []
        if row["slug_hint"]:
            candidates.append(row["slug_hint"])
            candidates.append(row["slug_hint"].replace("-", "_"))
        short_name = _slugify_problem_token(row["short_name"])
        if short_name:
            candidates.append(short_name)
            candidates.append(short_name.replace("-", "_"))
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen or candidate in used:
                continue
            seen.add(candidate)
            folder = folder_map.get(candidate)
            if folder is not None:
                return folder
        for token in sorted(folder_map):
            if token not in used:
                return folder_map[token]
        return ""

    def _build_problem_package_bytes(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        folder: str,
    ) -> bytes:
        if not folder:
            raise ValueError("invalid problem folder")
        prefix = f"problems/{folder}/"
        buffer = io.BytesIO()
        copied = 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for path, info in entries.items():
                if not path.startswith(prefix):
                    continue
                target_rel = path[len(prefix):]
                if not target_rel:
                    continue
                out_zip.writestr(target_rel, _read_bytes_from_zip(zf, info))
                copied += 1
        if copied <= 0:
            raise ValueError(f"problem folder is empty: {folder}")
        payload = buffer.getvalue()
        with zipfile.ZipFile(io.BytesIO(payload), "r") as check_zip:
            check_entries = _entry_map_from_zip(check_zip, "problem.xml")
            if "problem.xml" not in check_entries:
                raise ValueError(f"problem.xml missing in problem folder: {folder}")
        return payload

    def parse_package(self, package_name: str, package_bytes: bytes) -> ParsedContestPackage:
        if not package_bytes:
            raise ValueError("empty package file")
        if len(package_bytes) > ZIP_MAX_BYTES:
            raise ValueError("contest package file is too large")
        package_name = package_name.strip()
        with zipfile.ZipFile(io.BytesIO(package_bytes), "r") as zf:
            entry_map = _entry_map_from_zip(zf, "contest.xml")
            contest = self._parse_contest_xml(_read_text_from_zip(zf, entry_map["contest.xml"]))
            statement_files = self._statement_source_rows(zf, entry_map)
            default_language = self._default_statement_language(statement_files)
            inferred_header = self._infer_statement_header_fields(statement_files, default_language)
            folder_map = self._problem_folder_map(entry_map)
            if not folder_map:
                raise ValueError("no problem.xml found under problems/ in contest package")
            used_folders: set[str] = set()
            imported_rows: list[ImportedContestProblem] = []
            for seq, row in enumerate(contest["problems"], start=1):
                folder = self._resolve_problem_folder(row, folder_map, used_folders)
                if not folder:
                    raise ValueError(f"cannot resolve contest problem folder for index #{seq}")
                used_folders.add(folder.lower())
                package_payload = self._build_problem_package_bytes(zf, entry_map, folder)
                source_slug = _slugify_problem_token(folder) or f"problem-{seq}"
                imported_rows.append(
                    {
                        "index": row["index"],
                        "source_slug": source_slug,
                        "source_folder": folder,
                        "package_name": f"{source_slug}.zip",
                        "package_bytes": package_payload,
                    }
                )
            title = contest["title"]
            if not title:
                title = inferred_header["title"] or Path(package_name or "imported-contest").stem
            return {
                "package_name": package_name,
                "title": title,
                "location": inferred_header["location"],
                "date": inferred_header["date"],
                "problems": imported_rows,
                "statement_files": statement_files,
                "default_language": default_language,
                "total_problems": len(imported_rows),
            }
