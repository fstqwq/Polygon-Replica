from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from app.service.problem.test_spec import load_tests_spec


NATIVE_MARKER = "polygonlike-native.json"
ZIP_MAX_BYTES = 256 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 64 * 1024 * 1024


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


def _read_bytes_from_zip(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if int(info.file_size) > ZIP_MAX_FILE_BYTES:
        raise ValueError(f"zip entry too large: {info.filename}")
    with zf.open(info, "r") as fh:
        raw = fh.read(ZIP_MAX_FILE_BYTES + 1)
    if len(raw) > ZIP_MAX_FILE_BYTES:
        raise ValueError(f"zip entry too large: {info.filename}")
    return raw


def _clear_workspace_tree(workspace: Path) -> None:
    for child in workspace.iterdir():
        if child.name in {".git", ".polygonlike.lock"}:
            continue
        if child.is_symlink():
            child.unlink(missing_ok=True)
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=False)
            continue
        child.unlink(missing_ok=True)


class NativePackageImportService:
    def import_package(
        self,
        workspace: Path,
        package_name: str,
        package_bytes: bytes,
        *,
        normalize_test_data_newlines: bool = False,
    ) -> dict[str, object]:
        del normalize_test_data_newlines
        raw = bytes(package_bytes or b"")
        if not raw:
            raise ValueError("empty package file")
        if len(raw) > ZIP_MAX_BYTES:
            raise ValueError("package file is too large")

        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            entry_map = _entry_map_from_zip(zf, NATIVE_MARKER)
            marker_info = entry_map.get(NATIVE_MARKER)
            if marker_info is None:
                raise ValueError(f"{NATIVE_MARKER} not found in package")
            try:
                marker_payload = json.loads(_read_bytes_from_zip(zf, marker_info).decode("utf-8"))
            except Exception as exc:
                raise ValueError("invalid native package marker") from exc
            if not isinstance(marker_payload, dict):
                raise ValueError("invalid native package marker")
            package_type = str(marker_payload.get("package_type") or "").strip()
            if package_type != "native":
                raise ValueError("invalid native package marker")
            title = str(marker_payload.get("problem_name") or "").strip()

            repo_entries = [rel for rel in entry_map if rel.startswith("repo/") and rel != "repo/"]
            if not repo_entries:
                raise ValueError("native package is missing repo payload")

            _clear_workspace_tree(workspace)
            for rel in sorted(repo_entries):
                rel_path = Path(rel)
                if len(rel_path.parts) < 2:
                    continue
                target_rel = Path(*rel_path.parts[1:])
                if not target_rel.parts:
                    continue
                payload = _read_bytes_from_zip(zf, entry_map[rel])
                target = workspace / target_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)

        tests_total = 0
        try:
            tests_total = len(load_tests_spec(workspace / "tests" / "spec.json"))
        except Exception:
            tests_total = 0
        return {
            "package_name": package_name.strip(),
            "title": title,
            "statement": {},
            "tests": {"total": tests_total},
            "solutions": {},
            "components": {},
        }
