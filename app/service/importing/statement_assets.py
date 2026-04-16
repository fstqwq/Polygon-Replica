from __future__ import annotations

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
    payload: bytes


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


def _write_statement_asset(workspace: Path, rel_path: str, payload: bytes) -> None:
    target = workspace / STATEMENT_ASSETS_DIR / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def merge_imported_statement_assets(
    workspace: Path,
    *,
    shared_assets: dict[str, bytes],
    legacy_assets: list[ImportedLegacyStatementAsset],
) -> StatementAssetMergeSummary:
    shutil.rmtree(workspace / STATEMENT_ASSETS_DIR, ignore_errors=True)
    written_payloads: dict[str, bytes] = {}
    copied_files = 0
    warnings: list[str] = []

    for rel_path in sorted(shared_assets):
        payload = bytes(shared_assets[rel_path])
        _write_statement_asset(workspace, rel_path, payload)
        written_payloads[rel_path] = payload
        copied_files += 1

    grouped: dict[str, list[ImportedLegacyStatementAsset]] = defaultdict(list)
    for row in legacy_assets:
        grouped[row["asset_rel"]].append(row)

    for asset_rel in sorted(grouped):
        rows = sorted(
            grouped[asset_rel],
            key=lambda item: (_language_sort_key(item["language"]), str(item["package_path"])),
        )
        canonical_payload = written_payloads.get(asset_rel)
        if canonical_payload is None:
            first = rows[0]
            payload = bytes(first["payload"])
            _write_statement_asset(workspace, asset_rel, payload)
            written_payloads[asset_rel] = payload
            copied_files += 1
            rows = rows[1:]
            canonical_payload = payload
        for row in rows:
            payload = bytes(row["payload"])
            if payload == canonical_payload:
                continue
            suffix = _select_conflict_suffix(row["language"])
            rename_index = 0
            while True:
                candidate_rel = _rename_asset_rel(asset_rel, suffix, rename_index)
                existing_payload = written_payloads.get(candidate_rel)
                if existing_payload is None:
                    _write_statement_asset(workspace, candidate_rel, payload)
                    written_payloads[candidate_rel] = payload
                    copied_files += 1
                    warnings.append(
                        "statement attachment conflict: "
                        f"{row['package_path']} imported as {STATEMENT_ASSETS_DIR.as_posix()}/{candidate_rel}; "
                        f"{normalize_statement_language(row['language']) or 'statement'} sources may still reference {asset_rel}"
                    )
                    break
                if existing_payload == payload:
                    break
                rename_index += 1

    return {"copied_files": copied_files, "warnings": warnings}
