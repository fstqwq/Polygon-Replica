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
from app.service.problem.runtime_config import ProblemConfigLimits
from app.service.problem.source_tree import load_problem_source_tree


POLYGON_REPLICA_PACKAGE_ANCHOR = "config/problem.json"


def _validated_source_entries(
    entry_map: dict[str, zipfile.ZipInfo],
) -> list[tuple[Path, zipfile.ZipInfo, str]]:
    validated: list[tuple[Path, zipfile.ZipInfo, str]] = []
    for rel in sorted(entry_map):
        target_rel = Path(rel)
        if not target_rel.parts:
            continue
        if is_hidden_workspace_path(target_rel.parts):
            raise ValueError(f"Polygon Replica package contains forbidden hidden path: {rel}")
        if not is_allowed_workspace_root_path(target_rel.parts):
            raise ValueError(f"Polygon Replica package contains forbidden root path: {rel}")
        if is_repository_answer_path(target_rel.parts):
            raise ValueError(f"Polygon Replica package contains repository answer file: {rel}")
        info = entry_map[rel]
        validated.append((target_rel, info, rel))
    return validated


def _clear_workspace_tree(workspace: Path) -> None:
    for child in workspace.iterdir():
        if child.name == ".git":
            continue
        if child.is_symlink():
            child.unlink(missing_ok=True)
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=False)
            continue
        child.unlink(missing_ok=True)


def _validated_source_directory(relative: str) -> Path:
    target_rel = Path(relative)
    if not target_rel.parts:
        raise ValueError("Polygon Replica package contains an empty directory path")
    if is_hidden_workspace_path(target_rel.parts):
        raise ValueError(
            f"Polygon Replica package contains forbidden hidden path: {relative}"
        )
    if not is_allowed_workspace_root_path(target_rel.parts):
        raise ValueError(
            f"Polygon Replica package contains forbidden root path: {relative}"
        )
    if is_repository_answer_path(target_rel.parts):
        raise ValueError(
            f"Polygon Replica package contains repository answer path: {relative}"
        )
    return target_rel


class PolygonReplicaPackageImportService:
    def import_package(
        self,
        workspace: Path,
        package_name: str,
        package: ArchiveView,
        *,
        normalize_test_data_newlines: bool = False,
        text_limit_bytes: int,
        statement_sample_max_bytes: int,
        problem_config_limits: ProblemConfigLimits,
    ) -> dict[str, object]:
        del normalize_test_data_newlines
        rooted = package.rooted_at(POLYGON_REPLICA_PACKAGE_ANCHOR)
        rooted_entries = rooted.entries
        complete_entry_map = {
            rel: info
            for rel, info in rooted_entries.items()
            if not info.is_dir()
        }
        # Native import selects only canonical authored roots. Derived and other
        # unknown package members remain unopened and consume no expansion budget.
        entry_map = {
            rel: info
            for rel, info in complete_entry_map.items()
            if is_allowed_workspace_root_path(Path(rel).parts)
        }
        problem_info = entry_map.get(POLYGON_REPLICA_PACKAGE_ANCHOR)
        if problem_info is None:
            raise ValueError(f"{POLYGON_REPLICA_PACKAGE_ANCHOR} is not a file")
        source_entries = _validated_source_entries(entry_map)
        staging_root = workspace.parent / f".polygon-replica-import-{uuid.uuid4().hex}"
        try:
            staging_root.mkdir(parents=True, exist_ok=False)
            for rel, info in sorted(rooted_entries.items()):
                if not info.is_dir():
                    continue
                if not is_allowed_workspace_root_path(Path(rel).parts):
                    continue
                target_rel = _validated_source_directory(rel)
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFDIR}:
                    raise ValueError(
                        f"Polygon Replica package contains a special file: {rel}"
                    )
                (staging_root / target_rel).mkdir(parents=True, exist_ok=True)
            for target_rel, info, rel in source_entries:
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG}:
                    raise ValueError(f"Polygon Replica package contains a special file: {rel}")
                rooted.zip_file.copy_to(info, staging_root / target_rel)
                if mode:
                    (staging_root / target_rel).chmod(stat.S_IMODE(mode))

            source_tree = load_problem_source_tree(
                staging_root,
                problem_limits=problem_config_limits,
                tests_spec_max_bytes=text_limit_bytes,
                statement_sample_max_bytes=statement_sample_max_bytes,
            )
            _clear_workspace_tree(workspace)
            for child in staging_root.iterdir():
                shutil.move(str(child), str(workspace / child.name))
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

        return {
            "package_name": package_name.strip(),
            "title": "",
            "statement": {},
            "tests": {"total": len(source_tree.tests)},
            "solutions": {},
            "components": {},
        }
