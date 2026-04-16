from __future__ import annotations

from pathlib import Path

from app.service.platform.hashing import quick_fp_digest, sha256_hex_bytes
from app.service.problem.test_spec import TESTS_SPEC_REL, load_tests_spec, payload_rel_path_for_test
from app.service.statement.constant import (
    STATEMENT_ASSETS_DIR,
    STATEMENT_DIR,
    STATEMENT_MAIN_REL,
    STATEMENT_RENDERED_DIR_REL,
    STATEMENT_RENDERER_SIGNATURE_VERSION,
    STATEMENT_SECTIONS_DIR,
    TESTS_ANSWERS_DIR_REL,
    is_canonical_statement_section_entry,
)


def _safe_workspace_regular_file(workspace: Path, rel: Path) -> Path | None:
    try:
        workspace_resolved = workspace.resolve()
        candidate = (workspace / rel).resolve()
    except OSError:
        return None
    if workspace_resolved not in candidate.parents:
        return None
    try:
        if candidate.is_symlink() or not candidate.exists() or not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate


def statement_sources_signature(workspace: Path, problem_title: str | None = None) -> str:
    """Stable signature of statement sources (excluding derived statement/main.tex)."""
    statement_root = workspace / STATEMENT_DIR
    entries: list[dict[str, object]] = [
        {"kind": "renderer-version", "value": STATEMENT_RENDERER_SIGNATURE_VERSION},
    ]
    if not statement_root.exists() or not statement_root.is_dir() or statement_root.is_symlink():
        entries.append({"kind": "statement-root", "state": "missing"})
        return quick_fp_digest(entries, schema="statement-signature")

    files: list[tuple[str, Path]] = []
    for base in (workspace / STATEMENT_DIR, workspace / STATEMENT_ASSETS_DIR):
        if not base.exists() or not base.is_dir() or base.is_symlink():
            continue
        for path in base.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(workspace).as_posix()
            except (OSError, ValueError):
                continue
            if rel == STATEMENT_MAIN_REL.as_posix():
                continue
            if rel.startswith(f"{STATEMENT_RENDERED_DIR_REL.as_posix()}/"):
                continue
            files.append((rel, path))
    sections_root = workspace / STATEMENT_SECTIONS_DIR
    if sections_root.exists() and sections_root.is_dir() and (not sections_root.is_symlink()):
        for language_root in sorted(
            [item for item in sections_root.iterdir() if item.is_dir() and (not item.is_symlink())],
            key=lambda item: item.name,
        ):
            for file_name in sorted(language_root.iterdir(), key=lambda item: item.name):
                try:
                    if file_name.is_symlink() or (not file_name.is_file()):
                        continue
                    rel_in_section = file_name.relative_to(language_root)
                    if not is_canonical_statement_section_entry(rel_in_section):
                        continue
                    rel = file_name.relative_to(workspace).as_posix()
                except (OSError, ValueError):
                    continue
                files.append((rel, file_name))
    files.sort(key=lambda item: item[0])

    for rel, path in files:
        try:
            stat_obj = path.stat()
            mtime_ns = int(getattr(stat_obj, "st_mtime_ns", int(float(stat_obj.st_mtime) * 1_000_000_000)))
            entries.append({"kind": "statement-file", "path": rel, "state": "ok", "size": int(stat_obj.st_size), "mtime_ns": mtime_ns})
        except OSError:
            entries.append({"kind": "statement-file", "path": rel, "state": "unreadable"})

    # Include sample source-of-truth so tests/sample changes mark statement preview stale.
    tests_spec_rel = TESTS_SPEC_REL.as_posix()
    tests_spec_path = _safe_workspace_regular_file(workspace, TESTS_SPEC_REL)
    if tests_spec_path is None:
        entries.append({"kind": "tests-spec", "path": tests_spec_rel, "state": "missing"})
    else:
        try:
            stat_obj = tests_spec_path.stat()
            mtime_ns = int(getattr(stat_obj, "st_mtime_ns", int(float(stat_obj.st_mtime) * 1_000_000_000)))
            entries.append({"kind": "tests-spec", "path": tests_spec_rel, "state": "ok", "size": int(stat_obj.st_size), "mtime_ns": mtime_ns})
        except OSError:
            entries.append({"kind": "tests-spec", "path": tests_spec_rel, "state": "unreadable"})

    spec_path = workspace / TESTS_SPEC_REL
    try:
        spec_rows = load_tests_spec(spec_path)
    except Exception as exc:
        raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc
    sample_related_files: list[Path] = []
    for index, row in enumerate(spec_rows, start=1):
        if not isinstance(row, dict):
            continue
        if not bool(row.get("sample")):
            continue
        test_id = row["id"].strip()
        kind = row["kind"].strip().lower()
        if kind not in {"manual", "gen"}:
            raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}: {kind}")
        if not test_id:
            continue
        # Custom sample text already changes tests/spec.json hash.
        if not row["sample_input"]:
            sample_in = _safe_workspace_regular_file(workspace, payload_rel_path_for_test(test_id, kind))
            if sample_in is not None:
                sample_related_files.append(sample_in)
        if not row["sample_output"]:
            sample_ans = _safe_workspace_regular_file(workspace, TESTS_ANSWERS_DIR_REL / f"{test_id}.ans")
            if sample_ans is not None:
                sample_related_files.append(sample_ans)
    uniq_sample_files = sorted(
        {path.relative_to(workspace).as_posix(): path for path in sample_related_files}.items(),
        key=lambda item: item[0],
    )
    for rel, path in uniq_sample_files:
        try:
            stat_obj = path.stat()
            mtime_ns = int(getattr(stat_obj, "st_mtime_ns", int(float(stat_obj.st_mtime) * 1_000_000_000)))
            digest = ""
            try:
                digest = sha256_hex_bytes(path.read_bytes())
            except OSError:
                digest = ""
            entries.append(
                {
                    "kind": "sample-file",
                    "path": rel,
                    "state": "ok",
                    "size": int(stat_obj.st_size),
                    "mtime_ns": mtime_ns,
                    "digest": digest,
                }
            )
        except OSError:
            entries.append({"kind": "sample-file", "path": rel, "state": "unreadable"})

    if problem_title is not None:
        entries.append({"kind": "problem-title", "value": str(problem_title or "").strip()})
    return quick_fp_digest(entries, schema="statement-signature")

