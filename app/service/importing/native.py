from __future__ import annotations

import json
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

from app.service.importing.archive import ArchiveView
from app.service.platform.workspace_path import (
    is_allowed_workspace_root_path,
    is_hidden_workspace_path,
    is_repository_answer_path,
)
from app.service.problem.test_spec import loads_tests_spec
from app.service.statement.constant import STATEMENT_SECTIONS_DIR


NATIVE_PACKAGE_ANCHOR = "config/problem.json"


def _validated_native_entries(
    entry_map: dict[str, zipfile.ZipInfo],
) -> list[tuple[Path, zipfile.ZipInfo, str]]:
    validated: list[tuple[Path, zipfile.ZipInfo, str]] = []
    for rel in sorted(entry_map):
        target_rel = Path(rel)
        if not target_rel.parts:
            continue
        if is_hidden_workspace_path(target_rel.parts):
            raise ValueError(f"native package contains forbidden hidden path: {rel}")
        if not is_allowed_workspace_root_path(target_rel.parts):
            raise ValueError(f"native package contains forbidden root path: {rel}")
        if is_repository_answer_path(target_rel.parts):
            raise ValueError(f"native package contains repository answer file: {rel}")
        info = entry_map[rel]
        validated.append((target_rel, info, rel))
    return validated


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

def _title_from_problem_config(payload: bytes) -> str:
    try:
        config = json.loads(payload.decode("utf-8"))
        if isinstance(config, dict):
            return str(config.get("name") or "").strip()
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
        package: ArchiveView,
        *,
        normalize_test_data_newlines: bool = False,
        text_limit_bytes: int,
    ) -> dict[str, object]:
        del normalize_test_data_newlines
        rooted = package.rooted_at(NATIVE_PACKAGE_ANCHOR)
        entry_map = {
            rel: info
            for rel, info in rooted.entries.items()
            if (not info.is_dir()) and (not rel.startswith("test_data/"))
        }
        problem_info = entry_map.get(NATIVE_PACKAGE_ANCHOR)
        if problem_info is None:
            raise ValueError(f"{NATIVE_PACKAGE_ANCHOR} is not a file")
        problem_metadata = rooted.read_metadata(
            problem_info,
            label=NATIVE_PACKAGE_ANCHOR,
        )
        title = _title_from_problem_config(problem_metadata)
        tests_total = 0
        tests_spec_info = entry_map.get("tests/spec.json")
        if tests_spec_info is not None:
            try:
                tests_total = len(
                    loads_tests_spec(
                        rooted.read_metadata(
                            tests_spec_info,
                            label="tests/spec.json",
                        ).decode("utf-8"),
                        max_bytes=text_limit_bytes,
                    )
                )
            except (UnicodeDecodeError, ValueError):
                tests_total = 0
        source_entries = _validated_native_entries(entry_map)
        staging_root = workspace.parent / f".native-import-{uuid.uuid4().hex}"
        try:
            staging_root.mkdir(parents=True, exist_ok=False)
            for target_rel, info, rel in source_entries:
                rooted.zip_file.copy_to(info, staging_root / target_rel)
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG}:
                    raise ValueError(f"native package contains a special file: {rel}")
                if mode:
                    (staging_root / target_rel).chmod(stat.S_IMODE(mode))

            _clear_workspace_tree(workspace)
            for child in staging_root.iterdir():
                shutil.move(str(child), str(workspace / child.name))
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

        _ensure_statement_name_from_problem_config(workspace, title)
        return {
            "package_name": package_name.strip(),
            "title": title,
            "statement": {},
            "tests": {"total": tests_total},
            "solutions": {},
            "components": {},
        }
