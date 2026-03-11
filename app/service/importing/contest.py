from __future__ import annotations

import io
import re
import xml.etree.ElementTree
ET = xml.etree.ElementTree
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse


ZIP_MAX_BYTES = 512 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 128 * 1024 * 1024
ZIP_TEXT_MAX_BYTES = 8 * 1024 * 1024


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


def _entry_map_from_zip(zf: zipfile.ZipFile, anchor_name: str) -> dict[str, zipfile.ZipInfo]:
    raw: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        normalized = _normalize_zip_path(info.filename)
        if not normalized:
            continue
        raw[normalized] = info
    safe_anchor = _normalize_zip_path(anchor_name)
    if not safe_anchor:
        raise ValueError("invalid package anchor")
    if safe_anchor in raw:
        return raw
    suffix = f"/{safe_anchor}"
    candidates = sorted([p for p in raw if p.endswith(suffix)], key=len)
    if not candidates:
        raise ValueError(f"{safe_anchor} not found in package")
    prefix = candidates[0][: -len(safe_anchor)]
    mapped: dict[str, zipfile.ZipInfo] = {}
    for path, info in raw.items():
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix) :]
        if rel:
            mapped[rel] = info
    if safe_anchor not in mapped:
        raise ValueError(f"{safe_anchor} not found in package")
    return mapped


def _slugify_problem_token(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > 64:
        token = token[:64].rstrip("-")
    return token


def _slug_hint_from_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        path = str(parsed.path or "").strip("/")
    except Exception:
        path = text
    if not path:
        return ""
    leaf = unquote(path.split("/")[-1]).strip()
    return _slugify_problem_token(leaf)


def _contest_idx_label(seq: int) -> str:
    value = max(1, int(seq))
    chars: list[str] = []
    while value > 0:
        value -= 1
        chars.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(chars))


class PolygonContestImportService:
    def _parse_contest_xml(self, xml_text: str) -> dict[str, object]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"invalid contest.xml: {exc}") from exc
        title = ""
        names: list[dict[str, str]] = []
        for node in root.findall("./names/name"):
            language = str(node.get("language") or "").strip()
            value = str(node.get("value") or "").strip()
            if language and value:
                names.append({"language": language, "value": value})
        for row in names:
            if str(row.get("language") or "").strip().lower() == "english":
                title = str(row.get("value") or "").strip()
                break
        if not title and names:
            title = str(names[0].get("value") or "").strip()
        problems: list[dict[str, str]] = []
        seq = 1
        for node in root.findall("./problems/problem"):
            index = str(node.get("index") or "").strip()
            if not index:
                index = _contest_idx_label(seq)
            seq += 1
            index = index.upper()
            url = str(node.get("url") or "").strip()
            short_name = str(node.get("short-name") or "").strip()
            slug_hint = _slug_hint_from_url(url)
            if not slug_hint and short_name:
                slug_hint = _slugify_problem_token(short_name)
            problems.append(
                {
                    "index": index,
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
            rel = str(path or "").replace("\\", "/")
            if not rel.startswith("problems/") or not rel.endswith("/problem.xml"):
                continue
            parts = rel.split("/")
            if len(parts) < 3:
                continue
            folder = str(parts[1] or "").strip()
            if not folder:
                continue
            mapping[folder.lower()] = folder
        return mapping

    def _resolve_problem_folder(
        self,
        row: dict[str, str],
        folder_map: dict[str, str],
        used: set[str],
    ) -> str:
        candidates: list[str] = []
        slug_hint = _slugify_problem_token(str(row.get("slug_hint") or ""))
        if slug_hint:
            candidates.append(slug_hint)
            candidates.append(slug_hint.replace("-", "_"))
        short_name = _slugify_problem_token(str(row.get("short_name") or ""))
        if short_name:
            candidates.append(short_name)
            candidates.append(short_name.replace("-", "_"))
        seen: set[str] = set()
        for candidate in candidates:
            token = str(candidate or "").strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            folder = folder_map.get(token)
            if folder and (token not in used):
                return folder
        for token in sorted(folder_map.keys()):
            if token in used:
                continue
            return str(folder_map[token] or "")
        return ""

    def _build_problem_package_bytes(
        self,
        zf: zipfile.ZipFile,
        entries: dict[str, zipfile.ZipInfo],
        folder: str,
    ) -> bytes:
        safe_folder = str(folder or "").strip()
        if not safe_folder:
            raise ValueError("invalid problem folder")
        prefix = f"problems/{safe_folder}/"
        buffer = io.BytesIO()
        copied = 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for path, info in entries.items():
                rel = str(path or "").replace("\\", "/")
                if not rel.startswith(prefix):
                    continue
                target_rel = rel[len(prefix) :]
                if not target_rel:
                    continue
                out_zip.writestr(target_rel, _read_bytes_from_zip(zf, info))
                copied += 1
        if copied <= 0:
            raise ValueError(f"problem folder is empty: {safe_folder}")
        payload = buffer.getvalue()
        with zipfile.ZipFile(io.BytesIO(payload), "r") as check_zip:
            check_entries = _entry_map_from_zip(check_zip, "problem.xml")
            if "problem.xml" not in check_entries:
                raise ValueError(f"problem.xml missing in problem folder: {safe_folder}")
        return payload

    def parse_package(self, package_name: str, package_bytes: bytes) -> dict[str, object]:
        raw = bytes(package_bytes or b"")
        if not raw:
            raise ValueError("empty package file")
        if len(raw) > ZIP_MAX_BYTES:
            raise ValueError("contest package file is too large")
        safe_package_name = str(package_name or "").strip()
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            entry_map = _entry_map_from_zip(zf, "contest.xml")
            contest_info = self._parse_contest_xml(_read_text_from_zip(zf, entry_map["contest.xml"]))
            folder_map = self._problem_folder_map(entry_map)
            if not folder_map:
                raise ValueError("no problem.xml found under problems/ in contest package")
            rows = contest_info.get("problems")
            if not isinstance(rows, list):
                raise ValueError("invalid contest problem list")
            used_folders: set[str] = set()
            imported_rows: list[dict[str, object]] = []
            for seq, raw_row in enumerate(rows, start=1):
                row = dict(raw_row) if isinstance(raw_row, dict) else {}
                folder = self._resolve_problem_folder(row, folder_map, used_folders)
                if not folder:
                    raise ValueError(f"cannot resolve contest problem folder for index #{seq}")
                used_folders.add(folder.lower())
                package_payload = self._build_problem_package_bytes(zf, entry_map, folder)
                idx = str(row.get("index") or "").strip().upper()
                if not idx:
                    idx = _contest_idx_label(seq)
                source_slug = _slugify_problem_token(folder) or f"problem-{seq}"
                imported_rows.append(
                    {
                        "index": idx,
                        "source_slug": source_slug,
                        "source_folder": folder,
                        "package_name": f"{source_slug}.zip",
                        "package_bytes": package_payload,
                    }
                )
            title = str(contest_info.get("title") or "").strip() or Path(safe_package_name or "imported-contest").stem
            return {
                "package_name": safe_package_name,
                "title": title,
                "problems": imported_rows,
                "total_problems": len(imported_rows),
            }
