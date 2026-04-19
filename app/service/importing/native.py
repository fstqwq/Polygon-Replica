from __future__ import annotations

import io
import json
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from app.main_util import UPLOAD_MAX_BYTES
from app.service.platform.zip_extract import extract_zip_entry_to_path_limited, validate_zip_entry_size
from app.service.platform.workspace_path import (
    is_allowed_workspace_root_path,
    is_hidden_workspace_path,
)
from app.service.problem.test_spec import load_tests_spec
from app.service.statement.constant import STATEMENT_SECTIONS_DIR


NATIVE_PACKAGE_ANCHOR = "config/problem.json"
ZIP_MAX_BYTES = UPLOAD_MAX_BYTES
ZIP_MAX_FILE_BYTES = UPLOAD_MAX_BYTES
ZIP_MAX_EXTRACTED_BYTES = UPLOAD_MAX_BYTES


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
        if is_hidden_workspace_path(target_rel.parts):
            raise ValueError(f"native package contains forbidden hidden path: {rel}")
        if not is_allowed_workspace_root_path(target_rel.parts):
            raise ValueError(f"native package contains forbidden root path: {rel}")
        info = entry_map[rel]
        entry_size = validate_zip_entry_size(
            info,
            total_before=total_size,
            max_file_bytes=ZIP_MAX_FILE_BYTES,
            max_total_bytes=ZIP_MAX_EXTRACTED_BYTES,
            display_name=rel,
            entry_too_large_prefix="zip entry too large",
            payload_too_large_prefix="native package repo payload is too large at",
        )
        total_size += entry_size
        validated.append((target_rel, info, rel))
    return validated


def _extract_zip_entry_to_path(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    extracted_before: int,
    display_name: str,
) -> int:
    return extract_zip_entry_to_path_limited(
        zf,
        info,
        target,
        total_before=extracted_before,
        max_file_bytes=ZIP_MAX_FILE_BYTES,
        max_total_bytes=ZIP_MAX_EXTRACTED_BYTES,
        display_name=display_name,
        entry_too_large_prefix="zip entry too large",
        payload_too_large_prefix="native package repo payload is too large at",
        normalize_utf8_newlines=False,
    )


def _clear_workspace_tree(workspace: Path) -> None:
    for child in workspace.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            child.unlink(missing_ok=True)
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=False)
            continue
        child.unlink(missing_ok=True)

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


def _ensure_statement_name_from_problem_config(workspace: Path, title: str) -> None:
    safe_title = str(title or "").strip()
    if not safe_title:
        return
    sections_root = workspace / STATEMENT_SECTIONS_DIR
    existing_names = sorted(sections_root.glob("*/name.tex")) if sections_root.exists() else []
    for path in existing_names:
        try:
            if path.is_file() and (not path.is_symlink()) and path.read_text(encoding="utf-8").strip():
                return
        except OSError:
            continue
    target = sections_root / "english" / "name.tex"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(safe_title + "\n", encoding="utf-8")


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
                    written_total += _extract_zip_entry_to_path(
                        zf,
                        info,
                        staging_root / target_rel,
                        extracted_before=written_total,
                        display_name=rel,
                    )

                _clear_workspace_tree(workspace)
                for child in staging_root.iterdir():
                    shutil.move(str(child), str(workspace / child.name))
            finally:
                shutil.rmtree(staging_root, ignore_errors=True)

        title = _read_title_from_problem_config(workspace)
        _ensure_statement_name_from_problem_config(workspace, title)
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
