import os
import shutil
from pathlib import Path

from app.service.platform.fs.layout import StorageLayout


class ContestSourceSnapshotService:
    """Copy canonical Contest sources into a caller-owned derived tree."""

    def __init__(self, storage_layout: StorageLayout) -> None:
        self._storage = storage_layout

    def copy_to(
        self,
        *,
        contest_slug: str,
        target: Path,
        language: str,
        default_statements_tex: str,
    ) -> Path:
        source = self._storage.contest_source(contest_slug)
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(
            source,
            topdown=True,
            followlinks=False,
        ):
            parent = Path(dirpath)
            relative_parent = parent.relative_to(source)
            destination = target / relative_parent
            destination.mkdir(parents=True, exist_ok=True)
            dirnames[:] = sorted(dirnames)
            for dirname in dirnames:
                path = parent / dirname
                if path.is_symlink() or not path.is_dir():
                    relative = path.relative_to(source)
                    raise RuntimeError(
                        f"contest source is not a regular directory: {relative}"
                    )
            for filename in sorted(filenames):
                path = parent / filename
                if path.is_symlink() or not path.is_file():
                    relative = path.relative_to(source)
                    raise RuntimeError(
                        f"contest source is not a regular file: {relative}"
                    )
                shutil.copy2(path, destination / filename)
        if language:
            statements_root = target / "statements" / language
            statements_root.mkdir(parents=True, exist_ok=True)
            statements_tex = statements_root / "statements.tex"
            if not statements_tex.exists():
                statements_tex.write_text(
                    default_statements_tex,
                    encoding="utf-8",
                    newline="\n",
                )
        return target
