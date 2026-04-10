from __future__ import annotations

import io
import json
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from app.service.problem.test_spec import load_tests_spec


NATIVE_PACKAGE_ANCHOR = "config/problem.json"
ZIP_MAX_BYTES = 256 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 64 * 1024 * 1024
ZIP_MAX_EXTRACTED_BYTES = 256 * 1024 * 1024


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


def _entry_map_from_zip(zf: zipfile.ZipFile, anchor: str) -> dict[str, zipfile.ZipInfo]:
    raw: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        normalized = _normalize_zip_path(info.filename)
        if not normalized:
            continue
        raw[normalized] = info
    if anchor in raw:
        return raw
    candidates = sorted([p for p in raw if p.endswith(f"/{anchor}")], key=len)
    if not candidates:
        raise ValueError(f"{anchor} not found in package")
    prefix = candidates[0][: -len(anchor)]
    mapped: dict[str, zipfile.ZipInfo] = {}
    for path, info in raw.items():
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix) :]
        if rel:
            mapped[rel] = info
    if anchor not in mapped:
        raise ValueError(f"{anchor} not found in package")
    return mapped


def _validated_native_entries(
    entry_map: dict[str, zipfile.ZipInfo],
) -> list[tuple[Path, zipfile.ZipInfo, str]]:
    validated: list[tuple[Path, zipfile.ZipInfo, str]] = []
    total_size = 0
    for rel in sorted(entry_map):
        target_rel = Path(rel)
        if not target_rel.parts:
            continue
        if _is_forbidden_workspace_path(target_rel):
            raise ValueError(f"native package contains forbidden hidden path: {rel}")
        info = entry_map[rel]
        entry_size = int(info.file_size)
        if entry_size > ZIP_MAX_FILE_BYTES:
            raise ValueError(f"zip entry too large: {rel}")
        total_size += entry_size
        if total_size > ZIP_MAX_EXTRACTED_BYTES:
            raise ValueError(f"native package repo payload is too large at: {rel}")
        validated.append((target_rel, info, rel))
    return validated


def _extract_zip_entry_to_path(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
) -> int:
    written = 0
    tmp_target = target.with_name(f"{target.name}.native-import-tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zf.open(info, "r") as src, tmp_target.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > ZIP_MAX_FILE_BYTES:
                    raise ValueError(f"zip entry too large: {info.filename}")
                dst.write(chunk)
        tmp_target.replace(target)
        return written
    except Exception:
        tmp_target.unlink(missing_ok=True)
        raise


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


def _is_forbidden_workspace_path(path: Path) -> bool:
    return any(str(part or "").startswith(".") for part in path.parts)


def _read_title_from_problem_config(workspace: Path) -> str:
    cfg_path = workspace / "config" / "problem.json"
    try:
        if cfg_path.exists() and cfg_path.is_file() and not cfg_path.is_symlink():
            payload = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return str(payload.get("name") or "").strip()
    except Exception:
        pass
    return ""


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
            entry_map = _entry_map_from_zip(zf, NATIVE_PACKAGE_ANCHOR)

            files_to_write = _validated_native_entries(entry_map)
            staging_root = workspace.parent / f".native-import-{uuid.uuid4().hex}"
            written_total = 0
            try:
                staging_root.mkdir(parents=True, exist_ok=False)
                for target_rel, info, rel in files_to_write:
                    written_total += _extract_zip_entry_to_path(zf, info, staging_root / target_rel)
                    if written_total > ZIP_MAX_EXTRACTED_BYTES:
                        raise ValueError(f"native package repo payload is too large at: {rel}")

                _clear_workspace_tree(workspace)
                for child in staging_root.iterdir():
                    shutil.move(str(child), str(workspace / child.name))
            finally:
                shutil.rmtree(staging_root, ignore_errors=True)

        title = _read_title_from_problem_config(workspace)
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
