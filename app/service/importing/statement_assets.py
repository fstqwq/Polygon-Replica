import shutil
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

from app.service.statement.constant import STATEMENT_ASSETS_DIR
from app.service.statement.context import normalize_statement_language


class ImportedLegacyStatementAsset(TypedDict):
    language: str
    package_path: str
    asset_rel: str
    source_path: Path


class StatementAssetMergeSummary(TypedDict):
    copied_files: int
    warnings: list[str]


_LANGUAGE_SUFFIX = {
    "english": "en",
    "chinese": "zh",
}


def _language_sort_key(language: str) -> tuple[int, str]:
    token = normalize_statement_language(language)
    if token == "english":
        return (0, token)
    if token == "chinese":
        return (1, token)
    return (2, token)


def _rename_asset_rel(asset_rel: str, suffix: str, index: int) -> str:
    raw_rel = Path(asset_rel)
    stem = raw_rel.stem
    ext = raw_rel.suffix
    marker = f"-{suffix}" if suffix else ""
    if index > 0:
        marker = f"{marker}-{index}" if marker else f"-{index}"
    return (raw_rel.parent / f"{stem}{marker}{ext}").as_posix() if raw_rel.parent != Path(".") else f"{stem}{marker}{ext}"


def _select_conflict_suffix(language: str) -> str:
    token = normalize_statement_language(language)
    return _LANGUAGE_SUFFIX.get(token, token or "asset")


def _copy_statement_asset(workspace: Path, rel_path: str, source_path: Path) -> Path:
    target = workspace / STATEMENT_ASSETS_DIR / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)
    return target


def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(1024 * 1024)
            right_chunk = right_handle.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def merge_imported_statement_assets(
    workspace: Path,
    *,
    shared_assets: dict[str, Path],
    legacy_assets: list[ImportedLegacyStatementAsset],
) -> StatementAssetMergeSummary:
    shutil.rmtree(workspace / STATEMENT_ASSETS_DIR, ignore_errors=True)
    written_paths: dict[str, Path] = {}
    copied_files = 0
    warnings: list[str] = []

    for rel_path in sorted(shared_assets):
        written_paths[rel_path] = _copy_statement_asset(
            workspace, rel_path, shared_assets[rel_path]
        )
        copied_files += 1

    grouped: dict[str, list[ImportedLegacyStatementAsset]] = defaultdict(list)
    for row in legacy_assets:
        grouped[row["asset_rel"]].append(row)

    for asset_rel in sorted(grouped):
        rows = sorted(
            grouped[asset_rel],
            key=lambda item: (_language_sort_key(item["language"]), str(item["package_path"])),
        )
        canonical_path = written_paths.get(asset_rel)
        if canonical_path is None:
            first = rows[0]
            canonical_path = _copy_statement_asset(
                workspace, asset_rel, first["source_path"]
            )
            written_paths[asset_rel] = canonical_path
            copied_files += 1
            rows = rows[1:]
        for row in rows:
            source_path = row["source_path"]
            if _files_equal(source_path, canonical_path):
                continue
            suffix = _select_conflict_suffix(row["language"])
            rename_index = 0
            while True:
                candidate_rel = _rename_asset_rel(asset_rel, suffix, rename_index)
                existing_path = written_paths.get(candidate_rel)
                if existing_path is None:
                    written_paths[candidate_rel] = _copy_statement_asset(
                        workspace, candidate_rel, source_path
                    )
                    copied_files += 1
                    warnings.append(
                        "statement attachment conflict: "
                        f"{row['package_path']} imported as {STATEMENT_ASSETS_DIR.as_posix()}/{candidate_rel}; "
                        f"{normalize_statement_language(row['language']) or 'statement'} sources may still reference {asset_rel}"
                    )
                    break
                if _files_equal(existing_path, source_path):
                    break
                rename_index += 1

    return {"copied_files": copied_files, "warnings": warnings}
